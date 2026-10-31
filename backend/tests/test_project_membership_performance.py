from __future__ import annotations

from pathlib import Path

from backend.conversations import repository as repository_module
from backend.conversations.repository import ConversationRepository


def test_project_membership_reuses_path_identity_and_observes_external_updates(tmp_path, monkeypatch):
    project = tmp_path / "project"
    source = tmp_path / "source"
    other = tmp_path / "other"
    for path in (project, source, other):
        path.mkdir()
    repo = ConversationRepository(tmp_path / "conversations")
    records = [repo.create_conversation(title=f"Chat {index}", workspace_root=str(source)) for index in range(18)]
    for record in records[:6]:
        repo.set_conversation_project(record.id, str(project))
    for record in records[6:12]:
        repo._mutate_meta(record.id, lambda item: setattr(item, "project_id", None))
    for record in records[12:]:
        repo.set_conversation_project(record.id, "")
    repo.set_archived(records[0].id, True)
    roots = (str(source),)
    expected = [item.id for item in repo.list_conversations() if repo._belongs_to_project(item, str(project), roots)]
    original = repository_module.canonical_file_path_key
    calls: list[str] = []

    def counted(path: str | Path) -> str:
        calls.append(str(path))
        return original(path)

    monkeypatch.setattr(repository_module, "canonical_file_path_key", counted)
    assert repo.project_conversation_ids(str(project), roots) == expected
    assert len(calls) <= 3

    other_process = ConversationRepository(repo.storage_root)
    other_process.set_conversation_project(records[0].id, str(other))
    expected = [item.id for item in repo.list_conversations() if repo._belongs_to_project(item, str(project), roots)]
    assert records[0].id not in expected
    assert repo.project_conversation_ids(str(project), roots) == expected
