from backend.agent.conversation_query_guard import ConversationQueryGuardRegistry
import threading


def test_query_guard_rejects_concurrent_start_and_fences_stale_cleanup():
    guards = ConversationQueryGuardRegistry()

    first = guards.try_start("conversation-1", owner_id="ws:first")
    assert first is not None
    assert guards.owns(first) is True
    assert guards.try_start("conversation-1", owner_id="rest:second") is None

    assert guards.end(first) is True
    second = guards.try_start("conversation-1", owner_id="rest:second")
    assert second is not None
    assert second.generation == first.generation + 1
    assert guards.owns(first) is False
    assert guards.owns(second) is True
    assert guards.end(first) is False
    assert guards.active_claim("conversation-1") == second
    assert guards.end(second) is True


def test_query_guard_keeps_conversations_independent():
    guards = ConversationQueryGuardRegistry()

    first = guards.try_start("conversation-1", owner_id="one")
    second = guards.try_start("conversation-2", owner_id="two")

    assert first is not None
    assert second is not None
    assert guards.end(first) is True
    assert guards.end(second) is True


def test_query_guard_check_and_start_is_atomic_across_threads():
    guards = ConversationQueryGuardRegistry()
    barrier = threading.Barrier(2)
    claims = []

    def start(owner: str) -> None:
        barrier.wait()
        claims.append(guards.try_start("conversation-threaded", owner_id=owner))

    threads = [
        threading.Thread(target=start, args=("one",)),
        threading.Thread(target=start, args=("two",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(claim is not None for claim in claims) == 1
    winner = next(claim for claim in claims if claim is not None)
    assert guards.end(winner) is True
