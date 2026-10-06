from __future__ import annotations

import asyncio
import json
from datetime import datetime
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from backend.agent.checkpoint import load_latest_checkpoint, save_run_checkpoint
from backend.agent.code_execution import CodeExecutionRuntime
from backend.agent.code_execution_store import CodeExecutionStore
from backend.agent.context import ContextBuilder
from backend.agent.mailbox_delivery import _run_is_conversation_leader
from backend.agent.query_recovery import prepare_query_recovery
from backend.agent.run_context import RunContext
from backend.agent.runtime_records import AgentRunRecord
from backend.agent.state import AgentState
from backend.agent.tool_execution_gate import ToolExecutionGate
from backend.agent.tool_runtime import resolve_tool_timeout, tool_is_idempotent, tool_side_effect_kind
from backend.agent.turn_kernel import TurnKernel
from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.lsp.client import LSPClient
from backend.services import plugin_settings_service
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.code_execution import ToolExecTool
from backend.tools.file_tools_common import record_file_hash
from backend.tools.registry import ToolRegistry
from backend.tools.subagent_context import build_subagent_permission_context
from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY, ToolsetPolicy
from backend.tools.worktree_tools import CreateWorktreeTool, RemoveWorktreeTool
from backend.ws.utils import build_effective_user_message


class MarkerTool(BaseTool):
    name = "marker_write"
    description = "Write an audit marker."
    mutates_workspace = True
    workspace_path_fields = ("file_path",)

    def __init__(self, after_write=lambda: None):
        self.after_write = after_write

    def get_schema(self):
        return ToolSchema(self.name, self.description, {
            "type": "object", "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        })

    async def execute(self, args, context=None):
        (context.workspace_root / args["file_path"]).write_text("written", encoding="utf-8")
        self.after_write()
        return ToolResult("written")


@pytest.mark.asyncio
@pytest.mark.parametrize("chunked", [False, True])
async def test_code_cell_refreshes_permission_after_wait_and_between_chunks(tmp_path, chunked):
    live = {"permission": PermissionContext(mode="bypass", workspace_root=tmp_path)}
    writes = 0

    def after_write():
        nonlocal writes
        writes += 1
        if writes == 10:
            live["permission"] = PermissionContext(mode="plan", workspace_root=tmp_path)

    registry = ToolRegistry()
    registry.register(MarkerTool(after_write))
    checker = PermissionChecker(PermissionSettings(), workspace_root=tmp_path)
    state = AgentState("audit", workspace_root=tmp_path, conversation_id="audit")
    owner = RunContext(code_store=CodeExecutionStore(), tool_execution_gate=ToolExecutionGate(limit=0, initial_completed=0))

    async def publish(event):
        pass

    owner.publish_nested_event = publish
    owner.permission_context_provider = lambda: live["permission"]
    context = ToolExecutionContext(
        permission=live["permission"], run_context=owner, workspace_root=tmp_path,
        permission_checker=checker, tool_registry=registry, conversation_id="audit",
        tool_call_id="execute-audit", metadata={ACTIVE_TOOLSET_POLICY_METADATA_KEY: ToolsetPolicy.default()},
    )

    def commit(permission):
        context.permission = permission
        return permission, None

    context.permission_context_committer = commit
    kernel = object.__new__(TurnKernel)
    kernel._tool_context = context
    kernel.run_context = owner
    runtime = CodeExecutionRuntime(context=ContextBuilder(), state=state, tool_context=context,
        permission_checker=checker, skill_manager=None, turn_kernel=kernel)
    owner.code_execution = runtime
    code = (
        'await Promise.all(Array.from({length: 11}, (_, i) => tools.marker_write({file_path: "marker-" + i}))); text("done");'
        if chunked else
        'await new Promise(resolve => setTimeout(resolve, 30)); text(await tools.marker_write({file_path: "marker"}));'
    )
    try:
        result = await runtime.execute(code, context, yield_time_ms=0)
        cell_id = json.loads(result.content)["cell_id"]
        if not chunked:
            live["permission"] = PermissionContext(mode="plan", workspace_root=tmp_path)
        await asyncio.wait_for(asyncio.shield(runtime.cells[cell_id].task), timeout=5)
        report = json.loads((await runtime.wait(cell_id, yield_time_ms=0)).content)
        assert report["status"] == "completed", report
        assert writes == (10 if chunked else 0)
        assert state.tool_calls[-1].status == "blocked"
        assert not (tmp_path / ("marker-10" if chunked else "marker")).exists()
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type", [CreateWorktreeTool, RemoveWorktreeTool])
async def test_worktree_cancellation_retains_worker_until_mutation_finishes(tmp_path, monkeypatch, tool_type):
    started, release = Event(), Event()
    target = tmp_path / ".minicode" / "worktrees" / "audit"

    def mutate(**kwargs):
        started.set()
        assert release.wait(3)
        (tmp_path / "settled").write_text("done", encoding="utf-8")
        return True

    manager = SimpleNamespace(repo_root=tmp_path, create_worktree=mutate, remove_worktree=mutate)

    async def resolve(context):
        return manager

    monkeypatch.setattr("backend.tools.worktree_tools._resolve_worktree_manager", resolve)
    context = ToolExecutionContext(PermissionContext(mode="bypass"), workspace_root=tmp_path)
    task = asyncio.create_task(tool_type().execute({"path": str(target)}, context))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert (tmp_path / "settled").read_text(encoding="utf-8") == "done"


@pytest.mark.parametrize("role,parent,conversation,expected", [
    ("main", "", "audit", True),
    ("main", "parent", "audit", False),
    ("subagent:general-purpose", "parent", "audit", False),
    ("side_query", "", "audit", False),
    ("main", "", "another", False),
])
def test_only_root_main_run_can_authorize_teammate_plan(role, parent, conversation, expected):
    record = AgentRunRecord("run", role=role, parent_run_id=parent, conversation_id=conversation)
    runtime = SimpleNamespace(get_run=lambda _: record)
    assert _run_is_conversation_leader(runtime, "run", "audit") is expected


@pytest.mark.parametrize("requested", ["plan", "confirm", "auto"])
def test_ordinary_child_honors_explicit_narrower_permission(requested):
    parent = ToolExecutionContext(PermissionContext(mode="bypass"))
    child = build_subagent_permission_context("general-purpose", parent, requested_mode=requested)
    assert child.mode == requested


def test_plugin_checkpoint_restores_canonical_ids_against_current_inventory(tmp_path, monkeypatch):
    inventory = [
        {"id": f"demo@{market}", "name": "demo", "displayName": "Demo UI", "enabled": True,
         "skill_count": 1, "mcp_server_names": ["server"]}
        for market in ("one", "two")
    ]
    roots = []
    monkeypatch.setattr("backend.config.load_config_layer_stack", lambda *, cwd=None: roots.append(cwd) or object())
    monkeypatch.setattr(plugin_settings_service, "get_plugin_snapshot", lambda **kwargs: {"plugins": inventory})
    mentions = [{"config_name": item["id"]} for item in inventory]
    state = AgentState("original request", conversation_id="audit", workspace_root=tmp_path)
    state.prompt_context["plugin_injections"] = plugin_settings_service.resolve_enabled_plugin_mentions(mentions, workspace_root=tmp_path)
    builder = ContextBuilder()
    builder.append_user("original request")
    kernel = object.__new__(TurnKernel)
    kernel.runtime = SimpleNamespace(state_root=tmp_path / "checkpoints")
    kernel.metadata = {}
    kernel.run_record = AgentRunRecord("old-run", conversation_id="audit")
    assert kernel._save_checkpoint(session_id="audit-session", user_message=state.user_message,
        state=state, context_builder=builder, reason="interrupted") == "saved"
    checkpoint = load_latest_checkpoint("audit-session", tmp_path / "checkpoints", conversation_id="audit")
    assert checkpoint.resume_payload["selected_plugins"] == mentions
    inventory[1]["enabled"] = False
    monkeypatch.setattr("backend.agent.query_recovery.load_latest_checkpoint", lambda *args, **kwargs: checkpoint)
    restored = AgentState("original request", conversation_id="audit", workspace_root=tmp_path)
    recovered = prepare_query_recovery(session_id="audit-session", conversation_id="audit",
        metadata={"resume_from_checkpoint": True}, state=restored, context_builder=ContextBuilder(),
        max_iterations_budget=3, current_run_id="new-run", connected_mcp_servers=("plugin:demo@one:server",))
    assert recovered.restored
    assert [item["config_name"] for item in restored.prompt_context["plugin_injections"]] == ["demo@one"]
    instructions = ContextBuilder._build_plugin_instructions(restored)
    assert "`demo@one:`" in instructions
    assert "plugin:demo@one:server" in instructions
    assert roots == [tmp_path, tmp_path]


def test_nested_instruction_order_and_patch_observed_path(tmp_path, monkeypatch):
    from backend.agent import instruction_discovery

    monkeypatch.setattr(instruction_discovery, "_get_managed_minicode_dir", lambda: tmp_path / "managed")
    monkeypatch.setattr(instruction_discovery, "get_minicode_config_home_dir", lambda: tmp_path / "user")
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("ROOT_INSTRUCTION", encoding="utf-8")
    rules = tmp_path / ".minicode" / "rules"
    rules.mkdir(parents=True)
    (rules / "all.md").write_text("ROOT_RULE", encoding="utf-8")
    child = tmp_path / "src"
    child.mkdir()
    (child / "AGENTS.md").write_text("NESTED_INSTRUCTION", encoding="utf-8")
    target = child / "from-patch.py"
    target.write_text("value = 1", encoding="utf-8")
    builder = ContextBuilder(workspace_root=tmp_path)
    context = ToolExecutionContext(PermissionContext(), workspace_root=tmp_path,
        metadata={"_read_file_hashes": builder.read_file_hashes()})
    record_file_hash(context, target, "observed")
    state = AgentState("audit", workspace_root=tmp_path)
    prompt = builder._build_prompt_parts(state, tmp_path).render_user_instructions()
    assert prompt.index("ROOT_INSTRUCTION") < prompt.index("ROOT_RULE") < prompt.index("NESTED_INSTRUCTION")


def test_metadata_failure_is_not_classified_as_safe_to_retry():
    class BrokenTool(MarkerTool):
        read_only = True

        def get_side_effect_kind(self, args=None):
            raise RuntimeError("broken classifier")

    registry = ToolRegistry()
    registry.register(BrokenTool())
    for classify in (tool_is_idempotent, tool_side_effect_kind):
        with pytest.raises(RuntimeError, match="broken classifier"):
            classify("marker_write", registry, {})


def test_tool_owned_unbounded_timeout_overrides_class_default():
    class Tool(MarkerTool):
        timeout_seconds = 1

        def resolve_timeout(self, args):
            return None

    registry = ToolRegistry()
    registry.register(Tool())
    assert resolve_tool_timeout("marker_write", registry) is None


@pytest.mark.asyncio
async def test_direct_lsp_start_cancellation_releases_process_and_readers(tmp_path):
    initialized = asyncio.Event()

    class Stream:
        async def read(self, size):
            await asyncio.Event().wait()

    process = SimpleNamespace(returncode=None, stdin=None, stdout=Stream(), stderr=Stream())

    class Runner:
        cleaned = False

        async def spawn_interactive(self, *args, **kwargs):
            return process

        async def cleanup(self):
            self.cleaned = True
            process.returncode = 0
            return True

    runner = Runner()
    client = LSPClient("audit-lsp", [], str(tmp_path), sandbox_runner=runner)

    async def initialize():
        initialized.set()
        await asyncio.Event().wait()

    client._initialize = initialize
    task = asyncio.create_task(client.start())
    await asyncio.wait_for(initialized.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert runner.cleaned
    assert client._process is None
    assert client._reader_task is None
    assert client._stderr_task is None


def test_long_lived_context_date_advances_across_midnight(monkeypatch):
    from backend.agent import context as context_module

    builder = ContextBuilder()
    current = datetime(2026, 10, 2, 12).astimezone()

    class Clock:
        @staticmethod
        def now():
            return current

    monkeypatch.setattr(context_module, "datetime", Clock)
    state = AgentState("audit")
    assert "<current_date>2026-10-02</current_date>" in builder._build_environment_context_xml(state)
    current = datetime(2026, 10, 3, 12).astimezone()
    assert "<current_date>2026-10-03</current_date>" in builder._build_environment_context_xml(state)


def test_attachment_names_remain_data_in_the_model_input_envelope():
    filename = 'report & "quoted" <section>.pdf'
    message = build_effective_user_message("", [{
        "file_name": filename, "kind": "document", "doc_id": "doc", "artifact_id": "artifact",
    }])
    root = ElementTree.fromstring(message)
    assert root.find("attachment").attrib["file_name"] == filename
    assert len(root) == 1


def test_code_mode_directory_uses_callable_javascript_access_for_mcp_names():
    tool = MarkerTool()
    tool.name = "mcp__server-with-dash__read-content"
    registry = ToolRegistry()
    registry.register(tool)
    registry.register(ToolExecTool())
    schemas = registry.get_schemas(toolset_policy=replace(
        ToolsetPolicy.default(), include_deferred_directly=True, code_mode_only=True,
    ))
    description = next(item["function"]["description"] for item in schemas if item["function"]["name"] == "tool_exec")
    assert 'tools["mcp__server-with-dash__read-content"](' in description
