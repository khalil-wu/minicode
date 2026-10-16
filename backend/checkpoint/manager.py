from __future__ import annotations

from backend.async_cleanup import to_thread_cancel_safe

import asyncio
import base64
import hashlib
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from backend.atomic_io import atomic_write_bytes, canonical_file_path_key, file_mutation_locks, run_blocking_io
from backend.checkpoint.store import (
    CheckpointFileSnapshot,
    CheckpointCorruptError,
    CheckpointRecord,
    CheckpointStore,
    checkpoint_owner_key,
)

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
        execution_context=None,
    ) -> CheckpointRecord | None:
        if tool_name not in WRITE_TOOL_NAMES:
            return None

        from backend.workspace.executor_service import uses_executor_io
        if uses_executor_io(execution_context):
            return await self._snapshot_executor(tool_name=tool_name, args=args, conversation_id=conversation_id,
                session_id=session_id, tool_call_id=tool_call_id, execution_context=execution_context)

        root = Path(workspace_root or Path.cwd()).resolve()
        paths = self._paths_for_tool(tool_name, args)
        if not paths:
            return None

        record_id = f"chk_{checkpoint_owner_key(conversation_id)}_{uuid.uuid4().hex[:12]}"
        files: list[CheckpointFileSnapshot] = []
        for raw_path in paths:
            target, rel_path = self._resolve_target(root, raw_path)
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
                await to_thread_cancel_safe(self._store.write_blob, blob, raw)
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

    async def rewind(self, checkpoint_id: str, *, execution_context=None) -> CheckpointRecord:
        record = self._store.get(checkpoint_id)
        if record is None:
            raise ValueError(f"Checkpoint '{checkpoint_id}' was not found.")

        from backend.workspace.executor_service import uses_executor_io
        if "environment_id" in record.metadata or uses_executor_io(execution_context):
            return await self._rewind_executor(record, execution_context)

        root = Path(record.workspace_root).resolve()
        # cc's applySnapshot restores every tracked file to the chosen point.
        # A sparse MiniCode record needs the first later snapshot for each file:
        # it contains that file's state immediately before its first edit after
        # the chosen point.
        restore_files: dict[str, CheckpointFileSnapshot] = {
            canonical_file_path_key(self._resolve_target(root, snapshot.path)[0]): snapshot
            for snapshot in record.files
        }
        conversation_id = record.conversation_id
        if conversation_id:
            records = {
                candidate.id: candidate
                for candidate in self._store.list_for_conversation(
                    conversation_id,
                    limit=None,
                    strict=True,
                )
            }
            records[record.id] = record
            ordered = sorted(
                reversed(tuple(records.values())),
                key=lambda candidate: candidate.created_at,
            )
            chosen_index = next(
                index
                for index, candidate in enumerate(ordered)
                if candidate.id == record.id
            )
            for newer in ordered[chosen_index + 1 :]:
                if canonical_file_path_key(Path(newer.workspace_root)) != canonical_file_path_key(root):
                    continue
                for snapshot in newer.files:
                    restore_files.setdefault(
                        canonical_file_path_key(self._resolve_target(root, snapshot.path)[0]),
                        snapshot,
                    )

        await run_blocking_io(
            self._restore_files, root, list(restore_files.values())
        )
        return replace(record, paths=[snapshot.path for snapshot in restore_files.values()], files=list(restore_files.values()))

    async def _snapshot_executor(self, *, tool_name, args, conversation_id, session_id, tool_call_id, execution_context):
        from backend.workspace.executor_service import ExecutorWorkspaceService

        paths = self._paths_for_tool(tool_name, args)
        if not paths:
            return None
        record_id = f"chk_{checkpoint_owner_key(conversation_id)}_{uuid.uuid4().hex[:12]}"
        async with ExecutorWorkspaceService(execution_context) as service:
            targets = list(dict.fromkeys(service.resolve(value) for value in paths))
            files = []
            async with service.mutate(targets):
                for path in targets:
                    value = await service.metadata(path)
                    if value is not None and not value["isFile"]:
                        raise IsADirectoryError(path.uri)
                    blob = None
                    existed = value is not None and value["isFile"]
                    if existed:
                        raw = await service.read_bytes(path, limit=value["size"])
                        blob = hashlib.sha256(f"{record_id}:{path.uri}".encode("utf-8")).hexdigest()
                        await to_thread_cancel_safe(self._store.write_blob, blob, raw)
                    files.append(CheckpointFileSnapshot(path=service.display(path), existed=existed,
                        encoding="binary", blob=blob, environment_id=service.scope.environment_id, path_uri=path.uri))
            record = CheckpointRecord(id=record_id, conversation_id=conversation_id, session_id=session_id,
                tool_call_id=tool_call_id, tool_name=tool_name, workspace_root=service.cwd.uri,
                paths=[item.path for item in files], files=files, metadata={
                    "environment_id": service.scope.environment_id, "cwd_uri": service.cwd.uri,
                    "args": {key: value for key, value in args.items() if key not in {"content", "patch"}}})
            return await to_thread_cancel_safe(self._store.save, record)

    async def _rewind_executor(self, record, execution_context):
        from backend.workspace.executor_service import ExecutorWorkspaceService, record_executor_file_hash

        async with ExecutorWorkspaceService(execution_context) as service:
            record = self._qualify_executor_record(record, service)
            if record.metadata["environment_id"] != service.scope.environment_id or service.resolve(record.workspace_root) != service.cwd:
                raise ValueError("Checkpoint does not belong to the accepted execution environment and workspace.")
            restore = {service.resolve(item.path_uri): item for item in record.files}
            records = self._store.list_for_conversation(record.conversation_id, limit=None, strict=True) if record.conversation_id else []
            ordered = sorted({item.id: item for item in [*records, record]}.values(), key=lambda item: (item.created_at, item.id))
            chosen = next(index for index, item in enumerate(ordered) if item.id == record.id)
            for newer in ordered[chosen + 1:]:
                if "environment_id" not in newer.metadata:
                    if service.context.turn_environment.is_remote or service.resolve(newer.workspace_root) != service.cwd:
                        continue
                    newer = self._qualify_executor_record(newer, service)
                if newer.metadata.get("environment_id") != service.scope.environment_id or service.resolve(newer.workspace_root) != service.cwd:
                    continue
                for item in newer.files:
                    restore.setdefault(service.resolve(item.path_uri), item)
            decoded = []
            for path, item in restore.items():
                if not service.scope.resolve_access(path.uri).can_write:
                    raise PermissionError(f"Checkpoint restore write denied by current execution scope: {path.uri}")
                raw = await asyncio.to_thread(self._snapshot_bytes, item) if item.existed else None
                decoded.append((path, item, raw))
            async with service.mutate(list(restore)):
                for path, item, raw in decoded:
                    if item.existed:
                        await service.write_bytes(path, raw)
                    elif await service.metadata(path) is not None:
                        await service.remove(path)
                    record_executor_file_hash(execution_context, path.uri, None)
            return replace(record, paths=[item.path for item in restore.values()], files=list(restore.values()))

    @staticmethod
    def _qualify_executor_record(record, service):
        if "environment_id" in record.metadata:
            return record
        if service.context.turn_environment.is_remote or service.resolve(record.workspace_root) != service.cwd:
            raise ValueError("Legacy checkpoint has no identity for this execution environment.")
        return replace(record, workspace_root=service.cwd.uri,
            metadata={**record.metadata, "environment_id": service.scope.environment_id, "cwd_uri": service.cwd.uri},
            files=[replace(item, environment_id=service.scope.environment_id, path_uri=service.resolve(item.path).uri) for item in record.files])

    def _snapshot_bytes(self, snapshot):
        if snapshot.blob:
            return self._store.read_blob(snapshot.blob)
        if snapshot.content is None:
            raise CheckpointCorruptError(f"Checkpoint does not contain restorable content for: {snapshot.path}")
        return base64.b64decode(snapshot.content, validate=True) if snapshot.encoding == "base64" else snapshot.content.encode(snapshot.encoding)

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
    def _resolve_target(root: Path, raw_path: str) -> tuple[Path, str]:
        """Return the file and the key it is recorded under.

        Files in the workspace are keyed relative to it. An authorized write
        outside it (the plan file, an edit in bypass mode) is keyed by its
        absolute path, so it is snapshotted and restored like any other file;
        whether such a write may happen at all is the permission checker's call.
        """
        candidate = Path(str(raw_path).strip())
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        try:
            return resolved, resolved.relative_to(root).as_posix()
        except ValueError:
            return resolved, resolved.as_posix()

    def _restore_files(self, root: Path, snapshots: list[CheckpointFileSnapshot]) -> None:
        targets = [self._resolve_target(root, snapshot.path)[0] for snapshot in snapshots]
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
                if snapshot.content is None:
                    raise CheckpointCorruptError(f"Checkpoint does not contain restorable content for: {snapshot.path}")
                # Legacy inline payload: base64 (or plain text) inside the
                # record JSON, written before the sidecar migration.
                if snapshot.encoding == "base64":
                    try:
                        raw = base64.b64decode(snapshot.content, validate=True)
                    except ValueError as exc:
                        raise ValueError(f"Checkpoint content is invalid for {snapshot.path}") from exc
                else:
                    try:
                        raw = snapshot.content.encode(snapshot.encoding)
                    except (LookupError, UnicodeEncodeError) as exc:
                        raise ValueError(f"Checkpoint encoding is invalid for {snapshot.path}") from exc
                decoded.append((target, snapshot, raw))

            for target, snapshot, raw in decoded:
                if snapshot.existed:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(target, raw or b"")
                elif target.exists() and target.is_file():
                    target.unlink()
        from backend.tools.file_tools_common import invalidate_workspace_file_caches

        invalidate_workspace_file_caches(file_tree_changed=True, clear_file_state=True)
