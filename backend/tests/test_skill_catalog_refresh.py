import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.ws.handlers.misc import handle_skills_list


def test_skill_list_refreshes_session_snapshot_after_install_and_remove(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    existing = root / "existing" / "SKILL.md"
    existing.parent.mkdir()
    existing.write_text("---\nname: existing\ndescription: Existing skill\n---\nExisting.\n")
    loader = SkillLoader()
    monkeypatch.setattr(loader, "_search_dirs", lambda: [("user", root)])
    manager = SkillManager(loader)
    manager.discover()
    session = SimpleNamespace(skill_manager=manager, active_conversation_id="conversation-1", send_payload=AsyncMock(), send_event=AsyncMock())
    skill = root / "review" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("---\nname: review\ndescription: Review code\n---\nReview the change.\n")
    assert [item["name"] for item in manager.list_all()] == ["existing"]
    asyncio.run(handle_skills_list(session, {}))
    payload = session.send_payload.call_args.args[0]
    assert payload["conversation_id"] == "conversation-1"
    assert [item["name"] for item in payload["skills"]] == ["existing", "review"]
    skill.unlink()
    asyncio.run(handle_skills_list(session, {}))
    assert [item["name"] for item in session.send_payload.call_args.args[0]["skills"]] == ["existing"]
