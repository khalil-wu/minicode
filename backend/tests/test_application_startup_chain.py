import asyncio
from pathlib import Path
import subprocess
import sys

import pytest

from backend import main
from backend.agent import runtime
from backend.permissions import profiles
from backend.sandbox.policy import SandboxEnforcement


def test_desktop_backend_imports_in_fresh_process():
    result = subprocess.run(
        [sys.executable, "-c", "from backend.main import app; assert app is not None"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_error", [RuntimeError("startup failed"), asyncio.CancelledError()])
async def test_interrupted_startup_closes_allocated_owners(tmp_path, monkeypatch, startup_error):
    events = []

    class Bootstrap:
        def __init__(self, **kwargs):
            pass

        async def startup(self):
            events.append("allocated")
            raise startup_error

        async def shutdown(self):
            events.append("services_closed")

    class Manager:
        async def shutdown(self, **kwargs):
            events.append("sessions_closed")

    class Runtime:
        def close(self, *, release_lease):
            assert release_lease
            events.append("lease_released")

    monkeypatch.setattr(main, "AppBootstrap", Bootstrap)
    monkeypatch.setattr(main._state, "ws_manager", Manager())
    monkeypatch.setattr(runtime, "default_runtime_if_initialized", lambda: Runtime())
    with pytest.raises(type(startup_error)):
        async with main.lifespan(main.app):
            raise AssertionError("Failed startup must not enter serving scope")
    assert events == ["allocated", "sessions_closed", "services_closed", "lease_released"]
    assert main._state.bootstrap is None


@pytest.mark.asyncio
async def test_session_shutdown_error_still_closes_services_and_releases_lease(monkeypatch):
    events = []

    class Bootstrap:
        def __init__(self, **kwargs):
            pass

        async def startup(self):
            pass

        async def shutdown(self):
            events.append("services_closed")

    class Manager:
        async def shutdown(self, **kwargs):
            raise RuntimeError("session teardown failed")

    class Runtime:
        def close(self, **kwargs):
            events.append("lease_released")

    monkeypatch.setattr(main, "AppBootstrap", Bootstrap)
    monkeypatch.setattr(main._state, "ws_manager", Manager())
    monkeypatch.setattr(runtime, "default_runtime_if_initialized", lambda: Runtime())
    with pytest.raises(RuntimeError, match="session teardown failed"):
        async with main.lifespan(main.app):
            pass
    assert events == ["services_closed", "lease_released"]
    assert main._state.bootstrap is None


def test_native_startup_probe_uses_actual_product_launch_policy(monkeypatch):
    from backend.sandbox import runner

    class Runner:
        def __init__(self, policy):
            self.policy = policy

        def capability(self):
            resolved = self.policy.resolve()
            assert resolved.enforcement is SandboxEnforcement.MANAGED
            assert resolved.root_read_baseline
            from types import SimpleNamespace

            return SimpleNamespace(available=True, filesystem_isolated=True)

    monkeypatch.setattr(runner, "SandboxRunner", Runner)
    assert profiles.refresh_native_os_sandbox()
