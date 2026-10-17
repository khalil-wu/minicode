from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.ws.command_scope import CommandScope
from backend.ws.handlers import diff


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [None, False, "false", "true", 0, 1, {}, {"accepted": False}, True])
async def test_git_revert_requires_the_actual_confirmed_boolean(monkeypatch, tmp_path, confirmed):
    monkeypatch.setattr(diff, "resolve_command_scope", lambda *args, **kwargs: CommandScope("owner", str(tmp_path), "revert"))
    snapshot = object()
    monkeypatch.setattr("backend.diff.review.current_snapshot", lambda *args: snapshot)
    revert = AsyncMock(return_value={"ok": True, "status": "success", "applied_targets": ["unstaged"]})
    monkeypatch.setattr("backend.diff.review.apply_action", revert)
    session = SimpleNamespace(validate_git_relative_path=lambda path: path,
        send_event=AsyncMock(), emit_command_result=AsyncMock())
    assert await diff.handle_diff_git_revert_file(session, {"path": "fixture.txt", "confirmed": confirmed,
        "source": "unstaged", "snapshot_generation": "displayed-generation"})
    if confirmed is True:
        revert.assert_awaited_once_with(session, "owner", snapshot, str(tmp_path), None, action="revert", paths=["fixture.txt"])
        session.emit_command_result.assert_awaited_once()
    else:
        revert.assert_not_awaited()
        event = session.send_event.await_args.args[0]
        assert event.type == "command.result" and event.data["level"] == "error"
        session.emit_command_result.assert_not_awaited()
