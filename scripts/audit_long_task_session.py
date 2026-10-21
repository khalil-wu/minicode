"""Run a real model and real backend through long work, navigation and reconnect.

Only the configured provider is read from --configured-state. Credentials stay
in process memory/environment. All chats, files and traces belong to --out.
No desktop application or browser is started.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import time
from datetime import datetime
from uuid import uuid4

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def backend_source_snapshot():
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "backend").rglob("*"))
            if path.is_file() and "tests" not in path.parts and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".pyo"}}

VERIFIER = '''import argparse
import importlib.util
import json
import os
from decimal import Decimal
from pathlib import Path
import random
import time

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=int, required=True)
args = parser.parse_args()
spec = importlib.util.spec_from_file_location("solution", "solution.py")
solution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(solution)
rng = random.Random(20261020)
started = time.monotonic()
batches = 0
assert solution.reconcile([]) == {}
with Path("progress.jsonl").open("w", encoding="utf-8") as progress:
    while time.monotonic() - started < args.seconds:
        rows = [{"id": str(rng.randrange(450)), "account": "acct-" + str(rng.randrange(8)),
                 "amount": str(Decimal(rng.randrange(-100000, 100001)) / 100)} for _ in range(600)]
        rows.extend([{"id": "zero-positive", "account": "zero-total", "amount": "10.00"},
                     {"id": "zero-negative", "account": "zero-total", "amount": "-10.00"}])
        first = {}
        for row in rows:
            first.setdefault(row["id"], row)
        expected = {}
        for row in first.values():
            expected[row["account"]] = expected.get(row["account"], Decimal(0)) + Decimal(row["amount"])
        expected = {key: format(value, ".2f") for key, value in sorted(expected.items())}
        original = json.dumps(rows, sort_keys=True)
        actual = solution.reconcile(rows)
        assert actual == expected, {"batch": batches, "actual": actual, "expected": expected}
        assert json.dumps(rows, sort_keys=True) == original, "Input rows were mutated"
        batches += 1
        entry = {"pid": os.getpid(), "batch": batches, "elapsed": round(time.monotonic() - started, 3), "checked_rows": batches * 602}
        progress.write(json.dumps(entry) + "\\n")
        progress.flush()
        print(json.dumps(entry), flush=True)
        time.sleep(1)
result = {"passed": True, "pid": os.getpid(), "batches": batches, "checked_rows": batches * 602,
          "elapsed_seconds": round(time.monotonic() - started, 3)}
Path("verification.json").write_text(json.dumps(result), encoding="utf-8")
print("INDEPENDENT_ORACLE_OK", json.dumps(result), flush=True)
'''

CHILD_PROBE = '''import argparse
import importlib.util
import json
import os
from decimal import Decimal
from pathlib import Path
import random
import time
parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=int, required=True)
args = parser.parse_args()
spec = importlib.util.spec_from_file_location("child_solution", "solution.py")
solution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(solution)
rng = random.Random(20261021)
started = time.monotonic()
checked_rows = 0
with Path("child-progress.jsonl").open("w", encoding="utf-8") as progress:
    while time.monotonic() - started < args.seconds:
        rows = [{"id": str(rng.randrange(200)), "account": "child-" + str(rng.randrange(5)),
                 "amount": str(Decimal(rng.randrange(-10000, 10001)) / 100)} for _ in range(300)]
        unique = {}
        for row in rows:
            unique.setdefault(row["id"], row)
        expected = {}
        for row in unique.values():
            expected[row["account"]] = expected.get(row["account"], Decimal(0)) + Decimal(row["amount"])
        assert solution.reconcile(rows) == {key: format(value, ".2f") for key, value in expected.items()}
        checked_rows += len(rows)
        row = {"pid": os.getpid(), "timestamp": time.time(), "elapsed": time.monotonic() - started,
               "checked_rows": checked_rows}
        progress.write(json.dumps(row) + "\\n")
        progress.flush()
        print(json.dumps(row), flush=True)
        time.sleep(1)
Path("child-result.json").write_text(json.dumps({"passed": True, "pid": os.getpid(), "elapsed": time.monotonic()-started,
                                               "checked_rows": checked_rows}))
print("CHILD_PROBE_OK", flush=True)
'''


class ProbeProcessAudit:
    """Observe the original backend-owned probes independently of model files."""

    interval = 0.2

    def __init__(self, backend_pid, state, workspace, seconds, *, child_seconds=None, record):
        self.backend = psutil.Process(backend_pid)
        self.backend_started = self.backend.create_time()
        self.tasks_root = state / "data/agent-runtime/background_tasks"
        self.workspace = workspace.resolve()
        self.expected = {"verify.py": seconds}
        if child_seconds is not None:
            self.expected["child_probe.py"] = child_seconds
        self.sources = {"verify.py": VERIFIER, "child_probe.py": CHILD_PROBE}
        self.probes = {}
        self.record = record
        self.offline = False
        self.stopping = False
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.observe())

    def check(self):
        if self.task.done():
            self.task.result()

    async def observe(self):
        while not self.stopping:
            self.sample()
            await asyncio.sleep(self.interval)

    def sample(self):
        assert self.backend.is_running() and self.backend.create_time() == self.backend_started, "Original backend process changed"
        for name in self.expected:
            assert (self.workspace / name).read_text(encoding="utf-8") == self.sources[name], f"Independent probe changed: {name}"
        for path in self.tasks_root.glob("*/*.json"):
            try:
                owner = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue  # A completed command atomically retires its live owner.
            names = [name for name in self.expected if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", owner["command"])]
            if not names or owner["pid"] is None or owner["status"] != "running":
                continue
            assert owner["owner_pid"] == self.backend.pid and owner["owner_start_time"] == self.backend_started, "Probe borrowed another backend owner"
            assert Path(owner["cwd"]).resolve() == self.workspace, "Probe used another workspace"
            try:
                shell = psutil.Process(owner["pid"])
                assert shell.create_time() == owner["process_start_time"], "Background shell PID was reused"
                assert self.backend.pid in {parent.pid for parent in shell.parents()}, "Probe shell left the original backend tree"
                processes = [shell, *shell.children(recursive=True)]
                for process in processes:
                    argv = process.cmdline()
                    if len(argv) < 2 or argv[1] in {"-c", "-m"} or Path(process.exe()).resolve() != Path(sys.executable).resolve():
                        continue
                    scripts = [(index, Path(argument).name) for index, argument in enumerate(argv[1:], 1)
                        if Path(argument).name in names]
                    if not scripts:
                        continue
                    assert len(scripts) == 1, "Probe command combined multiple sources"
                    script_index, name = scripts[0]
                    assert len(argv) == 5 and script_index == 2 and argv[1] == "-u" and argv[3] == "--seconds", "Probe changed its required command arguments"
                    assert Path(process.cwd()).resolve() == self.workspace and Path(process.cwd(), argv[2]).resolve() == self.workspace / name, "Probe executed another source file"
                    assert argv[4] == str(self.expected[name]), "Probe shortened its runtime argument"
                    self.capture(name, process, owner, path.parent.name)
            except psutil.NoSuchProcess:
                continue  # Process exit races the live-owner scan; held waits retain its exit status.
        now = time.monotonic()
        for name, probe in self.probes.items():
            wait = probe["wait"]
            if wait.done():
                if "exit_code" not in probe:
                    probe.update(exit_code=wait.result(), exited_monotonic=now, exited_at=time.time())
                    assert probe["exit_code"] == 0, f"Original {name} process exited with {probe['exit_code']}"
                    assert now - probe["first_seen"] + self.interval * 2 >= self.expected[name], f"Original {name} process ended before the required duration"
                    self.record("audit", {"type": "probe.exited", **self.public_probe(name)})
                continue
            process = probe["process"]
            if process.is_running():
                assert process.create_time() == probe["identity"]["create_time"], "Original probe PID was reused"
                probe["samples"] += 1
                if self.offline:
                    probe["offline_samples"] += 1
                self.record("audit", {"type": "probe.observed", "script": name, "pid": process.pid,
                    "create_time": probe["identity"]["create_time"], "command_id": probe["identity"]["command_id"],
                    "offline": self.offline, "observed_seconds": round(now - probe["first_seen"], 3)})

    def capture(self, name, process, owner, session_id):
        identity = {"pid": process.pid, "create_time": process.create_time(), "command_id": owner["task_id"],
            "shell_pid": owner["pid"], "shell_create_time": owner["process_start_time"],
            "backend_pid": owner["owner_pid"], "backend_create_time": owner["owner_start_time"],
            "conversation_id": owner["conversation_id"], "owner_task_id": owner["owner_task_id"],
            "parent_run_id": owner["parent_run_id"], "session_id": session_id, "command": owner["command"]}
        if name in self.probes:
            assert self.probes[name]["identity"] == identity, f"Probe was restarted or replaced: {name}"
            return
        self.probes[name] = {"identity": identity, "process": process, "first_seen": time.monotonic(),
            "samples": 0, "offline_samples": 0,
            "wait": asyncio.create_task(asyncio.to_thread(process.wait, self.expected[name] + 600))}
        self.record("audit", {"type": "probe.captured", **self.public_probe(name)})

    def public_probe(self, name):
        probe = self.probes[name]
        return {"script": name, **probe["identity"], "samples": probe["samples"], "offline_samples": probe["offline_samples"],
            **({"exit_code": probe["exit_code"], "observed_seconds": round(probe["exited_monotonic"] - probe["first_seen"], 3),
                "exited_at": probe["exited_at"]} if "exit_code" in probe else {})}

    async def stop(self):
        self.stopping = True
        await self.task

    def evidence(self, *, conversation_id, main_turn_id, child_id=None, child_turn_id=None):
        self.check()
        self.sample()
        assert set(self.probes) == set(self.expected), "A required probe never ran under a real backend command owner"
        for name, probe in self.probes.items():
            assert probe.get("exit_code") == 0, f"Original {name} has no independently observed successful exit"
            assert probe["identity"]["conversation_id"] == conversation_id, "Probe belongs to another conversation"
        main = self.probes["verify.py"]
        assert main["identity"]["parent_run_id"] == main_turn_id and main["offline_samples"] >= 2, "Original main probe lost its turn or offline observation"
        if child_id is not None:
            child = self.probes["child_probe.py"]
            assert child["identity"]["owner_task_id"] == child_id and child["identity"]["session_id"] == child_id, "Probe was not launched by the original reviewer"
            assert child["identity"]["parent_run_id"] == child_turn_id, "Child probe belongs to another reviewer turn"
        return {name: self.public_probe(name) for name in self.expected}


def original_probe_handle_evidence(processes, events, *, conversation_id, message_id, child_messages=None):
    """Bind external process identities to actual original turn tool receipts."""
    main = processes["verify.py"]
    calls = [event for event in events if event.get("type") == "tool_call"
        and event.get("conversation_id") == conversation_id and event.get("message_id") == message_id]
    results = {event["id"]: event for event in events if event.get("type") == "tool_result"
        and event.get("conversation_id") == conversation_id and event.get("message_id") == message_id
        and event.get("turn_id") == main["parent_run_id"] and event.get("is_error") is False}
    launches = [call for call in calls if call.get("args", {}).get("command") == main["command"]
        and call.get("turn_id") == main["parent_run_id"] and call["id"] in results
        and results[call["id"]].get("command_id") == main["command_id"]]
    polls = [call for call in calls if call.get("name") == "monitor"
        and call.get("turn_id") == main["parent_run_id"]
        and call.get("args", {}).get("command_id") == main["command_id"] and call["id"] in results
        and results[call["id"]].get("command_id") == main["command_id"] and results[call["id"]].get("exit_code") == 0]
    assert len(launches) == 1 and polls, "Main turn did not launch once and inspect its original completed command handle"
    receipt = {"main_launch_call_id": launches[0]["id"], "main_poll_call_ids": [call["id"] for call in polls]}
    if child_messages is None:
        return receipt
    child = processes["child_probe.py"]
    child_id = child["owner_task_id"]
    started = [event for event in events if event.get("type") == "subagent.start"
        and event.get("subagent_id") == child_id and event.get("conversation_id") == conversation_id
        and event.get("parent_message_id") == message_id and event.get("parent_run_id") == main["parent_run_id"]
        and event.get("run_id") == child["parent_run_id"]]
    assert len(started) == 1, "Child probe is not owned by the original main-turn reviewer"
    completed = [event for event in events if event.get("type") == "subagent.done"
        and event.get("subagent_id") == child_id and event.get("status") == "completed"]
    assert len({event["mailbox_epoch"] for event in completed}) >= 2, "Original reviewer has no actual final and follow-up"
    first_done = datetime.fromisoformat(completed[0]["timestamp"]).timestamp()
    assert child["create_time"] < first_done < child["exited_at"] - 5, "Original child process did not survive its initial final"
    launches = []
    polls = []
    for message in child_messages:
        if message["role"] != "assistant":
            continue
        for block in message.get("blocks", []):
            if block.get("type") != "tool_call":
                continue
            call = block["record"]
            if call.get("status") != "success":
                continue
            if call.get("args", {}).get("command") == child["command"]:
                launches.append((message, call))
            if (message["turn_id"] != child["parent_run_id"] and message["timestamp"] / 1000 > first_done
                    and call.get("name") == "monitor" and call.get("args", {}).get("command_id") == child["command_id"]
                    and call.get("exitCode") == 0 and "CHILD_PROBE_OK" in call.get("outputPreview", "")):
                polls.append(call)
    assert len(launches) == 1 and launches[0][0]["turn_id"] == child["parent_run_id"], "Reviewer restarted or borrowed the probe launch"
    assert polls, "Reviewer follow-up did not successfully poll its original completed command handle"
    return {**receipt, "child_id": child_id, "child_launch_call_id": launches[0][1]["id"],
        "child_followup_poll_call_ids": [call["id"] for call in polls], "initial_final_at": first_done}


def successful_file_reads(events, *, conversation_id, message_id, required_files):
    completed = {event["id"] for event in events if event.get("type") == "tool_result"
        and event.get("conversation_id") == conversation_id and event.get("message_id") == message_id
        and event.get("is_error") is False}
    reads = {Path(event["args"]["file_path"]).name: event["id"] for event in events
        if event.get("type") == "tool_call" and event.get("name") == "read_file"
        and event.get("conversation_id") == conversation_id and event.get("message_id") == message_id
        and event["id"] in completed}
    assert set(required_files) <= reads.keys(), {"required_files": required_files, "successful_reads": reads}
    return {name: reads[name] for name in required_files}


def provider_identity(settings):
    return {"model": settings.model, "wire_api": settings.wire_api,
        "endpoint_sha256": hashlib.sha256(settings.base_url.rstrip("/").encode()).hexdigest()}


def v2_board_tool_schemas(tool_namespace="collaboration"):
    """Use the production V2/board schema producers and namespace projection."""
    from backend.agents.source_roles import source_agent_options
    from backend.config import AppConfig, LLMSettings
    from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
    from backend.feature_flags import FeatureFlags
    from backend.tools.agent_tools import TaskTool
    from backend.tools.agent_message_board_tools import message_board_tools
    from backend.tools.collaboration_tools import SpawnAgentTool, ListAgentsTool, WaitAgentTool, InterruptAgentTool
    from backend.tools.swarm_tools import SourceSendMessageTool, FollowupTaskTool
    from backend.tools.toolsets import ToolsetPolicy

    config = AppConfig(llm=LLMSettings(api_key=""),
        feature_flags=FeatureFlags({"multi_agent_v2": True, "agent_message_board": True}),
        config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"),
            {"features": {"multi_agent_v2": {"enabled": True, "tool_namespace": tool_namespace},
                "agent_message_board": True}}),)))
    policy = ToolsetPolicy(coordination_options=source_agent_options(config))
    # This task instance produces the actual spawn schema; preflight never executes it.
    task = TaskTool(artifact_store=None, config_snapshot=config)
    tools = [ListAgentsTool(), SpawnAgentTool(task), SourceSendMessageTool(), FollowupTaskTool(),
        WaitAgentTool(config), InterruptAgentTool(), *message_board_tools()]
    return [policy.project_tool_schema(tool.name, tool.get_schema()).to_openai_tool() for tool in tools]


async def namespace_preflight(settings, probe_root, tool_namespace="collaboration"):
    """Prove the exact configured provider's native namespace round-trip."""
    from backend.agent.provider_lifecycle import LIFECYCLE_RUNTIME_METADATA_KEY
    from backend.llm.base import LLMMessage, StreamEventType
    from backend.llm.openai_adapter import OpenAIAdapter
    from backend.agent.run_context import RunContext
    from backend.agent.runtime import AgentRuntime
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.tools.collaboration_tools import ListAgentsTool

    tools = v2_board_tool_schemas(tool_namespace)
    expected_tools = OpenAIAdapter._convert_tools_to_responses_format(tools)
    receipt = {**provider_identity(settings), "namespace": tool_namespace,
        "tool_names": sorted(tool["function"]["name"] for tool in tools),
        "passed": False, "requests": [], "responses": []}
    if settings.wire_api != "responses":
        receipt["error"] = "The configured wire API does not carry native Source tool namespaces"
        return receipt

    class Recorder:
        async def emit_before_provider_request(self, payload):
            receipt["requests"].append({key: payload[key] for key in ("model", "tools", "input")})
            return payload

        async def emit_after_provider_response(self, status, headers):
            receipt["responses"].append({"status": status,
                "request_id": headers.get("x-request-id", headers.get("request-id", ""))})

    nonce = uuid4().hex
    tool = ListAgentsTool()
    messages = [LLMMessage(role="user", content=f"Call the real Source {tool_namespace}.list_agents tool exactly once with {{}}. "
        f"After receiving its result, reply NAMESPACE_PROBE_OK followed by {nonce}. Do not claim to call a tool in text.")]
    metadata = {LIFECYCLE_RUNTIME_METADATA_KEY: Recorder()}
    adapter = OpenAIAdapter(settings)
    async def capture_provider_error(response):
        if response.is_error:
            body = (await response.aread()).decode("utf-8", errors="replace")
            receipt["provider_error_body"] = body.replace(settings.api_key, "[redacted]")[:4096]
    adapter._http_client.event_hooks["response"].append(capture_provider_error)
    started = time.monotonic()
    try:
        async with asyncio.timeout(90):
            events = [event async for event in adapter.stream_chat(messages, tools, metadata)]
            errors = [event.content for event in events if event.type == StreamEventType.ERROR]
            assert not errors, errors
            calls = {call.id: call for event in events for call in event.tool_calls}
            assert len(calls) == 1, "Provider did not return exactly one native tool call"
            call = next(iter(calls.values()))
            assert (call.name, call.native_namespace, call.native_name, call.arguments) == (
                "list_agents", tool_namespace, "list_agents", {}), "Provider changed the namespace, name or arguments"
            assert not call.arguments_repaired and not call.duplicate_id
            assert receipt["requests"][0]["tools"] == expected_tools, "Actual request changed the complete native tool inventory"
            runtime = AgentRuntime(swarm_store_dir=probe_root / "swarm", enable_lease_heartbeat=False)
            try:
                run_id, conversation_id = "namespace-" + nonce, "namespace-tree-" + nonce
                runtime.start_run(run_id=run_id, conversation_id=conversation_id)
                context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), conversation_id=conversation_id,
                    turn_id=run_id, metadata={"run_id": run_id}, run_context=RunContext(agent_runtime=runtime, multi_agent_version="v2"))
                actual = await tool.execute(call.arguments, context)
                assert not actual.is_error and json.loads(actual.content)["agents"][0]["agent_name"] == "/root"
                receipt["actual_tool_result"] = json.loads(actual.content)
                runtime.commit_terminal(run_id, "completed")
            finally:
                await runtime.aclose(release_lease=True)
            native_items = next((event.provider_items for event in reversed(events) if event.provider_items), [])
            messages.extend([LLMMessage(role="assistant", tool_calls=[call], provider_items=native_items),
                LLMMessage(role="tool", tool_call_id=call.id, name=call.name, content=actual.content)])
            events = [event async for event in adapter.stream_chat(messages, tools, metadata)]
            errors = [event.content for event in events if event.type == StreamEventType.ERROR]
            assert not errors and not any(event.tool_calls for event in events), errors
            assert receipt["requests"][1]["tools"] == expected_tools, "Tool-result request changed the complete native tool inventory"
            answer = "".join(event.content for event in events if event.type == StreamEventType.TEXT_CHUNK)
            assert "NAMESPACE_PROBE_OK" in answer and nonce in answer, "Provider did not acknowledge the native tool result"
            receipt.update(passed=True, native_call={"id": call.id, "namespace": call.native_namespace,
                "name": call.native_name, "arguments": call.arguments}, answer=answer)
    except Exception as error:
        receipt["error"] = f"{type(error).__name__}: {error}".replace(settings.api_key, "[redacted]")
    finally:
        await adapter.aclose()
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return receipt


def configure_v2_board_audit(state, profile, config, preflight, tool_namespace="collaboration"):
    """Capture production V2 metadata after proving the exact provider transport."""
    assert preflight["passed"] and all(preflight[key] == value for key, value in provider_identity(config.llm).items()), \
        "No successful namespace proof for the exact configured model, endpoint and wire API"
    from backend.llm.openai_adapter import OpenAIAdapter
    schemas = v2_board_tool_schemas(tool_namespace)
    expected_tools = OpenAIAdapter._convert_tools_to_responses_format(schemas)
    assert preflight.get("namespace") == tool_namespace, "Namespace proof does not match the explicitly selected namespace"
    assert preflight.get("tool_names") == sorted(tool["function"]["name"] for tool in schemas), \
        "Namespace proof does not cover all six V2 and nine board tools"
    assert len(preflight["requests"]) == 2 and all(request["tools"] == expected_tools for request in preflight["requests"]), \
        "Namespace proof did not send the complete production schemas in both requests"
    from backend.llm.model_runtime import ModelRuntime
    from backend.llm.provider_models import ProviderModelsStorage
    from backend.llm.model_request_policy import source_model_request_capabilities

    settings = config.config_layer_stack.effective_config() if config.config_layer_stack is not None else profile
    captured = ModelRuntime(settings_snapshot=settings)
    try:
        definition = captured.get_model(config.llm.provider, config.llm.model)
        assert definition is not None and definition.api == "openai-responses", \
            "The production model catalog does not advertise the configured Responses model"
        assert definition.extra.get("multi_agent_version") == "v2", \
            "The production model catalog does not advertise V2; namespace support alone is insufficient"
        capabilities = source_model_request_capabilities(config.llm.model, definition)
        assert capabilities["multi_agent_version"] == "v2"
        metadata = {key: definition.extra[key] for key in ("multi_agent_version", "multi_agent_reasoning_effort")
            if key in definition.extra}
        source = definition.source_model_metadata
        public_definition = {"provider": definition.provider, "model": definition.id, "name": definition.name,
            "api": definition.api, "context_window": definition.context_window,
            "supports_custom_tools": definition.supports_custom_tools, **metadata,
            "source_model_metadata": {"reference_commit": source.reference_commit, "catalog_file": source.catalog_file}
                if source is not None else None}
        definition_sha256 = hashlib.sha256(json.dumps(public_definition, sort_keys=True).encode()).hexdigest()
    finally:
        captured.retire()

    profile["features"] = {"multi_agent_v2": {"enabled": True, "tool_namespace": tool_namespace}, "agent_message_board": True}
    profile["agent"]["code_mode_only"] = False
    folder = state / ".minicode"
    folder.mkdir()
    catalog = {"providers": {"custom": {"api": "openai-responses", "base_url": config.llm.base_url,
        "models": [{"id": config.llm.model, "name": public_definition["name"], **metadata,
            "context_window": config.llm.context_window, "supports_custom_tools": config.llm.supports_custom_tools}]}}}
    path = folder / "models.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    runtime = ModelRuntime(models_path=path, models_store=ProviderModelsStorage(folder / "models-store.json"), settings_snapshot=profile)
    try:
        definition = runtime.get_model("custom", config.llm.model)
        assert definition is not None and definition.api == "openai-responses"
        capabilities = source_model_request_capabilities(config.llm.model, definition)
        assert capabilities["multi_agent_version"] == "v2", "Captured child model catalog did not retain V2"
        assert all(definition.extra[key] == value for key, value in metadata.items()), "Captured child metadata changed"
        return {"provider": definition.provider, "model": definition.id, "api": definition.api,
            **metadata, "catalog_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "production_model_definition": public_definition, "production_definition_sha256": definition_sha256,
            "namespace_preflight": {**provider_identity(config.llm), "namespace": tool_namespace,
                "tool_names": preflight["tool_names"]}, "board_backend": "local", "namespace": tool_namespace}
    finally:
        runtime.retire()


def v2_board_evidence(events, state, *, conversation_id, message_id, channel, nonce, child_probe, tool_namespace="collaboration"):
    import sqlite3
    from backend.agent.agent_identity import AgentPath

    calls = [event for event in events if event.get("type") == "tool_call"
        and event.get("conversation_id") == conversation_id and event.get("message_id") == message_id
        and not event.get("streaming_announcement")]
    turns = {event["turn_id"] for event in calls}
    assert len(turns) == 1, ("Main audit tool calls lost their turn identity", turns)
    turn_id = next(iter(turns))
    results = {event["id"]: event for event in events if event.get("type") == "tool_result"
        and event.get("conversation_id") == conversation_id and event.get("message_id") == message_id
        and event.get("turn_id") == turn_id and event.get("is_error") is False}
    completed = [event for event in calls if event["id"] in results]
    required = {"create_channel", "post", "spawn_agent"} | ({"followup_task"} if child_probe else set())
    assert required <= {event["name"] for event in completed}, ("Missing actual Source operations", required)
    from backend.tools.toolsets import SOURCE_V2_COORDINATION_TOOLS, SOURCE_MESSAGE_BOARD_TOOLS
    assert all(event.get("native_namespace") == tool_namespace for event in completed
        if event["name"] in SOURCE_V2_COORDINATION_TOOLS | SOURCE_MESSAGE_BOARD_TOOLS), \
        "V2/board audit used a flat or wrong native namespace"
    assert not {"task", "task_status", "task_stop", "task_create", "team_create"} & {event["name"] for event in completed}, \
        "V2 audit silently used legacy coordination"
    spawn_ids = {event["id"] for event in completed if event["name"] == "spawn_agent"}
    children = {event["subagent_id"]: event for event in events if event.get("type") == "subagent.start"
        and event.get("conversation_id") == conversation_id and event.get("parent_message_id") == message_id
        and event.get("parent_tool_call_id") in spawn_ids and event.get("parent_run_id") == turn_id}
    assert children, "No genuine child belongs to the Source spawn and original main turn"
    databases = list(state.rglob("agent_message_board_1.sqlite"))
    assert len(databases) == 1, databases
    with sqlite3.connect(databases[0].resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("SELECT * FROM posts WHERE board=? AND channel=? ORDER BY seq", (conversation_id, channel))]
        creators = list(db.execute("SELECT author FROM channels WHERE board=? AND name=?", (conversation_id, channel)))
    assert creators and creators[0][0] == "/root", "Channel does not belong to the genuine root"
    root = next(row for row in rows if row["author"] == "/root" and json.loads(row["payload"])["text"] == "ROOT_BOARD " + nonce)
    assert root["request_id"].startswith(f"/root:{turn_id}:")
    child_rows = []
    for child_id, child in children.items():
        path = AgentPath.parse(child["agent_path"]).logical_name
        matches = [row for row in rows if row["author"] == path and row["root"] == root["id"]
            and row["request_id"].startswith(f"{path}:{child['run_id']}:")]
        initial = [row for row in matches if json.loads(row["payload"])["text"] == "CHILD_BOARD " + nonce]
        followup = [row for row in matches if json.loads(row["payload"])["text"] == "CHILD_FOLLOWUP_BOARD " + nonce]
        if initial and (followup or not child_probe):
            child_rows = initial + followup
            selected_child = child_id
            break
    assert child_rows, "No actual original child posted the initial/follow-up acknowledgements in the original board thread"
    last_write = max(row["timestamp"] for row in child_rows) // 1000
    reads = [event for event in completed if event["name"] in {"read_post", "read_thread"}
        and event.get("started_at", 0) >= last_write and (event["args"].get("thread_id") == root["id"]
            or event["args"].get("message_id") in {row["id"] for row in child_rows})]
    assert reads, "Root did not successfully read the actual child's latest board result"
    return {"namespace": tool_namespace, "turn_id": turn_id, "channel": channel, "thread_id": root["id"], "child_id": selected_child,
        "child_post_ids": [row["id"] for row in child_rows], "root_read_call_ids": [event["id"] for event in reads],
        "source_call_ids": {name: [event["id"] for event in completed if event["name"] == name] for name in sorted(required)}}


class Client:
    def __init__(self, url: str, trace, secret: str):
        self.url, self.trace, self.secret = url, trace, secret
        self.events: list[dict] = []
        self.last_seq = 0
        self.ws = None
        self.reader = None
        self._recovering = False
        self._recovery_command_id = None
        self._recovery_snapshot = None
        self._recovery_replay_pending = False
        self._recovery_owner_pending = False
        self._recovery_events: list[dict] = []
        self._delivered_event_ids: dict[str, int] = {}
        self._snapshot_seq = 0
        self.process_audit = None

    def record(self, direction: str, payload: dict):
        row = {"direction": direction, "elapsed": round(time.monotonic() - self.started, 3), "payload": payload}
        self.trace.write(json.dumps(row, ensure_ascii=False).replace(self.secret, "[redacted]") + "\n")
        self.trace.flush()

    async def connect(self):
        import websockets
        if self.reader is not None:
            self._begin_recovery()
        self.ws = await websockets.connect(self.url, max_size=None, ping_timeout=60)
        self.reader = asyncio.create_task(self.read())

    def _begin_recovery(self):
        # Match useWebSocket's open/restore gate: live durable frames can
        # arrive before the canonical owner and its replay are projected.
        self._recovering = True
        self._recovery_command_id = None
        self._recovery_snapshot = None
        self._recovery_replay_pending = False
        self._recovery_owner_pending = False
        self._recovery_events = []

    def _deliver_durable(self, event):
        event_id = event.get("event_id")
        if event_id in self._delivered_event_ids:
            assert self._delivered_event_ids[event_id] == event["seq"], event
            return
        if event["seq"] <= self.last_seq:
            # An empty-replay snapshot is the authoritative projection through
            # current_seq. Buffered frames inside that snapshot are covered.
            assert event["seq"] <= self._snapshot_seq, event
            return
        assert event["previous_replay_seq"] == self.last_seq and event["seq"] > self.last_seq, event
        self.last_seq = event["seq"]
        if event_id:
            self._delivered_event_ids[event_id] = event["seq"]
        self.events.append(event)

    def _finish_recovery(self):
        if (self._recovery_snapshot is None or self._recovery_replay_pending
                or self._recovery_owner_pending):
            return
        buffered, self._recovery_events = self._recovery_events, []
        for event in buffered:
            self._deliver_durable(event)
        self._recovering = False
        self._recovery_command_id = None

    def _receive(self, event):
        kind = event["type"]
        recovery_projection = kind in {"session.restored", "session.synced", "session.replay"}
        if self._recovering and recovery_projection and event.get("client_command_id") != self._recovery_command_id:
            return  # Its raw frame remains in the trace; it is not our restore.
        if (self._recovering and kind == "command.result"
                and event.get("client_command_id") == self._recovery_command_id
                and event.get("level") in {"error", "failed"}):
            self.events.append(event)
            raise AssertionError(event)
        if self._recovering and "previous_replay_seq" in event and not recovery_projection:
            self._recovery_events.append(event)
            return
        if kind == "session.replay":
            if self._recovering:
                assert self._recovery_snapshot is not None and self._recovery_replay_pending, event
                assert event["current_seq"] == self._recovery_snapshot["current_seq"], event
                assert event["replayed_events"] == self._recovery_snapshot["replayed_events"], event
            assert event["last_seq"] == self.last_seq, event
            assert event["replayed_events"] == len(event["events"]), event
            for replayed in event["events"]:
                assert replayed["previous_replay_seq"] == self.last_seq and replayed["seq"] > self.last_seq, replayed
                self._deliver_durable(replayed)
            assert self.last_seq == event["current_seq"], event
            self._recovery_replay_pending = False
        elif kind in {"session.restored", "session.synced"}:
            if event["replayed_events"] == 0:
                if event.get("cursor_reset") is True:
                    assert event["requested_last_seq"] > event["current_seq"] == event["last_seq"], event
                    self._delivered_event_ids.clear()
                else:
                    assert event["last_seq"] == self.last_seq and event["current_seq"] >= self.last_seq, event
                self.last_seq = event["current_seq"]
                self._snapshot_seq = self.last_seq
            if self._recovering:
                self._recovery_snapshot = event
                self._recovery_replay_pending = event["replayed_events"] > 0
                self._recovery_owner_pending = kind == "session.restored" and event.get("conversation_switched_follows") is True
        elif "previous_replay_seq" in event:
            self._deliver_durable(event)
            return
        elif (self._recovering and kind == "conversation.switched"
                and event.get("client_command_id") == self._recovery_command_id
                and self._recovery_snapshot is not None
                and event.get("conversation_id") == self._recovery_snapshot.get("active_conversation_id")):
            self._recovery_owner_pending = False
        self.events.append(event)
        if self._recovering:
            self._finish_recovery()

    async def read(self):
        async for raw in self.ws:
            event = json.loads(raw)
            self.record("server", event)
            self._receive(event)

    async def send(self, kind: str, **data):
        payload = {"type": kind, "client_command_id": uuid4().hex, **data}
        if kind in {"session.restore", "session.sync"}:
            if not self._recovering:
                self._begin_recovery()
            self._recovery_command_id = payload["client_command_id"]
        self.record("client", payload)
        await self.ws.send(json.dumps(payload))
        return payload["client_command_id"]

    async def until(self, predicate, label: str, *, after=0, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process_audit is not None:
                self.process_audit.check()
            if self.reader.done():
                await self.reader
            for event in self.events[after:]:
                if predicate(event):
                    return event
            if self.reader.done():
                raise RuntimeError(f"Transport ended while awaiting {label}")
            await asyncio.sleep(0.1)
        raise TimeoutError(label)

    async def command(self, kind: str, **data):
        offset = len(self.events)
        command_id = await self.send(kind, **data)
        projection = {"conversation.switch": "conversation.switched", "session.restore": "session.restored", "session.sync": "session.synced"}.get(kind)
        if projection:
            result = await self.until(lambda event: event.get("type") == projection
                and event.get("client_command_id") == command_id
                and (kind != "conversation.switch" or event.get("conversation_id") == data["conversation_id"]), kind, after=offset)
            if kind == "session.restore" and result.get("conversation_switched_follows") is True:
                # useWebSocket releases this gate at the canonical owner receipt;
                # is_hydrating tracks the subsequent context hydration separately.
                await self.until(lambda event: event.get("type") == "conversation.switched"
                    and event.get("client_command_id") == command_id
                    and event.get("conversation_id") == result.get("active_conversation_id"),
                    "canonical restored owner", after=offset)
            if kind in {"session.restore", "session.sync"} and result["replayed_events"]:
                await self.until(lambda event: event.get("type") == "session.replay"
                    and event.get("client_command_id") == command_id
                    and event["current_seq"] == result["current_seq"], "complete session replay", after=offset)
            return result
        result = await self.until(lambda event: event.get("type") == "command.result"
            and event.get("client_command_id") == command_id, kind, after=offset)
        assert result.get("level") not in {"error", "failed"}, result
        return result

    async def terminal(self, owner: str, message_id: str, *, timeout: float = 420):
        def completed(event):
            if event.get("type") == "done" and event.get("conversation_id") == owner and event.get("message_id") == message_id:
                assert event["status"] == "completed", event
                return True
            if event.get("type") in {"session.restored", "session.synced"} and event.get("active_conversation_id") == owner:
                return any(message["id"] == message_id and message.get("terminal_status") == "completed"
                           for message in event.get("messages", []))
            if event.get("type") == "conversation.switched" and event.get("conversation_id") == owner:
                return any(message["id"] == message_id and message.get("terminal_status") == "completed"
                           for message in event["conversation"]["transcript"])
            return False
        return await self.until(completed, "terminal " + message_id, timeout=timeout)

    async def close(self):
        await self.ws.close()
        await self.reader


async def exercise(args, config):
    import httpx
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    state, workspace = out / "state", out / "workspace"
    data = state / "data"
    data.mkdir(parents=True)
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "verify.py").write_text(VERIFIER, encoding="utf-8")
    if args.child_probe:
        (workspace / "child_probe.py").write_text(CHILD_PROBE, encoding="utf-8")
    side_identity = "side-identity-" + uuid4().hex
    (workspace / "B_NOTE.txt").write_text(side_identity, encoding="utf-8")
    requirements = ("Implement solution.py with reconcile(rows). Each row is a dict with string id, account, amount. "
        "For duplicate ids keep the first row globally, including when later duplicates name another account. "
        "Sum signed amounts exactly with Decimal per account. Return account -> amount string with exactly two "
        "fractional digits. Include accounts whose total is zero. Input order and duplicate conflicts must work. "
        "Do not mutate the caller's rows. Use only the Python standard library.\n")
    (workspace / "REQUIREMENTS.md").write_text(requirements, encoding="utf-8")
    profile = {"llm": {"provider": "custom", "custom": {"base_url": config.llm.base_url,
        "model": config.llm.model, "wire_api": config.llm.wire_api,
        "available_models": [config.llm.model], "reasoning_effort": config.llm.reasoning_effort,
        "context_window": config.llm.context_window, "proxy_mode": config.llm.proxy_mode}},
        "agent": {"max_turn_seconds": args.turn_budget_seconds}}
    v2_snapshot = None
    if args.multi_agent_v2_board:
        if args.namespace_preflight_receipt is not None:
            preflight = json.loads(args.namespace_preflight_receipt.read_text(encoding="utf-8"))
        else:
            preflight = await namespace_preflight(config.llm, out / "namespace-runtime", args.tool_namespace)
        (out / "namespace-preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")
        v2_snapshot = configure_v2_board_audit(state, profile, config, preflight, args.tool_namespace)
        (out / "v2-model-snapshot.json").write_text(json.dumps(v2_snapshot, indent=2), encoding="utf-8")
    (state / "settings.json").write_text(json.dumps(profile), encoding="utf-8")
    (data / "trusted_workspaces.json").write_text(json.dumps({"version": 1, "roots": [str(workspace)]}), encoding="utf-8")
    (data / "active_workspace.json").write_text(json.dumps({"root": str(workspace)}), encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, MINICODE_STATE_ROOT=str(state), PYTHONUTF8="1", LLM_PROVIDER="custom",
        CUSTOM_API_KEY=config.llm.api_key, CUSTOM_BASE_URL=config.llm.base_url,
        CUSTOM_MODEL=config.llm.model, CUSTOM_WIRE_API=config.llm.wire_api)
    # This isolated loopback process uses the normal tokenless-local auth path.
    env.pop("MINICODE_RUNTIME_TOKEN", None)
    run_id = uuid4().hex
    a, b = "", ""
    messages = {"a": "assistant_a_" + run_id, "b": "assistant_b_" + run_id, "queue": "assistant_q_" + run_id}
    result = {"model": config.llm.model, "wire_api": config.llm.wire_api,
        "message_ids": messages, "passed": False, "acceptance_parameters": {
            "oracle_seconds": args.seconds, "child_seconds": args.child_seconds,
            "turn_budget_seconds": args.turn_budget_seconds, "disconnect_seconds": args.disconnect_seconds,
            "roundtrip_interval_seconds": args.roundtrip_interval, "multi_agent_v2_board": args.multi_agent_v2_board,
            "tool_namespace": args.tool_namespace}}
    if v2_snapshot is not None:
        result["v2_model_snapshot"] = v2_snapshot
    board_channel, board_nonce = "audit-" + run_id, uuid4().hex
    started = time.monotonic()
    source_snapshot = backend_source_snapshot()
    (out / "source-snapshot.json").write_text(json.dumps(source_snapshot, indent=2), encoding="utf-8")
    with (out / "backend.log").open("w", encoding="utf-8") as backend_log, (out / "wire.jsonl").open("w", encoding="utf-8") as trace:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
            "--port", str(port), "--log-level", "warning"], cwd=ROOT, env=env, stdout=backend_log,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        client = Client(f"ws://127.0.0.1:{port}/ws?session_id=session_audit_{run_id}", trace, config.llm.api_key)
        client.started = started
        process_audit = ProbeProcessAudit(process.pid, state, workspace, args.seconds,
            child_seconds=args.child_seconds if args.child_probe else None, record=client.record)
        client.process_audit = process_audit
        process_audit.start()
        backend_ready = False
        try:
            async with httpx.AsyncClient(trust_env=False) as http:
                deadline = time.monotonic() + 60
                while True:
                    try:
                        response = await http.get(f"http://127.0.0.1:{port}/health")
                        if response.is_success:
                            break
                    except httpx.ConnectError:
                        pass  # Expected before uvicorn begins listening.
                    assert process.poll() is None, "Backend exited during startup; see backend.log"
                    if time.monotonic() > deadline:
                        raise TimeoutError("Backend startup; see backend.log")
                    await asyncio.sleep(0.2)
            backend_ready = True
            await client.connect()
            await client.send("ping")
            await client.until(lambda e: e.get("type") == "pong", "initial handshake")
            for label, title, activate in (("a", "Long reconciliation task", True), ("b", "Independent side task", False)):
                created = await client.command("conversation.create", title=title,
                    workspace_root=str(workspace), permission_mode="bypass", activate=activate)
                identity = created["data"]["conversation_id"]
                if label == "a":
                    a = identity
                else:
                    b = identity
                result["conversation_" + label] = identity
                await client.command("conversation.memory_mode.set", conversation_id=identity, memory_mode="disabled")
            python_command = Path(sys.executable).as_posix()
            prompt = ("Work only in the provided workspace. Read REQUIREMENTS.md, implement solution.py, and verify it. "
                "Do not edit verify.py, B_NOTE.txt or REQUIREMENTS.md. Delegate one read-only review of your solution "
                "to an available subagent and integrate any defects it actually finds. Use the real tools. "
                f"Run exactly this PowerShell command: & '{python_command}' -u verify.py --seconds {args.seconds}\n"
                "Use explicit run_in_background=true so the command has no foreground runtime limit. "
                "This verification deliberately runs longer than a tool's first yield. Keep following its actual command handle "
                "until it exits, inspect verification.json, and wait for your reviewer. Do not send a final answer while work "
                "is running. Only after independent verification passes, finish with A_LONG_OK and the actual batch count.")
            if args.child_probe:
                prompt += ("\nAlso verify the reviewer's persistent thread resources. In its INITIAL review task, require it "
                    f"to launch exactly once `& '{python_command}' -u child_probe.py --seconds {args.child_seconds}` using the real command tool with run_in_background=true and a short yield. "
                    "Have the reviewer return its review and actual running command handle in a final answer while that "
                    f"{args.child_seconds}-second independent business probe is still running. The probe only writes child-progress.jsonl/child-result.json in this "
                    "workspace; do not edit child_probe.py or change the business solution as part of the read-only review. "
                    "Do NOT close or interrupt the reviewer after its initial final. After the main verifier completes, "
                    "send a real follow-up task to THE SAME reviewer asking it to poll its ORIGINAL command handle and "
                    "read child-result.json, without rerunning or replacing the probe. Wait for the actual follow-up "
                    "completion and CHILD_PROBE_OK before your own final answer.")
            if args.multi_agent_v2_board:
                prompt += (f"\nThis is an explicit Source V2 and message-board audit in the {args.tool_namespace} namespace. First use {args.tool_namespace}.create_channel "
                    f"to create {board_channel}, then {args.tool_namespace}.post text exactly `ROOT_BOARD {board_nonce}` to that channel. "
                    f"Delegate the reviewer through the native {args.tool_namespace}.spawn_agent tool with task_name=reviewer. "
                    "In its initial task, provide the ACTUAL returned board thread_id, require the reviewer to read that thread "
                    f"and post a reply with text exactly `CHILD_BOARD {board_nonce}` before its initial final. "
                    f"Use the actual {args.tool_namespace} task_name for coordination. "
                    + (f"For the original reviewer's real {args.tool_namespace}.followup_task after the main verifier completes, also require it to post "
                       f"`CHILD_FOLLOWUP_BOARD {board_nonce}` to that SAME board thread. " if args.child_probe else "")
                    + f"After the reviewer finishes, read the actual latest board reply with {args.tool_namespace}.read_thread/{args.tool_namespace}.read_post before your own final. "
                    f"Use direct native {args.tool_namespace} tools where offered. Do not substitute legacy task/team helpers, command writes "
                    "to the board database, invented tool names, or a second reviewer.")
            await client.command("user_message", conversation_id=a, content=prompt, permission_mode="bypass",
                user_message_id="user_a_" + run_id, assistant_message_id=messages["a"])
            deadline = time.monotonic() + 360
            while "verify.py" not in process_audit.probes or not (workspace / "progress.jsonl").exists():
                process_audit.check()
                if any(e.get("type") == "done" and e.get("message_id") == messages["a"] for e in client.events):
                    raise AssertionError("Main turn ended before starting the independent long verifier")
                if time.monotonic() > deadline:
                    raise TimeoutError("Real model did not start the verifier")
                await asyncio.sleep(0.2)
            result["long_tool_started_at"] = round(time.monotonic() - started, 3)
            print("Long verifier started; switching to B, queuing A follow-up and disconnecting", flush=True)
            await client.command("conversation.switch", conversation_id=b)
            await client.command("user_message", conversation_id=b,
                content="Read B_NOTE.txt with the file tool. Reply B_SIDE_OK followed by its exact contents. Do not edit any file.",
                permission_mode="bypass", user_message_id="user_b_" + run_id, assistant_message_id=messages["b"])
            queued = await client.command("user_message", conversation_id=a, content="After the previous task completes, read verification.json. Reply A_QUEUE_OK with the verified batch count. Do not edit anything.",
                permission_mode="bypass", queue_if_busy=True, streaming_behavior="follow_up",
                user_message_id="user_q_" + run_id, assistant_message_id=messages["queue"])
            assert queued["data"]["queued"] is True, queued
            for identity in (a, b, a, b):
                await client.command("conversation.switch", conversation_id=identity)
            before = len((workspace / "progress.jsonl").read_text(encoding="utf-8").splitlines())
            cursor = client.last_seq
            process_audit.offline = True
            await client.close()
            client.record("audit", {"type": "offline", "last_seq": cursor, "progress_batches": before})
            await asyncio.sleep(args.disconnect_seconds)
            after = len((workspace / "progress.jsonl").read_text(encoding="utf-8").splitlines())
            assert after > before + 20, ("Verifier stopped during transport retirement", before, after)
            result["offline_progress"] = {"before": before, "after": after, "offline_seconds": args.disconnect_seconds}
            await client.connect()
            await client.command("session.restore", last_seq=cursor, last_conversation_id=b)
            process_audit.offline = False
            client.record("audit", {"type": "reconnected", "active_conversation_id": b})
            for identity in (a, b, a, b):
                await client.command("conversation.switch", conversation_id=identity)
            live_checks = []
            next_check = time.monotonic() + args.roundtrip_interval
            while args.roundtrip_interval > 0 and not (workspace / "verification.json").exists():
                await asyncio.sleep(min(30, max(0, next_check - time.monotonic())))
                process_audit.check()
                if time.monotonic() < next_check:
                    continue
                assert process.poll() is None, "Original backend exited during the endurance run"
                if any(e.get("type") == "done" and e.get("message_id") == messages["a"] for e in client.events):
                    assert (workspace / "verification.json").exists(), "Main turn ended before the oracle completed"
                    break
                marker = "B_LIVE_OK_" + str(len(live_checks) + 1)
                message_id = "assistant_live_" + uuid4().hex
                event_start = len(client.events)
                before_batches = len((workspace / "progress.jsonl").read_text(encoding="utf-8").splitlines())
                await client.command("conversation.switch", conversation_id=b)
                await client.command("user_message", conversation_id=b,
                    content=f"Make new successful dedicated read_file calls for BOTH B_NOTE.txt and the current tail of progress.jsonl. Prior chat memory is not accepted for this check. Reply {marker} with the exact B_NOTE.txt contents and CURRENT_BATCH=<the actual integer batch from the current progress tail>. Do not modify files.",
                    permission_mode="bypass", user_message_id="user_live_" + uuid4().hex,
                    assistant_message_id=message_id)
                await client.terminal(b, message_id)
                read_calls = successful_file_reads(client.events[event_start:], conversation_id=b,
                    message_id=message_id, required_files=("B_NOTE.txt", "progress.jsonl"))
                live_checks.append({"message_id": message_id, "marker": marker,
                    "elapsed": round(time.monotonic() - started, 3),
                    "successful_read_calls": read_calls, "before_batches": before_batches,
                    "progress_batches": len((workspace / "progress.jsonl").read_text(encoding="utf-8").splitlines())})
                await client.command("conversation.switch", conversation_id=a)
                print("Live model round-trip completed", json.dumps(live_checks[-1]), flush=True)
                next_check = time.monotonic() + args.roundtrip_interval
            result["live_roundtrips"] = live_checks
            observation_timeout = max(420, args.seconds + args.child_seconds + 600)
            for label, message_id in messages.items():
                terminal = await client.terminal(b if label == "b" else a, message_id, timeout=observation_timeout)
                result.setdefault("terminal_evidence", {})[label] = {"type": terminal["type"], "event_id": terminal["event_id"]}
            queue_receipts = [event for event in client.events if event.get("type") == "command.result"
                and event.get("client_command_id") == queued["client_command_id"]]
            assert any(event.get("data", {}).get("admitted") is True for event in queue_receipts)
            assert all(event["data"]["client_command_id"] == queued["client_command_id"] for event in queue_receipts)
            result["queue_origin"] = {"client_command_id": queued["client_command_id"],
                "receipt_event_ids": sorted({event["event_id"] for event in queue_receipts}),
                "nested_identity_matches": True}
            await client.command("conversation.switch", conversation_id=a)
            await client.command("session.sync", last_seq=client.last_seq)
            child_messages = None
            if args.child_probe:
                assert "child_probe.py" in process_audit.probes, "No original child probe process was observed"
                child_id = process_audit.probes["child_probe.py"]["identity"]["owner_task_id"]
                child_transcript = await client.command("subagent.transcript", conversation_id=a, subagent_id=child_id)
                assert child_transcript["data"]["conversation_id"] == a and child_transcript["data"]["subagent_id"] == child_id
                child_messages = child_transcript["data"]["messages"]
                (out / "child-transcript.json").write_text(json.dumps(child_transcript["data"], ensure_ascii=False), encoding="utf-8")
            await client.close()
            assert (workspace / "verify.py").read_text(encoding="utf-8") == VERIFIER, "Oracle was modified"
            verified = json.loads((workspace / "verification.json").read_text(encoding="utf-8"))
            assert verified["passed"] and verified["elapsed_seconds"] >= args.seconds
            from backend.conversations.repository import ConversationRepository
            repository = ConversationRepository(data / "conversations")
            records = {key: repository.get_conversation(identity) for key, identity in (("a", a), ("b", b))}
            by_id = {message["id"]: message for record in records.values() for message in record.transcript}
            main_turns = {event["turn_id"] for event in client.events if event.get("type") == "tool_call"
                and event.get("conversation_id") == a and event.get("message_id") == messages["a"]}
            assert len(main_turns) == 1, "Main probe lost its original turn"
            child_start = next((event for event in client.events if args.child_probe and event.get("type") == "subagent.start"
                and event.get("subagent_id") == child_id), None)
            result["probe_processes"] = process_audit.evidence(conversation_id=a, main_turn_id=next(iter(main_turns)),
                child_id=child_id if args.child_probe else None, child_turn_id=child_start["run_id"] if child_start is not None else None)
            result["probe_handle_evidence"] = original_probe_handle_evidence(result["probe_processes"], client.events,
                conversation_id=a, message_id=messages["a"], child_messages=child_messages)
            assert verified["pid"] == result["probe_processes"]["verify.py"]["pid"]
            main_progress = [json.loads(line) for line in (workspace / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
            assert {row["pid"] for row in main_progress} == {verified["pid"]}
            assert verified["batches"] == len(main_progress) and verified["checked_rows"] == len(main_progress) * 602
            assert [row["batch"] for row in main_progress] == list(range(1, verified["batches"] + 1))
            for label, marker in (("a", "A_LONG_OK"), ("b", "B_SIDE_OK"), ("queue", "A_QUEUE_OK")):
                assert marker in by_id[messages[label]]["content"], (label, by_id[messages[label]])
            assert side_identity in by_id[messages["b"]]["content"]
            for check in live_checks:
                assert check["marker"] in by_id[check["message_id"]]["content"]
                assert side_identity in by_id[check["message_id"]]["content"]
                actual_batch = re.search(r"CURRENT_BATCH\s*=\s*(\d+)", by_id[check["message_id"]]["content"])
                assert actual_batch is not None, by_id[check["message_id"]]["content"]
                assert check["before_batches"] <= int(actual_batch.group(1)) <= check["progress_batches"]
            assert str(verified["batches"]) in by_id[messages["queue"]]["content"]
            assert all(message["id"] != messages["b"] for message in records["a"].transcript)
            assert all(message["id"] not in {messages["a"], messages["queue"]} for message in records["b"].transcript)
            reviewers = {event["subagent_id"] for event in client.events if event.get("type") == "subagent.start"}
            completed_reviewers = {event["subagent_id"] for event in client.events
                if event.get("type") == "subagent.done" and event.get("status") == "completed"}
            assert reviewers and reviewers <= completed_reviewers, (reviewers, completed_reviewers)
            if args.multi_agent_v2_board:
                result["v2_board"] = v2_board_evidence(client.events, state, conversation_id=a, message_id=messages["a"],
                    channel=board_channel, nonce=board_nonce, child_probe=args.child_probe, tool_namespace=args.tool_namespace)
            if args.child_probe:
                assert (workspace / "child_probe.py").read_text(encoding="utf-8") == CHILD_PROBE
                probe = [json.loads(line) for line in (workspace / "child-progress.jsonl").read_text(encoding="utf-8").splitlines()]
                child_result = json.loads((workspace / "child-result.json").read_text(encoding="utf-8"))
                first_done = next(event for event in client.events if event.get("type") == "subagent.done"
                    and event.get("subagent_id") == child_id and event.get("status") == "completed")
                finished_at = datetime.fromisoformat(first_done["timestamp"]).timestamp()
                epochs = {event["mailbox_epoch"] for event in client.events if event.get("type") == "subagent.done"
                    and event.get("subagent_id") == first_done["subagent_id"] and event.get("status") == "completed"}
                original_pid = result["probe_processes"]["child_probe.py"]["pid"]
                assert len(epochs) >= 2 and {row["pid"] for row in probe} == {original_pid} and child_result["pid"] == original_pid
                assert probe[0]["timestamp"] < finished_at < probe[-1]["timestamp"] - 5
                assert child_result["passed"] and child_result["elapsed"] >= args.child_seconds
                assert child_result["checked_rows"] >= 300
                result["child_probe"] = {**child_result, "mailbox_epochs": sorted(epochs),
                    "seconds_after_initial_final": round(probe[-1]["timestamp"] - finished_at, 3)}
            for key, record in records.items():
                (out / f"cold-{key}.json").write_text(json.dumps(record.to_dict(), ensure_ascii=False), encoding="utf-8")
            await process_audit.stop()
            result.update(passed=True, verification=verified, completed_reviewers=sorted(completed_reviewers), events=len(client.events),
                tool_calls=sum(e.get("type") == "tool_call" for e in client.events),
                subagent_events=sum(str(e.get("type", "")).startswith("subagent.") for e in client.events))
        except BaseException as error:
            result["error"] = f"{type(error).__name__}: {error}".replace(config.llm.api_key, "[redacted]")
            raise
        finally:
            process_audit.stopping = True
            try:
                if client.ws is not None:
                    await client.ws.close()
                    await asyncio.gather(client.reader, return_exceptions=True)
                if process.poll() is None:
                    if backend_ready:
                        async with httpx.AsyncClient(trust_env=False) as http:
                            response = await http.post(f"http://127.0.0.1:{port}/api/runtime/shutdown")
                            response.raise_for_status()
                            assert response.json()["accepted"] is True
                    else:
                        process.terminate()
                result["shutdown_exit_code"] = await asyncio.to_thread(process.wait, 30)
                assert not backend_ready or result["shutdown_exit_code"] == 0
                await process_audit.task
                await asyncio.gather(*(probe["wait"] for probe in process_audit.probes.values()))
            except Exception as error:
                result.update(passed=False, shutdown_error=f"{type(error).__name__}: {error}".replace(config.llm.api_key, "[redacted]"))
                raise
            else:
                result["source_unchanged_during_run"] = backend_source_snapshot() == source_snapshot
                if not result["source_unchanged_during_run"]:
                    result.update(passed=False, source_error="Backend source changed during this acceptance run")
                assert result["source_unchanged_during_run"], result["source_error"]
            finally:
                result["elapsed_seconds"] = round(time.monotonic() - started, 3)
                (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--configured-state", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=100)
    parser.add_argument("--child-seconds", type=int, default=90)
    parser.add_argument("--turn-budget-seconds", type=float, default=600)
    parser.add_argument("--disconnect-seconds", type=float, default=36)
    parser.add_argument("--roundtrip-interval", type=float, default=0,
        help="While the main oracle runs, execute real side-conversation model/file round-trips at this interval.")
    parser.add_argument("--child-probe", action="store_true", help="Require a real child final/follow-up with a live process in between.")
    parser.add_argument("--multi-agent-v2-board", action="store_true", help="Require native Source V2 and a real root/child board exchange.")
    parser.add_argument("--namespace-preflight-only", action="store_true", help="Only test the exact configured provider's native namespace round-trip.")
    parser.add_argument("--namespace-preflight-receipt", type=Path, help="Reuse a successful preflight for the exact endpoint/model/wire combination.")
    parser.add_argument("--tool-namespace", default="collaboration", help="Explicit native namespace for the complete V2/board audit; never changed automatically.")
    args = parser.parse_args()
    os.environ["MINICODE_STATE_ROOT"] = str(args.configured_state.resolve())
    from backend.config import load_config
    config = load_config(cwd=None)
    if not config.llm.api_key:
        parser.error("The selected provider has no credential")
    if args.namespace_preflight_only:
        args.out.mkdir(parents=True, exist_ok=False)
        receipt = asyncio.run(namespace_preflight(config.llm, args.out / "namespace-runtime", args.tool_namespace))
        (args.out / "namespace-preflight.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(json.dumps({key: receipt[key] for key in ("model", "wire_api", "namespace", "tool_names", "passed")}))
        raise SystemExit(0 if receipt["passed"] else 1)
    asyncio.run(exercise(args, config))


if __name__ == "__main__":
    main()
