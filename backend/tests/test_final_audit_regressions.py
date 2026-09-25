"""Integration checks for the final audit's confirmed capability defects."""
from __future__ import annotations

import asyncio

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import execute_tool_batch
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, ToolCallEvent, UsageInfo
from backend.mcp.client import MCPToolDef
from backend.mcp.registry import MCPToolRegistry
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.tools.registry import ToolRegistry
from backend.tools.swarm_tools import SendMessageTool
from backend.tools.write_file import WriteFileTool


def test_read_only_child_reports_through_normal_tool_pipeline_but_cannot_write(tmp_path):
    async def run():
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm")
        runtime.start_run(run_id="parent", conversation_id="audit")
        child = runtime.start_subagent(subagent_id="child", parent_run_id="parent", agent_type="explore")
        registry = ToolRegistry()
        registry.register(SendMessageTool())
        registry.register(WriteFileTool())
        permission = PermissionContext(mode="plan", sandbox_mode="read-only")
        context = ToolExecutionContext(
            permission=permission, workspace_root=tmp_path, conversation_id="audit", task_id="child",
            metadata={"agent_role": "subagent:explore", "agent_mode": "subagent", "read_only": True,
                      "run_id": "child", "agent_path": child.agent_path, "mailbox_epoch": child.mailbox_epoch},
            run_context=RunContext(agent_runtime=runtime),
        )
        try:
            events = [event async for event in execute_tool_batch(
                [ToolCallEvent(id="report", name="send_message", arguments={"recipient": "parent", "message": "Found the bug"}),
                 ToolCallEvent(id="write", name="write_file", arguments={"file_path": "forbidden.txt", "content": "bad"})],
                ctx=ContextBuilder(TokenBudget()), state=AgentState(user_message="inspect"),
                tool_registry=registry, permission_checker=PermissionChecker(PermissionSettings(), workspace_root=tmp_path),
                approval_handler=None, skill_manager=None, permission_context=permission, tool_ctx=context,
            )]
            results = {event.data["id"]: event.data for event in events if event.type == "tool_result"}
            assert results["report"]["status"] == "success"
            assert results["write"]["status"] == "blocked"
            assert not (tmp_path / "forbidden.txt").exists()
            messages = runtime.list_swarm_messages(participant_id="parent", conversation_id="audit")
            assert [message.content for message in messages] == ["Found the bug"]
        finally:
            runtime.close()
    asyncio.run(run())


def _skill(root, directory, name, body):
    path = root / directory / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: Test skill\n---\n{body}", encoding="utf-8")
    return path


def test_next_turn_refreshes_skill_changes_without_rewriting_active_snapshot(tmp_path, monkeypatch):
    path = _skill(tmp_path, "review", "review", "Version one")
    loader = SkillLoader(tmp_path)
    monkeypatch.setattr(loader, "_search_dirs", lambda: [("workspace", tmp_path)])
    manager = SkillManager(loader)
    manager.discover()
    active = manager.snapshot(tmp_path)
    assert "Version one" in active.load_skill_payload("review")["content"]
    _skill(tmp_path, "review", "review", "Version two")
    _skill(tmp_path, "new", "new", "New installation")
    next_turn = manager.snapshot(tmp_path)
    assert "Version two" in next_turn.load_skill_payload("review")["content"]
    assert next_turn.load_skill_payload("new")
    assert "Version one" in active.load_skill_payload("review")["content"]
    path.unlink()
    assert manager.snapshot(tmp_path).load_skill_payload("review") is None


def test_case_insensitive_skill_collision_requires_exact_path(tmp_path, monkeypatch):
    first = _skill(tmp_path, "a", "Review", "First")
    second = _skill(tmp_path, "b", "review", "Second")
    loader = SkillLoader(tmp_path)
    monkeypatch.setattr(loader, "_search_dirs", lambda: [("workspace", tmp_path)])
    manager = SkillManager(loader)
    manager.discover()
    assert manager.detect("$review") == []
    assert manager.get_meta("Review") is None
    assert manager.load_skill_payload("review") is None
    assert "First" in manager.load_skill_payload("Review", first)["content"]
    assert "Second" in manager.load_skill_payload("review", second)["content"]


@pytest.mark.parametrize("schema", [{"type": "oops"}, {"type": "object", "required": "value"}, {"type": "array"}])
def test_invalid_mcp_schema_is_excluded_before_model_request(tmp_path, caplog, schema):
    registry = ToolRegistry()
    bridge = MCPToolRegistry(registry)
    count = bridge.register_server_tools("audit", [
        MCPToolDef(name="bad", description="Bad", input_schema=schema),
        MCPToolDef(name="good", description="Good", input_schema={"type": "object", "properties": {}}),
    ], None)
    assert count == 1
    assert not registry.has_tool("mcp__audit__bad")
    assert registry.has_tool("mcp__audit__good")
    assert "mcp__audit__bad" in caplog.text


def test_small_window_compacts_an_oversized_final_tool_group_without_orphans():
    class SummaryModel(LLMAdapter):
        _model = "summary-test"
        prompt = ""

        async def stream_chat(self, messages, tools=None):
            if False:
                yield

        async def simple_chat(self, messages, **kwargs):
            self.prompt = "\n".join(message.content for message in messages)
            return ("## Goal\nRepair the scheduler.\n## Constraints & Preferences\nPreserve public API.\n"
                    "## Progress\nRead the full source.\n## Key Decisions\nRepair the root cause.\n"
                    "## Next Steps\nImplement and test.\n## Critical Context\nTAIL_SENTINEL found.")

    model = SummaryModel()
    budget = TokenBudget(total=24000)
    builder = ContextBuilder(budget, AgentSettings(compaction_keep_recent_tokens=6000), llm=model)
    prompt = "Repair the scheduler; preserve the public API."
    asyncio.run(builder.start_turn(prompt, AgentState(user_message=prompt)))
    builder.append_assistant_tool_calls([ToolCallEvent(id="large-read", name="read_file", arguments={"file_path": "scheduler.py"})])
    # Reproduce oversized history already persisted by an older runtime.
    snapshot = builder.export_snapshot()
    snapshot["history"].append({"role": "tool", "name": "read_file", "tool_call_id": "large-read",
                                "content": "source output " * 4500 + "TAIL_SENTINEL"})
    builder.load_snapshot(snapshot)
    asyncio.run(builder.compact())
    history = builder.export_snapshot()["history"]
    assert "TAIL_SENTINEL" in model.prompt
    assert "large-read" in model.prompt
    assert all(not message.get("tool_calls") and message["role"] != "tool" for message in history)
    assert any("preserve the public API" in message["content"] for message in history)
    assert builder.export_snapshot()["compaction_count"] == 1


def test_compaction_tail_keeps_small_complete_tool_group():
    from backend.tools.base import ToolResult
    builder = ContextBuilder()
    builder.append_user("Old history " * 3000)
    builder.append_assistant_tool_calls([ToolCallEvent(id="small-read", name="read_file", arguments={"file_path": "a.py"})])
    builder.append_tool_result("small-read", "read_file", ToolResult(content="small source"))
    assert builder._compaction_cut(500) == 1


def test_restored_files_share_small_window_with_tool_schemas_and_runtime_guidance(tmp_path):
    state = AgentState(user_message="continue the repair", workspace_root=tmp_path)
    state.tool_runtime_guidance = "Tool usage guidance. " * 500
    for index in range(4):
        path = tmp_path / f"source{index}.py"
        path.write_text("print('source line')\n" * 2500, encoding="utf-8")
        state.record_tool_call("read_file", {"file_path": path.name}, "ok")
    builder = ContextBuilder(TokenBudget(total=24000), workspace_root=tmp_path)
    builder.append_user("Compaction checkpoint: repair the bug without changing the public API.")
    schemas = [{"type": "function", "function": {"name": "large_schema", "description": "schema " * 4500,
                "parameters": {"type": "object", "properties": {}}}}]
    builder.get_budget_snapshot(state, tool_schemas=schemas)
    builder._restore_recent_files_after_compaction(state)
    notes = builder.export_snapshot()["persistent_notes"]
    assert any(note["kind"] == "post_compaction_restore" for note in notes)
    assert not builder.needs_compaction(state, tool_schemas=schemas)


def test_small_window_externalizes_fresh_tool_output_and_preserves_full_source(tmp_path, monkeypatch):
    from backend.agent import tool_result_persistence as persistence
    from backend.tools.base import ToolResult
    storage = tmp_path / "results"
    monkeypatch.setattr(persistence, "TOOL_RESULT_DATA_DIR", storage)
    monkeypatch.setattr(persistence, "_INITIALIZED", False)
    builder = ContextBuilder(TokenBudget(total=24000), workspace_root=tmp_path, conversation_id="small-window")
    builder.append_user("Inspect source")
    builder.append_assistant_tool_calls([ToolCallEvent(id="small", name="read_file", arguments={"file_path": "small.py"})])
    builder.append_tool_result("small", "read_file", ToolResult(content="already read"))
    existing = builder._history[-1].content
    state = AgentState(user_message="Inspect source", workspace_root=tmp_path)
    builder.begin_provider_request(asyncio.run(builder.build(state)), [])
    builder.record_actual_usage(UsageInfo(input_tokens=12000))
    builder.append_assistant_tool_calls([ToolCallEvent(id="large", name="read_file", arguments={"file_path": "large.py"})])
    source = "source line\n" * 3500 + "EXACT_END"
    builder.append_tool_result("large", "read_file", ToolResult(content=source))
    assert builder._history[-1].content.startswith("<persisted-output>")
    assert "Full output saved to:" in builder._history[-1].content
    assert next(message.content for message in builder._history if message.tool_call_id == "small") == existing
    assert builder._last_actual_prompt_tokens == 12000
    stored = list(storage.glob("large_*.txt"))
    assert len(stored) == 1
    assert source in stored[0].read_text(encoding="utf-8")
