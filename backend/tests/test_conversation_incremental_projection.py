from __future__ import annotations

import copy
import json

import pytest

from backend.conversations import repository as repository_module
from backend.conversations.repository import ConversationRepository, _normalize_loaded_transcript


@pytest.mark.parametrize("operation", ["append_transcript_message", "upsert_transcript_message"])
@pytest.mark.parametrize("replace_index", [None, 12, 39])
def test_message_write_projects_only_the_changed_suffix(tmp_path, monkeypatch, operation, replace_index):
    repo = ConversationRepository(tmp_path)
    original = repo.create_conversation(transcript=[
        {"id": f"message-{index}", "role": "user", "content": f"历史消息 {index}"}
        for index in range(40)
    ])
    token = "sk-" + "testcredential" * 4
    message = {"id": f"message-{replace_index}" if replace_index is not None else "new-message",
               "role": "user", "content": f"new {token}", "private_transport": "hidden"}
    expected = copy.deepcopy(original.transcript)
    if replace_index is None:
        expected.append(message)
    else:
        expected[replace_index] = message
    expected = _normalize_loaded_transcript(expected)
    projected_ids = []
    project = repository_module.project_public_transcript

    def observe(messages):
        projected_ids.extend(item["id"] for item in messages)
        return project(messages)

    with monkeypatch.context() as scoped:
        scoped.setattr(repository_module, "project_public_transcript", observe)
        saved = getattr(repo, operation)(original.id, message)
    start = replace_index if replace_index is not None else 40
    assert projected_ids == [item["id"] for item in expected[start:]]
    assert saved.transcript == expected
    assert token not in json.dumps(saved.transcript)
    assert all("private_transport" not in item for item in saved.transcript)
    assert ConversationRepository(tmp_path).get_conversation(original.id).transcript == expected
    assert original.message_count == 40


@pytest.mark.parametrize("operation", ["append_transcript_message", "upsert_transcript_message"])
def test_new_legacy_message_ids_use_absolute_transcript_offset(tmp_path, operation):
    repo = ConversationRepository(tmp_path)
    created = repo.create_conversation(transcript=[{"role": "user", "content": "first"}])
    saved = getattr(repo, operation)(created.id, {
        "role": "assistant", "content": "stale answer",
        "blocks": [{"type": "text", "content": "actual answer", "source": "model_final",
                    "itemId": "answer", "status": "completed"}],
    })
    assert [item["id"] for item in saved.transcript] == ["legacy-message-0", "legacy-message-1"]
    assert saved.transcript[-1]["content"] == "actual answer"
    assert ConversationRepository(tmp_path).get_conversation(created.id).transcript == saved.transcript


def test_cold_legacy_prefix_and_detached_save_still_project_every_message(tmp_path):
    token = "sk-" + "testcredential" * 4
    legacy = {"id": "conv_legacy", "title": "legacy", "transcript": [
        {"id": "old", "role": "user", "content": token, "private_transport": "hidden"},
    ]}
    (tmp_path / "conv_legacy.json").write_text(json.dumps(legacy), encoding="utf-8")
    repo = ConversationRepository(tmp_path)
    saved = repo.append_transcript_message("conv_legacy", {"id": "new", "role": "user", "content": "hello"})
    assert token not in json.dumps(saved.transcript)
    saved.transcript[0]["content"] = token
    saved.transcript[0]["private_transport"] = "hidden"
    repo.save_conversation(saved)
    restored = ConversationRepository(tmp_path).get_conversation(saved.id)
    assert token not in json.dumps(restored.transcript)
    assert all("private_transport" not in item for item in restored.transcript)


def test_failed_append_keeps_previous_generation_and_cached_history(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    original = repo.create_conversation(transcript=[{"id": "old", "role": "user", "content": "kept"}])
    write = repo._safe_write_text

    def fail_manifest(path, text, encoding="utf-8"):
        if path.name.endswith(".manifest.json"):
            raise OSError("controlled manifest write failure")
        return write(path, text, encoding)

    with monkeypatch.context() as scoped:
        scoped.setattr(repo, "_safe_write_text", fail_manifest)
        with pytest.raises(OSError, match="controlled manifest"):
            repo.append_transcript_message(original.id, {"id": "lost", "role": "user", "content": "not committed"})
    assert repo.get_conversation(original.id).transcript == original.transcript
    assert ConversationRepository(tmp_path).get_conversation(original.id).transcript == original.transcript
    retried = repo.append_transcript_message(original.id, {"id": "retry", "role": "user", "content": "committed"})
    assert [item["id"] for item in retried.transcript] == ["old", "retry"]


@pytest.mark.parametrize("replace_user", [False, True])
@pytest.mark.parametrize("steer", [False, True])
def test_turn_admission_projects_from_the_first_changed_message(tmp_path, monkeypatch, replace_user, steer):
    repo = ConversationRepository(tmp_path)
    original = repo.create_conversation(transcript=[
        {"id": f"message-{index}", "role": "user" if index % 2 == 0 else "assistant", "content": f"history {index}"}
        for index in range(40)
    ])
    token = "sk-" + "testcredential" * 4
    user = {"id": "message-38" if replace_user else "new-user", "role": "user", "content": f"input {token}"}
    assistant = {"id": "message-39", "role": "assistant", "content": "before steer"} if steer else None
    snapshot = {"history": [{"role": "user", "content": "execution context"}]}
    expected = copy.deepcopy(original.transcript)
    if steer:
        expected[39] = assistant
    if replace_user:
        expected[38] = user
    else:
        expected.append(user)
    expected = _normalize_loaded_transcript(expected)
    projected_ids = []
    project = repository_module.project_public_transcript

    def observe(messages):
        projected_ids.extend(item["id"] for item in messages)
        return project(messages)

    with monkeypatch.context() as scoped:
        scoped.setattr(repository_module, "project_public_transcript", observe)
        saved = repo.commit_turn_admission(original.id, user_message=user, assistant_message=assistant,
                                           context_snapshot=snapshot, expected_revision=original.revision)
    start = 38 if replace_user else 39 if steer else 40
    assert projected_ids == [item["id"] for item in expected[start:]]
    assert saved.transcript == expected
    restored = ConversationRepository(tmp_path).get_conversation(original.id)
    assert restored.transcript == expected and restored.context_snapshot == snapshot
    replay = repo.commit_turn_admission(original.id, user_message=user, assistant_message=assistant,
                                        context_snapshot=snapshot, expected_revision=original.revision)
    assert replay.revision == saved.revision and replay.transcript == saved.transcript
    assert token not in json.dumps(restored.transcript)


@pytest.mark.parametrize("mode", ["new", "replace", "context_only", "save_context"])
def test_full_turn_and_context_checkpoints_skip_unchanged_history(tmp_path, monkeypatch, mode):
    repo = ConversationRepository(tmp_path)
    original = repo.create_conversation(transcript=[
        {"id": f"message-{index}", "role": "assistant", "content": f"history {index}"}
        for index in range(40)
    ])
    assistant = {"id": "message-35" if mode == "replace" else "new-answer", "role": "assistant", "content": "done"}
    snapshot = {"history": [{"role": "assistant", "content": "new checkpoint"}]}
    expected = copy.deepcopy(original.transcript)
    if mode == "new":
        expected.append(assistant)
    elif mode == "replace":
        expected[35] = assistant
    expected = _normalize_loaded_transcript(expected)
    projected_ids = []
    project = repository_module.project_public_transcript

    def observe(messages):
        projected_ids.extend(item["id"] for item in messages)
        return project(messages)

    with monkeypatch.context() as scoped:
        scoped.setattr(repository_module, "project_public_transcript", observe)
        if mode == "save_context":
            saved = repo.save_context_snapshot(original.id, snapshot)
        else:
            saved = repo.commit_turn_projection(original.id, assistant_message=None if mode == "context_only" else assistant,
                                                context_snapshot=snapshot, expected_revision=original.revision)
    start = 35 if mode == "replace" else 40
    assert projected_ids == [item["id"] for item in expected[start:]]
    assert saved.transcript == expected
    restored = ConversationRepository(tmp_path).get_conversation(original.id)
    assert restored.transcript == expected and restored.context_snapshot == snapshot


def test_rewind_admission_does_not_trust_replacement_history(tmp_path):
    repo = ConversationRepository(tmp_path)
    original = repo.create_conversation(transcript=[{"id": "first", "role": "user", "content": "before"}])
    token = "sk-" + "testcredential" * 4
    rewound = [{"id": "first", "role": "assistant", "content": "stale content", "private_transport": "hidden",
                "blocks": [{"type": "text", "content": f"final {token}", "itemId": "answer", "source": "model_final", "status": "completed"}]}]
    saved = repo.commit_turn_admission(original.id, user_message={"id": "retry", "role": "user", "content": "retry"},
        context_snapshot={}, rewind_projection={"transcript": rewound, "summary": "", "memory_pollution_sources": []})
    expected = _normalize_loaded_transcript([*rewound, {"id": "retry", "role": "user", "content": "retry"}])
    assert saved.transcript == expected
    assert token not in json.dumps(saved.transcript)
    assert "private_transport" not in saved.transcript[0]
    assert ConversationRepository(tmp_path).get_conversation(original.id).transcript == expected


def test_context_checkpoint_uses_compact_json_without_changing_recovery(tmp_path):
    repo = ConversationRepository(tmp_path)
    snapshot = {"history": [{"role": "user", "content": "中文\n  keep indentation"}],
                "metadata": {"enabled": True, "value": None, "count": 7}}
    created = repo.create_conversation(context_snapshot=snapshot)
    manifest = json.loads((tmp_path / f"{created.id}.manifest.json").read_text(encoding="utf-8"))
    path = repo._generation_paths(created.id, manifest["current_generation"])[2]
    raw = path.read_text(encoding="utf-8")
    assert raw == json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    assert ConversationRepository(tmp_path).get_conversation(created.id).context_snapshot == snapshot
