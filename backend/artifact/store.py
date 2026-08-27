"""
Artifact Store - session-scoped artifacts with short-lived disk persistence.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING  # noqa: F401  (reserved for forward references in later tasks)

from backend.config import DATA_ROOT
from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.owner_scope import (
    OwnerScope,
    grant_owner_scope,
    normalize_owner_scopes,
    owner_scope_matches,
    remove_conversation_scopes,
)

# ── Storage paths and limits ─────────────────────────────────
ARTIFACT_DATA_DIR = DATA_ROOT / "artifacts"
DEFAULT_ARTIFACT_TTL_SECONDS = 86_400
DEFAULT_MAX_CACHE_ENTRIES = 128
DEFAULT_CLEANUP_INTERVAL_SECONDS = 600.0
MAX_CONTENT_LENGTH = 10_485_760
ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# ── On-disk file naming ──────────────────────────────────────
META_SIDECAR_SUFFIX = ".meta.json"
CONTENT_UNIT_SUFFIX = ".txt"

# ── Schema versioning ────────────────────────────────────────
META_SIDECAR_SCHEMA_VERSION = 4

# ── Module logger ────────────────────────────────────────────
logger = logging.getLogger(__name__)


class ArtifactPersistenceError(RuntimeError):
    """Structured evidence that a durable artifact record is unreadable."""

    def __init__(self, path: Path, reason: str, detail: str = "") -> None:
        self.path = str(path)
        self.reason = reason
        self.detail = detail
        message = f"Artifact persistence failure ({reason}): {path}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


@dataclass
class ArtifactMeta:
    artifact_id: str
    source: str
    type: str
    size: int
    preview: str
    media_type: str = ""
    conversation_id: str = ""
    conversation_ids: tuple[str, ...] = ()
    workspace_root: str = ""
    owner_scopes: tuple[OwnerScope, ...] = ()


def _build_preview(content: str, preview_lines: int) -> str:
    """Build a preview string from ``content`` containing the first ``preview_lines`` lines.

    A non-positive ``preview_lines`` short-circuits to the empty string. When the
    content has more lines than requested, an overflow suffix is appended in the
    canonical ``"... (N lines total)"`` form.
    """

    if preview_lines <= 0:
        return ""
    lines = content.split("\n")
    preview = "\n".join(lines[:preview_lines])
    if len(lines) > preview_lines:
        preview += f"\n... ({len(lines)} lines total)"
    return preview


_OVERFLOW_SUFFIX_RE = re.compile(r"^\.\.\. \(\d+ lines total\)$")


def _strip_preview_overflow_suffix(preview: str) -> str:
    """Remove the canonical ``"... (N lines total)"`` overflow suffix line if present.

    Idempotent: stripping a preview that has no suffix returns it unchanged.
    Used by ``get_preview`` when serving a request that fits inside the cached
    preview, so the suffix line is never returned to the caller.
    """

    if not preview:
        return preview
    parts = preview.split("\n")
    if parts and _OVERFLOW_SUFFIX_RE.match(parts[-1]):
        parts.pop()
    return "\n".join(parts)


def _is_safe_artifact_id(artifact_id: str) -> bool:
    return bool(ARTIFACT_ID_RE.fullmatch(str(artifact_id or "").strip()))


@dataclass(frozen=True)
class MetaSidecar:
    """Internal on-disk metadata record for a single artifact.

    The sidecar is the durable record persisted next to the content unit
    (``art_xxxxxxxx.meta.json`` + ``art_xxxxxxxx.txt``). Public callers only see
    the projected :class:`ArtifactMeta` produced by :meth:`to_meta`.
    """

    schema_version: int
    artifact_id: str
    source: str
    type: str
    size: int
    preview: str
    preview_lines: int
    created_at: float
    media_type: str = ""
    conversation_id: str = ""
    conversation_ids: tuple[str, ...] = ()
    workspace_root: str = ""
    owner_scopes: tuple[OwnerScope, ...] = ()

    def to_meta(self) -> ArtifactMeta:
        """Project the sidecar onto the public ``ArtifactMeta`` shape."""

        return ArtifactMeta(
            artifact_id=self.artifact_id,
            source=self.source,
            type=self.type,
            size=self.size,
            preview=self.preview,
            media_type=self.media_type,
            conversation_id=self.conversation_id,
            conversation_ids=self.conversation_ids,
            workspace_root=self.workspace_root,
            owner_scopes=self.owner_scopes,
        )

    @classmethod
    def from_save(
        cls,
        *,
        artifact_id: str,
        source: str,
        type: str,
        content: str,
        preview_lines: int,
        created_at: float,
        media_type: str = "",
        conversation_id: str = "",
        workspace_root: str = "",
    ) -> MetaSidecar:
        """Build a sidecar from the inputs supplied to ``ArtifactStore.save``."""

        return cls(
            schema_version=META_SIDECAR_SCHEMA_VERSION,
            artifact_id=artifact_id,
            source=source,
            type=type,
            size=len(content),
            preview=_build_preview(content, preview_lines),
            preview_lines=preview_lines,
            created_at=created_at,
            media_type=media_type,
            conversation_id=conversation_id,
            conversation_ids=(conversation_id,) if conversation_id else (),
            workspace_root=workspace_root,
            owner_scopes=normalize_owner_scopes(
                None,
                conversation_id=conversation_id,
                workspace_root=workspace_root,
            ),
        )

    def to_json_payload(self) -> dict[str, object]:
        """Return the on-disk JSON shape documented in design.md "Sidecar schema"."""

        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "source": self.source,
            "type": self.type,
            "size": self.size,
            "preview": self.preview,
            "preview_lines": self.preview_lines,
            "created_at": self.created_at,
            "media_type": self.media_type,
            "conversation_id": self.conversation_id,
            "conversation_ids": list(self.conversation_ids),
            "workspace_root": self.workspace_root,
            "owner_scopes": [scope.to_json() for scope in self.owner_scopes],
        }

    @classmethod
    def from_json_payload(cls, payload: dict[str, object]) -> MetaSidecar | None:
        """Parse a sidecar from a decoded JSON payload.

        Returns ``None`` whenever any required field is missing or has the wrong
        type so the read path can use ``None`` as the parse-failure indicator.
        Logging of parse failures is the caller's responsibility.
        """

        if not isinstance(payload, dict):
            return None

        schema_version = payload.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            return None
        if schema_version != META_SIDECAR_SCHEMA_VERSION:
            return None

        artifact_id = payload.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            return None

        source = payload.get("source")
        if not isinstance(source, str):
            return None

        type_field = payload.get("type")
        if not isinstance(type_field, str):
            return None

        size = payload.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return None

        preview = payload.get("preview")
        if not isinstance(preview, str):
            return None

        preview_lines = payload.get("preview_lines")
        if (
            not isinstance(preview_lines, int)
            or isinstance(preview_lines, bool)
            or preview_lines < 1
        ):
            return None

        created_at = payload.get("created_at")
        if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
            return None
        if created_at <= 0:
            return None

        media_type = payload.get("media_type", "")
        if not isinstance(media_type, str):
            return None

        conversation_id = str(payload.get("conversation_id") or "")
        raw_conversation_ids = payload.get("conversation_ids")
        if raw_conversation_ids is None:
            conversation_ids = (conversation_id,) if conversation_id else ()
        elif not isinstance(raw_conversation_ids, list) or any(
            not isinstance(value, str) for value in raw_conversation_ids
        ):
            return None
        else:
            conversation_ids = tuple(dict.fromkeys(value for value in raw_conversation_ids if value))

        workspace_root = str(payload.get("workspace_root") or "")
        try:
            owner_scopes = normalize_owner_scopes(
                payload.get("owner_scopes"),
                conversation_id=conversation_id,
                conversation_ids=conversation_ids,
                workspace_root=workspace_root,
                strict=True,
            )
        except (TypeError, ValueError):
            return None
        return cls(
            schema_version=META_SIDECAR_SCHEMA_VERSION,
            artifact_id=artifact_id,
            source=source,
            type=type_field,
            size=size,
            preview=preview,
            preview_lines=preview_lines,
            created_at=float(created_at),
            media_type=media_type,
            conversation_id=conversation_id,
            conversation_ids=conversation_ids,
            workspace_root=workspace_root,
            owner_scopes=owner_scopes,
        )


# ── Disk_Worker helpers (run via asyncio.to_thread) ──────────


def _write_split_payload(
    meta_path: Path,
    content_path: Path,
    sidecar: MetaSidecar,
    content: str,
) -> None:
    """Persist a split-layout artifact to disk.

    Writes the content unit first, then publishes the metadata sidecar as the
    authoritative record. If the sidecar write fails, the orphan content unit
    is removed and the error propagates to the caller.

    This is a pure-function ``Disk_Worker`` helper intended to run on a worker
    thread via :func:`asyncio.to_thread`; it must not be called from the event
    loop thread itself.
    """

    with file_mutation_locks([meta_path, content_path]):
        atomic_write_text(content_path, content)
        try:
            atomic_write_text(
                meta_path,
                json.dumps(sidecar.to_json_payload(), ensure_ascii=False, indent=2),
            )
        except OSError:
            content_path.unlink(missing_ok=True)
            raise


def _read_meta_sidecar(meta_path: Path) -> MetaSidecar | None:
    """Read a metadata sidecar from disk.

    Returns ``None`` when the file is missing or when any parse step fails.
    Failures are logged at ``WARNING`` with
    a ``reason`` field in ``{"io", "json", "missing-fields"}``. The file on disk
    is never modified, regardless of failure path.

    This is a pure-function ``Disk_Worker`` helper intended to run on a worker
    thread via :func:`asyncio.to_thread`.
    """

    try:
        raw = meta_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ArtifactPersistenceError(meta_path, "io", str(exc)) from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArtifactPersistenceError(meta_path, "json", str(exc)) from exc

    sidecar = MetaSidecar.from_json_payload(payload)
    if sidecar is None:
        raise ArtifactPersistenceError(
            meta_path,
            "invalid_record",
            "metadata does not match the current artifact schema",
        )
    return sidecar


def _read_content_unit(content_path: Path) -> str | None:
    """Read a content unit from disk.

    Returns ``None`` when the file is missing. On any other ``OSError`` logs
    ``WARNING`` with reason ``"io"`` and returns ``None``. The file on disk is
    never modified.

    This is a pure-function ``Disk_Worker`` helper intended to run on a worker
    thread via :func:`asyncio.to_thread`.
    """

    try:
        return content_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ArtifactPersistenceError(content_path, "io", str(exc)) from exc


def _is_expired(created_at: float, now: float, ttl_seconds: int) -> bool:
    """Strict-greater-than TTL classifier used by both ``_scan_expired`` and tests.

    An artifact is expired when the elapsed time since its creation strictly
    exceeds the configured TTL (Requirement 7.2). Equality at the boundary is
    deliberately not expired.
    """

    return (now - created_at) > ttl_seconds


def _scan_expired(storage_dir: Path, ttl_seconds: int, now: float) -> list[str]:
    """Enumerate expired artifacts under ``storage_dir`` and unlink their files.

    Returns the list of ``artifact_id`` strings whose files were successfully
    removed during this scan. Artifacts use one split layout:
    ``art_<id>.meta.json`` plus ``art_<id>.txt``.

    Failure handling:

    * ``FileNotFoundError`` during stat or unlink (concurrent removal) is
      silently skipped — the artifact is excluded from the returned list and no
      log entry is emitted.
    * Any other persistence failure aborts the cleanup transaction with
      structured evidence; callers retain ``cleanup_pending``.

    This is a pure-function ``Disk_Worker`` helper intended to run on a worker
    thread via :func:`asyncio.to_thread`.
    """

    removed: list[str] = []

    for meta_path in storage_dir.glob(f"art_*{META_SIDECAR_SUFFIX}"):
        artifact_id = meta_path.name.removesuffix(META_SIDECAR_SUFFIX)
        content_path = storage_dir / f"{artifact_id}{CONTENT_UNIT_SUFFIX}"
        with file_mutation_locks([meta_path, content_path]):
            # Re-read and re-check under the same lock as deletion. A fresh
            # save or an owner-scope update must not be deleted based on a
            # stale pre-lock directory scan.
            sidecar = _read_meta_sidecar(meta_path)
            if sidecar is None:
                # Parse helper already logged when applicable; skip the artifact.
                continue
            if not _is_expired(sidecar.created_at, now, ttl_seconds):
                continue
            try:
                meta_path.unlink()
                # The content unit may legitimately be missing if the prior write
                # rolled back after the sidecar landed.
                content_path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ArtifactPersistenceError(
                    meta_path,
                    "cleanup_failed",
                    f"{type(exc).__name__}: {exc}",
                ) from exc
            removed.append(artifact_id)

    return removed


# ── Bounded LRU content cache ──────────────────────────────


class Content_Cache:
    """A bounded least-recently-used cache of artifact content strings.

    The cache is a thin wrapper around :class:`collections.OrderedDict` that
    enforces a maximum entry count. Hits are promoted to most-recently-used via
    ``move_to_end``; insertions that overflow the configured capacity evict the
    oldest entries one at a time until the invariant ``len <= max_entries``
    holds again. The eviction loop is expressed as ``while`` rather than ``if``
    so the invariant remains correct even if ``max_entries`` were changed
    externally; with the "insert then evict" pattern it runs at most once per
    :meth:`put` in practice.

    The class is intentionally **not** thread-safe on its own. Synchronization
    is the responsibility of the surrounding :class:`ArtifactStore`, which
    holds ``ArtifactStore._lock`` across all cache mutations.
    """

    def __init__(self, *, max_entries: int) -> None:
        if (
            not isinstance(max_entries, int)
            or isinstance(max_entries, bool)
            or not (1 <= max_entries <= 100_000)
        ):
            raise ValueError(f"invalid max_cache_entries: {max_entries!r}")
        self._max_entries = max_entries
        self._od: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str) -> str | None:
        """Return the cached value and promote ``key`` to MRU on hit; ``None`` on miss."""

        if key not in self._od:
            return None
        value = self._od[key]
        self._od.move_to_end(key)
        return value

    def put(self, key: str, value: str) -> list[str]:
        """Insert ``value`` at ``key`` as MRU and evict oldest entries on overflow.

        Re-inserting an existing key promotes it to MRU because
        ``OrderedDict`` does not move a key on plain assignment;
        ``move_to_end`` is what makes the promotion explicit.

        Returns the list of evicted keys (oldest first), which may be empty.
        """

        self._od[key] = value
        self._od.move_to_end(key)
        evicted: list[str] = []
        while len(self._od) > self._max_entries:
            evicted_key, _ = self._od.popitem(last=False)
            evicted.append(evicted_key)
        return evicted

    def pop(self, key: str) -> None:
        """Best-effort removal of ``key``; absent keys are a no-op."""

        self._od.pop(key, None)

    def clear(self) -> None:
        """Drop every entry from the cache."""

        self._od.clear()

    def __len__(self) -> int:
        return len(self._od)

    def __contains__(self, key: object) -> bool:
        return key in self._od

    def keys_in_lru_order(self) -> list[str]:
        """Return cached keys ordered from oldest (LRU) to newest (MRU)."""

        return list(self._od.keys())


class ArtifactStore:
    """Session-scoped artifact store with bounded LRU and durable writes."""

    def __init__(
        self,
        *,
        storage_dir: str | Path | None = None,
        ttl_seconds: int = DEFAULT_ARTIFACT_TTL_SECONDS,
        max_cache_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
        cleanup_interval_seconds: float = DEFAULT_CLEANUP_INTERVAL_SECONDS,
    ) -> None:
        # Validate scalar parameters BEFORE touching disk so an invalid call
        # never partially constructs the store. Validation order: ttl_seconds →
        # max_cache_entries → cleanup_interval_seconds → mkdir.
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or not (1 <= ttl_seconds <= 31_536_000)
        ):
            raise ValueError(f"invalid ttl_seconds: {ttl_seconds!r}")

        # ``Content_Cache.__init__`` raises ``ValueError`` for invalid capacity.
        content_cache = Content_Cache(max_entries=max_cache_entries)

        if (
            isinstance(cleanup_interval_seconds, bool)
            or not isinstance(cleanup_interval_seconds, (int, float))
            or cleanup_interval_seconds <= 0
        ):
            raise ValueError(
                f"invalid cleanup_interval_seconds: {cleanup_interval_seconds!r}"
            )

        # Resolve and create the storage directory. ``OSError`` from mkdir
        # propagates without further partial initialization.
        self._storage_dir = Path(storage_dir or ARTIFACT_DATA_DIR)
        self._storage_dir.mkdir(parents=True, exist_ok=True)

        self._ttl_seconds = ttl_seconds
        self._max_cache_entries = max_cache_entries
        self._cleanup_interval = cleanup_interval_seconds

        self._content_cache = content_cache
        self._metadata_index: dict[str, ArtifactMeta] = {}
        self._preview_lines_index: dict[str, int] = {}
        self._lock = threading.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._cleanup_pending = False
        self._cleanup_error = ""
        self._owner_context: ContextVar[tuple[str, str]] = ContextVar(
            f"artifact_owner_{id(self)}",
            default=("", ""),
        )

    def bind_owner(self, conversation_id: str, workspace_root: str = "") -> Token:
        return self._owner_context.set((str(conversation_id or ""), str(workspace_root or "")))

    def reset_owner(self, token: Token) -> None:
        self._owner_context.reset(token)

    @staticmethod
    def _owner_matches(meta: ArtifactMeta, conversation_id: str, workspace_root: str) -> bool:
        return owner_scope_matches(meta.owner_scopes, conversation_id, workspace_root)

    # ── Small accessors ───────────────────────────────────────

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._content_cache)

    def clear(self) -> None:
        with self._lock:
            self._content_cache.clear()
            self._metadata_index.clear()
            self._preview_lines_index.clear()

    def delete_for_conversation(self, conversation_id: str) -> int:
        """Delete durable tool artifacts owned by one conversation."""
        owner = str(conversation_id or "").strip()
        if not owner:
            return 0
        artifact_ids: list[str] = []
        for meta_path in self._storage_dir.glob(f"art_*{META_SIDECAR_SUFFIX}"):
            content_path = self._storage_dir / f"{meta_path.name.removesuffix(META_SIDECAR_SUFFIX)}{CONTENT_UNIT_SUFFIX}"
            with file_mutation_locks([meta_path, content_path]):
                # Re-read inside the mutation lock. A concurrent fork/share or
                # delete may have changed ownership after the directory scan.
                sidecar = _read_meta_sidecar(meta_path)
                if sidecar is None:
                    continue
                scopes = sidecar.owner_scopes
                if not any(scope.conversation_id == owner for scope in scopes):
                    continue
                remaining_scopes = remove_conversation_scopes(scopes, owner)
                if remaining_scopes:
                    remaining = tuple(dict.fromkeys(
                        scope.conversation_id for scope in remaining_scopes if scope.conversation_id
                    ))
                    updated = replace(
                        sidecar,
                        schema_version=META_SIDECAR_SCHEMA_VERSION,
                        conversation_id=remaining[0] if remaining else "",
                        conversation_ids=remaining,
                        workspace_root=remaining_scopes[0].workspace_root,
                        owner_scopes=remaining_scopes,
                    )
                    atomic_write_text(
                        meta_path,
                        json.dumps(updated.to_json_payload(), ensure_ascii=False, indent=2),
                    )
                    with self._lock:
                        self._metadata_index[updated.artifact_id] = updated.to_meta()
                    continue
                try:
                    meta_path.unlink()
                    content_path.unlink(missing_ok=True)
                except FileNotFoundError:
                    continue
                artifact_ids.append(sidecar.artifact_id)
        if artifact_ids:
            with self._lock:
                for artifact_id in artifact_ids:
                    self._content_cache.pop(artifact_id)
                    self._metadata_index.pop(artifact_id, None)
                    self._preview_lines_index.pop(artifact_id, None)
        return len(artifact_ids)

    def share_for_conversation(
        self,
        source_conversation_id: str,
        target_conversation_id: str,
        workspace_root: str | Path | None = None,
    ) -> int:
        """Grant a cloned/forked transcript access to immutable tool artifacts."""

        source = str(source_conversation_id or "").strip()
        target = str(target_conversation_id or "").strip()
        if not source or not target or source == target:
            return 0
        shared = 0
        for meta_path in self._storage_dir.glob(f"art_*{META_SIDECAR_SUFFIX}"):
            content_path = self._storage_dir / f"{meta_path.name.removesuffix(META_SIDECAR_SUFFIX)}{CONTENT_UNIT_SUFFIX}"
            with file_mutation_locks([meta_path, content_path]):
                sidecar = _read_meta_sidecar(meta_path)
                if sidecar is None:
                    continue
                scopes = sidecar.owner_scopes
                updated_scopes = grant_owner_scope(
                    scopes,
                    source_conversation_id=source,
                    target_conversation_id=target,
                    target_workspace_root=workspace_root,
                )
                if updated_scopes == scopes:
                    continue
                owners = tuple(dict.fromkeys(
                    scope.conversation_id for scope in updated_scopes if scope.conversation_id
                ))
                updated = replace(
                    sidecar,
                    schema_version=META_SIDECAR_SCHEMA_VERSION,
                    conversation_id=owners[0] if owners else "",
                    conversation_ids=owners,
                    workspace_root=updated_scopes[0].workspace_root,
                    owner_scopes=updated_scopes,
                )
                atomic_write_text(
                    meta_path,
                    json.dumps(updated.to_json_payload(), ensure_ascii=False, indent=2),
                )
                with self._lock:
                    self._metadata_index[updated.artifact_id] = updated.to_meta()
                shared += 1
        return shared

    # ── Save ──────────────────────────────────────────────────

    def save(
        self,
        content: str,
        source: str,
        type: str = "text",
        preview_lines: int = 5,
        conversation_id: str | None = None,
        workspace_root: str | Path | None = None,
        media_type: str = "",
    ) -> str:
        # Validate content BEFORE any cache mutation, index mutation, or disk
        # write so a rejection leaves all state untouched.
        if not isinstance(content, str) or len(content) > MAX_CONTENT_LENGTH:
            length: object
            if isinstance(content, (str, bytes, list)):
                length = len(content)
            else:
                length = "n/a"
            raise ValueError(
                f"invalid content: type={type_of(content)} length={length}"
            )

        artifact_id = f"art_{uuid.uuid4().hex[:8]}"
        created_at = time.time()
        normalized_media_type = str(media_type or "").split(";", 1)[0].strip().lower()
        if normalized_media_type and (
            len(normalized_media_type) > 127
            or re.fullmatch(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*", normalized_media_type) is None
        ):
            raise ValueError(f"invalid media_type: {media_type!r}")
        bound_conversation, bound_workspace = self._owner_context.get()
        owner_conversation = bound_conversation if conversation_id is None else str(conversation_id or "")
        owner_workspace = bound_workspace if workspace_root is None else str(workspace_root or "")
        sidecar = MetaSidecar.from_save(
            artifact_id=artifact_id,
            source=source,
            type=type,
            content=content,
            preview_lines=preview_lines,
            created_at=created_at,
            media_type=normalized_media_type,
            conversation_id=owner_conversation,
            workspace_root=owner_workspace,
        )
        meta = sidecar.to_meta()

        meta_path = self._storage_dir / f"{artifact_id}{META_SIDECAR_SUFFIX}"
        content_path = self._storage_dir / f"{artifact_id}{CONTENT_UNIT_SUFFIX}"
        _write_split_payload(meta_path, content_path, sidecar, content)

        # Publish to the in-memory indexes only after the durable record exists.
        with self._lock:
            self._metadata_index[artifact_id] = meta
            self._preview_lines_index[artifact_id] = preview_lines
            self._content_cache.put(artifact_id, content)

        self._ensure_cleanup_scheduled()
        return artifact_id

    # ── Read methods ──────────────────────────────────────────

    def get(
        self,
        artifact_id: str,
        *,
        conversation_id: str | None = None,
        workspace_root: str | Path | None = None,
    ) -> str | None:
        if not _is_safe_artifact_id(artifact_id):
            return None
        bound_conversation, bound_workspace = self._owner_context.get()
        owner_conversation = bound_conversation if conversation_id is None else str(conversation_id or "")
        owner_workspace = bound_workspace if workspace_root is None else str(workspace_root or "")
        meta_path = self._storage_dir / f"{artifact_id}{META_SIDECAR_SUFFIX}"
        content_path = self._storage_dir / f"{artifact_id}{CONTENT_UNIT_SUFFIX}"
        sidecar = _read_meta_sidecar(meta_path)
        if sidecar is not None:
            meta = sidecar.to_meta()
            if not self._owner_matches(
                meta,
                owner_conversation,
                owner_workspace,
            ):
                return None
            with self._lock:
                cached_meta = self._metadata_index.get(artifact_id)
                cached = self._content_cache.get(artifact_id)
            if cached is not None and cached_meta == meta:
                confirmed = _read_meta_sidecar(meta_path)
                if (
                    confirmed is not None
                    and confirmed == sidecar
                    and self._owner_matches(
                        confirmed.to_meta(), owner_conversation, owner_workspace
                    )
                ):
                    return cached
                with self._lock:
                    self._content_cache.pop(artifact_id)
            content = _read_content_unit(content_path)
            if content is not None:
                confirmed = _read_meta_sidecar(meta_path)
                if confirmed is None or confirmed != sidecar:
                    return None
                if not self._owner_matches(
                    confirmed.to_meta(), owner_conversation, owner_workspace
                ):
                    return None
                with self._lock:
                    self._metadata_index[artifact_id] = meta
                    self._preview_lines_index[artifact_id] = sidecar.preview_lines
                    self._content_cache.put(artifact_id, content)
                return content

        return None

    def get_meta(
        self,
        artifact_id: str,
        *,
        refresh: bool = False,
        conversation_id: str | None = None,
        workspace_root: str | Path | None = None,
    ) -> ArtifactMeta | None:
        bound_conversation, bound_workspace = self._owner_context.get()
        owner_conversation = bound_conversation if conversation_id is None else str(conversation_id or "")
        owner_workspace = bound_workspace if workspace_root is None else str(workspace_root or "")
        meta = self._get_meta_unchecked(
            artifact_id,
            refresh=True,
        )
        if meta is None or not self._owner_matches(meta, owner_conversation, owner_workspace):
            return None
        return meta

    def _get_meta_unchecked(self, artifact_id: str, *, refresh: bool = False) -> ArtifactMeta | None:
        if not _is_safe_artifact_id(artifact_id):
            return None
        if not refresh:
            with self._lock:
                cached = self._metadata_index.get(artifact_id)
            if cached is not None:
                return cached

        meta_path = self._storage_dir / f"{artifact_id}{META_SIDECAR_SUFFIX}"
        sidecar = _read_meta_sidecar(meta_path)
        if sidecar is not None:
            meta = sidecar.to_meta()
            with self._lock:
                self._metadata_index[artifact_id] = meta
                self._preview_lines_index[artifact_id] = sidecar.preview_lines
            return meta
        return None

    def get_preview(
        self,
        artifact_id: str,
        lines: int = 5,
        *,
        conversation_id: str | None = None,
        workspace_root: str | Path | None = None,
    ) -> str | None:
        if lines <= 0:
            return ""

        bound_conversation, bound_workspace = self._owner_context.get()
        owner_conversation = bound_conversation if conversation_id is None else str(conversation_id or "")
        owner_workspace = bound_workspace if workspace_root is None else str(workspace_root or "")
        meta = self.get_meta(
            artifact_id,
            refresh=bool(owner_conversation or owner_workspace),
            conversation_id=owner_conversation,
            workspace_root=owner_workspace,
        )
        if meta is None:
            return None

        with self._lock:
            preview_lines_for_id = self._preview_lines_index.get(artifact_id, 5)

        if lines <= preview_lines_for_id:
            stripped = _strip_preview_overflow_suffix(meta.preview)
            return "\n".join(stripped.split("\n")[:lines])

        content = self.get(
            artifact_id,
            conversation_id=owner_conversation,
            workspace_root=owner_workspace,
        )
        if content is None:
            return None
        return _build_preview(content, lines)

    def list_artifacts(
        self,
        *,
        conversation_id: str | None = None,
        workspace_root: str | Path | None = None,
    ) -> list[ArtifactMeta]:
        with self._lock:
            seen_metas = list(self._metadata_index.values())
            seen_ids = set(self._metadata_index.keys())
            preview_lines_snapshot = dict(self._preview_lines_index)

        # Map artifact_id → (created_at, meta). seen_metas keep their insertion
        # order at the head of the list because we cannot reconstruct
        # ``created_at`` without re-reading the sidecar; later sort is stable.
        result_by_id: dict[str, tuple[float, ArtifactMeta]] = {}
        for meta in seen_metas:
            # Use 0.0 as the in-memory ordering key so seen entries sort before
            # disk-discovered ones with real timestamps.
            result_by_id[meta.artifact_id] = (0.0, meta)

        # Pass 1: new-format sidecars.
        for path in self._storage_dir.glob(f"art_*{META_SIDECAR_SUFFIX}"):
            artifact_id = path.name.removesuffix(META_SIDECAR_SUFFIX)
            if artifact_id in seen_ids or artifact_id in result_by_id:
                continue
            sidecar = _read_meta_sidecar(path)
            if sidecar is None:
                continue
            with self._lock:
                self._metadata_index[artifact_id] = sidecar.to_meta()
                self._preview_lines_index[artifact_id] = sidecar.preview_lines
            result_by_id[artifact_id] = (sidecar.created_at, sidecar.to_meta())

        # Sort by created_at ascending (stable) — seen_metas with key 0.0 stay
        # at the head; disk-discovered entries are interleaved by their actual
        # created_at / mtime.
        ordered = sorted(result_by_id.items(), key=lambda kv: kv[1][0])
        # Preserve preview_lines snapshot so the static analyzer does not
        # complain about unused locals; the dict was captured for symmetry with
        # the design's "snapshot under the lock" rule.
        del preview_lines_snapshot
        bound_conversation, bound_workspace = self._owner_context.get()
        owner_conversation = bound_conversation if conversation_id is None else str(conversation_id or "")
        owner_workspace = bound_workspace if workspace_root is None else str(workspace_root or "")
        visible: list[ArtifactMeta] = []
        for artifact_id, (_ts, _meta) in ordered:
            current = self.get_meta(
                artifact_id,
                refresh=True,
                conversation_id=owner_conversation,
                workspace_root=owner_workspace,
            )
            if current is not None:
                visible.append(current)
        return visible

    # ── Cleanup ──────────────────────────────────────────────

    def cleanup_expired(self, *, now: float | None = None) -> int:
        current = time.time() if now is None else now
        # ``_scan_expired`` is bounded by directory size and is invoked rarely
        # from the public sync facade; running it on the calling thread is
        # acceptable per the design tradeoff. The recurring background task
        # uses ``asyncio.to_thread`` via ``_async_cleanup_once`` to keep the
        # loop responsive.
        removed_ids = _scan_expired(self._storage_dir, self._ttl_seconds, current)

        if removed_ids:
            with self._lock:
                for artifact_id in removed_ids:
                    self._content_cache.pop(artifact_id)
                    self._metadata_index.pop(artifact_id, None)
                    self._preview_lines_index.pop(artifact_id, None)

        # Re-register the recurring task in case a prior run completed or was
        # cancelled; this is a no-op when no loop is running.
        self._ensure_cleanup_scheduled()
        return len(removed_ids)

    async def _async_cleanup_once(self) -> None:
        try:
            removed = await asyncio.to_thread(
                _scan_expired, self._storage_dir, self._ttl_seconds, time.time()
            )
        except Exception as exc:
            self._cleanup_pending = True
            self._cleanup_error = f"{type(exc).__name__}: {exc}"
            raise
        self._cleanup_pending = False
        self._cleanup_error = ""
        if not removed:
            return
        with self._lock:
            for artifact_id in removed:
                self._content_cache.pop(artifact_id)
                self._metadata_index.pop(artifact_id, None)
                self._preview_lines_index.pop(artifact_id, None)

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._cleanup_interval)
                await self._async_cleanup_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("artifact cleanup pending error=%r", exc)
                return

    def cleanup_status(self) -> dict[str, object]:
        return {
            "cleanup_pending": self._cleanup_pending,
            "error": self._cleanup_error,
        }

    def _ensure_cleanup_scheduled(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop; the next save / cleanup_expired observed by a
            # loop will schedule the task.
            return
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = loop.create_task(
                self._cleanup_loop(),
                name=f"artifact-cleanup[{id(self):x}]",
            )

    def shutdown(self) -> None:
        """Cancel the recurring cleanup task if one is running.

        Idempotent and safe when no loop exists. Used by tests and may be
        called optionally from ``bootstrap/app.py`` during shutdown.
        """

        if self._cleanup_task is not None:
            self._cleanup_task.cancel()

    async def flush(self) -> None:
        """Session lifecycle fence; save() is durable when it returns."""
        if self._cleanup_pending:
            raise RuntimeError(self._cleanup_error or "Artifact cleanup is pending")
        return None


def type_of(value: object) -> str:
    """Return ``type(value).__name__`` without shadowing the ``type`` parameter.

    ``ArtifactStore.save`` accepts ``type`` as a positional/keyword argument
    matching the prior public signature, which shadows the builtin inside the
    method body. This tiny helper sidesteps the shadowing without renaming the
    public parameter.
    """

    return type(value).__name__
