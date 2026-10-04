from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.skills.executor import SkillExecutor
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.skills.marketplace import import_local_skill


def _bundle(root: Path, name: str) -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: >-\n  Read documents --- safely:\n  preserve exact metadata.\n---\nRead scripts/run.py.\n",
        encoding="utf-8",
    )
    (root / "scripts").mkdir()
    (root / "scripts/run.py").write_text("print('skill')\n", encoding="utf-8")
    (root / "agents").mkdir()
    (root / "agents/openai.yaml").write_text(
        "interface:\n  display_name: Folded Skill\n  default_prompt: Run the workflow\n",
        encoding="utf-8",
    )
    return root


def _catalog(monkeypatch, installed: Path, workspace: Path) -> SkillManager:
    monkeypatch.setattr(
        SkillLoader,
        "_search_dirs",
        lambda loader: [
            ("user", installed),
            ("workspace", loader._project_root / ".minicode/skills"),
        ],
    )
    manager = SkillManager(SkillLoader(workspace))
    manager.discover()
    return manager


def test_local_import_publishes_complete_bundle_and_matching_catalog(tmp_path, monkeypatch):
    from backend.plugins import materializer

    source = _bundle(tmp_path / "source", "folded-skill")
    installed = tmp_path / "installed"
    final = installed / "folded-skill"
    copytree = materializer.shutil.copytree
    observed = []

    def copy_before_publish(src, dst, *args, **kwargs):
        observed.append(final.exists())
        return copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(materializer.shutil, "copytree", copy_before_publish)
    result = import_local_skill(source, installed)
    manager = _catalog(monkeypatch, installed, tmp_path / "workspace")
    entry = manager.list_all()[0]
    assert observed and not any(observed)
    assert result["installed"] is True
    assert result["skill"]["description"] == entry["description"]
    assert entry["description"] == "Read documents --- safely: preserve exact metadata."
    assert entry["display_name"] == "Folded Skill"
    assert entry["default_prompt"] == "Run the workflow"
    assert (final / "scripts/run.py").read_bytes() == (source / "scripts/run.py").read_bytes()
    assert entry["description"] in SkillExecutor(manager).build_layer1_summary()
    assert manager.load_skill_payload("folded-skill")["content"] == (source / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("content", [
    "No frontmatter", "---\nname: broken\n---\nBody",
    "---\nname: broken\ndescription: [invalid\n---\nBody",
    "---\nname: broken\ndescription: {}\n---\nBody",
])
def test_invalid_local_skill_is_not_installed(tmp_path, content):
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text(content, encoding="utf-8")
    installed = tmp_path / "installed"
    with pytest.raises(ValueError):
        import_local_skill(source, installed)
    assert not installed.exists()


@pytest.mark.asyncio
async def test_ws_install_keeps_catalog_and_notices_with_original_owner(tmp_path, monkeypatch):
    from backend.services import skills_api_service
    from backend.ws.handlers.misc import handle_skills_install

    installed = tmp_path / "installed"
    first, second = tmp_path / "first", tmp_path / "second"
    _bundle(first / ".minicode/skills/only-first", "only-first")
    _bundle(second / ".minicode/skills/only-second", "only-second")
    source = _bundle(tmp_path / "source", "folded-skill")
    manager = _catalog(monkeypatch, installed, first)
    events, payloads = [], []

    async def send_event(event):
        events.append(event)

    async def send_payload(payload, **_kwargs):
        payloads.append(payload)

    session = SimpleNamespace(
        active_conversation_id="first-owner", skill_manager=manager,
        send_event=send_event, send_payload=send_payload,
        resolve_requested_workspace=lambda _: first,
        session_lifecycle=SimpleNamespace(workspace_root=first, current_workspace_root=lambda: first),
    )

    async def install(name):
        assert name == "folded-skill"
        await asyncio.sleep(0)
        session.active_conversation_id = "second-owner"
        manager.set_project_root(second)
        return import_local_skill(source, installed)

    monkeypatch.setattr(skills_api_service, "install_marketplace_skill", install)
    assert await handle_skills_install(session, {"name": "folded-skill"})
    assert payloads[0]["conversation_id"] == "first-owner"
    assert {entry["name"] for entry in payloads[0]["skills"]} == {"only-first", "folded-skill"}
    assert events[0].data["conversation_id"] == "first-owner"
    assert events[-1].data["data"]["conversation_id"] == "first-owner"
    assert events[-1].data["data"]["installed"] is True


@pytest.mark.asyncio
async def test_ws_failed_install_keeps_error_with_original_owner(monkeypatch):
    from backend.services import skills_api_service
    from backend.ws.handlers.misc import handle_skills_install

    events = []

    async def send_event(event):
        events.append(event)

    session = SimpleNamespace(
        active_conversation_id="first-owner", skill_manager=None,
        send_event=send_event,
        resolve_requested_workspace=lambda _: None,
        session_lifecycle=SimpleNamespace(workspace_root=None, current_workspace_root=lambda: None),
    )

    async def install(_name):
        session.active_conversation_id = "second-owner"
        raise ValueError("Invalid SKILL.md")

    monkeypatch.setattr(skills_api_service, "install_marketplace_skill", install)
    assert await handle_skills_install(session, {"name": "broken"})
    assert len(events) == 1
    assert events[0].data["level"] == "error"
    assert events[0].data["data"]["conversation_id"] == "first-owner"
