"""MiniCode command-hook lifecycle tests."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from backend.agent.loop_preflight import prepare_turn_input
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.hooks import manager as hook_manager_module
from backend.hooks.manager import (
    HookEvent,
    HookManager,
    HookResult,
    get_hook_manager_for_session,
    load_hook_manager_for_workspace,
    register_hook_manager_for_session,
    set_hook_manager,
)


def _hook(command: str, *, matcher: str = ".*") -> dict[str, object]:
    return {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command}],
    }


def _command(script: Path) -> str:
    return f'python "{script}"'


def test_hook_manager_parses_canonical_minicode_configuration() -> None:
    settings = {
        "hooks": {
            "session_start": [_hook("echo start")],
            "user_prompt_submit": [_hook("echo prompt")],
            "pre_tool_use": [_hook("echo pre", matcher="write_file")],
            "post_tool_use": [_hook("echo post")],
            "pre_compact": [_hook("echo compact")],
            "stop": [_hook("echo stop")],
        }
    }
    mgr = HookManager.from_settings(settings)
    for event in (
        HookEvent.SESSION_START,
        HookEvent.USER_PROMPT_SUBMIT,
        HookEvent.PRE_TOOL_USE,
        HookEvent.POST_TOOL_USE,
        HookEvent.PRE_COMPACT,
        HookEvent.STOP,
    ):
        assert mgr.has_hooks(event)


def test_hook_manager_rejects_external_event_names_and_flat_handlers() -> None:
    settings = {
        "hooks": {
            "pre_tool_use": [{"matcher": ".*", "command": "echo flat"}],
            "PreToolUse": [{"matcher": ".*", "command": "echo flat"}],
        }
    }
    mgr = HookManager.from_settings(settings)
    assert not mgr.has_hooks(HookEvent.PRE_TOOL_USE)


def test_hook_manager_invalid_matcher_is_skipped() -> None:
    settings = {
        "hooks": {
            "pre_tool_use": [
                _hook("echo bad", matcher="[invalid"),
                _hook("echo good", matcher="write_file"),
            ]
        }
    }
    mgr = HookManager.from_settings(settings)
    entries = mgr.hooks[HookEvent.PRE_TOOL_USE]
    assert len(entries) == 1
    assert entries[0].raw_matcher == "write_file"


def test_exit_code_2_blocks_and_returns_feedback(tmp_path: Path) -> None:
    script = tmp_path / "block.py"
    script.write_text(
        'import sys\nsys.stderr.write("dangerous operation")\nsys.exit(2)\n',
        encoding="utf-8",
    )
    mgr = HookManager.from_settings(
        {"hooks": {"pre_tool_use": [_hook(_command(script), matcher="rm_file")]}},
        workspace_root=tmp_path,
    )
    result = asyncio.run(mgr.run_pre_tool("rm_file", {"path": "/etc/passwd"}))
    assert result.blocked
    assert "dangerous operation" in result.message
    assert "dangerous operation" in result.feedback


def test_other_nonzero_exit_is_visible_but_non_blocking(tmp_path: Path) -> None:
    script = tmp_path / "failure.py"
    script.write_text('import sys\nsys.stderr.write("hook failed")\nsys.exit(1)\n', encoding="utf-8")
    mgr = HookManager.from_settings(
        {"hooks": {"pre_tool_use": [_hook(_command(script), matcher="write_file")]}},
        workspace_root=tmp_path,
    )
    result = asyncio.run(mgr.run_pre_tool("write_file", {"file_path": "README.md"}))
    assert not result.blocked
    assert not result.has_feedback


def test_pre_tool_matcher_filters_by_tool_name(tmp_path: Path) -> None:
    script = tmp_path / "block.py"
    script.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    mgr = HookManager.from_settings(
        {"hooks": {"pre_tool_use": [_hook(_command(script), matcher="write_file")]}},
        workspace_root=tmp_path,
    )
    result = asyncio.run(mgr.run_pre_tool("read_file", {"file_path": "README.md"}))
    assert not result.blocked


def test_pre_tool_stdin_uses_minicode_top_level_fields(tmp_path: Path) -> None:
    capture = tmp_path / "hook-input.json"
    script = tmp_path / "capture.py"
    script.write_text(
        "import json, sys\n"
        f"json.dump(json.load(sys.stdin), open(r'{capture}', 'w', encoding='utf-8'), ensure_ascii=False)\n",
        encoding="utf-8",
    )
    mgr = HookManager.from_settings(
        {"hooks": {"pre_tool_use": [_hook(_command(script), matcher="write_file")]}},
        workspace_root=tmp_path,
    )
    asyncio.run(
        mgr.run_pre_tool(
            "write_file",
            {"file_path": "README.md", "content": "hello"},
            tool_call_id="tool-123",
        )
    )
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload == {
        "session_id": "",
        "transcript_path": "",
        "cwd": str(tmp_path),
        "event": "pre_tool_use",
        "tool_name": "write_file",
        "tool_input": {"file_path": "README.md", "content": "hello"},
        "tool_use_id": "tool-123",
    }


def test_pre_tool_json_output_preserves_decision_input_and_context(tmp_path: Path) -> None:
    script = tmp_path / "pre.py"
    script.write_text(
        "import json\n"
        "print(json.dumps({'event': 'pre_tool_use', 'permission_decision': 'ask', "
        "'permission_decision_reason': 'review it', "
        "'updated_input': {'file_path': 'safe.txt', 'content': 'ok'}, "
        "'additional_context': 'policy context'}))\n",
        encoding="utf-8",
    )
    mgr = HookManager.from_settings(
        {"hooks": {"pre_tool_use": [_hook(_command(script), matcher="write_file")]}},
        workspace_root=tmp_path,
    )
    result = asyncio.run(mgr.run_pre_tool("write_file", {"file_path": "old.txt"}))
    assert result.permission_decision == "ask"
    assert result.permission_decision_reason == "review it"
    assert result.updated_input == {"file_path": "safe.txt", "content": "ok"}
    assert result.additional_context == "policy context"


def test_permission_request_reads_canonical_decision(tmp_path: Path) -> None:
    script = tmp_path / "permission.py"
    script.write_text(
        "import json\n"
        "print(json.dumps({'event': 'permission_request', "
        "'permission_decision': 'allow', "
        "'updated_input': {'command': 'git status'}}))\n",
        encoding="utf-8",
    )
    mgr = HookManager.from_settings(
        {"hooks": {"permission_request": [_hook(_command(script), matcher="run_command")]}},
        workspace_root=tmp_path,
    )
    result = asyncio.run(
        mgr.run_permission_request("run_command", {"command": "git clean -fd"})
    )
    assert result.permission_decision == "allow"
    assert result.updated_input == {"command": "git status"}
    assert not result.blocked


def test_stop_hook_uses_minicode_fields_and_drops_tool_results(tmp_path: Path) -> None:
    capture = tmp_path / "stop-input.json"
    script = tmp_path / "capture.py"
    script.write_text(
        "import json, sys\n"
        f"json.dump(json.load(sys.stdin), open(r'{capture}', 'w', encoding='utf-8'))\n",
        encoding="utf-8",
    )
    mgr = HookManager.from_settings(
        {"hooks": {"stop": [_hook(_command(script))]}},
        workspace_root=tmp_path,
    )
    asyncio.run(mgr.run_stop("question", "final reply", tool_results=[object()]))
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["event"] == "stop"
    assert payload["stop_hook_active"] is False
    assert payload["last_assistant_message"] == "final reply"
    assert "TOOL_RESULTS_JSON" not in payload

    asyncio.run(
        mgr.run_stop(
            "question",
            "revised reply",
            stop_hook_active=True,
        )
    )
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["stop_hook_active"] is True
    assert payload["last_assistant_message"] == "revised reply"


def test_hook_result_properties() -> None:
    assert HookResult(feedback="fix this").has_feedback
    assert not HookResult(feedback="  ").has_feedback
    assert HookResult(additional_context="context").has_additional_context
    assert not HookResult(additional_context=" ").has_additional_context
    assert HookResult(updated_input={}).has_updated_input
    assert not HookResult().has_updated_input


def test_untrusted_workspace_excludes_project_and_local_hooks(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(hook_manager_module, "is_workspace_trusted_for_hooks", lambda _root: False)
    stack = ConfigLayerStack((ConfigLayer(
        ConfigLayerSource("project", project_config_folder=str(workspace / ".minicode")),
        {"hooks": {"pre_tool_use": [_hook("echo project")]}},
    ),))

    manager = load_hook_manager_for_workspace(workspace, config_layer_stack=stack)

    assert not manager.has_hooks(HookEvent.PRE_TOOL_USE)


def test_trusted_workspace_includes_project_hooks(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(hook_manager_module, "is_workspace_trusted_for_hooks", lambda _root: True)
    stack = ConfigLayerStack((ConfigLayer(
        ConfigLayerSource("project", project_config_folder=str(workspace / ".minicode")),
        {"hooks": {"pre_tool_use": [_hook("echo project")]}},
    ),))

    manager = load_hook_manager_for_workspace(workspace, config_layer_stack=stack)

    assert [entry.command for entry in manager.hooks[HookEvent.PRE_TOOL_USE]] == [
        "echo project",
    ]


def test_concurrent_turns_keep_separate_hook_managers() -> None:
    class _PromptHooks:
        def __init__(self, label: str) -> None:
            self.label = label

        def has_hooks(self, event: HookEvent) -> bool:
            return event == HookEvent.USER_PROMPT_SUBMIT

        async def run_user_prompt_submit(self, message: str) -> HookResult:
            await asyncio.sleep(0)
            return HookResult(updated_input=f"{self.label}:{message}")

    async def observe(manager: _PromptHooks, message: str) -> str:
        state = SimpleNamespace(user_message=message)
        result = await prepare_turn_input(
            message,
            state=state,
            turn_kernel=SimpleNamespace(schedule_user_input=lambda _message: None),
            session_id="",
            deadline=None,
            cancel_event=None,
            hook_manager=manager,
        )
        return result.user_message

    async def run() -> list[str]:
        return list(
            await asyncio.gather(
                observe(_PromptHooks("first"), "one"),
                observe(_PromptHooks("second"), "two"),
            )
        )

    assert asyncio.run(run()) == ["first:one", "second:two"]


def test_json_continue_false_blocks_prompt_and_preserves_system_message(tmp_path: Path) -> None:
    script = tmp_path / "prompt.py"
    script.write_text(
        "import json\n"
        "print(json.dumps({'continue': False, 'stop_reason': 'policy stop', "
        "'system_message': 'Visible policy notice'}))\n",
        encoding="utf-8",
    )
    manager = HookManager.from_settings(
        {"hooks": {"user_prompt_submit": [_hook(_command(script))]}},
        workspace_root=tmp_path,
    )

    result = asyncio.run(manager.run_user_prompt_submit("continue"))

    assert result.blocked is True
    assert result.prevent_continuation is True
    assert result.message == "policy stop"
    assert result.system_message == "Visible policy notice"

    scheduled: list[str] = []
    kernel = SimpleNamespace(schedule_user_input=scheduled.append)

    async def run_preflight():
        return await prepare_turn_input(
            "continue",
            state=SimpleNamespace(user_message="continue"),
            turn_kernel=kernel,
            session_id="",
            deadline=None,
            cancel_event=None,
            hook_manager=manager,
        )

    preflight = asyncio.run(run_preflight())
    assert preflight.blocked is True
    assert scheduled == []


def test_session_start_side_channels_preserve_initial_message_and_watch_paths() -> None:
    class _HookManager:
        def has_hooks(self, event):
            return event == HookEvent.SESSION_START

        async def run_session_start_once(self, _session_id):
            return HookResult(
                initial_user_message="bootstrap from hook",
                watch_paths=("C:/repo/.env", "C:/repo/.env"),
            )

    scheduled: list[str] = []
    state = SimpleNamespace(user_message="", prompt_context={})
    result = asyncio.run(
        prepare_turn_input(
            "",
            state=state,
            turn_kernel=SimpleNamespace(schedule_user_input=scheduled.append),
            session_id="session-side-channel",
            deadline=None,
            cancel_event=None,
            hook_manager=_HookManager(),
        )
    )

    assert result.user_message == "bootstrap from hook"
    assert result.initial_user_message == "bootstrap from hook"
    assert result.watch_paths == ("C:/repo/.env",)
    assert state.user_message == "bootstrap from hook"
    assert state.prompt_context["hook_watch_paths"] == ["C:/repo/.env"]
    assert scheduled == ["bootstrap from hook"]


def test_session_end_clears_only_the_registered_session_owner() -> None:
    session_manager = HookManager()
    global_manager = HookManager()
    register_hook_manager_for_session("session-a", session_manager)
    set_hook_manager(global_manager)
    try:
        assert get_hook_manager_for_session("session-a") is session_manager
        asyncio.run(session_manager.run_session_end(session_id="session-a", reason="closed"))
        assert get_hook_manager_for_session("session-a") is None
    finally:
        set_hook_manager(None)
