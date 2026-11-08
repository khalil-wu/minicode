from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from backend.atomic_io import atomic_write_text
from backend.conversations.repository import ConversationRepository


def test_published_identity_does_not_wait_for_a_history_publication_lock(tmp_path):
    repository = ConversationRepository(tmp_path)
    identity = repository.store_instance_id()
    entered, release = threading.Event(), threading.Event()
    def writer():
        with repository._store_lock():
            entered.set()
            assert release.wait(5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(writer)
        assert entered.wait(5)
        reading = pool.submit(repository.store_instance_id)
        try:
            assert reading.result(timeout=1) == identity
        finally:
            release.set()
        writing.result(timeout=5)


def test_simultaneous_first_openers_publish_one_store_identity(tmp_path):
    repositories = [ConversationRepository(tmp_path) for _ in range(6)]
    barrier = threading.Barrier(len(repositories))
    def read(repository):
        barrier.wait(timeout=5)
        return repository.store_instance_id()
    with ThreadPoolExecutor(max_workers=6) as pool:
        identities = list(pool.map(read, repositories))
    assert len(set(identities)) == 1
    assert ConversationRepository(tmp_path).store_instance_id() == identities[0]


def test_identity_replacement_and_corruption_are_observed_by_existing_readers(tmp_path):
    repository = ConversationRepository(tmp_path)
    previous = repository.store_instance_id()
    replacement = "1" * 32 if previous != "1" * 32 else "2" * 32
    atomic_write_text(tmp_path / ".conversation-store.instance", replacement)
    assert repository.store_instance_id() == replacement
    atomic_write_text(tmp_path / ".conversation-store.instance", "invalid identity")
    with pytest.raises(ValueError, match="store instance id is malformed"):
        repository.store_instance_id()
