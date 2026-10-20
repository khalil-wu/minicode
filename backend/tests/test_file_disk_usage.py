from pathlib import Path
import os
from datetime import UTC, datetime, timedelta
import subprocess

import pytest

from backend.conversations.repository import ConversationRepository
from backend.conversations.compression import read_history_bytes
from backend.services import file_disk_usage
from backend.services.general_maintenance import compress_cold_history
from backend.services.storage_settings_service import disk_usage


def test_ordinary_file_usage_retains_its_existing_storage_count(tmp_path):
    file = tmp_path / "content.txt"
    file.write_bytes(b"Original contents")
    assert file_disk_usage.file_disk_bytes(file) == len(b"Original contents")


def test_storage_categories_measure_the_actual_zstd_gain_and_keep_content_readable(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    record = repository.create_conversation(title="Fixture compression usage", transcript=[{
        "id": "fixture-original-user", "role": "user", "content": "Original conversation text " * 20000,
    }], context_snapshot={"history": [{"role": "user", "content": "Original provider history " * 20000}]})
    history_files = [file for file in repository.storage_root.iterdir()
        if file.name.endswith((".transcript.jsonl", ".snapshot.json"))]
    original = {file: file.read_bytes() for file in history_files}
    draft = tmp_path / "draft.json"
    draft.write_text('{"draft":"KEEP UNSENT CONTENT"}', encoding="utf8")
    before = disk_usage(tmp_path, repository=repository)
    now = datetime.now(UTC)
    old = (now - timedelta(days=8)).timestamp()
    for file in history_files:
        os.utime(file, (old, old))
    assert compress_cold_history(repository, history_files, now=now) == len(history_files)
    after = disk_usage(tmp_path, repository=repository)
    before_chats = next(category["bytes"] for category in before["categories"] if category["name"] == "chats")
    after_chats = next(category["bytes"] for category in after["categories"] if category["name"] == "chats")
    assert after_chats < before_chats
    assert after["total_bytes"] < before["total_bytes"]
    assert {file: read_history_bytes(file) for file in history_files} == original
    fresh = ConversationRepository(repository.storage_root)
    assert fresh.get_transcript_page(record.id)["transcript"] == record.transcript
    assert fresh.get_conversation(record.id).context_snapshot == record.context_snapshot
    assert draft.read_text(encoding="utf8") == '{"draft":"KEEP UNSENT CONTENT"}'


@pytest.mark.skipif(os.name != "nt", reason="Existing NTFS-compressed history is Windows-only")
def test_existing_ntfs_history_remains_readable_and_can_transition_to_zstd(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    record = repository.create_conversation(transcript=[{"id": "original-user", "role": "user", "content": "Original bytes " * 5000}],
        context_snapshot={"history": [{"role": "user", "content": "Original private history"}]})
    files = repository.history_paths(record.id)
    for file in files:
        subprocess.run(["compact.exe", "/C", "/Q", str(file)], check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    assert ConversationRepository(repository.storage_root).get_conversation(record.id).context_snapshot == record.context_snapshot
    now = datetime.now(UTC)
    old = (now - timedelta(days=8)).timestamp()
    for file in files:
        os.utime(file, (old, old))
    assert compress_cold_history(repository, list(files), now=now) == len(files)
    reloaded = ConversationRepository(repository.storage_root).get_conversation(record.id)
    assert reloaded.transcript == record.transcript
    assert reloaded.context_snapshot == record.context_snapshot


@pytest.mark.skipif(os.name != "nt", reason="GetCompressedFileSizeW is a Windows-only storage consumer")
def test_win32_count_combines_high_and_low_words_and_storage_propagates_real_file_errors(tmp_path, monkeypatch):
    import ctypes

    calls = []
    def large_count(path, high):
        calls.append(path)
        high._obj.value = 3
        return 17
    monkeypatch.setattr(file_disk_usage, "_get_compressed_file_size", large_count)
    assert file_disk_usage.file_disk_bytes(Path("fixture-history.jsonl")) == (3 << 32) + 17
    assert len(calls) == 1
    assert calls[0].startswith("\\\\?\\")
    monkeypatch.setattr(file_disk_usage, "_get_compressed_file_size", lambda _path, _high: 0xFFFFFFFF)
    assert file_disk_usage.file_disk_bytes(Path("fixture-large-file")) == 0xFFFFFFFF
    def denied(_path, _high):
        ctypes.set_last_error(5)
        return 0xFFFFFFFF
    monkeypatch.setattr(file_disk_usage, "_get_compressed_file_size", denied)
    file = tmp_path / "unreadable.txt"
    file.write_bytes(b"fixture original")
    with pytest.raises(OSError) as error:
        disk_usage(tmp_path)
    assert error.value.winerror == 5
    assert error.value.filename == str(file)
