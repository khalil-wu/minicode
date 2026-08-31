from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from backend.agent.loop_session import collect_mcp_instructions, mcp_registry_version
from backend.config import PermissionSettings
from backend.llm import model_runtime
from backend.llm import model_runtime_definitions
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.base import PermissionLevel
from backend.ws.handlers.conversation import _clear_active_conversation_runtime


def test_config_command_fails_closed_when_bash_is_unavailable(monkeypatch) -> None:
    """A missing Bash must not make provider config use platform shell=True."""

    monkeypatch.setattr(model_runtime_definitions.Path, "is_file", lambda _path: False)
    monkeypatch.setattr(model_runtime_definitions.shutil, "which", lambda _name: None)

    def forbidden_subprocess(*_args, **_kwargs):
        pytest.fail("config command execution must not fall back to shell=True")

    monkeypatch.setattr(model_runtime_definitions.subprocess, "run", forbidden_subprocess)

    with pytest.raises(model_runtime.ProviderRegistrationError, match="Failed to resolve"):
        model_runtime.resolve_config_value(
            "!echo should-not-run",
            description="test provider value",
        )


class _BrokenMCPManager:
    def get_server_instructions(self) -> dict[str, str]:
        raise RuntimeError("instruction read failed")

    @property
    def registry_version(self) -> int:
        raise RuntimeError("registry read failed")


def test_mcp_instruction_failures_are_not_replaced_with_empty_context() -> None:
    with pytest.raises(RuntimeError, match="instruction read failed"):
        collect_mcp_instructions(_BrokenMCPManager())


def test_mcp_registry_failures_are_not_replaced_with_generation_zero() -> None:
    with pytest.raises(RuntimeError, match="registry read failed"):
        mcp_registry_version(_BrokenMCPManager())


def test_permission_metadata_failure_keeps_the_stricter_level_and_is_logged(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken_side_effect(_args):
        raise RuntimeError("side-effect metadata failed")

    tool = SimpleNamespace(
        name="broken_metadata_tool",
        permission=PermissionLevel.DIFF_REVIEW,
        check_permission=lambda _args, _context: None,
        is_read_only=lambda _args: False,
        get_side_effect_kind=broken_side_effect,
        get_schema=lambda: SimpleNamespace(parameters={}),
    )
    checker = PermissionChecker(PermissionSettings(), tmp_path)

    with caplog.at_level(logging.WARNING, logger="backend.permissions.checker"):
        level = checker.check(
            "broken_metadata_tool",
            {},
            context=PermissionContext(mode="auto"),
            tool=tool,
        )

    assert level == PermissionLevel.DIFF_REVIEW
    assert "Failed to determine" in caplog.text


def test_active_conversation_cleanup_reports_context_clear_failure() -> None:
    workspace_clear_calls: list[str] = []

    def broken_clear() -> None:
        raise RuntimeError("context clear failed")

    session = SimpleNamespace(
        active_conversation_id="conversation-1",
        context_builder=SimpleNamespace(clear=broken_clear),
        session_lifecycle=SimpleNamespace(
            clear_workspace_runtime=lambda: workspace_clear_calls.append("cleared")
        ),
    )

    with pytest.raises(RuntimeError, match="Failed to clear session context"):
        _clear_active_conversation_runtime(session)

    # Keep the active id until every part of the transition can be cleared;
    # the caller can surface the failure and retry instead of using stale
    # context under a blank conversation.
    assert session.active_conversation_id == "conversation-1"
    assert workspace_clear_calls == []


def test_active_conversation_cleanup_clears_context_before_workspace_runtime() -> None:
    calls: list[str] = []

    session = SimpleNamespace(
        active_conversation_id="conversation-1",
        context_builder=SimpleNamespace(clear=lambda: calls.append("context")),
        session_lifecycle=SimpleNamespace(
            clear_workspace_runtime=lambda: calls.append("workspace")
        ),
    )

    _clear_active_conversation_runtime(session)

    assert calls == ["context", "workspace"]
    assert session.active_conversation_id is None
