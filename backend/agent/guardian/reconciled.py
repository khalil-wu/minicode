"""Read-only Source retained/live reconciliation in accepted input order."""

from __future__ import annotations

from dataclasses import dataclass

from backend.agent.guardian.heartbeat import Heartbeat
from backend.agent.guardian.retained import OrderedEvidence, RetainedMessage


def same_source(retained: RetainedMessage, candidate: RetainedMessage) -> bool:
    return candidate.message_id == retained.message_id if retained.message_id is not None else candidate.turn_id == retained.turn_id and candidate.text == retained.text


@dataclass(frozen=True)
class ReconciledRetainedContext:
    entries: tuple[OrderedEvidence, ...]
    latest_user_turn_id: str | None
    missing_user_messages: bool
    unmatched_legacy: tuple[RetainedMessage, ...] = ()

    @classmethod
    def capture(cls, retained, live_messages=()):
        entries = [record for _, record in retained.ordered_entries()]
        users = [(index, entry) for index, entry in enumerate(entries) if isinstance(entry.value, RetainedMessage) and entry in retained.user_messages]
        latest = next(((entry.key, entry.value.turn_id) for _, entry in reversed(users)), None)
        recover = retained.user_messages_incomplete or any(not entry.value.complete for _, entry in users)
        missing = retained.user_messages_incomplete
        used = {entry.key for entry in entries}
        matched = set()
        live = list(live_messages)
        for order, message in live:
            key = (1, order) if order is not None else None
            if key is not None and (latest is None or key > latest[0]):
                latest = key, message.turn_id
            if not recover:
                continue
            matching = next((index for index, entry in users if index not in matched and same_source(entry.value, message)), None)
            if matching is not None:
                matched.add(matching)
                continue
            if key is None or key in used:
                missing = True
                continue
            used.add(key)
            entries.append(OrderedEvidence(order, message))
        entries.sort(key=lambda entry: entry.key)
        versions = {}
        scope = None
        selected = []
        for entry in entries:
            if scope != entry.inherited:
                versions.clear()
                scope = entry.inherited
            if isinstance(entry.value, RetainedMessage) and (entry in retained.user_messages or entry not in retained.assistant_messages):
                message = entry.value
                if message.origin == "heartbeat":
                    heartbeat = Heartbeat.parse(message.text)
                    if heartbeat is None:
                        versions.clear()
                    else:
                        previous = versions.get(heartbeat.automation_id)
                        versions[heartbeat.automation_id] = heartbeat.instructions
                        if previous == heartbeat.instructions:
                            continue
            selected.append(entry)
        represented = [entry.value for entry in selected if isinstance(entry.value, RetainedMessage) and entry not in retained.assistant_messages]
        legacy = tuple(message for order, message in live if order is None and not any(same_source(source, message) for source in represented))
        return cls(tuple(selected), latest[1] if latest is not None else None, missing, legacy)
