"""Owner-bound hooks, MCP requests and recovery boundaries.

Each test drives the production entry the read-only audit reproduced with its
probes: relay detachment, skill metadata discovery, aggregate MCP resource
listing, the executor handshake, terminal reasons, the model's context window,
MCP control-plane results, prompt-hook model selection, MCP request ownership
and the UserPromptSubmit hook for steered input.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.context_window import ContextWindowConfig, ContextWindowState
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.terminal_projection import terminal_boundary_events
from backend.artifact.store import ArtifactStore
from backend.bootstrap.app import AppBootstrap
from backend.config import AgentSettings, AppConfig, LLMSettings, PermissionSettings, TokenBudget
from backend.desktop_remote.host import DesktopHost
from backend.desktop_remote.relay import Relay
from backend.environments import client as executor_client_module
from backend.environments.client import ExecutorClient
from backend.environments.config import EnvironmentSpec
from backend.environments.errors import ExecutorDisconnected
from backend.environments.transport import TransportCloseReceipt
from backend.hooks.manager import HookEvent, HookManager, HookResult, _hook_verdict, _substitute_hook_arguments
from backend.hooks.runners import HookRuntimeBindings, execute_hook
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent, UsageInfo
from backend.llm.provider_contracts import ModelDefinition
from backend.mcp.manager import MCPServerManager
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.services import llm_adapter_factory
from backend.skills.loader import SkillLoader
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.mcp_tools import _list_resource_payload
from backend.tools.registry import ToolRegistry
from backend.ws.handlers import mcp as mcp_handlers


# ---------------------------------------------------------------- C-T03

class _MemoryWriter:
    def __init__(self, *, reset_on_close=False):
        self.reset_on_close = reset_on_close
        self.output = bytearray()
        self.closed = False

    def write(self, value):
        self.output.extend(value)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        if self.reset_on_close:
            raise ConnectionResetError("attachment connection reset")


@pytest.mark.asyncio
async def test_relay_releases_the_host_attachment_even_when_the_transport_resets(tmp_path):
    host = DesktopHost(tmp_path, tmp_path)
    writer = _MemoryWriter(reset_on_close=True)
    detached = []
    relay = Relay(asyncio.StreamReader(), writer, backend_port=1,
                  on_closed=lambda: (detached.append(True), host._detached(relay)))
    host.relay = relay
    with pytest.raises(ConnectionResetError):
        await relay.aclose()
    assert relay.closed and writer.closed
    assert detached == [True]
    assert host.relay is None
    reader = asyncio.StreamReader()
    reader.feed_data(json.dumps({"token": host.control_token, "action": "relay"}).encode() + b"\n")
    reader.feed_eof()
    response = _MemoryWriter()
    host.runtime = {"backend_port": 1}
    # The next relay is admitted instead of being refused as a second attachment.
    await asyncio.wait_for(host._control(reader, response), 5)
    assert json.loads(response.output.splitlines()[0]) == {"attached": True}
    assert host.relay is None


# ---------------------------------------------------------------- C-T02

@pytest.mark.parametrize("policy", ["policy: invalid\n", "policy: [codex]\n", "policy: true\n"])
def test_scalar_skill_policy_metadata_is_a_warning_not_a_discovery_failure(tmp_path, caplog, policy):
    skill = tmp_path / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: review\ndescription: Actual workflow\n---\n# Workflow\n", encoding="utf8", newline="")
    metadata = skill.parent / "agents" / "openai.yaml"
    metadata.parent.mkdir()
    metadata.write_text(policy, encoding="utf8")
    with caplog.at_level(logging.WARNING, logger="backend.skills.loader"):
        assert SkillLoader._load_skill_metadata(skill.parent) == {}
        meta = SkillLoader(tmp_path)._parse_frontmatter(skill, "project")
    assert meta is not None and meta.name == "review" and meta.products == ()
    assert any("Ignoring invalid Skill policy metadata" in record.message for record in caplog.records)


# ---------------------------------------------------------------- C-T01

class _ResourceClient:
    async def list_all_resource_items(self, *, templates):
        return [{"uri": "mem://one", "name": "one"}]


class _ResourceManager:
    def __init__(self):
        self.healthy = _ResourceClient()
        self.prepared = []

    def iter_connected_clients(self):
        return [("z-healthy", self.healthy)]

    def get_all_tools(self):
        return {"a-stale": [], "z-healthy": []}

    def get_client(self, name):
        return self.healthy if name == "z-healthy" else None

    async def prepare_server_call(self, name):
        self.prepared.append(name)
        if name == "a-stale":
            raise ConnectionError("stale cached server unavailable")


@pytest.mark.asyncio
async def test_aggregate_resource_listing_continues_past_a_stale_server():
    manager = _ResourceManager()
    result = await _list_resource_payload(manager, {}, None, templates=False, prepare_call=manager.prepare_server_call)
    assert not result.is_error
    assert json.loads(result.content)["resources"] == [{"server": "z-healthy", "uri": "mem://one", "name": "one"}]
    assert manager.prepared == ["a-stale", "z-healthy"]


# ---------------------------------------------------------------- H-C02

@pytest.mark.asyncio
async def test_executor_client_retries_a_handshake_that_failed_before_initialized(monkeypatch):
    sent = []
    transports = []

    class MemoryTransport:
        def __init__(self, index):
            self.index = index
            self.incoming = asyncio.Queue()

        async def send(self, payload):
            message = json.loads(payload)
            sent.append((self.index, message.get("method")))
            if message.get("method") == "initialize":
                resumed = message["params"].get("resumeSessionId")
                await self.incoming.put(json.dumps({"id": message["id"], "result": {"sessionId": resumed or f"session-{self.index}"}}))
            elif message.get("method") == "initialized" and self.index == 1:
                raise ExecutorDisconnected("transient initialized-notification send failure")

        async def recv(self):
            return await self.incoming.get()

        async def close(self):
            return TransportCloseReceipt("memory")

    async def memory_open(_spec):
        transport = MemoryTransport(len(transports) + 1)
        transports.append(transport)
        return transport

    monkeypatch.setattr(executor_client_module, "open_transport", memory_open)
    client = ExecutorClient(EnvironmentSpec(id="audit", url="ws://audit.invalid", initialize_timeout_sec=1))
    try:
        with pytest.raises(ExecutorDisconnected):
            await client.get_connection()
        assert client.session_id is None
        connection = await client.get_connection()
        assert connection.connected and client.session_id == "session-2"
        assert await client.get_connection() is connection
        assert sent == [(1, "initialize"), (1, "initialized"), (2, "initialize"), (2, "initialized")]
    finally:
        await client.close()


# ---------------------------------------------------------------- A09

@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason,reply", [
    ("failed", "timeout", ""),
    ("failed", "max_turn_tokens", "useful output"),
    ("cancelled", "interrupted", "partial output"),
])
async def test_checkpoint_save_failure_keeps_an_ended_turns_own_reason(status, reason, reply):
    state = AgentState(user_message="probe", stopped_reason=reason, terminal_status=status, reply=reply)
    finalizer = SimpleNamespace(run_context=RunContext(), finalize_checkpoint=lambda **kwargs: "save_failed",
                                checkpoint_evidence=lambda: {"status": "save_failed"})
    events = await terminal_boundary_events(turn_kernel=finalizer, session_id="probe-session", user_message="probe",
        state=state, context_builder=None, usage=UsageInfo(), terminal_projection=None, status=status, reason=reason)
    terminal = {event.type: event.data for event in events}
    assert (terminal["agent.terminal.intent"]["status"], terminal["agent.terminal.intent"]["reason"]) == (status, reason)
    assert (terminal["done"]["status"], terminal["done"]["reason"]) == (status, reason)
    assert terminal["done"]["checkpoint"] == {"status": "save_failed"}
    assert (state.terminal_status, state.stopped_reason) == (status, reason)


@pytest.mark.asyncio
async def test_completed_turn_still_downgrades_on_retained_checkpoint_failure():
    state = AgentState(user_message="probe", stopped_reason="completed", terminal_status="completed", reply="answer")
    finalizer = SimpleNamespace(run_context=RunContext(), finalize_checkpoint=lambda **kwargs: "save_failed",
                                checkpoint_evidence=lambda: {"status": "save_failed"})
    events = await terminal_boundary_events(turn_kernel=finalizer, session_id="probe-session", user_message="probe",
        state=state, context_builder=None, usage=UsageInfo(), terminal_projection=None, status="completed", reason="")
    done = next(event.data for event in events if event.type == "done")
    assert (done["status"], done["reason"]) == ("partial", "checkpoint_save_failed")
    assert (state.terminal_status, state.stopped_reason) == ("partial", "checkpoint_save_failed")


# ---------------------------------------------------------------- A08

def test_full_context_window_follows_the_models_effective_percent():
    model = ModelDefinition(provider="probe", id="probe-model", name="probe", api="openai-responses",
                            base_url="http://invalid.local", context_window=100000,
                            extra={"effective_context_window_percent": 80})
    llm = SimpleNamespace(_model=model)
    config = ContextWindowConfig(model_auto_compact_token_limit_scope="body_after_prefix", model_auto_compact_token_limit=200000)
    assert config.limits(llm, model) == (200000, 80000)
    assert config.limits(llm) == (200000, 95000)
    status = ContextWindowState(prefill_input_tokens=60000).status(config, llm, 85000, model)
    assert status["full_context_window_limit"] == 80000
    assert status["token_limit_reached"] is True and status["tokens_left"] == 0


# ---------------------------------------------------------------- C-T05

def _mcp_session(tmp_path):
    manager = SimpleNamespace(workspace_root=tmp_path)
    session = SimpleNamespace(active_conversation_id="A", mcp_manager=manager, send_payload=AsyncMock(),
                              send_event=AsyncMock(), refresh_tool_registry_if_mcp_changed=lambda **_kwargs: True)
    return manager, session


@pytest.mark.asyncio
@pytest.mark.parametrize("settled,level,fragment", [
    ("error", "error", "MCP runtime update failed: MCPAuthenticationError: authorization did not complete"),
    ("connected", "info", ""),
])
async def test_restart_reports_the_servers_settled_state(tmp_path, settled, level, fragment):
    manager, session = _mcp_session(tmp_path)

    async def invoke(_manager, _data):
        entry = {"name": "shared", "status": settled, "project_workspace": str(tmp_path)}
        if settled == "error":
            entry["error"] = "MCPAuthenticationError: authorization did not complete"
        return [entry]

    await mcp_handlers._run_mcp_server_command(session, {"name": "shared"}, command="mcp.restart", invoke=invoke)
    result = session.send_event.await_args.args[0]
    assert result.data["command"] == "mcp.restart"
    assert result.data["level"] == level
    assert result.data["message"] == fragment
    assert result.data["data"]["connection_status"] == settled


@pytest.mark.asyncio
async def test_login_that_settles_in_error_is_reported_as_a_failure(tmp_path, monkeypatch):
    manager, session = _mcp_session(tmp_path)
    monkeypatch.setattr(mcp_handlers, "_command_mcp_manager", lambda _session, _data: manager)

    async def login(_manager, name):
        return [{"name": name, "status": "error", "error": "MCPAuthenticationError: authorization did not complete",
                 "project_workspace": str(tmp_path)}]

    monkeypatch.setattr(mcp_handlers, "login_mcp_server", login)
    await mcp_handlers.handle_mcp_oauth_login(session, {"type": "mcp.oauth.login", "name": "shared"})
    result = session.send_event.await_args.args[0]
    assert result.data["level"] == "error"
    assert result.data["message"] == "MCP login failed: MCPAuthenticationError: authorization did not complete"
    assert result.data["data"]["connection_status"] == "error"


# ---------------------------------------------------------------- H-C01

class _ObservedAdapter:
    provider_adapter_spec = None

    def __init__(self, settings):
        self.settings = settings
        self.requests = []
        self.closed = False
        self.policy = None

    def apply_model_request_policy(self, policy):
        self.policy = policy

    async def side_query(self, messages, *, options, turn_context=None):
        self.requests.append((options.operation, messages[-1].content))
        return '{"ok": true}'

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_prompt_hook_model_selection_stays_with_the_owning_execution(tmp_path, monkeypatch):
    built = []

    def observe_wire(settings, **kwargs):
        adapter = _ObservedAdapter(settings)
        built.append(adapter)
        return adapter

    monkeypatch.setattr(llm_adapter_factory, "build_wire_adapter", observe_wire)
    monkeypatch.setattr("backend.config.get_llm_provider",
                        lambda *args, **kwargs: pytest.fail("the process-wide provider is not the hook's owner"))
    owner_settings = LLMSettings(provider="openai", model="owner-model", api_key="owner-key",
                                 base_url="http://captured-owner.invalid/v1")
    execution = ModelExecutionSnapshot(config=AppConfig(llm=owner_settings), llm=SimpleNamespace(),
                                       provider="openai", model="owner-model")
    session_llm = _ObservedAdapter(owner_settings)
    tool_context = SimpleNamespace(run_context=SimpleNamespace(llm_turn_context=None), model_execution=execution)
    entry = SimpleNamespace(hook_type="prompt", prompt="Owner hook payload", model="owner-evaluator",
                            async_timeout=None, additional_context_limit=None)
    result = await execute_hook(entry, event=HookEvent.USER_PROMPT_SUBMIT, json_input='{"prompt":"owner data"}',
        event_name="user_prompt_submit",
        runtime=HookRuntimeBindings(workspace_root=tmp_path, llm=session_llm, tool_context=tool_context),
        substitute_arguments=_substitute_hook_arguments, parse_verdict=_hook_verdict)
    assert result.exit_code == 0
    assert session_llm.requests == []
    assert len(built) == 1
    assert built[0].settings.base_url == "http://captured-owner.invalid/v1"
    assert built[0].settings.model == "owner-evaluator"
    assert built[0].requests[0][0] == "hook_prompt:user_prompt_submit"
    assert built[0].closed

    without_owner = await execute_hook(entry, event=HookEvent.USER_PROMPT_SUBMIT, json_input='{"prompt":"owner data"}',
        event_name="user_prompt_submit",
        runtime=HookRuntimeBindings(workspace_root=tmp_path, llm=session_llm, tool_context=None),
        substitute_arguments=_substitute_hook_arguments, parse_verdict=_hook_verdict)
    assert without_owner.exit_code == 1 and "owning model execution" in without_owner.stderr
    assert len(built) == 1


# ---------------------------------------------------------------- C-T06

def test_turn_receiving_mcp_manager_resolves_to_its_host_for_server_requests(tmp_path, monkeypatch):
    host = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=None, runtime_configs=())
    receiving = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=None, runtime_configs=(),
                                 source_manager=host)
    foreign = MCPServerManager(config_path=tmp_path / "other.json", workspace_root=None, runtime_configs=())
    session = SimpleNamespace(session_id="audit-session", is_connected=True,
        conversation_repo=SimpleNamespace(get_conversation=lambda _id: object()),
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda _conversation: None))
    app = object.__new__(AppBootstrap)
    app.ws_manager = SimpleNamespace(iter_sessions=lambda: [session])
    app._mcp_managers = {"<projectless>": host}
    app.mcp_apps = SimpleNamespace(owns_request=lambda _owner: False)
    monkeypatch.setattr("backend.agent.conversation_query_guard.conversation_query_guards",
                        lambda: SimpleNamespace(active_claim=lambda _id: SimpleNamespace(generation=42),
                                                open_admission=lambda: None))

    def owner(manager):
        return {"_minicode_owner": {"session_id": "audit-session", "conversation_id": "audit-conversation",
                                     "conversation_run_generation": 42, "mcp_manager": manager}}

    assert app._resolve_mcp_request_session(owner(host))[0] is session
    assert app._resolve_mcp_request_session(owner(receiving))[0] is session
    assert app._resolve_mcp_request_session(owner(foreign))[0] is None


# ---------------------------------------------------------------- A01 / H17

class _SteerTool(BaseTool):
    name = "enqueue_steer"
    description = "Enqueue a user steer while the turn runs"
    read_only = True

    def __init__(self, content):
        super().__init__()
        self.content = content

    def get_schema(self):
        return ToolSchema(name=self.name, description=self.description, parameters={"type": "object", "properties": {}})

    async def execute(self, args, context=None):
        queued = context.run_context.turn_input_queue.enqueue_command(SimpleNamespace(data={
            "content": self.content, "user_message_id": "steer-user", "assistant_message_id": "steer-assistant"}))
        assert queued is not None
        return ToolResult(content="steer queued")


class _ToolThenTextProvider(LLMAdapter):
    def __init__(self):
        self.calls = []

    async def stream_chat(self, messages, tools=None):
        self.calls.append([dict(message) if isinstance(message, dict) else message for message in messages])
        if len(self.calls) == 1:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(id="steer-call", name="enqueue_steer", arguments={})])
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="answer", phase="final_answer")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


def _steered_query(tmp_path, runtime, provider, steer_content, hooks):
    registry = ToolRegistry()
    registry.register(_SteerTool(steer_content))
    owner = AgentSession(llm=provider, tool_registry=registry, artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path), agent_settings=AgentSettings(max_iterations=4),
        token_budget=TokenBudget())
    submission = QuerySubmission(user_message="initial prompt", session=owner, state=None,
        runtime=AgentLoopSessionContext(session_id="steer-session", workspace_root=tmp_path,
            permission_context=PermissionContext(mode="bypass"), metadata={"conversation_id": "steer-conversation"},
            run_context=RunContext(agent_runtime=runtime)))
    submission.runtime.run_context.hook_manager = hooks
    return owner, submission


def _message_text(message):
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


@pytest.mark.parametrize("blocked", [True, False])
def test_steered_input_runs_the_user_prompt_submit_hook_before_the_model_sees_it(tmp_path, blocked):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    hooked = []

    class Hooks(HookManager):
        def has_hooks(self, event):
            return event is HookEvent.USER_PROMPT_SUBMIT

        async def run_user_prompt_submit(self, message):
            hooked.append(message)
            if "HOOK-BLOCK" in message:
                return HookResult(blocked=True, message="steer refused by hook")
            if message.startswith("steer"):
                return HookResult(updated_input=message + " (rewritten by hook)")
            return HookResult()

    steer = "steer HOOK-BLOCK marker" if blocked else "steer accepted marker"
    provider = _ToolThenTextProvider()
    owner, submission = _steered_query(tmp_path, runtime, provider, steer, Hooks())
    try:
        events = asyncio.run(_collect(QueryEngine().submit(submission)))
    finally:
        runtime.close()
    done = next(event for event in events if event.type == "done")
    assert hooked == ["initial prompt", steer]
    if blocked:
        assert len(provider.calls) == 1
        error = next(event for event in events if event.type == "error")
        assert error.data["error_code"] == "user_prompt_blocked" and "steer refused by hook" in error.data["message"]
        assert (done.data["status"], done.data["reason"]) == ("failed", "blocked")
        history = owner.context_builder.export_snapshot()["history"]
        assert not any("HOOK-BLOCK" in _message_text(message) for message in history)
    else:
        assert done.data["status"] == "completed"
        assert len(provider.calls) == 2
        second = [_message_text(message) for message in provider.calls[1]]
        assert any("steer accepted marker (rewritten by hook)" in text for text in second)
        assert not any(text.rstrip().endswith("steer accepted marker") for text in second)


async def _collect(stream):
    return [event async for event in stream]
