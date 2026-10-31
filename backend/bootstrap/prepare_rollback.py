"""Prepare current durable cancellation state for the supported R6 rollback reader.

The desktop stops its backend before calling this trusted maintenance command.
Normal runtime reads keep cancelled originals in receipts; only rollback needs
the old reader's inline projection. This command never starts or replays work.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from filelock import FileLock, Timeout

from backend.agent.message import UserCommand
from backend.atomic_io import atomic_write_text, canonical_file_path_key, file_mutation_locks
from backend.ws.client_command_log import _clean_command_id
from backend.ws.durable_user_queue import DurableUserMessageQueue

_SESSION_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
ReceiptKey = tuple[str, str]


class RollbackPreparationError(RuntimeError):
    """The old reader cannot safely start with this profile yet."""


def _contained(root: Path, candidate: Path) -> Path:
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise RollbackPreparationError("Rollback queue or receipt path escapes the declared state root")
    return resolved


def _directories(root: Path, parent: Path) -> list[Path]:
    parent = _contained(root, parent)
    if not parent.exists():
        return []
    result = []
    with os.scandir(parent) as entries:
        for entry in entries:
            path = _contained(root, Path(entry.path))
            if path.is_dir():
                result.append(path)
    return sorted(result)


def _read_receipts(root: Path, directory: Path) -> dict[str, dict[str, Any]]:
    records = {}
    with os.scandir(_contained(root, directory)) as entries:
        paths = sorted(_contained(root, Path(entry.path)) for entry in entries if entry.name.endswith(".json"))
    for path in paths:
        record = json.loads(path.read_text(encoding="utf-8"))
        data = record.get("command", {}).get("data", {}) if isinstance(record, dict) else {}
        command_id = _clean_command_id(data.get("client_command_id")) if isinstance(data, dict) else ""
        expected_name = hashlib.sha256(command_id.encode("utf-8")).hexdigest() + ".json"
        if not command_id or path.name != expected_name or not DurableUserMessageQueue._valid_cancelled_receipt(command_id, record):
            raise RollbackPreparationError("A durable Stop receipt is malformed or bound to the wrong command id")
        records[command_id] = record
    return records


def _origin_key(value: Any) -> str:
    if not isinstance(value, str) or _SESSION_KEY.fullmatch(value) is None:
        raise RollbackPreparationError("A Stop receipt has an invalid original window identity")
    return value


def _same_receipt(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # A supported migration adds provenance to the destination copy. Source
    # bytes, the first cancellation time, reason and complete input stay intact.
    return {key: value for key, value in left.items() if key != "client_queue_key"} == {
        key: value for key, value in right.items() if key != "client_queue_key"}


def _archive_inventory(root: Path, base: Path, stores: list[Path]) -> dict[Path, dict[ReceiptKey, dict[str, Any]]]:
    archives: dict[Path, dict[ReceiptKey, dict[str, Any]]] = {}
    for store in stores:
        with os.scandir(store) as entries:
            for entry in entries:
                if entry.name.endswith(".json"):
                    archives[_contained(root, Path(entry.path))] = {}
        for namespace in _directories(root, store / "cancelled-inputs"):
            _origin_key(namespace.name)
            queue = _contained(root, store / (namespace.name + ".json"))
            records = archives.setdefault(queue, {})
            for command_id, receipt in _read_receipts(root, namespace).items():
                origin = namespace.name if store == base else receipt.get("client_queue_key", "")
                if receipt.get("client_queue_key") is not None and _origin_key(receipt["client_queue_key"]) != origin:
                    raise RollbackPreparationError("A Stop receipt is bound to another window namespace")
                records[(origin, command_id)] = receipt
            if store != base:
                for origin_dir in _directories(root, namespace):
                    origin = _origin_key(origin_dir.name)
                    for command_id, receipt in _read_receipts(root, origin_dir).items():
                        if _origin_key(receipt.get("client_queue_key")) != origin:
                            raise RollbackPreparationError("A scoped Stop receipt is bound to another window namespace")
                        key = (origin, command_id)
                        if key in records and not _same_receipt(records[key], receipt):
                            raise RollbackPreparationError("Scoped and legacy Stop receipts conflict")
                        records[key] = receipt
    return archives


def _claim_closed_owners(root: Path, stores: list[Path], stack: ExitStack, claimed: set[str]) -> None:
    for store in stores:
        for session_dir in _directories(root, store / ".owner-leases"):
            with os.scandir(session_dir) as entries:
                leases = sorted(_contained(root, Path(entry.path)) for entry in entries if entry.name.endswith(".lock"))
            for path in leases:
                key = canonical_file_path_key(path)
                if key in claimed:
                    continue
                probe = FileLock(str(path), timeout=0)
                try:
                    probe.acquire(timeout=0)
                except Timeout as exc:
                    raise RollbackPreparationError("A live user-message queue owner prevents rollback; stop the backend first") from exc
                stack.callback(probe.release, force=True)
                claimed.add(key)


def _submitted_input(command: Any) -> str:
    if not isinstance(command, dict) or not isinstance(command.get("data"), dict):
        raise RollbackPreparationError("A queued client input is malformed")
    return DurableUserMessageQueue.cancellation_input_identity(UserCommand(**command))


def _read_queue(queue: Path) -> dict[str, Any]:
    if queue.exists():
        payload = json.loads(queue.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RollbackPreparationError("Durable user queue must contain a JSON object")
    else:
        payload = {"version": 6, "queues": {}, "inflight": {}, "turn_inputs": {}, "client_pending": [], "client_inflight": {}, "ownership": {}}
    version = payload.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version > 8:
        raise RollbackPreparationError("This queue format has no supported R6 rollback projection")
    return payload


def _inline_receipts(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    inline = payload.get("client_cancelled", {})
    if not isinstance(inline, dict) or any(
        not isinstance(command_id, str) or _clean_command_id(command_id) != command_id
        or not DurableUserMessageQueue._valid_cancelled_receipt(command_id, record)
        for command_id, record in inline.items()
    ):
        raise RollbackPreparationError("Existing inline cancellation state is malformed")
    return dict(inline)


def _receipt_corpus(
    root: Path, base: Path, archives: dict[Path, dict[ReceiptKey, dict[str, Any]]],
    payloads: dict[Path, dict[str, Any]],
) -> tuple[dict[ReceiptKey, dict[str, Any]], set[Path]]:
    """Recover the original window even if the shared commit preceded its mirror."""
    corpus: dict[ReceiptKey, dict[str, Any]] = {}
    origins: set[Path] = set()
    # Physical window stores establish the scope of receipts predating the
    # provenance field. Read them before resolving old shared flat records.
    for queue in sorted(archives, key=lambda path: (path.parent != base, canonical_file_path_key(path))):
        records = dict(archives[queue])
        for command_id, receipt in _inline_receipts(payloads[queue]).items():
            origin = queue.stem if queue.parent == base else receipt.get("client_queue_key", "")
            if receipt.get("client_queue_key") is not None and _origin_key(receipt["client_queue_key"]) != origin:
                raise RollbackPreparationError("Inline Stop receipt belongs to another window")
            key = (origin, command_id)
            if key in records and not _same_receipt(records[key], receipt):
                raise RollbackPreparationError("Archived and inline Stop receipts conflict; no user input was replaced")
            records[key] = receipt
        for (origin, command_id), receipt in records.items():
            if not origin:
                matches = [key for key, existing in corpus.items() if key[1] == command_id and _same_receipt(existing, receipt)]
                if len(matches) != 1:
                    raise RollbackPreparationError("A legacy shared Stop receipt has no unique original window")
                origin = matches[0][0]
            origin = _origin_key(origin)
            key = (origin, command_id)
            if key in corpus and not _same_receipt(corpus[key], receipt):
                raise RollbackPreparationError("Profile Stop receipts conflict within one producer; no user input was replaced")
            if key not in corpus or queue.parent == base:
                corpus[key] = receipt
            origins.add(_contained(root, base / (origin + ".json")))
    return corpus, origins


def _queue_inputs(payload: dict[str, Any]):
    for name in ("queues", "turn_inputs"):
        lane = payload.get(name, {})
        if not isinstance(lane, dict) or any(not isinstance(items, list) for items in lane.values()):
            raise RollbackPreparationError("User input queue lane is malformed")
        for owner, items in lane.items():
            for item in items:
                yield owner, item
    lane = payload.get("inflight", {})
    pending = payload.get("client_pending", [])
    client_inflight = payload.get("client_inflight", {})
    if not isinstance(lane, dict) or not isinstance(pending, list) or not isinstance(client_inflight, dict):
        raise RollbackPreparationError("Accepted client input state is malformed")
    for owner, item in lane.items():
        yield owner, item
    for item in [*pending, *client_inflight.values()]:
        yield "", item


def _legacy_candidates(base: Path, payloads: dict[Path, dict[str, Any]], receipts: dict[ReceiptKey, dict[str, Any]]) -> list[tuple[str, UserCommand]]:
    candidates = [(origin, UserCommand(**receipt["command"])) for (origin, _cid), receipt in receipts.items()]
    for queue, payload in payloads.items():
        for owner, item in _queue_inputs(payload):
            if not isinstance(item, dict) or not isinstance(item.get("data"), dict):
                raise RollbackPreparationError("An accepted user input is malformed")
            if item.get("type") != "user_message" or not _clean_command_id(item["data"].get("client_command_id")):
                continue
            origin = queue.stem if queue.parent == base else item["data"].get("_client_queue_key")
            if origin is None:
                continue
            origin = _origin_key(origin)
            data = dict(item["data"])
            if not data.get("conversation_id") and owner:
                data["conversation_id"] = owner
            candidates.append((origin, UserCommand(type="user_message", data=data)))
    return candidates


def _project(
    queue: Path, payload: dict[str, Any], receipts: dict[ReceiptKey, dict[str, Any]],
    *, window_scope: str, candidates: list[tuple[str, UserCommand]],
) -> tuple[dict[str, Any], bool]:
    original = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
    inline = _inline_receipts(payload)
    for (origin, command_id), receipt in receipts.items():
        if window_scope and origin == window_scope:
            inline[command_id] = receipt
    pending = payload.get("client_pending", [])
    inflight = payload.get("client_inflight", {})
    ownership = payload.get("ownership", {})
    if not isinstance(pending, list) or not isinstance(inflight, dict) or not isinstance(ownership, dict):
        raise RollbackPreparationError("Client input queue or ownership state is malformed")
    receipt_ids = {command_id for _origin, command_id in receipts}

    def legacy_signature(command: dict[str, Any], conversation_id: str):
        data = command["data"]
        return (_clean_command_id(data.get("client_command_id")),
                str(data.get("conversation_id") or conversation_id), _submitted_input(command),
                *(str(data.get(key) or "") for key in ("user_message_id", "assistant_message_id", "window_instance_id", "window_owner_id")))

    ambiguous = set()
    if not window_scope:
        for lane_name in ("queues", "turn_inputs"):
            lane = payload.get(lane_name, {})
            if not isinstance(lane, dict) or any(not isinstance(items, list) for items in lane.values()):
                raise RollbackPreparationError("User input queue lane is malformed")
            for owner, items in lane.items():
                counts = Counter(legacy_signature(item, owner) for item in items
                    if isinstance(item, dict) and isinstance(item.get("data"), dict)
                    and not item["data"].get("_client_queue_key")
                    and _clean_command_id(item["data"].get("client_command_id")) in receipt_ids)
                ambiguous.update(signature for signature, count in counts.items() if count > 1)

    def was_cancelled(command: Any, claimed_id: str = "", conversation_id: str = "") -> bool:
        if not isinstance(command, dict) or not isinstance(command.get("data"), dict):
            raise RollbackPreparationError("A queued user input is malformed")
        data = command.get("data", {}) if isinstance(command, dict) else {}
        command_id = _clean_command_id(data.get("client_command_id")) if isinstance(data, dict) else ""
        command_id = command_id or claimed_id
        if not command_id or command_id not in receipt_ids:
            return False
        private_origin = data.get("_client_queue_key")
        origin = _origin_key(private_origin) if private_origin is not None else window_scope
        if window_scope and origin != window_scope:
            raise RollbackPreparationError("A window input is bound to another producer")
        bound_owner = str(data.get("conversation_id") or conversation_id or "")
        if origin:
            receipt = receipts.get((origin, command_id))
            if receipt is None:
                return False
        else:
            if legacy_signature(command, conversation_id) in ambiguous:
                raise RollbackPreparationError("Repeated legacy shared inputs have no proven individual producer; originals were retained")
            possible_origins = DurableUserMessageQueue.legacy_input_origins(
                UserCommand(**command), conversation_id=conversation_id, candidates=candidates)
            if not possible_origins:
                return False
            if len(possible_origins) != 1:
                raise RollbackPreparationError("A legacy shared input has no unique cancellation producer")
            origin = next(iter(possible_origins))
            receipt = receipts.get((origin, command_id))
            if receipt is None:
                # This is another producer's valid unstarted input.
                data["_client_queue_key"] = origin
                return False
        if _submitted_input(command) != _submitted_input(receipt["command"]):
            raise RollbackPreparationError("A cancelled command id now contains different input; rollback did not replace it")
        stopped_owner = str(receipt["command"]["data"].get("conversation_id") or "")
        if bound_owner and stopped_owner and bound_owner != stopped_owner:
            raise RollbackPreparationError("A cancelled command id is bound to another conversation")
        if window_scope:
            inline[command_id] = receipt
        return True

    payload["client_pending"] = [command for command in pending if not was_cancelled(command)]
    removed = {command_id for command_id, command in inflight.items() if was_cancelled(command, command_id)}
    payload["client_inflight"] = {command_id: command for command_id, command in inflight.items() if command_id not in removed}
    client_owners = ownership.get("client_inflight", {})
    if not isinstance(client_owners, dict):
        raise RollbackPreparationError("Client input ownership state is malformed")
    ownership = dict(ownership)
    ownership["client_inflight"] = {command_id: owner for command_id, owner in client_owners.items() if command_id not in removed}
    for lane_name in ("queues", "turn_inputs"):
        lane = payload.get(lane_name, {})
        if not isinstance(lane, dict) or any(not isinstance(commands, list) for commands in lane.values()):
            raise RollbackPreparationError("User input queue lane is malformed")
        projected = {}
        for conversation_id, commands in lane.items():
            kept = [command for command in commands if not was_cancelled(command, conversation_id=conversation_id)]
            if kept:
                projected[conversation_id] = kept
        payload[lane_name] = projected
        if lane_name == "turn_inputs":
            owners = ownership.get(lane_name, {})
            if not isinstance(owners, dict):
                raise RollbackPreparationError("Turn input ownership state is malformed")
            ownership[lane_name] = {owner: value for owner, value in owners.items() if owner in projected}
    lane = payload.get("inflight", {})
    owners = ownership.get("inflight", {})
    if not isinstance(lane, dict) or not isinstance(owners, dict):
        raise RollbackPreparationError("Inflight user input or ownership state is malformed")
    removed = {owner for owner, command in lane.items() if was_cancelled(command, conversation_id=owner)}
    payload["inflight"] = {owner: command for owner, command in lane.items() if owner not in removed}
    ownership["inflight"] = {owner: value for owner, value in owners.items() if owner not in removed}
    payload["ownership"] = ownership
    payload["client_cancelled"] = inline
    if payload.get("version") in (7, 8) or inline:
        payload["version"] = 6
    changed = not queue.exists() or json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False) != original
    return payload, changed


def prepare_rollback(state_root: Path) -> dict[str, Any]:
    root = Path(state_root).expanduser().resolve(strict=True)
    base = _contained(root, root / "data/user-message-queue")
    if not base.exists():
        return {"ok": True, "schema_version": 6, "target_reader": "r6-inline-cancellation", "queue_count": 0, "cancellation_count": 0, "rewritten_count": 0}
    stores = [base]
    conversations = _contained(root, base / "conversations")
    if conversations.exists():
        stores.append(conversations)
    queues = _archive_inventory(root, base, stores)
    payloads = {path: _read_queue(path) for path in queues}
    corpus, origins = _receipt_corpus(root, base, queues, payloads)
    for path in origins:
        if path not in queues:
            queues[path] = {}
    paths = sorted(queues, key=canonical_file_path_key)
    rewritten = 0
    with ExitStack() as stack:
        claimed: set[str] = set()
        _claim_closed_owners(root, stores, stack, claimed)
        stack.enter_context(file_mutation_locks(paths))
        # Recheck after waiting for writers. New owners must not be promoted to
        # "closed" merely because their predecessors released a queue lock.
        _claim_closed_owners(root, stores, stack, claimed)
        # Read again under all queue locks. The desktop owns the stopped
        # profile; an unexpected new origin must not escape this locked batch.
        payloads = {path: _read_queue(path) for path in paths}
        current_archives = _archive_inventory(root, base, stores)
        if set(current_archives).difference(paths):
            raise RollbackPreparationError("Queue inventory changed while preparing rollback")
        corpus, origins = _receipt_corpus(root, base, current_archives, payloads)
        if origins.difference(paths):
            raise RollbackPreparationError("Stop receipt origins changed while preparing rollback")
        candidates = _legacy_candidates(base, payloads, corpus)
        prepared = []
        for queue in paths:
            payload, changed = _project(queue, payloads[queue], corpus,
                window_scope=queue.stem if queue.parent == base else "", candidates=candidates)
            prepared.append((queue, payload, changed))
        # Validate the entire profile before the first mutation. Atomic per-file
        # publish plus retained originals makes an interrupted pass repeatable.
        for queue, payload, changed in prepared:
            if changed:
                atomic_write_text(queue, json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
                rewritten += 1
    return {"ok": True, "schema_version": 6, "target_reader": "r6-inline-cancellation", "queue_count": len(paths), "cancellation_count": len(corpus), "rewritten_count": rewritten}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = prepare_rollback(args.state_root)
    except Exception as exc:
        print(json.dumps({"ok": False, "error_kind": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
