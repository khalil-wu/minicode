"""The existing command owner backed by a captured Source executor handle."""
from __future__ import annotations

import asyncio
import codecs
import inspect
import os
import tempfile
import secrets
from uuid import uuid4

from backend.environments.errors import ExecutorEventsLagged, ExecutorProtocolError, ExecutorRpcError
from backend.permissions.environment_command import EnvironmentCommandPolicy
from backend.sandbox.result import SandboxResult
from backend.sandbox.runner import SandboxCapability
from backend.tools.base import truncate_text_tail


class _ExecutorInput:
    def __init__(self, process):
        self.process = process
        self.pending = bytearray()

    def write(self, chunk):
        self.pending.extend(chunk)

    async def drain(self):
        chunk = bytes(self.pending)
        self.pending.clear()
        status = await self.process.handle.write(chunk)
        if status != "accepted":
            raise ExecutorProtocolError(f"executor process write returned {status}")

    def close(self):
        raise ExecutorProtocolError("The executor process protocol does not support closing stdin independently; send input or terminate the owned process.")


class ExecutorCommandProcess:
    def __init__(self, handle, *, local: bool, stdin: bool):
        self.handle, self.local = handle, local
        self.stdin = _ExecutorInput(self) if stdin else None

    @property
    def pid(self):
        return self.handle.process.pid if self.local and self.handle.process is not None else None

    @property
    def process_id(self):
        return self.handle.process_id

    @property
    def returncode(self):
        return self.handle.exit_code if self.handle.exited else None

    async def interrupt(self):
        await self.handle.signal("interrupt")


class ExecutorCommandRunner:
    def __init__(self, policy: EnvironmentCommandPolicy, *, cleanup_callbacks=()):
        self.policy = policy
        self.lease = policy.attachment.lease.clone()
        self.environment = self.lease.environment
        self.handle = None
        self.process = None
        self.pump = None
        self.closed = False
        self.never_started = False
        self.container_ownership = {}
        self._container_name = ""
        self.network_lease = None
        self.network_registration = None
        self.network_outcome = None
        self.network_audit = []
        self.network_observer_tasks = set()
        self.network_finish_reason = "process_finished"
        self.network_registration_closed = False
        self.network_closed = False
        self.process_cleanup_failure = None
        self.cleanup_callbacks = list(cleanup_callbacks)
        self._cleanup_lock = asyncio.Lock()

    def capability(self, *, cwd=None):
        managed = not self.policy.disable_os_sandbox
        return SandboxCapability(True, "executor-managed" if managed else "full-access", False, False)

    async def _kill_tree(self, process):
        await process.handle.terminate()
        return await self.cleanup()

    async def cleanup(self):
        async with self._cleanup_lock:
            return await self._cleanup_owned_resources()

    async def add_cleanup_callback(self, callback):
        async with self._cleanup_lock:
            if self.closed:
                await callback()
            else:
                self.cleanup_callbacks.append(callback)

    async def _cleanup_owned_resources(self):
        if self.closed:
            return True
        if self.handle is None or self.never_started:
            completed = True
        else:
            receipt = await self.handle.aclose()
            completed = receipt["closed"]
            self.process_cleanup_failure = receipt.get("cleanup_failure")
        if completed:
            errors = []
            resources_closed = True
            if self.network_lease is not None and not self.network_closed:
                try:
                    await self.network_lease.flush_audit()
                except Exception as error:
                    errors.append(error)
            await self._settle_network_observers(errors)
            if self.network_registration is not None and not self.network_registration_closed:
                try:
                    self.network_outcome = await self.network_registration.finish(reason=self.network_finish_reason)
                    self.network_registration_closed = True
                except Exception as error:
                    errors.append(error)
                    resources_closed = False
            if self.network_lease is not None and not self.network_closed:
                try:
                    await self.network_lease.aclose()
                    self.network_closed = True
                except Exception as error:
                    errors.append(error)
                    resources_closed = False
            # Proxy shutdown flushes its final real decisions before its ACK.
            await self._settle_network_observers(errors)
            self._capture_network_audit()
            if resources_closed:
                try:
                    while self.cleanup_callbacks:
                        await self.cleanup_callbacks[0]()
                        self.cleanup_callbacks.pop(0)
                except Exception as error:
                    errors.append(error)
                    resources_closed = False
            if resources_closed:
                try:
                    await self.lease.aclose()
                    self.closed = True
                except Exception as error:
                    errors.append(error)
            if errors:
                error = errors[0] if len(errors) == 1 else ExceptionGroup("executor cleanup reported multiple errors", errors)
                error.cleanup_pending = not self.closed
                error.cleanup_reason = "executor_process_close_unconfirmed" if not self.closed else ""
                raise error
        return completed

    async def _settle_network_observers(self, errors):
        while pending := tuple(self.network_observer_tasks):
            results = await asyncio.gather(*pending, return_exceptions=True)
            self.network_observer_tasks.difference_update(pending)
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    errors.append(RuntimeError("Network policy observer was cancelled before returning its result"))
                elif isinstance(result, Exception):
                    errors.append(result)

    def _capture_network_audit(self):
        self.network_audit = ([{"source": "executor", "payload": item} for item in self.handle.network_audit]
            if self.handle is not None and self.policy.network_policy is not None else [])
        if self.network_lease is not None:
            self.network_audit.extend({"source": "controller", "payload": item} for item in self.network_lease.network_audit)

    async def run(self, command, *, cwd, argv, tty=False, keep_stdin_open=False,
            cancel_event=None, stream_callback=None, process_started_callback=None,
            process_ready_callback=None, executor_started_callback=None,
            preserve_full_output=False, host_command=None, process_id=None):
        decoders = {name: codecs.getincrementaldecoder("utf-8")(errors="replace") for name in ("stdout", "stderr")}
        output = {"stdout": "", "stderr": ""}
        totals = {"stdout": 0, "stderr": 0}
        captures, paths = {}, {"stdout": "", "stderr": ""}
        if preserve_full_output:
            for name in output:
                descriptor, path = tempfile.mkstemp(prefix=f"minicode-executor-{name}-", suffix=".log")
                captures[name], paths[name] = os.fdopen(descriptor, "wb"), path
        timed_out = cancelled = False
        close_completed = False
        startup_completed = False
        execution_error = None

        async def emit(chunk, name, *, final=False):
            stream = "stderr" if name == "stderr" else "stdout"
            totals[stream] += len(chunk)
            text = decoders[stream].decode(chunk, final=final)
            if captures:
                captures[stream].write(text.encode("utf-8"))
            output[stream] = truncate_text_tail(output[stream] + text).content
            if text and stream_callback is not None:
                value = stream_callback(text, stream)
                if inspect.isawaitable(value):
                    await value

        async def drain(receiver):
            last_seq = 0
            exit_seen = False
            while True:
                try:
                    event = await receiver.recv()
                except ExecutorEventsLagged:
                    recovered = await self.handle.read(after_seq=last_seq, max_bytes=None, wait_ms=0)
                    if recovered.failure:
                        raise ExecutorProtocolError(recovered.failure)
                    for chunk in recovered.chunks:
                        if chunk.seq != last_seq + 1:
                            if recovered.exited and not exit_seen and chunk.seq == last_seq + 2:
                                exit_seen = True
                                last_seq += 1
                            else:
                                raise ExecutorProtocolError(f"executor command output has a sequence gap after {last_seq}; received {chunk.seq}")
                        await emit(chunk.chunk, chunk.stream)
                        last_seq = chunk.seq
                    target = recovered.next_seq - 1
                    if recovered.closed:
                        target -= 1
                    if target == last_seq + 1 and recovered.exited and not exit_seen:
                        exit_seen = True
                        last_seq += 1
                    if target != last_seq:
                        raise ExecutorProtocolError(f"executor command output has a sequence gap after {last_seq}; recovery reached {target}")
                    if recovered.closed:
                        for name in output:
                            await emit(b"", name, final=True)
                        return
                    continue
                kind = event["method"]
                if kind == "process/failed":
                    raise ExecutorProtocolError(event["error"])
                seq = event["seq"]
                if seq <= last_seq:
                    continue
                if seq != last_seq + 1:
                    raise ExecutorProtocolError(f"executor command output has a sequence gap after {last_seq}; received {seq}")
                last_seq = seq
                if kind == "process/output":
                    await emit(event["chunk"], event["stream"])
                elif kind == "process/exited":
                    exit_seen = True
                elif kind == "process/closed":
                    for name in output:
                        await emit(b"", name, final=True)
                    return

        def observe_process(handle):
            self.handle = handle
            if self.policy.network_policy is not None and not self.policy.attachment.is_remote:
                self.network_lease = handle.network_lease.clone()
            self.process = ExecutorCommandProcess(handle, local=not self.policy.attachment.is_remote,
                stdin=keep_stdin_open or tty)
            self.pump = asyncio.create_task(drain(handle.subscribe_events()), name=f"executor-command-output-{handle.process_id}")

        cancel_waiter = None
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            identity = process_id or uuid4().hex
            network = self.policy.network_policy
            network_kwargs = {}
            if network is not None:
                async def cancel_network_execution(_reason):
                    if self.handle is not None and not self.handle.exited and not self.handle.closed:
                        await self.handle.terminate()

                async def apply_network_amendment(**amendment):
                    await self.network_lease.apply_amendment(**amendment)

                async def observe_blocked(audit):
                    connection = await network.runtime.connection()
                    result = await connection.request("network/deniedMessage", {"event": audit})
                    if result["message"] is not None:
                        if self.network_registration is not None:
                            await self.network_registration.observe_blocked(result["message"])
                        else:
                            self.network_outcome = result["message"]
                            await cancel_network_execution(result["message"])

                def network_observer(audit):
                    if audit["decision"] == "deny":
                        task = asyncio.create_task(observe_blocked(audit), name=f"network-blocked-{identity}")
                        self.network_observer_tasks.add(task)

                if self.policy.network_approval_factory is not None:
                    self.network_registration = self.policy.network_approval_factory(identity,
                        cancel_network_execution, apply_network_amendment)
                decider = self.network_registration.decide if self.network_registration is not None else None
                if self.policy.attachment.is_remote:
                    self.network_lease = await network.prepare_remote(environment_id=self.policy.scope.environment_id,
                        execution_id=identity, attribution_token=secrets.token_hex(32), decider=decider, observer=network_observer)
                    network_kwargs = {"enforce_managed_network": True, "network_proxy": self.network_lease.remote_launch,
                        "network_decider": self.network_lease.decide, "network_observer": network_observer}
                else:
                    network_kwargs = {"managed_network_policy": network, "network_decider": decider, "network_observer": network_observer}
            self.handle = await self.environment.process.start(list(argv), cwd,
                sandbox=self.policy.scope.process_sandbox_context(enforce_managed_network=network is not None), env=self.policy.env_overrides,
                env_policy=self.policy.env_policy_wire(), tty=tty, pipe_stdin=keep_stdin_open and not tty,
                process_observer=observe_process, process_id=identity, **network_kwargs)
            startup_completed = True
            if process_ready_callback is not None:
                await process_ready_callback(self.process)
            if self.process.pid is not None and process_started_callback is not None:
                await process_started_callback(self.process.pid)
            elif executor_started_callback is not None:
                await executor_started_callback(self.handle.process_id)
            if cancel_event is not None:
                cancel_waiter = asyncio.create_task(cancel_event.wait())
            waiting = {self.pump, *({cancel_waiter} if cancel_waiter is not None else set())}
            done, _ = await asyncio.wait(waiting, timeout=self.policy.timeout or None,
                return_when=asyncio.FIRST_COMPLETED)
            if self.pump not in done:
                cancelled = cancel_waiter is not None and cancel_waiter in done
                timed_out = not cancelled
                await self.handle.aclose()
            await self.pump
            if not self.handle.exited or self.handle.exit_code is None:
                raise ExecutorProtocolError("executor closed a command without an observed exit code")
        except asyncio.CancelledError:
            cancelled = True
            if self.handle is not None:
                await self.handle.aclose()
            if self.pump is not None:
                await self.pump
        except ExecutorRpcError as error:
            execution_error = error
            self.network_finish_reason = str(error)
            if not startup_completed:
                self.never_started = True  # The peer rejected this actual start RPC.
            raise
        except Exception as error:
            execution_error = error
            self.network_finish_reason = str(error)
            if (not startup_completed and not self.policy.attachment.is_remote and self.handle is not None
                    and self.handle.process is None and self.handle.released):
                self.never_started = True  # The local owner closed its rejected launch before returning.
            raise
        finally:
            if cancel_waiter is not None:
                cancel_waiter.cancel()
                await asyncio.gather(cancel_waiter, return_exceptions=True)
            if not startup_completed and self.pump is not None:
                self.pump.cancel()
                await asyncio.gather(self.pump, return_exceptions=True)
            try:
                try:
                    close_completed = await self.cleanup()
                    if not close_completed and execution_error is not None:
                        execution_error.cleanup_pending = True
                        execution_error.cleanup_reason = "executor_process_close_unconfirmed"
                        if self.process_cleanup_failure:
                            execution_error.cleanup_failure = self.process_cleanup_failure
                            execution_error.add_note(self.process_cleanup_failure)
                except Exception as error:
                    if execution_error is not None and error is not execution_error:
                        error = ExceptionGroup(f"{execution_error}; executor cleanup also reported errors", [execution_error, error])
                    error.cleanup_pending = not self.closed
                    error.cleanup_reason = "executor_process_close_unconfirmed" if not self.closed else ""
                    raise error
            finally:
                self._capture_network_audit()
                for stream in captures.values():
                    stream.close()
        return SandboxResult(output["stdout"], output["stderr"], self.handle.exit_code if self.handle is not None else None,
            timed_out=timed_out, cancelled=cancelled, stdout_path=paths["stdout"], stderr_path=paths["stderr"],
            stdout_total_bytes=totals["stdout"], stderr_total_bytes=totals["stderr"],
            cleanup_pending=not close_completed, cleanup_reason="executor_process_close_unconfirmed" if not close_completed else "",
            execution_error=self.network_outcome or "", network_audit=tuple(self.network_audit))


def command_runner(policy):
    if isinstance(policy, EnvironmentCommandPolicy):
        return ExecutorCommandRunner(policy)
    from backend.sandbox import SandboxRunner

    return SandboxRunner(policy)


def command_capability(policy, *, cwd=None):
    if isinstance(policy, EnvironmentCommandPolicy):
        return SandboxCapability(True, "executor-managed" if not policy.disable_os_sandbox else "full-access", False, False)
    from backend.sandbox import SandboxRunner

    return SandboxRunner(policy).capability(cwd=cwd)
