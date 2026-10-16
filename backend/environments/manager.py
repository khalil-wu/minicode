"""Configured execution environments, independent from thread authority."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from .client import ExecutorClient
from .config import EnvironmentSpec, load_environment_snapshot, parse_environment_spec
from .errors import ExecutorClosed, ExecutorProtocolError
from .filesystem import ExecutorFileSystem
from .process import ProcessBackend
from .http import ExecutorHttpClient


class LocalEnvironmentBackend(Protocol):
    filesystem: object
    process: object

    async def info(self) -> dict: ...
    async def close(self) -> object: ...


class Environment:
    # Configured direct transports are ordinary Source registrations. Owner
    # provisioning (and its ReadyInfo) belongs to the excluded Noise provider.
    registration_kind = "ordinary"

    def __init__(self, spec: EnvironmentSpec, *, client_name: str, inbound_request=None):
        self.id = spec.id
        self.required_skills = spec.required_skills
        self.client = ExecutorClient(spec, client_name=client_name, inbound_request=inbound_request)
        self.filesystem = ExecutorFileSystem(self)
        self.process = ProcessBackend(self)
        self.http = ExecutorHttpClient(self)
        self.startup_task: asyncio.Task | None = None
        self.retired = False
        self.closed = False
        self.borrowers = 0
        self.close_task: asyncio.Task | None = None
        self.client.on_idle = self.close_if_unborrowed

    def close_if_unborrowed(self):
        if not self.retired or self.closed or self.borrowers:
            return self.close_task
        client = self.client
        connections = (client.connection, client.connecting_connection)
        if client.processes or client.files or client.http_streams or client.cleanup_tasks or any(rpc and rpc.pending for rpc in connections):
            return None
        if client.connect_task is not None and not client.connect_task.done():
            return None
        self.closed = True
        self.close_task = asyncio.create_task(client.close(), name=f"retired-environment-close-{self.id}")
        # Host close awaits this same receipt, including any cleanup error.
        self.close_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        return self.close_task

    def borrow(self):
        return EnvironmentLease(self)

    def start_connecting(self) -> None:
        # Source eagerly starts URL transports, while stdio remains lazy.
        if self.client.spec.url is not None and self.startup_task is None:
            self.startup_task = asyncio.create_task(self.client.get_connection(), name=f"executor-start-{self.id}")
            # Failure remains on the actual client and is visible in status.
            def finished(task):
                if not task.cancelled():
                    task.exception()
                self.close_if_unborrowed()
            self.startup_task.add_done_callback(finished)

    async def get_client(self) -> ExecutorClient:
        if self.closed:
            raise ExecutorClosed(f"environment `{self.id}` is closed")
        await self.client.get_connection()
        return self.client

    async def wait_until_ready(self) -> None:
        await self.get_client()

    def last_ready_info(self) -> None:
        return None

    def selected_capability_roots(self) -> tuple[dict, ...]:
        # Peer environment/info is machine metadata, not an owner Ready report.
        return ()

    async def info(self) -> dict:
        return await (await self.get_client()).info()

    async def status(self) -> dict:
        if self.closed:
            return {"status": "disconnected", "error": "environment is closed"}
        return await self.client.status()

    async def close(self):
        self.closed = True
        if self.close_task is None:
            self.close_task = asyncio.create_task(self.client.close(), name=f"environment-close-{self.id}")
        return await asyncio.shield(self.close_task)


class LocalEnvironment:
    id = "local"
    registration_kind = "ordinary"
    required_skills: tuple[str, ...] = ()

    def __init__(self, backend: LocalEnvironmentBackend):
        self.backend = backend
        self.filesystem = backend.filesystem
        self.process = backend.process
        self.closed = False
        self.borrowers = 0

    async def info(self) -> dict:
        if self.closed:
            raise ExecutorClosed("local environment is closed")
        return await self.backend.info()

    async def wait_until_ready(self) -> None:
        if self.closed:
            raise ExecutorClosed("local environment is closed")

    def last_ready_info(self) -> None:
        return None

    def selected_capability_roots(self) -> tuple[dict, ...]:
        return ()

    def borrow(self):
        return EnvironmentLease(self)

    async def status(self) -> dict:
        return {"status": "disconnected", "error": "environment is closed"} if self.closed else {"status": "ready"}

    async def close(self):
        self.closed = True
        return await self.backend.close()


class EnvironmentLease:
    """A receiving thread's explicit ownership of its selected environment."""
    def __init__(self, environment):
        if environment.closed:
            raise ExecutorClosed("cannot borrow a closed environment")
        self.environment = environment
        self.released = False
        environment.borrowers += 1

    def clone(self) -> EnvironmentLease:
        return EnvironmentLease(self.environment)

    async def aclose(self) -> None:
        if self.released:
            return
        self.released = True
        self.environment.borrowers -= 1
        if isinstance(self.environment, Environment) and not self.environment.closed:
            closing = self.environment.close_if_unborrowed()
            if closing is not None:
                await asyncio.shield(closing)

    async def __aenter__(self):
        return self.environment

    async def __aexit__(self, *_args) -> None:
        await self.aclose()


class EnvironmentManager:
    def __init__(self, config_dir: Path, *, local_backend: LocalEnvironmentBackend | None = None,
                 environ: Mapping[str, str] | None = None, client_name: str = "minicode-environment", inbound_request=None):
        self.config_dir = Path(config_dir)
        self.snapshot = load_environment_snapshot(self.config_dir, environ=environ)
        self.default_environment_id = self.snapshot.default_environment_id
        self.client_name = client_name
        self.inbound_request = inbound_request
        self.environments: dict[str, Environment | LocalEnvironment] = {}
        if self.snapshot.include_local:
            if local_backend is None:
                raise ExecutorProtocolError("include_local requires the host's explicit local environment backend")
            self.environments["local"] = LocalEnvironment(local_backend)
        for spec in self.snapshot.environments:
            self.environments[spec.id] = Environment(spec, client_name=client_name, inbound_request=inbound_request)
        # A replaced registry entry may still be borrowed by a thread/process.
        # Keep that exact object until host close; never migrate its handles.
        self.replaced_environments: list[Environment] = []
        self.closed = False
        self.close_task: asyncio.Task | None = None
        self._network_runtime = None
        self.start()

    def network_runtime(self):
        if self.closed:
            raise ExecutorClosed("environment manager is closed")
        if self._network_runtime is None:
            from backend.network.native import NativeNetworkRuntime

            self._network_runtime = NativeNetworkRuntime()
        return self._network_runtime

    def network_provisioning_status(self):
        prerequisite = self._network_runtime.local_prerequisite if self._network_runtime is not None else None
        return prerequisite.status() if prerequisite is not None else None

    def start(self) -> None:
        if self.closed:
            raise ExecutorClosed("environment manager is closed")
        for environment in self.environments.values():
            if isinstance(environment, Environment):
                environment.start_connecting()

    def environment_ids(self) -> list[str]:
        if self.default_environment_id is None:
            return []
        return [self.default_environment_id, *(name for name in self.environments if name != self.default_environment_id)]

    def get_environment(self, environment_id: str) -> Environment | LocalEnvironment | None:
        return self.environments.get(environment_id)

    def borrow(self, environment_id: str) -> EnvironmentLease:
        if self.closed:
            raise ExecutorClosed("environment manager is closed")
        environment = self.get_environment(environment_id)
        if environment is None:
            raise ExecutorProtocolError(f"unknown environment id `{environment_id}`")
        return environment.borrow()

    async def status(self, environment_id: str) -> dict:
        environment = self.get_environment(environment_id)
        if environment is None:
            return {"status": "unknown", "error": f"unknown environment id `{environment_id}`"}
        return await environment.status()

    def upsert(self, data: Mapping[str, object]) -> Environment:
        if self.closed:
            raise ExecutorClosed("environment manager is closed")
        spec = parse_environment_spec(data, self.config_dir)
        environment = Environment(spec, client_name=self.client_name, inbound_request=self.inbound_request)
        previous = self.environments.get(spec.id)
        self.environments[spec.id] = environment
        if previous is not None:
            previous.retired = True
            self.replaced_environments.append(previous)
            previous.close_if_unborrowed()
        environment.start_connecting()
        return environment

    async def _close(self) -> list:
        self.closed = True
        results = await asyncio.gather(*(environment.close() for environment in
            (*self.environments.values(), *self.replaced_environments)), return_exceptions=True)
        if self._network_runtime is not None:
            try:
                await self._network_runtime.aclose()
            except Exception as error:
                results.append(error)
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise ExceptionGroup("environment host close was not confirmed", failures)
        return results

    async def close(self) -> list:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="environment-manager-close")
        return await asyncio.shield(self.close_task)

    async def __aenter__(self) -> EnvironmentManager:
        self.start()
        return self

    async def __aexit__(self, *_args) -> None:
        await self.close()
