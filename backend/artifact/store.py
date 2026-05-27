"""
Artifact Store - session-scoped artifacts with short-lived disk persistence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING  # noqa: F401  (reserved for forward references in later tasks)

from backend.config import PROJECT_ROOT

# ── Storage paths and limits ─────────────────────────────────
ARTIFACT_DATA_DIR = PROJECT_ROOT / "data" / "artifacts"
DEFAULT_ARTIFACT_TTL_SECONDS = 86_400
DEFAULT_MAX_CACHE_ENTRIES = 128
DEFAULT_CLEANUP_INTERVAL_SECONDS = 600.0
MAX_CONTENT_LENGTH = 10_485_760
ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# ── On-disk file naming ──────────────────────────────────────
META_SIDECAR_SUFFIX = ".meta.json"
CONTENT_UNIT_SUFFIX = ".txt"
LEGACY_SUFFIX = ".json"

# ── Schema versioning ────────────────────────────────────────
META_SIDECAR_SCHEMA_VERSION = 1

# ── Module logger ────────────────────────────────────────────
logger = logging.getLogger(__name__)


@dataclass
class ArtifactMeta:
    artifact_id: str
    source: str
    type: str
    size: int
    preview: str


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

    def to_meta(self) -> ArtifactMeta:
        """Project the sidecar onto the public ``ArtifactMeta`` shape."""

        return ArtifactMeta(
            artifact_id=self.artifact_id,
            source=self.source,
            type=self.type,
            size=self.size,
            preview=self.preview,
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

        return cls(
            schema_version=schema_version,
            artifact_id=artifact_id,
            source=source,
            type=type_field,
            size=size,
            preview=preview,
            preview_lines=preview_lines,
            created_at=float(created_at),
        )


@dataclass(frozen=True)
class LegacyPayload:
    """Internal record for an artifact persisted by the prior implementation.

    Legacy files are single ``art_xxxxxxxx.json`` documents that bundle metadata
    and content together. They predate :class:`MetaSidecar` and have no
    ``schema_version``, ``preview_lines``, or ``created_at`` field — that's how
    the read path distinguishes them from new-format sidecars. New writes never
    produce this format; the reader path is read-only.
    """

    artifact_id: str
    source: str
    type: str
    size: int
    preview: str
    content: str

    def to_meta(self) -> ArtifactMeta:
        """Project the legacy payload onto the public ``ArtifactMeta`` shape."""

        return ArtifactMeta(
            artifact_id=self.artifact_id,
            source=self.source,
            type=self.type,
            size=self.size,
            preview=self.preview,
        )

    @classmethod
    def from_json_payload(cls, payload: dict[str, object]) -> LegacyPayload | None:
        """Parse a legacy payload from a decoded JSON object.

        Returns ``None`` whenever the input is not a dict or any of the six
        required fields (``artifact_id``, ``source``, ``type``, ``size``,
        ``preview``, ``content``) is missing or has the wrong type, so the read
        path can treat the file as unparseable. Logging of parse failures is
        the caller's responsibility.
        """

        if not isinstance(payload, dict):
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

        content = payload.get("content")
        if not isinstance(content, str):
            return None

        return cls(
            artifact_id=artifact_id,
            source=source,
            type=type_field,
            size=size,
            preview=preview,
            content=content,
        )


# ── Disk_Worker helpers (run via asyncio.to_thread) ──────────


def _write_split_payload(
    meta_path: Path,
    content_path: Path,
    sidecar: MetaSidecar,
    content: str,
) -> None:
    """Persist a split-layout artifact to disk.

    Writes the metadata sidecar JSON first, then the raw content unit. If the
    content write fails after the sidecar has already landed, the sidecar is
    rolled back via ``meta_path.unlink(missing_ok=True)`` and the original
    ``OSError`` is re-raised so the caller can surface the failure. A sidecar
    write failure has nothing to roll back and propagates directly.

    This is a pure-function ``Disk_Worker`` helper intended to run on a worker
    thread via :func:`asyncio.to_thread`; it must not be called from the event
    loop thread itself.
    """

    meta_path.write_text(
        json.dumps(sidecar.to_json_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        content_path.write_text(content, encoding="utf-8")
    except OSError:
        meta_path.unlink(missing_ok=True)
        raise


def _read_meta_sidecar(meta_path: Path) -> MetaSidecar | None:
    """Read a metadata sidecar from disk.

    Returns ``None`` when the file is missing (caller falls through to the legacy
    reader) or when any parse step fails. Failures are logged at ``WARNING`` with
    a ``reason`` field in ``{"io", "json", "missing-fields"}``. The file on disk
    is never modified, regardless of failure path.

    This is a pure-function ``Disk_Worker`` helper intended to run on a worker
    thread via :func:`asyncio.to_thread`.
    """

    try:
        raw = meta_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("artifact parse failed path=%s reason=%s", meta_path, "io")
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("artifact parse failed path=%s reason=%s", meta_path, "json")
        return None

    sidecar = MetaSidecar.from_json_payload(payload)
    if sidecar is None:
        # ``from_json_payload`` collapses both missing fields and wrong types into
        # ``None``; we surface that as a single "missing-fields" reason.
        logger.warning(
            "artifact parse failed path=%s reason=%s", meta_path, "missing-fields"
        )
        return None
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
    except OSError:
        logger.warning("artifact parse failed path=%s reason=%s", content_path, "io")
        return None


def _read_legacy_payload(legacy_path: Path) -> LegacyPayload | None:
    """Read a legacy combined-payload JSON file from disk.

    Returns ``None`` when the file is missing or any parse step fails. Failures
    are logged at ``WARNING`` with a ``reason`` field in
    ``{"io", "json", "missing-fields"}``. The file on disk is never modified,
    regardless of failure path.

    This is a pure-function ``Disk_Worker`` helper intended to run on a worker
    thread via :func:`asyncio.to_thread`.
    """

    try:
        raw = legacy_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("artifact parse failed path=%s reason=%s", legacy_path, "io")
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("artifact parse failed path=%s reason=%s", legacy_path, "json")
        return None

    legacy = LegacyPayload.from_json_payload(payload)
    if legacy is None:
        logger.warning(
            "artifact parse failed path=%s reason=%s", legacy_path, "missing-fields"
        )
        return None
    return legacy


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
    removed during this scan. Both new-format split layouts
    (``art_<id>.meta.json`` + ``art_<id>.txt``) and legacy single-file payloads
    (``art_<id>.json``) are considered.

    For new-format artifacts the sidecar's ``created_at`` is the expiry
    reference; for legacy files (which have no ``created_at`` field on disk) the
    file's ``mtime`` is the closest approximation, per design.md.

    Failure handling:

    * ``FileNotFoundError`` during stat or unlink (concurrent removal) is
      silently skipped — the artifact is excluded from the returned list and no
      log entry is emitted.
    * Any other ``OSError`` during unlink is logged at ``WARNING`` as
      ``"artifact cleanup failed id=%s error=%r"`` and the artifact is excluded
      from the returned list. The scan continues with the remaining files.
    * Sidecar parse failures (``_read_meta_sidecar`` returns ``None``) are
      already logged inside that helper and the artifact is silently skipped
      here.

    This is a pure-function ``Disk_Worker`` helper intended to run on a worker
    thread via :func:`asyncio.to_thread`.
    """

    removed: list[str] = []

    # New-format: ``art_<id>.meta.json`` paired with ``art_<id>.txt``.
    for meta_path in storage_dir.glob(f"art_*{META_SIDECAR_SUFFIX}"):
        artifact_id = meta_path.name.removesuffix(META_SIDECAR_SUFFIX)
        sidecar = _read_meta_sidecar(meta_path)
        if sidecar is None:
            # Parse helper already logged when applicable; skip the artifact.
            continue
        if not _is_expired(sidecar.created_at, now, ttl_seconds):
            continue
        try:
            (storage_dir / f"{artifact_id}{META_SIDECAR_SUFFIX}").unlink()
            # The content unit may legitimately be missing if the prior write
            # rolled back after the sidecar landed.
            (storage_dir / f"{artifact_id}{CONTENT_UNIT_SUFFIX}").unlink(missing_ok=True)
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("artifact cleanup failed id=%s error=%r", artifact_id, exc)
            continue
        removed.append(artifact_id)

    # Legacy-format: ``art_<id>.json`` (single combined payload). The glob also
    # matches new-format meta sidecars (``art_*.meta.json`` ends in ``.json``);
    # filter those out by suffix exactness.
    for legacy_path in storage_dir.glob(f"art_*{LEGACY_SUFFIX}"):
        if legacy_path.name.endswith(META_SIDECAR_SUFFIX):
            continue
        artifact_id = legacy_path.stem
        try:
            created_at = legacy_path.stat().st_mtime
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("artifact cleanup failed id=%s error=%r", artifact_id, exc)
            continue
        if not _is_expired(created_at, now, ttl_seconds):
            continue
        try:
            (storage_dir / f"{artifact_id}{LEGACY_SUFFIX}").unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("artifact cleanup failed id=%s error=%r", artifact_id, exc)
            continue
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
    """Session-scoped artifact store with bounded LRU cache and async disk I/O.

    Public methods are synchronous so legacy call sites work unchanged; disk
    writes are dispatched via :func:`asyncio.to_thread` when a running loop is
    available, with a synchronous fallback when it is not. Cleanup runs on a
    recurring background task scheduled lazily on the first save observed by a
    running loop.
    """

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
        self._pending_writes: set[asyncio.Task[None]] = set()
        self._cleanup_task: asyncio.Task[None] | None = None

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

    # ── Save ──────────────────────────────────────────────────

    def save(
        self,
        content: str,
        source: str,
        type: str = "text",
        preview_lines: int = 5,
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
        sidecar = MetaSidecar.from_save(
            artifact_id=artifact_id,
            source=source,
            type=type,
            content=content,
            preview_lines=preview_lines,
            created_at=created_at,
        )
        meta = sidecar.to_meta()

        # Synchronous in-memory update under a short-held lock. Eviction is
        # bounded to OrderedDict ops; we do not touch disk while the lock is
        # held.
        with self._lock:
            self._metadata_index[artifact_id] = meta
            self._preview_lines_index[artifact_id] = preview_lines
            self._content_cache.put(artifact_id, content)

        # Schedule the disk write off the calling thread when a loop is
        # available; otherwise fall back to a synchronous write so callers
        # constructing the store outside any loop (e.g. unit tests) still
        # observe the artifact on disk.
        meta_path = self._storage_dir / f"{artifact_id}{META_SIDECAR_SUFFIX}"
        content_path = self._storage_dir / f"{artifact_id}{CONTENT_UNIT_SUFFIX}"

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.info(
                "artifact deferred write id=%s reason=no-running-loop", artifact_id
            )
            try:
                _write_split_payload(meta_path, content_path, sidecar, content)
            except OSError as exc:
                logger.error(
                    "artifact write failed id=%s error=%r", artifact_id, exc
                )
        else:
            try:
                task = loop.create_task(
                    asyncio.to_thread(
                        _write_split_payload,
                        meta_path,
                        content_path,
                        sidecar,
                        content,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "artifact deferred write id=%s reason=%r", artifact_id, exc
                )
            else:
                self._pending_writes.add(task)

                def _on_done(
                    t: asyncio.Task[None], _aid: str = artifact_id
                ) -> None:
                    self._pending_writes.discard(t)
                    try:
                        exc = t.exception()
                    except asyncio.CancelledError:
                        return
                    if exc is not None:
                        logger.error(
                            "artifact write failed id=%s error=%r", _aid, exc
                        )

                task.add_done_callback(_on_done)

        self._ensure_cleanup_scheduled()
        return artifact_id

    # ── Read methods ──────────────────────────────────────────

    def get(self, artifact_id: str) -> str | None:
        if not _is_safe_artifact_id(artifact_id):
            return None
        with self._lock:
            cached = self._content_cache.get(artifact_id)
        if cached is not None:
            return cached

        meta_path = self._storage_dir / f"{artifact_id}{META_SIDECAR_SUFFIX}"
        content_path = self._storage_dir / f"{artifact_id}{CONTENT_UNIT_SUFFIX}"
        sidecar = _read_meta_sidecar(meta_path)
        if sidecar is not None:
            content = _read_content_unit(content_path)
            if content is not None:
                with self._lock:
                    self._metadata_index[artifact_id] = sidecar.to_meta()
                    self._preview_lines_index[artifact_id] = sidecar.preview_lines
                    self._content_cache.put(artifact_id, content)
                return content

        legacy_path = self._storage_dir / f"{artifact_id}{LEGACY_SUFFIX}"
        legacy = _read_legacy_payload(legacy_path)
        if legacy is not None:
            with self._lock:
                self._metadata_index[artifact_id] = legacy.to_meta()
                # Legacy payloads have no preview_lines field; default to 5,
                # which matches the historical save default and makes
                # ``get_preview`` short-circuit safe.
                self._preview_lines_index[artifact_id] = 5
                self._content_cache.put(artifact_id, legacy.content)
            return legacy.content

        return None

    def get_meta(self, artifact_id: str) -> ArtifactMeta | None:
        if not _is_safe_artifact_id(artifact_id):
            return None
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

        legacy_path = self._storage_dir / f"{artifact_id}{LEGACY_SUFFIX}"
        legacy = _read_legacy_payload(legacy_path)
        if legacy is not None:
            meta = legacy.to_meta()
            with self._lock:
                self._metadata_index[artifact_id] = meta
                self._preview_lines_index[artifact_id] = 5
            return meta

        return None

    def get_preview(self, artifact_id: str, lines: int = 5) -> str | None:
        if lines <= 0:
            return ""

        meta = self.get_meta(artifact_id)
        if meta is None:
            return None

        with self._lock:
            preview_lines_for_id = self._preview_lines_index.get(artifact_id, 5)

        if lines <= preview_lines_for_id:
            stripped = _strip_preview_overflow_suffix(meta.preview)
            return "\n".join(stripped.split("\n")[:lines])

        content = self.get(artifact_id)
        if content is None:
            return None
        return _build_preview(content, lines)

    def list_artifacts(self) -> list[ArtifactMeta]:
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

        # Pass 2: legacy single-file payloads. ``art_*.json`` also matches
        # ``art_*.meta.json`` so filter by exact suffix. Skip ids already added
        # by the sidecar pass; sidecar wins on conflict.
        for path in self._storage_dir.glob(f"art_*{LEGACY_SUFFIX}"):
            if path.name.endswith(META_SIDECAR_SUFFIX):
                continue
            artifact_id = path.stem
            if artifact_id in seen_ids or artifact_id in result_by_id:
                continue
            legacy = _read_legacy_payload(path)
            if legacy is None:
                continue
            try:
                created_at = path.stat().st_mtime
            except FileNotFoundError:
                continue
            with self._lock:
                self._metadata_index[artifact_id] = legacy.to_meta()
                self._preview_lines_index[artifact_id] = 5
            result_by_id[artifact_id] = (created_at, legacy.to_meta())

        # Sort by created_at ascending (stable) — seen_metas with key 0.0 stay
        # at the head; disk-discovered entries are interleaved by their actual
        # created_at / mtime.
        ordered = sorted(result_by_id.items(), key=lambda kv: kv[1][0])
        # Preserve preview_lines snapshot so the static analyzer does not
        # complain about unused locals; the dict was captured for symmetry with
        # the design's "snapshot under the lock" rule.
        del preview_lines_snapshot
        return [meta for _aid, (_ts, meta) in ordered]

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
        removed = await asyncio.to_thread(
            _scan_expired, self._storage_dir, self._ttl_seconds, time.time()
        )
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
                logger.warning(
                    "artifact cleanup loop iteration failed error=%r", exc
                )

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


def type_of(value: object) -> str:
    """Return ``type(value).__name__`` without shadowing the ``type`` parameter.

    ``ArtifactStore.save`` accepts ``type`` as a positional/keyword argument
    matching the prior public signature, which shadows the builtin inside the
    method body. This tiny helper sidesteps the shadowing without renaming the
    public parameter.
    """

    return type(value).__name__
