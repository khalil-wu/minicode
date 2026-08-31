import asyncio
import threading
from datetime import datetime
from pathlib import Path

from backend.services.session_restore_service import seq_from_restore_payload as _seq_from_restore_payload
from backend.ws.event_outbox import EventOutbox
from backend.ws.handler import WebSocketSession
from backend.ws.session_lifecycle import SessionLifecycle


def test_seq_from_restore_payload_uses_single_last_seq_field() -> None:
    """The wire contract has one canonical replay cursor field: ``last_seq``.

    Old alias spellings were removed during protocol convergence; they must
    not be silently accepted (the renderer only sends ``last_seq``).
    """
    assert _seq_from_restore_payload({"last_seq": 7}) == 7
    assert _seq_from_restore_payload({"last_seq": "8"}) == 8
    assert _seq_from_restore_payload({"last_seen_seq": "8"}) == 0
    assert _seq_from_restore_payload({"last_event_seq": 9}) == 0


def test_seq_from_restore_payload_rejects_invalid_values() -> None:
    assert _seq_from_restore_payload({"last_seq": 0}) == 0
    assert _seq_from_restore_payload({"last_seq": -1}) == 0
    assert _seq_from_restore_payload({"last_seq": "not-a-number"}) == 0
    assert _seq_from_restore_payload({"last_seq": True}) == 0
    assert _seq_from_restore_payload({"last_seq": 1.5}) == 0
    assert _seq_from_restore_payload({"last_seq": 9_007_199_254_740_992}) == 0


def _outbox(tmp_path: Path, websocket) -> EventOutbox:
    return EventOutbox(
        session_id="session",
        websocket=websocket,
        replay_root=tmp_path,
        replay_limit=1000,
        cleanup_tasks=set(),
        has_active_run=lambda: False,
        requires_conversation_owner=lambda _event_type, _payload: False,
        workspace_scoped_event_types=(),
    )


def test_websocket_envelope_overrides_turn_local_seq(tmp_path: Path) -> None:
    class WebSocket:
        async def send_json(self, payload) -> None:
            self.sent.append(dict(payload))

        def __init__(self) -> None:
            self.sent: list[dict] = []

    websocket = WebSocket()
    outbox = _outbox(tmp_path, websocket)
    outbox._event_seq = 50
    asyncio.run(outbox.send_payload({"type": "pong", "seq": 1}, log_context="first"))
    asyncio.run(outbox.send_payload({"type": "pong", "seq": 1}, log_context="second"))
    first, second = websocket.sent

    assert first["seq"] == 51
    assert second["seq"] == 52


def test_file_changed_envelope_uses_rfc3339_transport_timestamp(tmp_path: Path) -> None:
    class WebSocket:
        async def send_json(self, payload) -> None:
            self.sent.append(dict(payload))

        def __init__(self) -> None:
            self.sent: list[dict] = []

    websocket = WebSocket()
    outbox = _outbox(tmp_path, websocket)

    async def scenario() -> None:
        await outbox.send_payload({
            "type": "file.changed",
            "conversation_id": "conv",
            "workspace_root": "C:\\repo",
            "path": "src/app.ts",
            "event": "modified",
        }, log_context="file.changed")
        if outbox.persistence_tail is not None:
            await outbox.persistence_tail

    asyncio.run(scenario())
    payload = websocket.sent[0]

    assert isinstance(payload["timestamp"], str)
    parsed = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_file_changed_business_payload_leaves_timestamp_to_envelope(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: list[dict] = []
    session = object.__new__(WebSocketSession)
    session.session_id = "session"
    session.session_lifecycle = SessionLifecycle(session)

    async def capture(payload, **_kwargs):
        captured.append(dict(payload))
        return True

    session.send_payload = capture
    monkeypatch.setattr(
        "backend.preview.launcher.running_preview_processes",
        lambda **_kwargs: [],
    )

    workspace_root = tmp_path.resolve()
    asyncio.run(
        session.session_lifecycle.on_file_changed(
            workspace_root / "src" / "app.ts",
            "modified",
            workspace_root=workspace_root,
            conversation_id="conv",
        )
    )

    assert captured[0] == {
        "type": "file.changed",
        "path": str(Path("src") / "app.ts"),
        "event": "modified",
        "conversation_id": "conv",
        "workspace_root": str(workspace_root),
    }
    assert "timestamp" not in captured[0]


def _replay_chain_outbox(
    events: list[dict],
    *,
    current_seq: int,
) -> EventOutbox:
    outbox = object.__new__(EventOutbox)
    outbox._events = [dict(event) for event in events]
    outbox._replay_cursor = current_seq
    outbox._persistence_failed_seqs = set()
    return outbox


def test_replay_window_accepts_transient_wire_sequence_gaps() -> None:
    outbox = _replay_chain_outbox(
        [
            {
                "type": "done",
                "conversation_id": "conv",
                "seq": 5,
                "previous_replay_seq": 0,
            },
            {
                "type": "done",
                "conversation_id": "conv",
                "seq": 8,
                "previous_replay_seq": 5,
            },
        ],
        current_seq=8,
    )

    events, has_gap = outbox.replay_window_after(5)

    assert has_gap is False
    assert [(event["seq"], event["previous_replay_seq"]) for event in events] == [(8, 5)]


def test_replay_window_rejects_an_internal_durable_chain_gap() -> None:
    outbox = _replay_chain_outbox(
        [
            {
                "type": "done",
                "conversation_id": "conv",
                "seq": 5,
                "previous_replay_seq": 0,
            },
            {
                "type": "done",
                "conversation_id": "conv",
                "seq": 8,
                "previous_replay_seq": 7,
            },
        ],
        current_seq=8,
    )

    assert outbox.replay_window_after(5) == ([], True)
    replay_events, has_gap = outbox.replay_window_after(5)
    assert replay_events == []
    assert has_gap is True


def test_replay_window_materializes_proven_legacy_chain_links() -> None:
    outbox = _replay_chain_outbox(
        [
            {"type": "done", "conversation_id": "conv", "seq": 5},
            {"type": "done", "conversation_id": "conv", "seq": 8},
            {"type": "done", "conversation_id": "conv", "seq": 11},
        ],
        current_seq=11,
    )

    events, has_gap = outbox.replay_window_after(5)

    assert has_gap is False
    assert [(event["seq"], event["previous_replay_seq"]) for event in events] == [
        (8, 5),
        (11, 8),
    ]
    assert "previous_replay_seq" not in outbox._events[1]


def test_replay_window_does_not_guess_a_legacy_link_without_its_anchor() -> None:
    outbox = _replay_chain_outbox(
        [{"type": "done", "conversation_id": "conv", "seq": 8}],
        current_seq=8,
    )

    assert outbox.replay_window_after(5) == ([], True)


def test_replay_sender_upgrades_legacy_records_before_wire_delivery() -> None:
    outbox = _replay_chain_outbox(
        [
            {"type": "done", "conversation_id": "conv", "seq": 5},
            {"type": "done", "conversation_id": "conv", "seq": 8},
        ],
        current_seq=8,
    )
    captured: list[dict] = []

    async def capture(payload, **_kwargs):
        captured.append(dict(payload))
        return True

    outbox.send_payload = capture

    sent = asyncio.run(outbox.replay_missed_events(5))

    assert sent == 1
    assert captured == [{
        "type": "session.replay",
        "last_seq": 5,
        "current_seq": 8,
        "replayed_events": 1,
        "events": [{
            "type": "done",
            "conversation_id": "conv",
            "seq": 8,
            "previous_replay_seq": 5,
            "replayed": True,
        }],
    }]


def test_replay_window_requires_the_chain_to_reach_the_durable_high_water() -> None:
    outbox = _replay_chain_outbox(
        [
            {
                "type": "done",
                "conversation_id": "conv",
                "seq": 8,
                "previous_replay_seq": 5,
            },
        ],
        current_seq=11,
    )

    assert outbox.replay_window_after(5) == ([], True)


def test_guideline_file_change_emits_actionable_reload_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: list[dict] = []
    session = object.__new__(WebSocketSession)
    session.session_id = "session"
    session.session_lifecycle = SessionLifecycle(session)

    async def capture(payload, **_kwargs):
        captured.append(dict(payload))
        return True

    session.send_payload = capture
    monkeypatch.setattr(
        "backend.preview.launcher.running_preview_processes",
        lambda **_kwargs: [],
    )

    workspace_root = tmp_path.resolve()
    guideline_path = workspace_root / "AGENTS.md"
    guideline_path.write_text("Project rules", encoding="utf-8")
    asyncio.run(
        session.session_lifecycle.on_file_changed(
            guideline_path,
            "modified",
            workspace_root=workspace_root,
            conversation_id="conv",
        )
    )

    assert captured[1] == {
        "type": "guidelines.updated",
        "message": "Project guidelines have been updated",
        "conversation_id": "conv",
        "workspace_root": str(workspace_root),
        "path": "AGENTS.md",
        "cache_cleared": True,
        "effective_from": "next_turn",
        "source_kind": "direct",
    }


def test_replay_persistence_does_not_block_later_websocket_send(tmp_path: Path) -> None:
    class _SlowStore:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.persisted: list[int] = []

        def append(self, payload) -> None:
            if not self.persisted:
                self.started.set()
                self.release.wait(timeout=5)
            self.persisted.append(int(payload["seq"]))

        def rewrite(self, events) -> None:
            self.persisted = [int(event["seq"]) for event in events]

    class _WebSocket:
        application_state = None
        client_state = None

        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_json(self, payload) -> None:
            self.sent.append(dict(payload))

    async def _run() -> tuple[list[dict], list[int]]:
        websocket = _WebSocket()
        outbox = _outbox(tmp_path, websocket)
        store = _SlowStore()
        outbox._store = store

        first = asyncio.create_task(outbox.send_payload(
            {
                "type": "agent_message.delta",
                "conversation_id": "conv",
                "item_id": "agent-message",
                "delta": "one",
            },
            log_context="first",
        ))
        await asyncio.to_thread(store.started.wait, 2)
        second = asyncio.create_task(outbox.send_payload(
            {"type": "done", "conversation_id": "conv"},
            log_context="second",
        ))

        async def _wait_for_second_send() -> None:
            while len(websocket.sent) < 2:
                await asyncio.sleep(0)

        await asyncio.wait_for(_wait_for_second_send(), timeout=1)
        store.release.set()
        await asyncio.gather(first, second)
        if outbox.persistence_tail is not None:
            await outbox.persistence_tail
        return websocket.sent, store.persisted

    sent, persisted = asyncio.run(_run())

    assert [(event["seq"], event["previous_replay_seq"]) for event in sent] == [
        (1, 0),
        (2, 1),
    ]
    assert persisted == [1, 2]
