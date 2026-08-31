"""MiniCode hook matcher and event-selection contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.hooks.dispatcher import matcher_matches, select_handlers
from backend.hooks.manager import HookEvent
from backend.hooks.manager import HookResult
from backend.hooks.policy import event_policy
from backend.hooks.runtime import (
    ConfigChangeHookBlocked,
    config_change_is_blocked,
    run_config_change_hook,
)


def _entry(matcher: str) -> SimpleNamespace:
    return SimpleNamespace(raw_matcher=matcher, condition="")


def test_plain_matcher_is_exact_not_substring() -> None:
    assert matcher_matches("Bash", "Bash") is True
    assert matcher_matches("Bash", "BashOutput") is False


def test_pipe_matcher_is_a_set_of_exact_names() -> None:
    assert matcher_matches("Edit|Write", "Edit") is True
    assert matcher_matches("Edit|Write", "Write") is True
    assert matcher_matches("Edit|Write", "WriteFile") is False


def test_empty_and_star_matchers_match_all() -> None:
    assert matcher_matches(None, "anything") is True
    assert matcher_matches("", "anything") is True
    assert matcher_matches("*", "anything") is True


def test_invalid_regex_fails_closed() -> None:
    assert matcher_matches("[invalid", "anything") is False


def test_empty_event_query_does_not_filter_configured_matcher() -> None:
    # "Setup" was an invented event and was removed; any real matcher-driven
    # event exercises the same empty-target selection path.
    selected = select_handlers(
        [_entry("only-this-value")],
        event=HookEvent.SESSION_START,
        match_target="",
        condition_matches=lambda _entry: True,
    )
    assert len(selected) == 1


def test_events_without_matchers_select_all_entries() -> None:
    entries = [_entry("never-matches"), _entry("also-never")]
    for event in (
        HookEvent.USER_PROMPT_SUBMIT,
        HookEvent.STOP,
        HookEvent.TEAMMATE_IDLE,
        HookEvent.TASK_CREATED,
        HookEvent.TASK_COMPLETED,
        HookEvent.WORKTREE_CREATE,
        HookEvent.WORKTREE_REMOVE,
        HookEvent.CWD_CHANGED,
    ):
        assert event_policy(event).matcher_applies is False
        assert len(
            select_handlers(
                entries,
                event=event,
                match_target="irrelevant",
                condition_matches=lambda _entry: True,
            )
        ) == len(entries)


def test_external_tool_aliases_are_not_rewritten() -> None:
    assert matcher_matches("Task", "task", tool_match=True) is False
    assert matcher_matches("Task", "task", tool_match=False) is False


def test_config_change_policy_source_is_audit_only(monkeypatch) -> None:
    result = HookResult(
        blocked=True,
        permission_decision="deny",
        permission_decision_reason="managed policy must win",
        feedback="audit detail",
    )

    class _Manager:
        async def run_config_change(self, **_kwargs):
            return result

    monkeypatch.setattr("backend.hooks.get_hook_manager", lambda: _Manager())
    effective = asyncio.run(
        run_config_change_hook(
            source="policy_settings",
            file_path="managed-settings.json",
        )
    )

    assert effective is not result
    assert effective.blocked is False
    assert effective.permission_decision == ""
    assert effective.feedback == "audit detail"
    assert config_change_is_blocked(
        effective,
        source="policy_settings",
        file_path="managed-settings.json",
    ) is False


def test_config_change_non_policy_veto_is_consumable() -> None:
    result = HookResult(blocked=True, message="security hook denied")
    assert config_change_is_blocked(result, source="user_settings", file_path="settings.json")
    try:
        from backend.hooks.runtime import raise_if_config_change_blocked

        raise_if_config_change_blocked(
            result,
            source="user_settings",
            file_path="settings.json",
        )
    except ConfigChangeHookBlocked as exc:
        assert "security hook denied" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected ConfigChangeHookBlocked")


def _execution_entry() -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="hook-1",
        source="project",
        source_path="hooks.json",
        display_order=0,
        status_message="",
        additional_context_limit=None,
    )


def test_pre_tool_runtime_failure_is_visible_non_blocking_failure() -> None:
    from backend.hooks.dispatcher import HookExecution
    from backend.hooks.reducer import reduce_hook_executions

    reduction = reduce_hook_executions(
        HookEvent.PRE_TOOL_USE,
        [
            HookExecution(
                entry=_execution_entry(),
                stdout="",
                stderr="Hook execution failed: failed to spawn bash",
                exit_code=1,
                configured_order=0,
                completion_order=0,
                duration_ms=3,
                execution_failed=True,
            )
        ],
        expected_event_name="pre_tool_use",
    )

    assert reduction.failed is False
    assert reduction.blocked is False
    assert reduction.permission_decision == ""
    assert reduction.errors == ("Hook execution failed: failed to spawn bash",)
    assert reduction.run_summaries[0]["status"] == "failed"
    assert reduction.run_summaries[0]["runtime_error"] is True


def test_hook_nonzero_exit_is_not_reclassified_as_runtime_failure() -> None:
    from backend.hooks.dispatcher import HookExecution
    from backend.hooks.reducer import reduce_hook_executions

    reduction = reduce_hook_executions(
        HookEvent.PRE_TOOL_USE,
        [
            HookExecution(
                entry=_execution_entry(),
                stdout="",
                stderr="lint hook failed",
                exit_code=1,
                configured_order=0,
                completion_order=0,
                duration_ms=3,
            )
        ],
        expected_event_name="pre_tool_use",
    )

    assert reduction.blocked is False
    assert reduction.errors == ("lint hook failed",)
    assert reduction.run_summaries[0]["status"] == "failed"


def test_custom_git_for_windows_mingw_git_resolves_root_bash(tmp_path, monkeypatch) -> None:
    from backend.hooks import runners

    git_root = tmp_path / "PortableGit"
    git_exe = git_root / "mingw64" / "bin" / "git.exe"
    bash_exe = git_root / "bin" / "bash.exe"
    git_exe.parent.mkdir(parents=True)
    bash_exe.parent.mkdir(parents=True)
    git_exe.write_text("", encoding="utf-8")
    bash_exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(runners.shutil, "which", lambda name: str(git_exe) if name == "git.exe" else None)
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)

    assert runners._find_git_bash() == bash_exe.resolve()
