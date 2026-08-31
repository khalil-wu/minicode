"""session_start hook fires once per session (first agent turn only)."""

import asyncio

from backend.hooks.manager import HookManager, HookResult


def test_run_session_start_once_fires_once_per_session():
    mgr = HookManager()
    calls: list[str] = []

    async def fake_run(_session_id: str) -> HookResult:
        calls.append("fired")
        return HookResult()

    mgr.run_session_start = fake_run  # type: ignore[method-assign]

    # First turn of session s1 → fires.
    asyncio.run(mgr.run_session_start_once("s1"))
    # Second turn of s1 → no-op (already fired).
    asyncio.run(mgr.run_session_start_once("s1"))
    # First turn of a different session s2 → fires.
    asyncio.run(mgr.run_session_start_once("s2"))

    assert calls == ["fired", "fired"]


def test_run_session_start_once_noop_without_session_id():
    mgr = HookManager()

    async def fake_run(_session_id: str) -> HookResult:
        raise AssertionError("should not fire without a session id")

    mgr.run_session_start = fake_run  # type: ignore[method-assign]
    result = asyncio.run(mgr.run_session_start_once(""))
    assert result.feedback == ""
