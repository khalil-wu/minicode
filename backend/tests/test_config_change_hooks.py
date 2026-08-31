import asyncio

from fastapi import Response
import pytest

from backend.api import routes_llm
from backend.api.models import LLMSettingsUpdateRequest, MCPConfigUpdateRequest
from backend.hooks.manager import HookResult
from backend.services import llm_settings_service, mcp_service


class _HookManager:
    def __init__(self, calls: list[dict[str, str]]) -> None:
        self.calls = calls

    async def run_config_change(self, **kwargs) -> None:
        self.calls.append(dict(kwargs))


def test_update_llm_settings_api_runs_config_change_hook(monkeypatch, tmp_path) -> None:
    import backend.config as config_mod

    settings_file = tmp_path / "settings.json"
    calls: list[dict[str, str]] = []

    monkeypatch.setattr(config_mod, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(routes_llm, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr("backend.hooks.get_hook_manager", lambda: _HookManager(calls))

    request = LLMSettingsUpdateRequest(
        provider="openai",
        confirm_sensitive_change=True,
    )

    asyncio.run(routes_llm.update_llm_settings_api(request))

    assert calls == [{"source": "llm", "file_path": str(settings_file)}]


def test_update_mcp_config_api_runs_config_change_hook(monkeypatch, tmp_path) -> None:
    config_file = tmp_path / ".mcp.json"
    calls: list[dict[str, str]] = []

    monkeypatch.setattr(routes_llm, "MCP_CONFIG_FILE", config_file)
    monkeypatch.setattr("backend.hooks.get_hook_manager", lambda: _HookManager(calls))

    request = MCPConfigUpdateRequest(
        content='{"servers": {}}',
        reload=False,
        confirm_sensitive_change=True,
    )

    asyncio.run(routes_llm.update_mcp_config_api(request, Response()))

    assert calls == [{"source": "mcp", "file_path": str(config_file)}]


def test_llm_settings_config_change_veto_prevents_secret_mutation(monkeypatch, tmp_path) -> None:
    request = LLMSettingsUpdateRequest(
        provider="openai",
        confirm_sensitive_change=True,
    )
    saved = []

    monkeypatch.setattr(
        llm_settings_service,
        "save_llm_settings",
        lambda payload: saved.append(payload) or {"saved": True},
    )

    async def blocked_hook(**_kwargs):
        return HookResult(blocked=True, message="managed user settings veto")

    with pytest.raises(llm_settings_service.LLMSettingsServiceError, match="managed user settings veto"):
        asyncio.run(
            llm_settings_service.update_llm_settings(
                request,
                settings_file=tmp_path / "settings.json",
                config_change_hook=blocked_hook,
            )
        )
    assert saved == []


def test_mcp_config_change_veto_prevents_write_and_reload(monkeypatch, tmp_path) -> None:
    writes: list[str] = []

    def write_config(content: str, _path) -> dict[str, object]:
        writes.append(content)
        return {"saved": True}

    monkeypatch.setattr(mcp_service.config_file_mod, "write_mcp_config", write_config)

    class _Manager:
        async def reload_config(self) -> None:
            raise AssertionError("reload must not happen after a veto")

        def get_all_status(self):
            return []

    async def blocked_hook(**_kwargs):
        return HookResult(blocked=True, message="MCP policy denied")

    with pytest.raises(mcp_service.MCPServiceError, match="MCP policy denied"):
        asyncio.run(
            mcp_service._write_config_data(
                {"servers": {}},
                config_change_hook=blocked_hook,
            )
        )
    assert writes == []
