from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.ws.command_scope import CommandScope
from backend.ws.handlers import diff


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [None, False, "false", "true", 0, 1, {}, {"accepted": False}, True])
async def test_git_revert_requires_the_actual_confirmed_boolean(monkeypatch, tmp_path, confirmed):
    monkeypatch.setattr(diff, "resolve_command_scope", lambda *args, **kwargs: CommandScope("owner", str(tmp_path), "revert"))
    revert = AsyncMock(return_value=True)
    monkeypatch.setattr("backend.diff.git_integration.revert_file", revert)
    session = SimpleNamespace(validate_git_relative_path=lambda path: path,
        send_event=AsyncMock(), emit_command_result=AsyncMock())
    assert await diff.handle_diff_git_revert_file(session, {"path": "fixture.txt", "confirmed": confirmed})
    if confirmed is True:
        revert.assert_awaited_once_with(str(tmp_path), "fixture.txt")
        session.emit_command_result.assert_awaited_once()
    else:
        revert.assert_not_awaited()
        event = session.send_event.await_args.args[0]
        assert event.type == "command.result" and event.data["level"] == "error"
        session.emit_command_result.assert_not_awaited()
