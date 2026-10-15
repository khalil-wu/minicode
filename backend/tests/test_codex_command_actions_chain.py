"""Codex command actions across actual execution, durable events and both transcripts."""

from __future__ import annotations

import asyncio
import json
import os
import time
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from backend.agent.command_actions import command_actions_for_tool, parse_command_actions
from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.state import AgentState
from backend.agent.tool_events import tool_call_start_event
from backend.agent.tool_execution import store_result
from backend.api.models import ToolCallRecord as ApiToolCallRecord
from backend.artifact.store import ArtifactStore
from backend.conversations.public_projection import project_public_tool_call
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.subagent_service import build_subagent_transcript_messages, project_subagent_transcript_events
from backend.tools.base import ToolResult, execution_exception_result
from backend.tools.command_tool import RunCommandTool
from backend.tools.read_file import ReadFileTool
from backend.tools.registry import ToolRegistry
from backend.ws.stream_state import apply_stream_event, create_stream_state, get_stream_content_blocks


@pytest.mark.parametrize("script,path", [
    (r"Get-Content C:\skills\demo\SKILL.md", "C:/skills/demo/SKILL.md"),
    ('get-content -Raw "C:\\skills and plugins\\SKILL.md"', "C:/skills and plugins/SKILL.md"),
    (r"Get-Content -Path C:\skills\demo\SKILL.md", "C:/skills/demo/SKILL.md"),
    (r"Get-Content -Raw -LiteralPath C:\skills\demo\SKILL.md", "C:/skills/demo/SKILL.md"),
    ("gc src/first.py", "C:/workspace/src/first.py"),
    ("type src/first.py", "C:/workspace/src/first.py"),
])
def test_powershell_official_simple_read_examples(script, path):
    assert parse_command_actions(script, "C:/workspace", powershell=True) == [
        {"type": "read", "command": script, "name": path.rsplit("/", 1)[-1], "path": path},
    ]


@pytest.mark.parametrize("script", [
    r"Get-Content 'C:\Users\O''Brien\skill\SKILL.md'",
    'Get-Content "$(Remove-Item C:/important)/skills/demo/SKILL.md"',
    "Get-Content -ReadCount:([IO.File]::Delete('C:/important')) C:/skills/demo/SKILL.md",
    "Get-Content C:/Users/Alice/.ssh/id_rsa,C:/skills/demo/SKILL.md",
    "Get-Content C:/skills/demo/SKILL.md -Raw; Remove-Item C:/important",
    "Get-Content C:/skills/demo/SKILL.md C:/important",
    "Get-Content C:/skills/*/SKILL.md",
    "Get-Content -Encoding UTF8 C:/skills/demo/SKILL.md",
    "Get-Content -Raw",
    "Get-Content src/first.py | Select-Object -First 5",
    "Write-Output 'cat src/first.py'",
    "rg -n search src",
])
def test_powershell_official_complex_reads_keep_original_unknown(script):
    assert parse_command_actions(script, "C:/workspace", powershell=True) == [{"type": "unknown", "command": script}]


@pytest.mark.parametrize("script,types", [
    ("cat src/first.py", ["read"]),
    ("Get-Content -Raw src/first.py", ["read"]),
    ("sed -n '10,20p' src/first.py", ["read"]),
    ("head -n 20 src/first.py", ["read"]),
    ("tail -n +20 src/first.py", ["read"]),
    ("bat --theme dark src/first.py", ["read"]),
    ("nl -ba src/first.py", ["read"]),
    ("awk '{ print $1 }' src/first.py", ["read"]),
    ("rg --files src | head -n 40", ["listFiles"]),
    ("rg -n -g '*.py' search src", ["search"]),
    ("grep -n -e search src", ["search"]),
    ("git grep -l search src", ["search"]),
    ("git ls-files --exclude target src", ["listFiles"]),
    ("ls --color always src", ["listFiles"]),
    ("fd --type f pattern src", ["search"]),
    ("find src -name '*.py'", ["search"]),
    ("cat src/first.py; rg search src; ls src", ["read", "search", "listFiles"]),
    ("cd src && cat first.py || true", ["read"]),
    ("echo starting && rg search src", ["search"]),
    ("rg --files src | xargs cat", ["listFiles"]),
    ("cat src/first.py | sed -n '1,5p'", ["read"]),
])
def test_posix_official_command_categories(script, types):
    assert [action["type"] for action in parse_command_actions(script, "/workspace", powershell=False)] == types


@pytest.mark.parametrize("script", [
    "git status", "printf '%s' 'cat src/first.py'", "cat first.py second.py",
    "cat $SOURCE", "cat '*.py' && npm test", "cat src/*.py", "cat src/first.py > result.txt",
    "for f in src; do cat $f; done", "cat src/first.py &", "cat $(echo file)",
    "rg --files | xargs sed -i 's/old/new/g'", "sed -ni '1,2p' src/first.py",
    "Get-Content -Encoding UTF8 unknown.ps1", "cat src/first.py; echo done",
])
def test_posix_unknown_never_hides_unclassified_command(script):
    assert parse_command_actions(script, "/workspace", powershell=False) == [{"type": "unknown", "command": script}]


def test_actions_preserve_order_resolve_executor_paths_and_deduplicate():
    actions = parse_command_actions("cd src && cat first.py; cat first.py; rg TODO ../lib; ls ../lib", "/workspace", powershell=False)
    assert actions == [
        {"type": "read", "command": "cat first.py", "name": "first.py", "path": "/workspace/src/first.py"},
        {"type": "search", "command": "rg TODO ../lib", "query": "TODO", "path": "lib"},
        {"type": "listFiles", "command": "ls ../lib", "path": "lib"},
    ]
    pipe = "cat src/first.py | sed -n '1,5p'"
    assert parse_command_actions(pipe, "/workspace", powershell=False)[0]["command"] == pipe


@pytest.mark.parametrize("requested_shell,program,command,kind", [
    ("bash", "bash.exe", "cat source.txt", "read"),
    ("sh", "sh", "rg TODO src", "search"),
    ("pwsh", "pwsh.exe", "Get-Content source.txt", "read"),
    ("cmd", "cmd.exe", "type source.txt", "unknown"),
    (None, "cmd.exe", "type source.txt", "unknown"),
])
def test_selected_shell_drives_the_same_main_and_result_projection(tmp_path, monkeypatch, requested_shell, program, command, kind):
    selections = []

    def select_shell(script, **kwargs):
        selections.append(kwargs)
        return [program, "-c", script]

    monkeypatch.setattr("backend.tools.command_support.command_shell_argv", select_shell)
    args = {"command": command, **({"shell": requested_shell} if requested_shell else {})}
    actions = command_actions_for_tool("run_command", args, tmp_path)
    assert selections == [{"shell": requested_shell, "login": False}]
    assert actions[0]["type"] == kind
    if kind == "unknown":
        assert actions == [{"type": "unknown", "command": command}]
    registry = ToolRegistry()
    registry.register(RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts")))
    call = ToolCallEvent(id="selected-shell-projection", name="run_command", arguments=args)
    start = tool_call_start_event(call, started_epoch=time.time(), iteration_id="shell-iter", turn_id="shell-turn",
        tool_registry=registry, workspace_root=tmp_path)
    result = store_result(call, ToolResult(content="Actual shell result"), ContextBuilder(),
        AgentState(user_message="inspect this command", workspace_root=tmp_path), tool_registry=registry,
        tool_ctx=ToolExecutionContext(permission=PermissionContext(workspace_root=tmp_path), workspace_root=tmp_path))
    assert start.data["command_actions"] == result.data["command_actions"] == actions


@pytest.mark.parametrize("status", ["failed", "blocked", "timeout", "cancelled", "partial"])
def test_status_retains_real_diagnostics_without_replacing_action(status, tmp_path):
    registry = ToolRegistry()
    registry.register(ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts")))
    state = AgentState(user_message="read the file")
    original = ToolResult(content="File not found: .gitmodules", is_error=True, status=status,
                          display_summary="Failed: Read", developer_detail="ENOENT .gitmodules")
    event = store_result(ToolCallEvent(id="read-missing", name="read_file", arguments={"file_path": ".gitmodules"}),
                         original, ContextBuilder(), state, tool_registry=registry)
    assert event.data["display_summary"] == "Read"
    assert event.data["status"] == status
    assert event.data["is_error"] is True
    assert event.data["summary"] == original.content
    assert event.data["developer_detail"] == "ENOENT .gitmodules"
    assert not event.data.get("user_summary")
    assert state.tool_calls[0].status == status
    assert original.display_summary == "Failed: Read"


def test_generic_error_constructor_preserves_actual_error_without_extra_label(tmp_path):
    error = execution_exception_result(RuntimeError("actual provider diagnostic"))
    assert error.is_error and error.status == "failed"
    assert "actual provider diagnostic" in error.content
    assert error.developer_detail == "actual provider diagnostic"
    assert not error.user_summary
    missing = ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))._error_result("File not found: .gitmodules")
    assert missing.content == "Error: File not found: .gitmodules"
    assert missing.developer_detail == "File not found: .gitmodules"
    assert not missing.user_summary


def test_real_command_execution_canonical_wire_main_resume_child_history_and_rest(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "source.txt").write_text("real file evidence\nsecond line\n", encoding="utf-8")
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass", workspace_root=root),
                                   workspace_root=root, conversation_id="actions-owner", session_id="actions-session")
    registry = ToolRegistry()
    registry.register(RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts")))
    state = AgentState(user_message="read and run", workspace_root=root)
    builder = ContextBuilder(workspace_root=root, conversation_id=context.conversation_id)
    read_command = "Get-Content -LiteralPath source.txt -Raw" if os.name == "nt" else "cat source.txt"
    failed_command = "Write-Output 'actual exit evidence'; exit 7" if os.name == "nt" else "printf 'actual exit evidence\\n'; exit 7"
    journal = ExecutionJournal("actions-owner", base_dir=tmp_path / "journals")
    journal.append("user_prompt", {"content": state.user_message})
    stream_state = create_stream_state(context.conversation_id, "actions-message", "turn:1")
    streams = {context.conversation_id: stream_state}
    wire = []
    for index, command in enumerate((read_command, failed_command, "exit 0")):
        call = ToolCallEvent(id=f"actions-{index}", name="run_command", arguments={"command": command})
        start = tool_call_start_event(call, started_epoch=time.time(), iteration_id="iter:1", turn_id="turn:1",
                                      tool_registry=registry, workspace_root=root)
        context.tool_call_id = call.id
        actual = asyncio.run(registry.execute("run_command", call.arguments, context=context))
        result = store_result(call, actual, builder, state, iteration_id="iter:1", turn_id="turn:1",
                              tool_ctx=context, tool_registry=registry)
        assert start.data["command_actions"] == result.data["command_actions"]
        assert result.data["display_summary"] == "Run"
        journal.append_tool_use(start.data)
        journal.append_tool_result(result.data, tool_name=call.name)
        for event in (start, result):
            wire.append(event.to_ws_message())
            apply_stream_event(streams, context.conversation_id, event.type, event.data)
        api = ApiToolCallRecord.from_internal(state.tool_calls[-1])
        assert api.command_actions == result.data["command_actions"]
        assert api.stdout_preview == actual.runtime_metadata["stdout_preview"] == result.data["stdout_preview"]
        assert api.stderr_preview == actual.runtime_metadata["stderr_preview"] == result.data["stderr_preview"]
        assert api.exit_code == result.data["exit_code"]
        assert json.loads(api.model_dump_json())["command_actions"] == api.command_actions
    assert wire[0]["command_actions"][0]["type"] == "read"
    assert wire[2]["command_actions"] == [{"type": "unknown", "command": failed_command}]
    assert "real file evidence" in wire[1]["summary"]
    assert wire[3]["status"] == "failed" and wire[3]["is_error"]
    assert "actual exit evidence" in wire[3]["summary"] and "Exit code: 7" in wire[3]["summary"]
    journal.append_terminal(status="completed", summary="Both actual commands were inspected.")
    reopened = ExecutionJournal("actions-owner", base_dir=tmp_path / "journals")
    events = [asdict(event) for event in reopened.read_events()]
    child_history = build_subagent_transcript_messages({"agent_id": "actions-owner", "events": events})
    child_incremental = project_subagent_transcript_events(events)
    main_blocks = get_stream_content_blocks(stream_state)
    child_records = [block["record"] for message in child_history for block in message.get("blocks", []) if block["type"] == "tool_call"]
    main_records = [project_public_tool_call(block["record"]) for block in main_blocks if block["type"] == "tool_call"]
    assert [record["commandActions"] for record in main_records] == [record["commandActions"] for record in child_records]
    assert [record["stdoutPreview"] for record in child_records] == [item["stdout_preview"] for item in wire if item["type"] == "tool_result"]
    assert [record["exitCode"] for record in child_records] == [0, 7, 0]
    assert child_records[2]["stdoutPreview"] == child_records[2]["stderrPreview"] == ""
    assert [record["stdoutPreview"] for record in main_records] == [record["stdoutPreview"] for record in child_records]
    assert child_records[1]["status"] == "failed" and "Exit code: 7" in child_records[1]["outputPreview"]
    assert child_incremental[1]["payload"]["tool_call"]["commandActions"] == wire[0]["command_actions"]
    detached = deepcopy(child_records)
    detached[0]["commandActions"][0]["name"] = "caller mutation"
    assert main_records[0]["commandActions"][0]["name"] == "source.txt"
    assert state.tool_calls[0].command_actions[0]["name"] == "source.txt"
    evidence_root = os.environ.get("MINICODE_CODEX_ACTIONS_EVIDENCE_ROOT")
    if evidence_root:
        destination = Path(evidence_root)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "real-command-actions-wire-history.json").write_text(json.dumps({
            "reference_revision": "822e58cc3d666166c7446c5b1ea2e52f5d09594c",
            "wire": wire, "main_blocks": main_blocks, "child_messages": child_history,
            "child_incremental": child_incremental,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
