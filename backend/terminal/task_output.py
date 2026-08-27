"""Durable output storage for owned background shell commands.

Background output is written continuously to a task file while callers read
only a bounded tail. The file hierarchy includes both the
desktop session and conversation owner so one chat cannot discover or delete
another chat's command output.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.agent.checkpoint import validate_storage_id
from backend.runtime_paths import agent_runtime_root


MAX_TASK_OUTPUT_BYTES = 5 * 1024 * 1024 * 1024
MAX_TASK_OUTPUT_BYTES_DISPLAY = "5GB"
_OUTPUT_SUFFIX = ".output"
_DISK_CAP_NOTICE = (
    f"\n[output truncated: exceeded {MAX_TASK_OUTPUT_BYTES_DISPLAY} disk cap]\n"
).encode("utf-8")


def _runtime_state_root(base_dir: Path | None = None) -> Path:
    return agent_runtime_root(base_dir)


def get_task_output_dir(
    session_id: str,
    conversation_id: str,
    base_dir: Path | None = None,
) -> Path:
    """Return the exact owner-scoped background-output directory."""

    clean_session_id = validate_storage_id(session_id, field_name="session_id")
    clean_conversation_id = validate_storage_id(
        conversation_id,
        field_name="conversation_id",
    )
    output_dir = (
        _runtime_state_root(base_dir)
        / "background_tasks"
        / clean_session_id
        / clean_conversation_id
        / "tasks"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def get_task_output_path(
    session_id: str,
    conversation_id: str,
    task_id: str,
    base_dir: Path | None = None,
) -> Path:
    clean_task_id = validate_storage_id(task_id, field_name="task_id")
    return get_task_output_dir(session_id, conversation_id, base_dir) / (
        clean_task_id + _OUTPUT_SUFFIX
    )


class DurableTaskOutput:
    """Single durable output file for one background command.

    The file is created exclusively before the command starts and kept open by
    the host process.  Sandbox code therefore cannot replace it with a symlink
    between streamed writes.  Appends are synchronous and non-yielding; the
    sandbox reader supplies small chunks and serializes each callback on the
    event loop.
    """

    def __init__(
        self,
        *,
        session_id: str,
        conversation_id: str,
        task_id: str,
        base_dir: Path | None = None,
    ) -> None:
        self.path = get_task_output_path(
            session_id,
            conversation_id,
            task_id,
            base_dir,
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        if os.name != "nt":
            flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        self._file = os.fdopen(descriptor, "wb", buffering=0)
        self._bytes_written = 0
        self._characters_written = 0
        self._capped = False
        self._closed = False

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    @property
    def characters_written(self) -> int:
        return self._characters_written

    @property
    def capped(self) -> bool:
        return self._capped

    def append(self, content: str) -> None:
        if not content or self._closed or self._capped:
            return
        encoded = content.encode("utf-8")
        self._characters_written += len(content)
        if self._bytes_written + len(encoded) > MAX_TASK_OUTPUT_BYTES:
            self._file.write(_DISK_CAP_NOTICE)
            self._bytes_written += len(_DISK_CAP_NOTICE)
            self._characters_written += len(_DISK_CAP_NOTICE.decode("utf-8"))
            self._capped = True
            return
        self._file.write(encoded)
        self._bytes_written += len(encoded)

    def flush(self) -> None:
        if self._closed:
            return
        self._file.flush()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._closed = True
            self._file.close()


def read_task_output_tail(path: str | Path, max_chars: int) -> tuple[str, bool]:
    """Read at most the required UTF-8 tail and report whether earlier text exists."""

    output_path = Path(path)
    limit = max(1, int(max_chars))
    size = output_path.stat().st_size
    if size <= 0:
        return "", False

    # The file is UTF-8 written by DurableTaskOutput.  Four bytes per requested
    # character plus one code point of overlap is enough to recover the tail
    # without loading a potentially multi-gigabyte task file.
    read_bytes = min(size, max(4096, limit * 4 + 4))
    with output_path.open("rb") as handle:
        handle.seek(size - read_bytes)
        raw = handle.read(read_bytes)
    decoded = raw.decode("utf-8", errors="ignore")
    if size == read_bytes and len(decoded) <= limit:
        return decoded, False
    if len(decoded) > limit:
        return decoded[-limit:], True
    # If a malformed or unexpectedly wide encoding ever reaches this path,
    # preserve the bounded read and truthfully mark the unseen prefix.
    return decoded, size > read_bytes


def format_task_output(path: str | Path, max_chars: int) -> tuple[str, bool]:
    """Project one durable output file into MiniCode's bounded tail format."""

    output_path = Path(path)
    limit = max(1, int(max_chars))
    content, truncated = read_task_output_tail(output_path, limit)
    if not truncated:
        return content, False

    header = f"[Truncated. Full output: {output_path}]\n\n"
    available = max(0, limit - len(header))
    tail = content[-available:] if available else ""
    return header + tail, True


def delete_task_output(path: str | Path) -> None:
    """Delete one already-resolved task output file, if it still exists."""

    try:
        Path(path).unlink()
    except FileNotFoundError:
        return


def cleanup_task_output_owner(
    session_id: str,
    conversation_id: str,
    base_dir: Path | None = None,
) -> None:
    """Remove only task-output files owned by one validated conversation."""

    clean_session_id = validate_storage_id(session_id, field_name="session_id")
    clean_conversation_id = validate_storage_id(
        conversation_id,
        field_name="conversation_id",
    )
    output_dir = (
        _runtime_state_root(base_dir)
        / "background_tasks"
        / clean_session_id
        / clean_conversation_id
        / "tasks"
    )
    if not output_dir.is_dir():
        return
    for candidate in output_dir.glob(f"*{_OUTPUT_SUFFIX}"):
        if candidate.is_file() and not candidate.is_symlink():
            candidate.unlink()
    try:
        output_dir.rmdir()
        output_dir.parent.rmdir()
    except OSError:
        # Unknown files or concurrently-created tasks keep the owner directory.
        pass
