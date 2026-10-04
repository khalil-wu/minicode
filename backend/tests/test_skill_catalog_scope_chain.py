from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from backend.api import _state
from backend.api.routes_skills import router
from backend.permissions.context import PermissionContext
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.workspace import trust
from backend.ws.handler import WebSocketSession
from backend.ws.handlers.misc import handle_skills_list
from backend.ws.session_lifecycle import SessionLifecycle


def make_skill(root: Path, name: str) -> Path:
    (root / ".git").mkdir(parents=True)
    directory = root / ".minicode" / "skills" / name
    (directory / "agents").mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Owned skill\n---\nOwned instructions", encoding="utf-8")
    (directory / "agents" / "openai.yaml").write_text("interface:\n  icon_small: ./icon.svg\n", encoding="utf-8")
    (directory / "icon.svg").write_text(f'<svg xmlns="http://www.w3.org/2000/svg"><title>{name}</title></svg>', encoding="utf-8")
    return directory / "SKILL.md"


def test_explicit_projectless_skill_snapshot_clears_parent_project_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "user-config"))
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    make_skill(root_a, "owned-a")
    make_skill(root_b, "owned-b")
    manager = SkillManager(SkillLoader(root_a))
    assert "owned-a" in {skill["name"] for skill in manager.snapshot().list_all()}
    assert "owned-a" not in {skill["name"] for skill in manager.snapshot(None).list_all()}
    switched = {skill["name"] for skill in manager.snapshot(root_b).list_all()}
    assert "owned-b" in switched and "owned-a" not in switched
    assert "owned-a" in {skill["name"] for skill in manager.snapshot().list_all()}


@pytest.mark.asyncio
async def test_skill_asset_resolves_exact_skill_in_trusted_catalog_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "user-config"))
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    path_a, path_b = make_skill(root_a, "owned-a"), make_skill(root_b, "owned-b")
    manager = SkillManager(SkillLoader(root_a))
    manager.discover()
    monkeypatch.setattr(_state, "bootstrap", SimpleNamespace(skill_manager=manager))
    ledger = tmp_path / "trusted.json"
    ledger.write_text(json.dumps({"roots": [str(root_b)]}), encoding="utf-8")
    monkeypatch.setattr(trust, "TRUSTED_WORKSPACES_FILE", ledger)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        owned = await client.get("/api/skills/asset", params={"skill_path": str(path_b), "workspace_root": str(root_b)})
        assert owned.status_code == 200 and "owned-b" in owned.text
        missing_scope = await client.get("/api/skills/asset", params={"skill_path": str(path_b)})
        assert missing_scope.status_code == 404
        untrusted = await client.get("/api/skills/asset", params={"skill_path": str(path_a), "workspace_root": str(root_a)})
        assert untrusted.status_code == 403
        foreign = await client.get("/api/skills/asset", params={"skill_path": str(path_a), "workspace_root": str(root_b)})
        assert foreign.status_code == 404


def test_runtime_capability_payload_carries_producer_owner(tmp_path):
    catalog = [{"name": "owned", "path": str(tmp_path / "SKILL.md")}]
    owner = SimpleNamespace(
        active_conversation_id="scope-owner", session_id="scope-session",
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda: tmp_path),
        runtime_capability_snapshot=lambda **kwargs: {"skills": kwargs["skill_catalog"]},
    )
    payload = WebSocketSession.runtime_capabilities_payload(owner, skill_catalog=catalog)
    assert payload["conversation_id"] == "scope-owner"
    assert payload["workspace_root"] == str(tmp_path)
    assert payload["capabilities"]["skills"] == catalog


@pytest.mark.asyncio
async def test_skill_list_captures_workspace_before_slow_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "user-config"))
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    make_skill(root_a, "owned-a")
    make_skill(root_b, "owned-b")
    started, release = threading.Event(), threading.Event()

    class HeldManager(SkillManager):
        def snapshot(self, project_root=None):
            started.set()
            release.wait(5)
            return super().snapshot(project_root)

    messages = []
    owner = SimpleNamespace(active_conversation_id="same-owner", root=root_a, skill_manager=HeldManager(SkillLoader(root_a)))
    owner.conversation_repo = SimpleNamespace(get_conversation=lambda _: SimpleNamespace(workspace_root=str(owner.root), worktree_path=""))
    owner.session_lifecycle = SimpleNamespace(workspace_root=root_a, current_workspace_root=lambda: owner.root)
    owner.resolve_requested_workspace = lambda _: owner.root

    async def send(payload, **kwargs):
        messages.append(copy.deepcopy(payload))

    owner.send_payload = send
    owner.send_event = send
    task = asyncio.create_task(handle_skills_list(owner, {"conversation_id": "same-owner", "workspace_root": str(root_a)}))
    while not started.is_set():
        await asyncio.sleep(0.001)
    owner.root = root_b
    release.set()
    await asyncio.wait_for(task, 8)
    payload = next(message for message in messages if isinstance(message, dict) and message.get("type") == "skills.list")
    assert payload["workspace_root"] == str(root_a)
    assert "owned-a" in {skill["name"] for skill in payload["skills"]}
    assert "owned-b" not in {skill["name"] for skill in payload["skills"]}


@pytest.mark.asyncio
async def test_old_sandbox_probe_never_publishes_as_new_workspace(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    owner = SimpleNamespace(session_id="scope-session", active_conversation_id="same-owner", root=root_a,
                            permission_context=PermissionContext(mode="bypass"), skill_manager=None, is_connected=False)
    lifecycle = SessionLifecycle(owner)
    lifecycle.workspace_root_for_conversation = lambda: owner.root
    messages = []

    def probe(workspace, permission):
        started.set()
        release.wait(5)
        return {"probe_workspace": str(workspace)}

    def payload(*, source, **kwargs):
        return {"workspace_root": str(owner.root), "probe": copy.deepcopy(lifecycle.sandbox_capability_payload)}

    async def send(message, **kwargs):
        messages.append(message)

    monkeypatch.setattr("backend.ws.session_lifecycle.sandbox_capability_for_context", probe)
    owner.runtime_capabilities_payload, owner.send_payload = payload, send
    first = asyncio.create_task(lifecycle.send_runtime_capabilities(source="test"))
    while not started.is_set():
        await asyncio.sleep(0.001)
    owner.root = root_b
    lifecycle.sandbox_capability_payload = None
    await lifecycle.send_runtime_capabilities(source="workspace.activate")
    release.set()
    await asyncio.wait_for(first, 8)
    await asyncio.wait_for(lifecycle.sandbox_capability_task, 8)
    assert lifecycle.sandbox_capability_payload == {"probe_workspace": str(root_b)}
    assert not any(message["workspace_root"] == str(root_b) and message["probe"] == {"probe_workspace": str(root_a)} for message in messages)
