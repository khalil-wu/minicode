from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from pathlib import Path
from typing import Any

from backend.atomic_io import atomic_write_bytes, file_mutation_locks
from backend.checkpoint.store import CheckpointFileSnapshot, CheckpointRecord, CheckpointStore

WRITE_TOOL_NAMES = {"write_file", "edit_file", "apply_patch", "notebook_edit"}


class CheckpointManager:
    def __init__(self, store: CheckpointStore | None = None) -> None:
        self._store = store or CheckpointStore()

    async def snapshot(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        workspace_root: str | Path | None,
        conversation_id: str = "",
        session_id: str = "",
        tool_call_id: str = "",
    ) -> CheckpointRecord | None:
        if tool_name not in WRITE_TOOL_NAMES:
            return None

        root = Path(workspace_root or Path.cwd()).resolve()
        paths = self._paths_for_tool(tool_name, args)
        if not paths:
            return None

        record_id = f"chk_{uuid.uuid4().hex[:12]}"
        files: list[CheckpointFileSnapshot] = []
        for raw_path in paths:
            target = self._resolve_under_root(root, raw_path)
            rel_path = target.relative_to(root).as_posix()
            if target.exists() and target.is_file():
                # cc's fileHistory backs snapshots with byte-exact file copies
                # (createBackup copyFile into a content-addressed sidecar
                # directory) instead of inlining payloads into its state JSON.
                # Raw blobs avoid base64's 33% inflation and keep the record
                # JSON small enough to list and parse cheaply; text reads
                # would also translate CRLF to LF and rewind would silently
                # rewrite every Windows file's line endings.
                raw = await asyncio.to_thread(target.read_bytes)
                blob = hashlib.sha256(f"{record_id}:{rel_path}".encode("utf-8")).hexdigest()
                await asyncio.to_thread(self._store.write_blob, blob, raw)
                files.append(
                    CheckpointFileSnapshot(
                        path=rel_path,
                        existed=True,
                        encoding="binary",
                        blob=blob,
                    )
                )
            else:
                files.append(CheckpointFileSnapshot(path=rel_path, existed=False, content=None))

        record = CheckpointRecord(
            id=record_id,
            conversation_id=conversation_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            workspace_root=str(root),
            paths=[item.path for item in files],
            files=files,
            metadata={"args": {key: value for key, value in args.items() if key not in {"content", "patch"}}},
        )
        return self._store.save(record)

    async def rewind(self, checkpoint_id: str) -> CheckpointRecord:
        record = self._store.get(checkpoint_id)
        if record is None:
            raise ValueError(f"Checkpoint '{checkpoint_id}' was not found.")

        root = Path(record.workspace_root).resolve()
        unrestorable = [
            snapshot.path
            for snapshot in record.files
            if snapshot.existed and snapshot.content is None and snapshot.blob is None
        ]
        if unrestorable:
            raise ValueError(
                "Checkpoint does not contain restorable content for: "
                + ", ".join(unrestorable)
            )

        # cc's applySnapshot restores every tracked file to the chosen point.
        # A sparse MiniCode record needs the first later snapshot for each file:
        # it contains that file's state immediately before its first edit after
        # the chosen point.
        restore_files: dict[str, CheckpointFileSnapshot] = {
            snapshot.path: snapshot for snapshot in record.files
        }
        conversation_id = record.conversation_id
        if conversation_id:
            records = {
                candidate.id: candidate
                for candidate in self._store.list_for_conversation(
                    conversation_id,
                    limit=None,
                )
            }
            records[record.id] = record
            ordered = sorted(
                records.values(),
                key=lambda candidate: (candidate.created_at, candidate.id),
            )
            chosen_index = next(
                index
                for index, candidate in enumerate(ordered)
                if candidate.id == record.id
            )
            for newer in ordered[chosen_index + 1 :]:
                if newer.workspace_root != record.workspace_root:
                    continue
                for snapshot in newer.files:
                    restore_files.setdefault(
                        snapshot.path,
                        snapshot,
                    )

        await asyncio.to_thread(
            self._restore_files, root, list(restore_files.values())
        )
        return record

    def get(self, checkpoint_id: str) -> CheckpointRecord | None:
        return self._store.get(checkpoint_id)

    def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int | None = 50,
    ) -> list[CheckpointRecord]:
        return self._store.list_for_conversation(conversation_id, limit=limit)

    def delete_for_conversation(self, conversation_id: str) -> int:
        return self._store.delete_for_conversation(conversation_id)

    @staticmethod
    def _paths_for_tool(tool_name: str, args: dict[str, Any]) -> list[str]:
        if tool_name in {"write_file", "edit_file"}:
            value = args.get("file_path") or args.get("path") or ""
            return [str(value)] if str(value).strip() else []
        if tool_name == "notebook_edit":
            value = args.get("notebook_path") or ""
            return [str(value)] if str(value).strip() else []
        if tool_name == "apply_patch":
            from backend.tools.apply_patch_parser import ApplyPatchError, patch_target_paths

            try:
                return patch_target_paths(str(args.get("patch") or ""))
            except ApplyPatchError:
                return []
        return []

    @staticmethod
    def _resolve_under_root(root: Path, raw_path: str) -> Path:
        candidate = Path(str(raw_path).strip())
        target = candidate if candidate.is_absolute() else root / candidate
        resolved = target.resolve()
        resolved.relative_to(root)
        return resolved

    def _restore_files(self, root: Path, snapshots: list[CheckpointFileSnapshot]) -> None:
        targets = [self._resolve_under_root(root, snapshot.path) for snapshot in snapshots]
        # Rewind participates in the same multi-file mutation queue as normal
        # edits and patches.  Prepare/decode everything while the lock is held,
        # then publish each file atomically so readers never observe a partial
        # file.  The outer async method keeps the blocking filesystem work off
        # the event loop.
        with file_mutation_locks(targets):
            decoded: list[tuple[Path, CheckpointFileSnapshot, bytes | None]] = []
            for target, snapshot in zip(targets, snapshots, strict=True):
                if not snapshot.existed:
                    decoded.append((target, snapshot, None))
                    continue
                if snapshot.blob:
                    # Sidecar payload (current format): raw byte copy.
                    decoded.append((target, snapshot, self._store.read_blob(snapshot.blob)))
                    continue
                # Legacy inline payload: base64 (or plain text) inside the
                # record JSON, written before the sidecar migration.
                if snapshot.encoding == "base64":
                    try:
                        raw = base64.b64decode(snapshot.content or "", validate=True)
                    except ValueError as exc:
                        raise ValueError(f"Checkpoint content is invalid for {snapshot.path}") from exc
                else:
                    try:
                        raw = (snapshot.content or "").encode(snapshot.encoding)
                    except (LookupError, UnicodeEncodeError) as exc:
                        raise ValueError(f"Checkpoint encoding is invalid for {snapshot.path}") from exc
                decoded.append((target, snapshot, raw))

            for target, snapshot, raw in decoded:
                if snapshot.existed:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(target, raw or b"")
                elif target.exists() and target.is_file():
                    target.unlink()
