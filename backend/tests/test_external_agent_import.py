from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from backend.services.external_agent_import import ExternalAgentImportService
from backend.services.external_agent_import_sources import read_json


def write(path: Path, content: str | dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content) if isinstance(content, dict) else content, encoding="utf-8")
    return path


def transcript(path: Path, cwd: Path, *, provider="claude", age=0, text="A real source question") -> Path:
    stamp = (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()
    key = "type" if provider == "claude" else "role"
    records = [{key: "user", "cwd": str(cwd), "timestamp": stamp, "message": {"content": text}},
               {key: "assistant", "timestamp": stamp, "message": {"content": [{"type": "text", "text": "Source answer"}, {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}]}},
               {key: "user", "timestamp": stamp, "message": {"content": [{"type": "tool_result", "is_error": True, "content": "actual source error"}]}}]
    write(path, "\n".join(json.dumps(record) for record in records))
    modified = (datetime.now(timezone.utc) - timedelta(days=age)).timestamp()
    os.utime(path, (modified, modified))
    return path


@pytest.fixture
def migration(tmp_path, monkeypatch):
    home = tmp_path / "source-home"
    home.mkdir()
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    service = ExternalAgentImportService(source_home=home, config_home=tmp_path / "target-home" / ".minicode",
        settings_file=tmp_path / "target-state" / "settings.json", state_dir=tmp_path / "target-state" / "data", memory_dir=tmp_path / "target-state" / "data" / "memory")
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(service.config_home))
    from backend.config_layers import load_config_layers_state
    def stack(**kwargs):
        return load_config_layers_state(state_root=service.settings_file.parent, user_config_file=service.settings_file,
            cwd=kwargs.get("cwd"), system_config_path=tmp_path / "system.toml", requirements_path=tmp_path / "requirements.toml", trust_resolver=lambda root: True)
    monkeypatch.setattr("backend.config.load_config_layer_stack", stack)
    monkeypatch.setattr("backend.services.plugin_settings_service._load_settings_json", lambda: read_json(service.settings_file))
    monkeypatch.setattr("backend.commands.plugins.default_plugin_roots", lambda: [service.state_dir.parent / "extensions" / "plugins" / "store"])
    return service, home, project, stack


@pytest.mark.asyncio
async def test_import_progress_keeps_real_selected_items_partial_results_and_terminal_identity(migration, monkeypatch):
    service, home, project, _stack = migration
    write(home / ".claude" / "CLAUDE.md", "Keep the real conventions.")
    write(home / ".claude" / "skills" / "progress-skill" / "SKILL.md", "---\nname: progress-skill\ndescription: Actual import progress\n---\nRead actual files.")
    detected = service.detect(workspace_roots=[str(project)], provider_ids=["claude"])
    selected = [item for item in detected["items"] if item["item_type"] in {"instructions", "skills"}]
    entered, release = threading.Event(), threading.Event()
    original = service._import_item

    def import_item(item, previous):
        if item.item_type == "skills":
            entered.set()
            assert release.wait(5)
        return original(item, previous)

    monkeypatch.setattr(service, "_import_item", import_item)
    task = asyncio.create_task(service.import_items(detected["detection_id"], [item["id"] for item in selected]))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        wire = json.loads(json.dumps(service.state()))
        progress = wire["import_progress"]
        assert progress["status"] == "running"
        assert progress["items"] == selected
        assert progress["detection_id"] == detected["detection_id"]
        assert progress["completed"] == 1 and progress["total"] == 2
        assert [item["status"] for item in progress["results"]] == ["success"]
        assert wire["running_import"]["id"] == progress["id"]
    finally:
        release.set()
        outcome = await task
    finished = service.state()
    assert finished["running_import"] is None
    assert finished["import_progress"]["status"] == "success"
    assert finished["import_progress"]["id"] == outcome["history"]["id"] == progress["id"]
    assert finished["import_progress"]["results"] == outcome["results"]
    await service.record_refresh_errors(progress["id"], ["Actual catalog refresh error"])
    assert service.state()["import_progress"]["status"] == "error"
    assert service.state()["import_progress"]["setup_errors"] == ["Actual catalog refresh error"]
    assert service.state()["history"][0]["setup_errors"] == ["Actual catalog refresh error"]


@pytest.mark.asyncio
async def test_import_ledger_error_retains_actual_copy_receipt_and_rethrows_original_error(migration, monkeypatch):
    service, home, _project, _stack = migration
    write(home / ".claude" / "CLAUDE.md", "The actual copy completes before ledger persistence.")
    detected = service.detect(provider_ids=["claude"])
    selected = next(item for item in detected["items"] if item["item_type"] == "instructions")

    def reject_ledger(_path, _value):
        raise OSError("Actual import ledger write denied")

    monkeypatch.setattr("backend.services.external_agent_import._write_json", reject_ledger)
    with pytest.raises(OSError, match="Actual import ledger write denied"):
        await service.import_items(detected["detection_id"], [selected["id"]])
    state = service.state()
    assert state["running_import"] is None
    assert state["import_progress"]["status"] == "error"
    assert state["import_progress"]["error"] == "Actual import ledger write denied"
    assert state["import_progress"]["results"][0]["status"] == "success"
    assert Path(selected["target_path"]).read_text() == "The actual copy completes before ledger persistence."
    assert state["history"] == []


@pytest.mark.asyncio
async def test_detect_select_import_reaches_native_consumers_without_modifying_sources(migration):
    service, home, project, stack = migration
    claude = home / ".claude"
    write(claude / "settings.json", {"env": {"IMPORT_FIXTURE": "fixture-secret-do-not-return"}, "sandbox": {"enabled": True},
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "python .claude/hooks/never-run.py"}]}]}})
    write(claude / "CLAUDE.md", "Use the actual project conventions.")
    write(claude / "hooks" / "never-run.py", f"from pathlib import Path\nPath({str(home / 'executed')!r}).write_text('ran')\n")
    write(claude / "skills" / "source-skill" / "SKILL.md", "---\nname: source-skill\ndescription: Local fixture skill\n---\nUse scripts/helper.py for the fixture.")
    write(claude / "skills" / "source-skill" / "scripts" / "helper.py", "print('fixture helper')")
    write(claude / "agents" / "reviewer.md", "---\nname: reviewer\ndescription: Review actual code\nmodel: source-vendor-model\ntools: Read, Grep\neffort: max\npermissionMode: readOnly\n---\nInspect actual files.")
    write(claude / "commands" / "review.md", "---\ndescription: Inspect the actual change\n---\nReview $ARGUMENTS")
    write(home / ".claude.json", {"mcpServers": {"fixture-server": {"command": "fixture-command", "args": ["--fixture"]}, "off": {"command": "never", "enabled": False}}})
    session = transcript(claude / "projects" / "source-project" / "chat.jsonl", project)
    write(claude / "projects" / "source-project" / "memory" / "MEMORY.md", "# Source memory\nA project-specific fact.")
    original = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    detected = service.detect(workspace_roots=[str(project)], provider_ids=["claude"])
    assert "fixture-secret-do-not-return" not in json.dumps(detected)
    assert {item["item_type"] for item in detected["items"]} >= {"settings", "instructions", "skills", "mcp", "agents", "hooks", "commands", "sessions", "memory"}
    assert all(item["title"] != "off" for item in detected["items"])
    result = await service.import_items(detected["detection_id"], [item["id"] for item in detected["items"] if item["supported"]])
    assert all(item["status"] == "success" for item in result["results"]), result
    assert "fixture-secret-do-not-return" not in json.dumps(result)
    assert "fixture-secret-do-not-return" not in json.dumps(service.state())
    assert all(path.read_bytes() == original[path] for path in original)
    assert not (home / "executed").exists()
    assert (service.config_home / "skills" / "source-skill" / "scripts" / "helper.py").is_file()
    assert (service.config_home / "INSTRUCTIONS.md").read_text() == "Use the actual project conventions."
    from backend.agent.instruction_discovery import load_project_guidelines
    assert "Use the actual project conventions." in load_project_guidelines(project)

    from backend.agents.loader import discover_agents
    agent = discover_agents(project)["reviewer"]
    assert agent.effort == "xhigh" and agent.permission_mode == "plan"
    assert not agent.model and not agent.tools
    from backend.commands.catalog import get_file_command_catalog
    assert any(command["command"] == "review" and "Review $ARGUMENTS" in command["template"] for command in get_file_command_catalog(project, resolve_active_workspace=False))
    from backend.mcp.config_file import read_mcp_config
    assert read_mcp_config(service.state_dir / ".mcp.json")["servers"][0]["name"] == "fixture-server"
    from backend.hooks.discovery import discover_hook_snapshot
    hooks = discover_hook_snapshot(config_stack=stack(cwd=project), workspace_root=project, workspace_trusted=True, plugin_sources=[]).entries
    assert hooks and all(not hook.enabled and not hook.executable for hook in hooks)
    assert (service.config_home / "hooks" / "never-run.py").is_file()

    from backend.conversations.repository import ConversationRepository
    imported = next(item for item in result["results"] if item["item_type"] == "sessions")
    conversation = ConversationRepository(service.state_dir / "conversations").get_conversation(imported["conversation_id"])
    assert conversation.permission_mode == "confirm"
    assert conversation.transcript[-1]["role"] == "assistant"
    assert "[external_agent_tool_result: error]" in conversation.transcript[-1]["content"]
    assert conversation.workspace_root == str(project)
    from backend.workspace.recent_projects import RecentProjectStore
    assert RecentProjectStore(service.state_dir / "recent_projects.json").project_for_root(str(project)).path == str(project)
    memory = next(item for item in result["results"] if item["item_type"] == "memory")
    resource = Path(memory["target_path"])
    assert resource.read_text().endswith("A project-specific fact.")
    assert read_json(resource.parent / "scope.json")["cwd"] == str(project)
    assert session.read_bytes() == original[session]


@pytest.mark.asyncio
async def test_sync_updates_owned_content_preserves_local_edits_and_obeys_pause(migration):
    service, home, project, _ = migration
    source = write(home / ".claude" / "CLAUDE.md", "Source v1")
    settings = write(home / ".claude" / "settings.json", {"env": {"IMPORTED": "v1", "KEEP": "source"}})
    write(service.settings_file, {"shell_environment_policy": {"set": {"KEEP": "user setting"}}})
    detected = service.detect(provider_ids=["claude"])
    await service.import_items(detected["detection_id"], [item["id"] for item in detected["items"] if item["supported"]])
    await service.set_sync(enabled=True, item_types=["instructions", "settings"])
    source.write_text("Source v2")
    write(settings, {"env": {"IMPORTED": "v2", "KEEP": "source"}})
    synced = await service.sync_connected()
    assert synced and all(result["status"] == "success" for result in synced[0]["results"])
    assert (service.config_home / "INSTRUCTIONS.md").read_text() == "Source v2"
    saved = read_json(service.settings_file)["shell_environment_policy"]["set"]
    assert saved == {"IMPORTED": "v2", "KEEP": "user setting"}
    (service.config_home / "INSTRUCTIONS.md").write_text("User edited target")
    source.write_text("Source v3")
    synced = await service.sync_connected()
    assert synced[0]["results"][0]["status"] == "skipped"
    assert (service.config_home / "INSTRUCTIONS.md").read_text() == "User edited target"
    assert await service.sync_connected() == []
    await service.set_sync(enabled=False, item_types=["instructions"])
    source.write_text("Source v4")
    assert await service.sync_connected() == []


@pytest.mark.asyncio
async def test_cursor_recent_history_and_project_categories_use_the_real_formats(migration):
    service, home, project, _ = migration
    write(project / ".cursorrules", "Project cursor instructions")
    write(project / ".cursor" / "cli.json", {"env": {"CURSOR_FIXTURE": "one"}})
    write(project / ".cursor" / "hooks.json", {"hooks": {"beforeSubmitPrompt": [{"command": "echo source"}], "unsupportedCustomEvent": [{"command": "never"}]}})
    encoded = str(project).replace("\\", "-").replace("/", "-").replace(":", "-").strip("-")
    base = home / ".cursor" / "projects" / encoded / "agent-transcripts"
    transcript(base / "new" / "new.jsonl", project, provider="cursor", text="<user_query>Current question</user_query>")
    transcript(base / "old.jsonl", project, provider="cursor", age=31)
    transcript(base / "subagents" / "child.jsonl", project, provider="cursor")
    write(base / "bad.jsonl", "this is not a supported transcript")
    detected = service.detect(provider_ids=["cursor"], workspace_roots=[str(project)], max_sessions=2)
    sessions = [item for item in detected["items"] if item["item_type"] == "sessions"]
    assert len(sessions) == 1 and sessions[0]["title"] == "Current question"
    assert any(issue["status"] == "unsupported" and issue["source_path"].endswith("bad.jsonl") for issue in detected["issues"])
    unsupported = [item for item in detected["items"] if not item["supported"]]
    assert unsupported and unsupported[0]["item_type"] == "hooks"
    result = await service.import_items(detected["detection_id"], [item["id"] for item in detected["items"]])
    assert any(item["status"] == "unsupported" for item in result["results"])
    assert (project / ".minicode" / "INSTRUCTIONS.md").read_text() == "Project cursor instructions"
    raw_config = (project / ".minicode" / "config.toml").read_text()
    assert 'CURSOR_FIXTURE' in raw_config
    write(project / ".cursor" / "cli.json", {"env": {"CURSOR_FIXTURE": "two"}})
    await service.set_sync(enabled=True, item_types=["settings"])
    await service.sync_connected()
    assert '"CURSOR_FIXTURE" = "two"' in (project / ".minicode" / "config.toml").read_text()


@pytest.mark.asyncio
async def test_cursor_empty_window_keeps_execution_scope_without_inventing_project_membership(migration):
    service, home, _, _ = migration
    path = home / ".cursor" / "projects" / "empty-window" / "agent-transcripts" / "chat.jsonl"
    write(path, json.dumps({"role": "user", "message": {"content": "Projectless question"}}))
    detected = service.detect(provider_ids=["cursor"])
    item = next(item for item in detected["items"] if item["item_type"] == "sessions")
    result = await service.import_items(detected["detection_id"], [item["id"]])
    from backend.conversations.repository import ConversationRepository
    imported = result["results"][0]
    conversation = ConversationRepository(service.state_dir / "conversations").get_conversation(imported["conversation_id"])
    assert conversation.workspace_root == "" and conversation.execution_cwd == str(home)
    assert imported["project_roots"] == []
    assert not (service.state_dir / "recent_projects.json").exists()


@pytest.mark.asyncio
async def test_projects_and_chats_are_independent_choices_with_real_scoped_consumers(migration):
    service, home, project, _ = migration
    source_config = write(project / ".claude" / "settings.json", {"env": {"PROJECT_IMPORT": "project-value"}})
    source_chat = transcript(home / ".claude" / "projects" / "project-a" / "chat.jsonl", project)
    original = {source_config: source_config.read_bytes(), source_chat: source_chat.read_bytes()}
    detected = service.detect(provider_ids=["claude"])
    chat = next(item for item in detected["items"] if item["item_type"] == "sessions")
    assert any(item["item_type"] == "projects" and item["workspace_root"] == str(project) for item in detected["items"])
    chat_result = await service.import_items(detected["detection_id"], [chat["id"]])
    assert chat_result["results"][0]["status"] == "success"
    assert chat_result["results"][0]["project_roots"] == []
    assert not (service.state_dir / "recent_projects.json").exists()
    assert not (project / ".minicode" / "config.toml").exists()

    pending = service.detect(provider_ids=["claude"])
    assert not any(item["item_type"] == "sessions" for item in pending["items"])
    selected = [item["id"] for item in pending["items"] if item["workspace_root"] == str(project)]
    result = await service.import_items(pending["detection_id"], selected)
    assert {item["item_type"] for item in result["results"]} == {"projects", "settings"}
    assert all(item["status"] == "success" for item in result["results"])
    from backend.workspace.recent_projects import RecentProjectStore
    assert RecentProjectStore(service.state_dir / "recent_projects.json").project_for_root(str(project)).path == str(project)
    assert '"PROJECT_IMPORT" = "project-value"' in (project / ".minicode" / "config.toml").read_text()
    assert all(path.read_bytes() == content for path, content in original.items())


@pytest.mark.asyncio
async def test_project_only_import_uses_source_metadata_without_creating_chats_or_renaming_user_projects(migration):
    service, home, project, _ = migration
    metadata = write(home / ".claude.json", {"projects": {str(project): {}}})
    detected = service.detect(provider_ids=["claude"])
    item = next(item for item in detected["items"] if item["item_type"] == "projects")
    from backend.workspace.recent_projects import RecentProjectStore
    store = RecentProjectStore(service.state_dir / "recent_projects.json")
    store.create("Existing project title", [str(project)], {"color": "blue"})
    existing = (service.state_dir / "recent_projects.json").read_bytes()
    result = await service.import_items(detected["detection_id"], [item["id"]])
    assert result["results"][0]["status"] == "skipped"
    assert (service.state_dir / "recent_projects.json").read_bytes() == existing
    assert store.project_for_root(str(project)).name == "Existing project title"
    assert not (service.state_dir / "conversations").exists()
    assert json.loads(metadata.read_text())["projects"] == {str(project): {}}
    assert not any(item["item_type"] == "projects" for item in service.detect(provider_ids=["claude"])["items"])


@pytest.mark.asyncio
async def test_project_selection_retains_actual_unavailable_folder_errors(migration):
    service, home, project, _ = migration
    removed = project.parent / "removed-source-project"
    removed.mkdir()
    write(home / ".claude.json", {"projects": {str(removed): {}}})
    detected = service.detect(provider_ids=["claude"])
    item = next(item for item in detected["items"] if item["item_type"] == "projects")
    removed.rmdir()
    result = await service.import_items(detected["detection_id"], [item["id"]])
    assert result["results"][0]["status"] == "error"
    assert "no longer available" in result["results"][0]["message"]
    assert service.state()["history"][0]["results"][0]["status"] == "error"
    assert not (service.state_dir / "recent_projects.json").exists()


@pytest.mark.asyncio
async def test_multi_provider_project_selection_imports_only_the_chosen_detected_scope(migration):
    service, home, project, _ = migration
    other = project.parent / "cursor-project"
    other.mkdir()
    write(home / ".claude.json", {"projects": {str(project): {}}})
    write(project / ".claude" / "settings.json", {"env": {"CLAUDE_ONLY": "one"}})
    write(other / ".cursor" / "cli.json", {"env": {"CURSOR_ONLY": "two"}})
    transcript(home / ".cursor" / "projects" / "cursor-project" / "agent-transcripts" / "chat.jsonl", other, provider="cursor")
    detected = service.detect(provider_ids=["claude", "cursor"])
    selected = [item["id"] for item in detected["items"] if item["provider_id"] == "cursor" and item["item_type"] != "sessions"]
    result = await service.import_items(detected["detection_id"], selected)
    assert {item["item_type"] for item in result["results"]} == {"projects", "settings"}
    assert all(item["status"] == "success" for item in result["results"])
    from backend.workspace.recent_projects import RecentProjectStore
    store = RecentProjectStore(service.state_dir / "recent_projects.json")
    assert store.project_for_root(str(other)).path == str(other)
    assert store.project_for_root(str(project)) is None
    assert not (project / ".minicode").exists()
    assert not (service.state_dir / "conversations").exists()
    assert '"CURSOR_ONLY" = "two"' in (other / ".minicode" / "config.toml").read_text()


@pytest.mark.asyncio
async def test_compatible_local_plugin_uses_existing_store_and_keeps_source_enabled(migration):
    service, home, project, stack = migration
    plugin = home / ".claude" / "plugins" / "cache" / "fixture-market" / "sample" / "1.0.0"
    write(plugin / ".claude-plugin" / "plugin.json", {"name": "sample", "description": "A local fixture plugin", "version": "1.0.0", "skills": "skills", "hooks": "hooks/hooks.json"})
    write(plugin / "skills" / "plugin-skill" / "SKILL.md", "---\nname: plugin-skill\ndescription: Fixture plugin skill\n---\nFixture content")
    write(plugin / "hooks" / "hooks.json", {"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": "echo plugin-source"}]}]}})
    write(home / ".claude" / "plugins" / "installed_plugins.json", {"plugins": {"sample@fixture-market": [{"installPath": str(plugin)}]}})
    write(home / ".claude" / "settings.json", {"enabledPlugins": {"sample@fixture-market": True}})
    detected = service.detect(provider_ids=["claude"])
    item = next(item for item in detected["items"] if item["item_type"] == "plugins")
    assert item["supported"]
    result = await service.import_items(detected["detection_id"], [item["id"]])
    imported = result["results"][0]
    assert imported["status"] == "success", imported
    assert (plugin / ".claude-plugin" / "plugin.json").is_file()
    assert not (plugin / ".minicode-plugin").exists()
    from backend.services.plugin_settings_service import get_plugin_settings, load_enabled_plugin_hook_sources
    store = service.state_dir.parent / "extensions" / "plugins" / "store"
    inventory = get_plugin_settings([store], config_stack=stack(cwd=project))
    assert next(plugin for plugin in inventory["plugins"] if plugin["name"] == "sample")["enabled"] is True
    from backend.hooks.discovery import discover_hook_snapshot
    hooks = discover_hook_snapshot(config_stack=stack(cwd=project), workspace_root=project, workspace_trusted=True,
        plugin_sources=load_enabled_plugin_hook_sources(config_stack=stack(cwd=project))).entries
    assert any(hook.plugin_id == "sample@external-claude" and not hook.executable for hook in hooks)


@pytest.mark.asyncio
async def test_import_settings_and_detection_cross_the_main_runtime_authorization_gate(migration, monkeypatch):
    service, _, project, _ = migration
    from backend.api import routes_import
    from backend.main import app

    token = "isolated-import-runtime-token"
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", token)
    monkeypatch.setattr(routes_import, "_service", service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        unauthenticated = await client.get("/api/settings/import")
        assert unauthenticated.status_code == 401
        authenticated = await client.get("/api/settings/import", headers={"X-MiniCode-Token": token})
        assert authenticated.status_code == 200
        assert authenticated.headers["cache-control"] == "no-store"
        assert authenticated.json()["history"] == []
        assert {provider["id"] for provider in authenticated.json()["providers"]} == {"claude", "cursor"}
        detected = await client.post("/api/settings/import/detect", headers={"X-MiniCode-Token": token},
            json={"workspace_roots": [str(project)], "provider_ids": ["claude", "cursor"]})
        assert detected.status_code == 200 and detected.json()["items"] == []
    assert not service.ledger_path.exists()


@pytest.mark.asyncio
async def test_import_http_contract_rejects_forged_selection_and_never_exposes_settings_values(migration, monkeypatch):
    service, home, _, _ = migration
    write(home / ".claude" / "settings.json", {"env": {"SECRET": "fixture-credential"}})
    from backend.api import routes_import
    monkeypatch.setattr(routes_import, "_service", service)
    app = FastAPI()
    app.include_router(routes_import.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        detected = await client.post("/api/settings/import/detect", json={"provider_ids": ["claude"]})
        assert detected.status_code == 200 and "fixture-credential" not in detected.text
        payload = detected.json()
        forged = await client.post("/api/settings/import", json={"detection_id": payload["detection_id"], "item_ids": ["not-a-detected-id"]})
        assert forged.status_code == 400
        imported = await client.post("/api/settings/import", json={"detection_id": payload["detection_id"], "item_ids": [payload["items"][0]["id"]]})
        assert imported.status_code == 200 and imported.json()["results"][0]["status"] == "success"
        state = await client.get("/api/settings/import")
        assert "fixture-credential" not in state.text
        sync = await client.put("/api/settings/import/sync", json={"enabled": True, "item_types": ["settings"]})
        assert sync.status_code == 200 and sync.json()["sync"]["enabled"] is True


@pytest.mark.asyncio
async def test_pausing_sync_during_detection_prevents_the_later_import(migration, monkeypatch):
    service, home, _, _ = migration
    source = write(home / ".claude" / "CLAUDE.md", "source version one")
    detected = service.detect(provider_ids=["claude"])
    await service.import_items(detected["detection_id"], [detected["items"][0]["id"]])
    await service.set_sync(enabled=True, item_types=["instructions"])
    source.write_text("source version two")
    started, release = threading.Event(), threading.Event()
    original = service.detect
    def slow_detection(**kwargs):
        started.set()
        release.wait(3)
        return original(**kwargs)
    monkeypatch.setattr(service, "detect", slow_detection)
    pending = asyncio.create_task(service.sync_connected())
    assert await asyncio.to_thread(started.wait, 2)
    try:
        await service.set_sync(enabled=False, item_types=["instructions"])
    finally:
        release.set()
    assert await pending == []
    assert (service.config_home / "INSTRUCTIONS.md").read_text() == "source version one"


@pytest.mark.asyncio
async def test_import_refreshes_existing_consumers_and_retains_actual_activation_errors(migration, monkeypatch):
    service, home, _, _ = migration
    write(home / ".claude" / "settings.json", {"env": {"FIXTURE": "value"}})
    write(home / ".claude.json", {"mcpServers": {"fixture": {"command": "fixture-only"}}})
    detected = service.detect(provider_ids=["claude"])
    result = await service.import_items(detected["detection_id"], [item["id"] for item in detected["items"]])
    from backend.api import routes_import, _state
    refresh = Mock()
    reload_mcp = AsyncMock(side_effect=RuntimeError("actual fixture connection setup error"))
    monkeypatch.setattr(routes_import, "_service", service)
    monkeypatch.setattr(_state, "bootstrap", SimpleNamespace(refresh_config=refresh, reload_mcp_managers=reload_mcp, skill_manager=None))
    errors = await routes_import.refresh_import_consumers(result, publish=False)
    refresh.assert_called_once()
    reload_mcp.assert_awaited_once()
    assert errors == ["actual fixture connection setup error"]
    assert service.state()["history"][0]["setup_errors"] == errors


@pytest.mark.asyncio
async def test_project_sync_publishes_existing_workspace_catalog_without_faking_new_chat_records(migration, monkeypatch):
    service, home, project, _ = migration
    write(home / ".claude.json", {"projects": {str(project): {}}})
    detected = service.detect(provider_ids=["claude"])
    result = await service.import_items(detected["detection_id"], [item["id"] for item in detected["items"]])
    from backend.api import routes_import, _state
    from backend.ws.handlers import workspace
    send_chats = AsyncMock()
    session = SimpleNamespace(is_connected=True, conversation_repo=SimpleNamespace(storage_root=(service.state_dir / "conversations").resolve()), send_conversation_list=send_chats)
    publish_projects = AsyncMock()
    monkeypatch.setattr(routes_import, "_service", service)
    monkeypatch.setattr(_state, "bootstrap", None)
    monkeypatch.setattr(_state.ws_manager, "iter_sessions", lambda: [session])
    monkeypatch.setattr(workspace, "handle_workspace_recent", publish_projects)
    assert await routes_import.refresh_import_consumers(result, publish=True) == []
    publish_projects.assert_awaited_once_with(session, {"type": "workspace.recent"})
    send_chats.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_sync_worker_consumes_saved_preferences_and_stops_its_actual_task(migration, monkeypatch):
    service, home, _, _ = migration
    source = write(home / ".claude" / "CLAUDE.md", "version one")
    detected = service.detect(provider_ids=["claude"])
    await service.import_items(detected["detection_id"], [detected["items"][0]["id"]])
    await service.set_sync(enabled=True, item_types=["instructions"])
    source.write_text("version two")
    from backend.api import routes_import, _state
    monkeypatch.setattr(routes_import, "_service", service)
    monkeypatch.setattr(_state, "bootstrap", None)
    consumed = asyncio.Event()
    original_sync = service.sync_connected
    original_sleep = asyncio.sleep
    async def run_sync():
        result = await original_sync()
        consumed.set()
        return result
    async def next_tick(_seconds):
        await original_sleep(0)
    monkeypatch.setattr(service, "sync_connected", run_sync)
    monkeypatch.setattr(routes_import.asyncio, "sleep", next_tick)
    await routes_import.start_import_sync()
    task = routes_import._sync_task
    try:
        await asyncio.wait_for(consumed.wait(), 3)
    finally:
        await routes_import.stop_import_sync()
    assert task.done() and routes_import._sync_task is None
    assert (service.config_home / "INSTRUCTIONS.md").read_text() == "version two"
    assert source.read_text() == "version two"
    assert service.state()["history"][0]["automatic"] is True
