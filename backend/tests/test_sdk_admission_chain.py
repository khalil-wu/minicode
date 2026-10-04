from contextlib import aclosing

import pytest

from backend import sdk
from backend.agent.message import AgentEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{"metadata": 123}, {"unknown_query_option": True}])
async def test_failed_sdk_admission_releases_session_for_next_request(tmp_path, monkeypatch, invalid):
    session = sdk.SDKSession(workspace_root=tmp_path)
    try:
        with pytest.raises(TypeError):
            async with aclosing(session.query("invalid", **invalid)) as stream:
                await anext(stream)

        async def accepted_query(message, **kwargs):
            yield AgentEvent.done(status="completed")

        monkeypatch.setattr(sdk, "query", accepted_query)
        async with aclosing(session.query("valid")) as stream:
            events = [event async for event in stream]
        assert len(events) == 1
        assert events[0].data["status"] == "completed"
    finally:
        await session.aclose()
