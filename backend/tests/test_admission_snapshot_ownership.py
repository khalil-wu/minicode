from copy import deepcopy

import pytest

from backend.conversations import repository as repository_module
from backend.conversations.repository import ConversationRepository


def seed(repo, count=1000):
    transcript = [{"id": f"m{index}", "role": "user" if index % 2 == 0 else "assistant", "content": f"history {index}"}
                  for index in range(count)]
    return repo.create_conversation(transcript=transcript,
        context_snapshot={"history": [{"role": item["role"], "content": item["content"]} for item in transcript]})


@pytest.mark.parametrize("checkpoint", [False, True])
def test_internal_admission_owns_normalized_history_and_nested_metadata(tmp_path, monkeypatch, checkpoint):
    repo = ConversationRepository(tmp_path)
    original = seed(repo)
    if checkpoint:
        monkeypatch.setattr(repo, "_MAX_PROJECTION_ADMISSIONS", 1)
    # Loading a snapshot adds canonical protocol fields even to old plain rows.
    # The first admitted snapshot can therefore differ from its first message.
    snapshot = {"history": [{**item, "tool_calls": [], "provider_items": [], "timestamp_ms": index}
                            for index, item in enumerate(original.context_snapshot["history"])],
                "turn_admissions": {"new-user": {"history_start": 1000, "history_end": 1001}},
                "retained": {"nested": ["owned"]}}
    snapshot["history"].append({"role": "user", "content": "new input", "tool_calls": []})
    expected = deepcopy(snapshot)
    copies = []
    clone = repository_module.copy.deepcopy
    history_ids = {id(item) for item in snapshot["history"]}

    def observe(value, memo=None):
        if id(value) in history_ids:
            copies.append(id(value))
        return clone(value, memo)

    with monkeypatch.context() as scoped:
        scoped.setattr(repository_module.copy, "deepcopy", observe)
        saved = repo.commit_turn_admission(original.id,
            user_message={"id": "new-user", "role": "user", "content": "new input"},
            context_snapshot=snapshot, expected_revision=original.revision, return_record=False)
    if not checkpoint:
        assert copies == [], "The written log already owns these serialized history values"
    assert not hasattr(saved, "context_snapshot")
    snapshot["history"][0]["tool_calls"].append({"id": "caller mutation"})
    snapshot["history"][-1]["content"] = "caller replaced input"
    snapshot["retained"]["nested"].append("caller mutation")
    snapshot["turn_admissions"]["new-user"]["history_end"] = -1
    assert repo.get_conversation(original.id).context_snapshot == expected
    assert ConversationRepository(tmp_path).get_conversation(original.id).context_snapshot == expected

    next_snapshot = deepcopy(expected)
    next_snapshot["history"].append({"role": "user", "content": "second input"})
    del next_snapshot["retained"]
    second = repo.commit_turn_admission(original.id,
        user_message={"id": "second-user", "role": "user", "content": "second input"},
        context_snapshot=next_snapshot, expected_revision=saved.revision, return_record=False)
    actual = repo.get_conversation(original.id)
    assert actual.context_snapshot == next_snapshot
    assert actual.revision == second.revision
    assert ConversationRepository(tmp_path).get_conversation(original.id).context_snapshot == next_snapshot


def test_failed_internal_admission_keeps_owned_snapshot_and_can_retry(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    original = seed(repo, 4)
    before = repo.get_conversation(original.id)
    snapshot = {"history": [{"role": "user", "content": "changed prefix", "provider_items": [{"nested": [1]}]}]}
    write = repo._safe_write_text

    def fail(path, text, encoding="utf-8"):
        if path == repo._manifest_path_for(original.id):
            raise OSError("publication failed")
        return write(path, text, encoding)

    arguments = {"user_message": {"id": "input", "role": "user", "content": "changed prefix"},
                 "context_snapshot": snapshot, "expected_revision": original.revision, "return_record": False}
    with monkeypatch.context() as scoped:
        scoped.setattr(repo, "_safe_write_text", fail)
        with pytest.raises(OSError, match="publication failed"):
            repo.commit_turn_admission(original.id, **arguments)
    assert repo.get_conversation(original.id) == before
    assert ConversationRepository(tmp_path).get_conversation(original.id) == before
    saved = repo.commit_turn_admission(original.id, **arguments)
    replay = repo.commit_turn_admission(original.id, **arguments)
    assert replay.revision == saved.revision
    snapshot["history"][0]["provider_items"][0]["nested"].append(2)
    assert repo.get_conversation(original.id).context_snapshot["history"][0]["provider_items"][0]["nested"] == [1]
