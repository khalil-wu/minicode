from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import config_helpers, managed_settings
from backend.artifact.store import ArtifactStore
from backend.config import load_config
from backend.config_layers import ConfigLayerStack
from backend.config_requirements import ConfigRequirementsError
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.context import PermissionContext
from backend.sdk import query


class WriteMarkerModel(LLMAdapter):
    def __init__(self, settings):
        self._settings = settings
        self._provider_id = "openai"
        self.calls = 0

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(
                id="managed-policy-write", name="write_file",
                arguments={"file_path": "marker.txt", "content": "owned marker"},
            )])
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Finished.", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop", end_turn=True)

    async def simple_chat(self, messages, *, max_tokens=None):
        return "Owned test transport."


@pytest.fixture
def managed_query_environment(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    managed.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    monkeypatch.setenv("MINICODE_MANAGED_SETTINGS_DIR", str(managed))
    monkeypatch.setenv("MINICODE_SYSTEM_CONFIG_FILE", str(tmp_path / "absent-system.toml"))
    monkeypatch.setenv("MINICODE_REQUIREMENTS_FILE", str(tmp_path / "absent-requirements.toml"))
    monkeypatch.setattr(managed_settings, "_load_admin_platform_settings", lambda: managed_settings.ManagedSettingsResult({}))
    monkeypatch.setattr(managed_settings, "_load_windows_registry_settings", lambda _hive: managed_settings.ManagedSettingsResult({}))
    config_helpers.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config_helpers.SETTINGS_FILE.write_text(json.dumps({
        "agent": {"code_mode_only": False},
        "llm": {"provider": "openai", "openai": {
            "base_url": "https://fixture.invalid/v1", "model": "fixture", "auth_header": False,
        }},
    }), encoding="utf-8")
    return managed / "managed-settings.json", workspace


@pytest.mark.asyncio
async def test_valid_managed_deny_blocks_real_sdk_write_dispatch(managed_query_environment, tmp_path):
    source, workspace = managed_query_environment
    source.write_text('{"permissions":{"always_deny":["write_file"]}}', encoding="utf-8")
    config = load_config(cwd=workspace, resolve_credentials=False)
    model = WriteMarkerModel(config.llm)
    events = [event async for event in query(
        "Write the owned marker", llm=model, config=config,
        permission_context=PermissionContext(mode="bypass", approval_policy="never", sandbox_mode="danger-full-access", workspace_root=workspace),
        workspace_root=workspace, max_iterations=3,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
    )]
    assert model.calls > 0
    assert any(event.type == "tool_result" and event.data.get("is_error") for event in events)
    assert not (workspace / "marker.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", "  \n", '{"permissions":{"always_deny":["write_file"]}', "[]", '{"enabled_plugins":[]}'])
async def test_invalid_selected_policy_never_publishes_config_or_starts_sdk_model(managed_query_environment, tmp_path, body):
    source, workspace = managed_query_environment
    source.write_text("{}", encoding="utf-8")
    model = WriteMarkerModel(load_config(cwd=workspace, resolve_credentials=False).llm)
    source.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigRequirementsError, match="Failed to load managed policy") as error:
        async for _event in query(
            "Write the owned marker", llm=model, workspace_root=workspace,
            permission_context=PermissionContext(mode="bypass", approval_policy="never", sandbox_mode="danger-full-access", workspace_root=workspace),
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        ):
            pass
    assert str(source) in str(error.value)
    assert model.calls == 0
    assert not (workspace / "marker.txt").exists()


def test_config_publication_rejects_selected_policy_read_errors():
    with pytest.raises(ConfigRequirementsError, match="fixture: read denied"):
        ConfigLayerStack((), managed_policy_errors=("fixture: read denied",))
