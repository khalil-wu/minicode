from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace

import pytest

from backend.agent.final_tool_request import canonical_tool_request_digest
from backend.agent.state import AgentState
from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.terminal.manager import BackgroundCommand, BackgroundCommandManager
from backend.tools import ast_tools, tree_sitter_parser
from backend.tools.ast_tools import FindReferencesTool, GoToDefinitionTool
from backend.tools.mcp_tools import GetMcpPromptTool
from backend.tools.monitor_tool import MonitorTool
from backend.tools.registry import ToolRegistry
from backend.tools.sleep_tool import SleepTool
from backend.tools.tool_search import ToolSearchTool


def _grammar(language):
    if tree_sitter_parser.get_parser(language) is None:
        pytest.skip(f"optional {language} grammar is not installed")


@pytest.mark.parametrize("language,source,symbol", [
    ("javascript", "const result = target;", "target"),
    ("typescript", "type Other = Target;", "Target"),
    ("tsx", "const view = <Target />;", "Target"),
    ("go", "package demo\nfunc other() { result := target() }", "target"),
    ("rust", "fn other() { let result = target(); }", "target"),
    ("java", "class Other { Target result = target(); }", "Target"),
])
def test_ast_usage_is_not_a_definition(language, source, symbol):
    _grammar(language)
    assert tree_sitter_parser.find_definitions(source, symbol, language) == []
    assert tree_sitter_parser.find_references(source, symbol, language, include_definitions=False)


@pytest.mark.parametrize("language,source,symbol", [
    ("javascript", "const {key: target} = source;", "target"),
    ("javascript", "const {target = source} = other;", "target"),
    ("typescript", "interface Target {}", "Target"),
    ("tsx", "const Target = () => <span />;", "Target"),
    ("go", "package demo\ntype Target struct {}", "Target"),
    ("go", "package demo\nfunc other() { target, another := source, other }", "target"),
    ("rust", "fn other() { let (target, another) = source; }", "target"),
    ("java", "class Other { Target another, target; }", "target"),
])
def test_ast_declaration_binding_is_found(language, source, symbol):
    _grammar(language)
    assert tree_sitter_parser.find_definitions(source, symbol, language)
    assert tree_sitter_parser.find_references(source, symbol, language, include_definitions=False) == []


@pytest.mark.parametrize("extension,language,source", [
    ("js", "javascript", "// function ghost() {}\nconst message = 'ghost';"),
    ("tsx", "tsx", "// interface ghost {}\nconst view = <span>ghost</span>;"),
])
@pytest.mark.parametrize("tool_type", [GoToDefinitionTool, FindReferencesTool])
def test_successful_ast_zero_match_stays_empty(tmp_path, extension, language, source, tool_type):
    _grammar(language)
    (tmp_path / ("source." + extension)).write_text(source, encoding="utf-8")
    registry = ToolRegistry()
    tool = tool_type()
    registry.register(tool)
    result = asyncio.run(registry.execute(tool.name, {"name": "ghost"}, context=ToolExecutionContext(
        permission=PermissionContext(), workspace_root=tmp_path,
    )))
    assert not result.is_error
    assert "source." + extension not in result.content


def test_same_line_reference_survives_definition_filter(tmp_path):
    _grammar("javascript")
    (tmp_path / "source.js").write_text("const target = () => target();", encoding="utf-8")
    result = asyncio.run(FindReferencesTool().execute({"name": "target", "include_definitions": False},
        context=ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path)))
    assert "source.js:1" in result.content


def test_missing_grammar_uses_documented_approximate_search(tmp_path, monkeypatch):
    monkeypatch.setattr(tree_sitter_parser, "get_parser", lambda language: None)
    (tmp_path / "source.js").write_text("// function ghost() {}", encoding="utf-8")
    result = asyncio.run(GoToDefinitionTool().execute({"name": "ghost"},
        context=ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path)))
    assert "source.js:1" in result.content
    assert "approximate" in GoToDefinitionTool().description


def test_broken_parser_is_reported_instead_of_becoming_regex_success(tmp_path, monkeypatch):
    class BrokenParser:
        def parse(self, source):
            raise RuntimeError("broken parser")
    monkeypatch.setattr(tree_sitter_parser, "get_parser", lambda language: BrokenParser())
    (tmp_path / "source.js").write_text("function target() {}", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(GoToDefinitionTool())
    result = asyncio.run(registry.execute("go_to_definition", {"name": "target"},
        context=ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path)))
    assert result.is_error
    assert "broken parser" in result.content


def test_ast_file_work_yields_to_the_host_and_retains_cancelled_worker(tmp_path, monkeypatch):
    (tmp_path / "source.py").write_text("def target(): pass", encoding="utf-8")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = ast_tools._read_safe
    def held(path):
        started.set()
        release.wait(2)
        try:
            return original(path)
        finally:
            finished.set()
    monkeypatch.setattr(ast_tools, "_read_safe", held)
    async def run():
        task = asyncio.create_task(GoToDefinitionTool().execute({"name": "target"},
            context=ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path)))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            await asyncio.sleep(0)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    asyncio.run(run())


def test_ast_directory_and_extension_use_context_permission_owner(tmp_path):
    (tmp_path / "visible.py").write_text("def target(): pass", encoding="utf-8")
    (tmp_path / "private.py").write_text("def target(): pass", encoding="utf-8")
    checker = PermissionChecker(PermissionSettings(path_denylist=["private.py"]), tmp_path)
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path, permission_checker=checker)
    result = asyncio.run(GoToDefinitionTool().execute({"name": "target", "file_extensions": ["py"]}, context))
    assert "visible.py" in result.content and "private.py" not in result.content
    projectless = asyncio.run(GoToDefinitionTool().execute({"name": "target"}, replace(context, workspace_root=None)))
    assert projectless.is_error and "open workspace" in projectless.content


def test_monitor_real_owner_and_utf8_cursor_chain(tmp_path):
    async def run():
        path = tmp_path / "command.output"
        text = "开头\nlast\n"
        path.write_text(text, encoding="utf-8")
        manager = BackgroundCommandManager(session_id="tool-audit")
        command = BackgroundCommand(command_id="bg_audit", command="record-only", conversation_id="owner",
            output_path=str(path), output_bytes=len(text.encode("utf-8")), status="completed", exit_code=0)
        manager._commands[command.command_id] = command
        context = ToolExecutionContext(permission=PermissionContext(), conversation_id="owner", background_manager=manager)
        registry = ToolRegistry()
        registry.register(MonitorTool())
        first = await registry.execute("monitor", {"command_id": command.command_id, "cursor": 0, "max_chars": 3}, context=context)
        cursor = first.runtime_metadata["next_cursor"]
        assert cursor == len("开头\n".encode("utf-8"))
        second = await registry.execute("monitor", {"command_id": command.command_id, "cursor": cursor}, context=context)
        assert "last" in second.content and "开头" not in second.content
        for action in ("status", "cancel", "write_stdin"):
            result = await registry.execute("monitor", {"action": action, "command_id": command.command_id, "chars": "x"}, context=replace(context, conversation_id="other"))
            assert result.is_error
        assert command.status == "completed"
    asyncio.run(run())


def test_monitor_wait_cancellation_keeps_command_running():
    async def run():
        manager = BackgroundCommandManager(session_id="tool-audit")
        command = BackgroundCommand(command_id="bg_audit", command="record-only", conversation_id="owner")
        manager._commands[command.command_id] = command
        cancel_event = asyncio.Event()
        context = ToolExecutionContext(permission=PermissionContext(), conversation_id="owner", background_manager=manager, cancel_event=cancel_event)
        task = asyncio.create_task(MonitorTool().execute({"command_id": command.command_id, "yield_time_ms": 100}, context))
        await asyncio.sleep(0)
        cancel_event.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert command.status == "running"
    asyncio.run(run())


@pytest.mark.parametrize("args", [{"action": "write_stdin", "command_id": "bg_audit"}, {"action": "cancel"}, {"action": "status", "cursor": -1}, {"action": "unknown"}])
def test_monitor_schema_and_semantics_reject_before_execution(args):
    registry = ToolRegistry()
    registry.register(MonitorTool())
    result = asyncio.run(registry.execute("monitor", args))
    assert result.error_kind == "validation_error"


def test_tool_search_registry_and_permission_changes_refresh_activation():
    async def run():
        registry = ToolRegistry()
        registry.register(SleepTool())
        search = ToolSearchTool(registry)
        registry.register(search)
        state = AgentState(user_message="audit")
        checker = PermissionChecker(PermissionSettings())
        context = ToolExecutionContext(permission=PermissionContext(), tool_registry=registry, permission_checker=checker, metadata={"_agent_state": state})
        result = await search.execute({"query": "select:sleep"}, context)
        assert json.loads(result.content)["activated"] == ["sleep"]
        context.permission = replace(context.permission, tool_deny_rules=["sleep"])
        result = await search.execute({"query": "select:sleep"}, context)
        assert json.loads(result.content)["matches"] == []
        context.permission = replace(context.permission, tool_deny_rules=[])
        registry.unregister("sleep")
        result = await search.execute({"query": "select:sleep"}, context)
        assert json.loads(result.content)["matches"] == []
    asyncio.run(run())


@pytest.mark.parametrize("value", [{"include": True}, 2, False, ["x"]])
def test_prompt_protocol_rejects_non_string_arguments_before_adapter(value):
    class Manager:
        def get_client(self, name):
            pytest.fail("invalid prompt input reached MCP adapter")
    registry = ToolRegistry()
    registry.register(GetMcpPromptTool(Manager()))
    result = asyncio.run(registry.execute("get_mcp_prompt", {"server": "docs", "name": "review", "arguments": {"options": value}}))
    assert result.error_kind == "validation_error"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_sleep_input_is_rejected_at_existing_request_boundary(value):
    with pytest.raises(ValueError):
        canonical_tool_request_digest("sleep", {"seconds": value})
