"""Ordinary registrations must not invent Source owner provisioning metadata."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.environments import EnvironmentManager, ExecutorClosed

PEER = Path(__file__).with_name("_environments_stdio_peer.py")


@pytest.mark.asyncio
async def test_ordinary_registration_roots_do_not_come_from_executor_info(tmp_path):
    manager = EnvironmentManager(tmp_path, environ={"CODEX_EXEC_SERVER_URL": "none"})
    environment = manager.upsert({"id": "owned", "program": sys.executable, "args": [str(PEER)]})
    assert environment.registration_kind == "ordinary"
    assert environment.last_ready_info() is None
    assert environment.selected_capability_roots() == ()
    assert environment.client.connection is None
    assert await environment.status() == {"status": "pending"}
    await environment.wait_until_ready()
    assert environment.client.connection.connected
    environment.client.environment_info = {"selectedCapabilityRoots": [{"id": "untrusted-peer-metadata"}]}
    assert environment.last_ready_info() is None
    assert environment.selected_capability_roots() == ()
    await manager.close()


@pytest.mark.asyncio
async def test_wait_until_ready_retains_actual_launcher_error(tmp_path):
    manager = EnvironmentManager(tmp_path, environ={"CODEX_EXEC_SERVER_URL": "none"})
    environment = manager.upsert({"id": "missing", "program": "minicode-registration-missing-peer"})
    with pytest.raises(FileNotFoundError):
        await environment.wait_until_ready()
    assert (await environment.status())["status"] == "disconnected"
    assert environment.selected_capability_roots() == ()
    await manager.close()


@pytest.mark.asyncio
async def test_local_registration_has_no_owner_report_and_wait_does_not_query_machine(tmp_path):
    local = SimpleNamespace(info=AsyncMock(), close=AsyncMock(), filesystem=object(), process=object())
    manager = EnvironmentManager(tmp_path, local_backend=local, environ={})
    environment = manager.get_environment("local")
    await environment.wait_until_ready()
    assert environment.registration_kind == "ordinary"
    assert environment.last_ready_info() is None
    assert environment.selected_capability_roots() == ()
    local.info.assert_not_awaited()
    await manager.close()
    with pytest.raises(ExecutorClosed):
        await environment.wait_until_ready()


@pytest.mark.asyncio
async def test_borrowed_registration_waits_on_original_transport_after_upsert(tmp_path):
    manager = EnvironmentManager(tmp_path, environ={"CODEX_EXEC_SERVER_URL": "none"})
    original = manager.upsert({"id": "owned", "program": sys.executable, "args": [str(PEER)]})
    lease = manager.borrow("owned")
    replacement = manager.upsert({"id": "owned", "program": "minicode-registration-new-missing-peer"})
    await lease.environment.wait_until_ready()
    assert lease.environment is original
    assert original.client.connection.connected
    assert replacement.client.connection is None
    assert original.selected_capability_roots() == ()
    await lease.aclose()
    assert original.closed
    await manager.close()
