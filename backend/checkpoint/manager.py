from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path
from typing import Any

from backend.checkpoint.store import CheckpointFileSnapshot, CheckpointRecord, CheckpointStore

WRITE_TOOL_NAMES = {"write_file", "edit_file"}


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

        files: list[CheckpointFileSnapshot] = []
        for raw_path in paths:
            target = self._resolve_under_root(root, raw_path)
            rel_path = target.relative_to(root).as_posix()
            if target.exists() and target.is_file():
                try:
                    content = await asyncio.to_thread(target.read_text, encoding="utf-8")
                except UnicodeDecodeError:
                    content = None
                files.append(CheckpointFileSnapshot(path=rel_path, existed=True, content=content))
            else:
                files.append(CheckpointFileSnapshot(path=rel_path, existed=False, content=None))

        record = CheckpointRecord(
            id=f"chk_{uuid.uuid4().hex[:12]}",
            conversation_id=conversation_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            workspace_root=str(root),
            paths=[item.path for item in files],
            files=files,
            git_head=await asyncio.to_thread(self._git_rev_parse, root, "HEAD"),
            git_stash_ref=await asyncio.to_thread(self._git_stash_create, root),
            metadata={"args": {key: value for key, value in args.items() if key != "content"}},
        )
        return self._store.save(record)

    async def rewind(self, checkpoint_id: str) -> CheckpointRecord:
        record = self._store.get(checkpoint_id)
        if record is None:
            raise ValueError(f"Checkpoint '{checkpoint_id}' was not found.")

        root = Path(record.workspace_root).resolve()
        for snapshot in record.files:
            target = self._resolve_under_root(root, snapshot.path)
            if snapshot.existed:
                if snapshot.content is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(target.write_text, snapshot.content, encoding=snapshot.encoding)
            else:
                if target.exists() and target.is_file():
                    await asyncio.to_thread(target.unlink)
        return record

    def get(self, checkpoint_id: str) -> CheckpointRecord | None:
        return self._store.get(checkpoint_id)

    def list_for_conversation(self, conversation_id: str, *, limit: int = 50) -> list[CheckpointRecord]:
        return self._store.list_for_conversation(conversation_id, limit=limit)

    @staticmethod
    def _paths_for_tool(tool_name: str, args: dict[str, Any]) -> list[str]:
        if tool_name in WRITE_TOOL_NAMES:
            value = args.get("file_path") or args.get("path") or ""
            return [str(value)] if str(value).strip() else []
        return []

    @staticmethod
    def _resolve_under_root(root: Path, raw_path: str) -> Path:
        candidate = Path(str(raw_path).strip())
        target = candidate if candidate.is_absolute() else root / candidate
        resolved = target.resolve()
        resolved.relative_to(root)
        return resolved

    @staticmethod
    def _git_rev_parse(root: Path, ref: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--verify", ref],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None

    @staticmethod
    def _git_stash_create(root: Path) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "stash", "create", "MiniCode checkpoint"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
