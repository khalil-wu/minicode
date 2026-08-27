"""Versioned, provenance-aware MiniCode plugin store."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from filelock import FileLock

from .identity import is_valid_identifier, parse_plugin_id, parse_plugin_id_strict, plugin_id
from .layout import plugin_manifest_path


STORE_METADATA_SCHEMA_VERSION = 1
ACTIVE_SELECTOR_SCHEMA_VERSION = 1

_VERSION_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._+\-]*$",
)
_SEMVER_RE = re.compile(
    r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$",
)
_WINDOWS_RESERVED_SEGMENTS = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)
_SOURCE_REQUIRED_FIELDS: Mapping[str, tuple[str, ...]] = {
    "directory": ("path",),
    "file": ("path",),
    "git": ("url",),
    "github": ("repo",),
    "url": ("url",),
    "npm": ("package",),
    "settings": ("name",),
}
_SOURCE_STRING_FIELDS = frozenset(
    {
        "path",
        "url",
        "repo",
        "package",
        "name",
        "ref",
        "sha",
        "registry",
    }
)


class PluginStoreRecordError(ValueError):
    """A materialized store record failed its on-disk integrity checks."""


def _store_lock_path(root: Path) -> Path:
    return root.parent / f".{root.name}.mutation.lock"


def _synchronized_store_mutation(method):
    """Serialize store scans and mutations across sessions/processes.

    PluginStore is intentionally instantiated on demand by several services;
    an instance-local lock would not protect the shared version tree or the
    active selector.  The lock file is kept beside (not inside) the store so
    it remains available before the store directory is first created.
    """

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        self.root.parent.mkdir(parents=True, exist_ok=True)
        with self._mutation_lock:
            return method(self, *args, **kwargs)

    return wrapper


@dataclass(frozen=True)
class StoredPlugin:
    plugin_id: str
    marketplace: str
    name: str
    version: str
    path: Path
    source: Mapping[str, Any]
    active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STORE_METADATA_SCHEMA_VERSION,
            "id": self.plugin_id,
            "name": self.name,
            "marketplace": self.marketplace,
            "version": self.version,
            "path": str(self.path),
            "source": dict(self.source),
            "active": self.active,
        }


@dataclass(frozen=True)
class StagedPluginRemoval:
    original: Path
    staged: Path


class PluginStore:
    def __init__(self, root: Path | None = None, *, create: bool = False) -> None:
        from backend.config import STATE_ROOT
        self.root = Path(
            root or (STATE_ROOT / "extensions" / "plugins" / "store")
        ).expanduser().resolve()
        self._mutation_lock = FileLock(str(_store_lock_path(self.root)), timeout=60)
        if create:
            self.root.mkdir(parents=True, exist_ok=True)

    def version_path(self, marketplace: str, name: str, version: str) -> Path:
        market_segment = _safe_segment(marketplace)
        name_segment = _safe_segment(name)
        version_segment = _safe_version_segment(version or "local")
        return self.root / market_segment / name_segment / version_segment

    @_synchronized_store_mutation
    def materialize(
        self,
        source_dir: Path,
        *,
        name: str,
        marketplace: str = "local",
        version: str = "local",
        source: Mapping[str, Any] | None = None,
        activate: bool = True,
        overwrite: bool = False,
    ) -> StoredPlugin:
        raw_source_dir = Path(source_dir).expanduser()
        if raw_source_dir.is_symlink():
            raise ValueError("plugin source directory cannot be a symbolic link")
        source_dir = raw_source_dir.resolve()
        if not source_dir.is_dir():
            raise ValueError("plugin source directory does not exist")
        if not is_valid_identifier(name, marketplace):
            raise ValueError("plugin name or marketplace is invalid")
        try:
            parse_plugin_id_strict(f"{name}@{marketplace}")
        except ValueError as exc:
            raise ValueError(f"plugin name or marketplace is invalid: {exc}") from exc
        clean_version = _safe_version_segment(version or "local")
        source_descriptor = _validate_source_descriptor(
            source if source is not None else {
                "source": "directory",
                "path": str(source_dir),
            },
            expected_marketplace=marketplace,
        )
        if any(path.is_symlink() for path in source_dir.rglob("*")):
            raise ValueError("plugin source cannot contain symbolic links")
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.version_path(marketplace, name, clean_version)
        if _lexists(target) and not overwrite:
            if target.is_symlink() or not target.is_dir():
                raise PluginStoreRecordError(f"existing plugin store target is unsafe: {target}")
            existing = self._read_record(
                target,
                name,
                marketplace,
                clean_version,
                source_descriptor,
                False,
                strict=True,
            )
            if activate and not existing.active:
                self.activate(existing.plugin_id, existing.version)
                existing = self._read_record(
                    target,
                    name,
                    marketplace,
                    clean_version,
                    source_descriptor,
                    True,
                    strict=True,
                )
            return existing
        _ensure_safe_store_parent(self.root, target.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.parent / f".{target.name}.{uuid4().hex}.tmp"
        backup = target.parent / f".{target.name}.{uuid4().hex}.backup"
        target_replaced = False
        backup_created = False
        try:
            shutil.copytree(source_dir, temp, symlinks=False)
            if _lexists(target):
                if target.is_symlink() or not target.is_dir():
                    raise PluginStoreRecordError(f"existing plugin store target is unsafe: {target}")
                target.replace(backup)
                backup_created = True
            temp.replace(target)
            record = StoredPlugin(
                plugin_id=plugin_id(name, marketplace),
                marketplace=marketplace,
                name=name,
                version=clean_version,
                path=target,
                source=source_descriptor,
                active=bool(activate),
            )
            target_replaced = True
            self._write_metadata(target, record)
            if activate:
                self.activate(record.plugin_id, record.version)
                record = StoredPlugin(
                    plugin_id=record.plugin_id,
                    marketplace=record.marketplace,
                    name=record.name,
                    version=record.version,
                    path=record.path,
                    source=record.source,
                    active=True,
                )
            if backup_created and backup.exists():
                shutil.rmtree(backup)
            return record
        except Exception:
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)
            if target_replaced and target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if backup_created and backup.exists() and not target.exists():
                backup.replace(target)
            raise

    @_synchronized_store_mutation
    def activate(self, plugin_id: str, version: str) -> None:
        try:
            parsed = parse_plugin_id_strict(plugin_id)
        except ValueError as exc:
            raise ValueError(f"plugin id is invalid: {exc}") from exc
        marketplace, name = parsed.marketplace, parsed.name
        if not is_valid_identifier(name, marketplace):
            raise ValueError("plugin id is invalid")
        if parsed.constraint:
            raise ValueError("plugin id must not contain a version constraint")
        clean_version = _safe_version_segment(version)
        base = self.version_path(marketplace, name, clean_version).parent
        target = base / clean_version
        self._assert_safe_record_path(target, name, marketplace, clean_version)
        if not target.is_dir():
            raise FileNotFoundError(str(target))
        # Do not activate a directory whose metadata is malformed or whose
        # identity is not derived from its physical path.
        self._read_record(
            target,
            name,
            marketplace,
            clean_version,
            {},
            False,
            strict=True,
        )
        payload = {
            "schema_version": ACTIVE_SELECTOR_SCHEMA_VERSION,
            "id": parsed.id,
            "version": clean_version,
            "path": str(target),
        }
        self._atomic_json(base / "active.json", payload)

    @_synchronized_store_mutation
    def active(self, plugin_id: str) -> StoredPlugin | None:
        try:
            parsed = parse_plugin_id_strict(plugin_id)
        except ValueError:
            return None
        marketplace, name = parsed.marketplace, parsed.name
        if parsed.constraint or not is_valid_identifier(name, marketplace):
            return None
        base = self.root / _safe_segment(marketplace) / _safe_segment(name)
        if not self._is_safe_directory(base):
            return None
        selected = self._selected_record(base, name, marketplace, repair=True)
        return selected

    @_synchronized_store_mutation
    def list(self, marketplace: str | None = None) -> list[StoredPlugin]:
        if self.root.is_symlink() or not self.root.is_dir():
            return []
        roots = (
            [self.root / _safe_segment(marketplace)]
            if marketplace
            else [p for p in self.root.iterdir() if p.is_dir() and not p.is_symlink()]
        )
        result: list[StoredPlugin] = []
        for market_root in roots:
            if not self._is_safe_directory(market_root):
                continue
            market_name = market_root.name
            if not _is_safe_segment(market_name) or market_name.casefold() in {".removals"}:
                continue
            for name_root in market_root.iterdir():
                if not self._is_safe_directory(name_root) or not _is_safe_segment(name_root.name):
                    continue
                selected = self._selected_record(
                    name_root,
                    name_root.name,
                    market_name,
                    repair=True,
                )
                selected_version = selected.version if selected is not None else ""
                for version_root in name_root.iterdir():
                    if (
                        not self._is_safe_directory(version_root)
                        or version_root.name.startswith(".")
                        or not _is_safe_version_segment(version_root.name)
                    ):
                        continue
                    record = self._read_record(
                        version_root,
                        name_root.name,
                        market_name,
                        version_root.name,
                        {},
                        version_root.name == selected_version,
                        strict=False,
                    )
                    if record is not None:
                        result.append(record)
        return sorted(
            result,
            key=lambda item: (
                item.marketplace.casefold(),
                item.name.casefold(),
                _version_sort_key(item.version),
            ),
        )

    @_synchronized_store_mutation
    def reconcile(self) -> dict[str, Any]:
        errors: list[str] = []
        repaired: list[str] = []
        valid_records = 0
        if not self.root.exists():
            return {"ok": True, "plugins": 0, "errors": [], "repaired": []}
        if self.root.is_symlink() or not self.root.is_dir():
            return {
                "ok": False,
                "plugins": 0,
                "errors": [f"unsafe store root: {self.root}"],
                "repaired": [],
            }
        for market_root in self.root.iterdir():
            if market_root.name.startswith("."):
                continue
            if market_root.is_symlink() or not market_root.is_dir():
                errors.append(f"unsafe marketplace path: {market_root}")
                continue
            if not _is_safe_segment(market_root.name):
                errors.append(f"invalid marketplace path segment: {market_root}")
                continue
            for name_root in market_root.iterdir():
                if name_root.name.startswith("."):
                    continue
                if name_root.is_symlink() or not name_root.is_dir():
                    errors.append(f"unsafe plugin path: {name_root}")
                    continue
                if not _is_safe_segment(name_root.name):
                    errors.append(f"invalid plugin path segment: {name_root}")
                    continue
                selector_path = name_root / "active.json"
                selector_present = _lexists(selector_path)
                selector_valid = _read_active_selector(selector_path) if selector_present else None
                before = self._active_selector_version(name_root)
                selected = self._selected_record(
                    name_root,
                    name_root.name,
                    market_root.name,
                    repair=True,
                )
                after = self._active_selector_version(name_root)
                if selected is not None and before != after:
                    repaired.append(str(selector_path))
                elif selected is None and (before or (selector_present and selector_valid is None)):
                    errors.append(f"stale or invalid active selector: {selector_path}")
                for version_root in name_root.iterdir():
                    if version_root.name.startswith("."):
                        continue
                    if version_root.is_symlink():
                        errors.append(f"unsafe version path: {version_root}")
                        continue
                    if not version_root.is_dir():
                        # Selector/metadata sidecars live directly under the
                        # plugin base and are not version roots.
                        continue
                    if not _is_safe_version_segment(version_root.name):
                        errors.append(f"invalid version path segment: {version_root}")
                        continue
                    try:
                        record = self._read_record(
                            version_root,
                            name_root.name,
                            market_root.name,
                            version_root.name,
                            {},
                            version_root.name == (selected.version if selected else ""),
                            strict=True,
                        )
                    except PluginStoreRecordError as exc:
                        errors.append(f"{version_root}: {exc}")
                        continue
                    if not plugin_manifest_path(record.path).is_file():
                        errors.append(f"missing manifest: {record.path}")
                        continue
                    valid_records += 1
        return {
            "ok": not errors,
            "plugins": valid_records,
            "errors": errors,
            "repaired": repaired,
        }

    @_synchronized_store_mutation
    def stage_remove(self, plugin_identity: str) -> StagedPluginRemoval | None:
        if self.root.is_symlink():
            raise PluginStoreRecordError(f"plugin store root is a symlink: {self.root}")
        try:
            parsed = parse_plugin_id_strict(plugin_identity)
        except ValueError as exc:
            raise ValueError(f"plugin id is invalid: {exc}") from exc
        if parsed.constraint:
            raise ValueError("plugin id must not contain a version constraint")
        if not is_valid_identifier(parsed.name, parsed.marketplace):
            raise ValueError("plugin id is invalid")
        original = self.root / _safe_segment(parsed.marketplace) / _safe_segment(parsed.name)
        if not original.exists():
            return None
        if original.is_symlink() or not original.is_dir():
            raise PluginStoreRecordError(f"unsafe plugin store path: {original}")
        removals = self.root / ".removals"
        _ensure_safe_store_parent(self.root, removals)
        removals.mkdir(parents=True, exist_ok=True)
        staged = removals / f"{_safe_segment(parsed.marketplace)}-{_safe_segment(parsed.name)}-{uuid4().hex}"
        original.replace(staged)
        return StagedPluginRemoval(original=original, staged=staged)

    @staticmethod
    def rollback_remove(removal: StagedPluginRemoval | None) -> None:
        if removal is None:
            return
        root = removal.original.parents[1]
        root.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(_store_lock_path(root)), timeout=60):
            if not removal.staged.exists():
                return
            removal.original.parent.mkdir(parents=True, exist_ok=True)
            if removal.original.exists():
                raise FileExistsError(str(removal.original))
            removal.staged.replace(removal.original)

    @staticmethod
    def commit_remove(removal: StagedPluginRemoval | None) -> None:
        if removal is None:
            return
        root = removal.original.parents[1]
        root.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(_store_lock_path(root)), timeout=60):
            if removal.staged.exists():
                if removal.staged.is_symlink():
                    raise PluginStoreRecordError(
                        f"refusing to remove symlinked staged plugin: {removal.staged}"
                    )
                if removal.staged.is_dir():
                    shutil.rmtree(removal.staged)
                else:
                    removal.staged.unlink()

    def _read_record(
        self,
        path: Path,
        name: str,
        marketplace: str,
        version: str,
        source: Mapping[str, Any],
        active: bool,
        *,
        strict: bool,
    ) -> StoredPlugin | None:
        try:
            actual_name, actual_marketplace, actual_version = self._assert_safe_record_path(
                path,
                name,
                marketplace,
                version,
            )
            metadata_path = path / ".plugin-store.json"
            if metadata_path.is_symlink():
                raise PluginStoreRecordError(
                    f"plugin store metadata is a symlink: {metadata_path}"
                )
            payload = _read_json_object(metadata_path)
            _validate_store_metadata(
                payload,
                path=Path(path).absolute(),
                name=actual_name,
                marketplace=actual_marketplace,
                version=actual_version,
            )
            metadata_source = payload["source"]
            validated_source = _validate_source_descriptor(
                metadata_source,
                expected_marketplace=actual_marketplace,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, PluginStoreRecordError, ValueError) as exc:
            if strict:
                if isinstance(exc, PluginStoreRecordError):
                    raise
                raise PluginStoreRecordError(str(exc)) from exc
            return None
        return StoredPlugin(
            # Identity and version are always derived from the physical path;
            # metadata is only accepted after exact consistency validation.
            plugin_id=plugin_id(actual_name, actual_marketplace),
            marketplace=actual_marketplace,
            name=actual_name,
            version=actual_version,
            path=Path(path).absolute(),
            source=validated_source,
            active=bool(active),
        )

    def _assert_safe_record_path(
        self,
        path: Path,
        name: str,
        marketplace: str,
        version: str,
    ) -> tuple[str, str, str]:
        candidate = Path(path).expanduser().absolute()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise PluginStoreRecordError(
                f"plugin store path escapes root: {candidate}"
            ) from exc
        if len(relative.parts) != 3:
            raise PluginStoreRecordError(
                f"plugin store path must be <marketplace>/<plugin>/<version>: {candidate}"
            )
        actual_marketplace, actual_name, actual_version = relative.parts
        if not _is_safe_segment(actual_marketplace):
            raise PluginStoreRecordError(f"invalid marketplace path segment: {actual_marketplace}")
        if not _is_safe_segment(actual_name):
            raise PluginStoreRecordError(f"invalid plugin path segment: {actual_name}")
        if not _is_safe_version_segment(actual_version):
            raise PluginStoreRecordError(f"invalid version path segment: {actual_version}")
        if (actual_name, actual_marketplace, actual_version) != (
            str(name),
            str(marketplace),
            str(version),
        ):
            raise PluginStoreRecordError(
                "record arguments do not match physical store path"
            )
        if not is_valid_identifier(actual_name, actual_marketplace):
            raise PluginStoreRecordError(
                f"invalid plugin identity: {actual_name}@{actual_marketplace}"
            )
        if candidate.is_symlink():
            raise PluginStoreRecordError(f"plugin store version is a symlink: {candidate}")
        _ensure_no_symlink_components(self.root, candidate)
        if not candidate.is_dir():
            raise PluginStoreRecordError(f"plugin store version is not a directory: {candidate}")
        return actual_name, actual_marketplace, actual_version

    def _is_safe_directory(self, path: Path) -> bool:
        candidate = Path(path).expanduser().absolute()
        if candidate.is_symlink() or not candidate.is_dir():
            return False
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return False
        try:
            _ensure_no_symlink_components(self.root, candidate)
        except PluginStoreRecordError:
            return False
        return True

    def _active_selector_version(self, name_root: Path) -> str:
        selector = _read_active_selector(name_root / "active.json")
        return selector["version"] if selector is not None else ""

    def _selected_record(
        self,
        name_root: Path,
        name: str,
        marketplace: str,
        *,
        repair: bool,
    ) -> StoredPlugin | None:
        selector = _read_active_selector(name_root / "active.json")
        selected: StoredPlugin | None = None
        if selector is not None:
            version = selector["version"]
            candidate = name_root / version
            if candidate.is_dir() and not candidate.is_symlink():
                selected = self._read_record(
                    candidate,
                    name,
                    marketplace,
                    version,
                    {},
                    True,
                    strict=False,
                )
                if selected is not None and (
                    selector["id"] != selected.plugin_id
                    or not _same_lexical_path(selector["path"], selected.path)
                ):
                    selected = None
        if selected is not None:
            return selected

        candidates: list[StoredPlugin] = []
        if not name_root.is_dir() or name_root.is_symlink():
            return None
        for version_root in name_root.iterdir():
            if (
                version_root.name.startswith(".")
                or not version_root.is_dir()
                or version_root.is_symlink()
                or not _is_safe_version_segment(version_root.name)
            ):
                continue
            record = self._read_record(
                version_root,
                name,
                marketplace,
                version_root.name,
                {},
                False,
                strict=False,
            )
            if record is not None:
                candidates.append(record)
        if not candidates:
            return None
        selected = max(candidates, key=lambda item: _version_sort_key(item.version))
        if repair:
            self._write_active_selector(selected)
            selected = StoredPlugin(
                plugin_id=selected.plugin_id,
                marketplace=selected.marketplace,
                name=selected.name,
                version=selected.version,
                path=selected.path,
                source=selected.source,
                active=True,
            )
        return selected

    def _write_active_selector(self, record: StoredPlugin) -> None:
        base = record.path.parent
        self._atomic_json(
            base / "active.json",
            {
                "schema_version": ACTIVE_SELECTOR_SCHEMA_VERSION,
                "id": record.plugin_id,
                "version": record.version,
                "path": str(record.path.absolute()),
            },
        )

    @staticmethod
    def _write_metadata(path: Path, record: StoredPlugin) -> None:
        PluginStore._atomic_json(path / ".plugin-store.json", record.to_dict())

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _safe_segment(value: str) -> str:
    clean = str(value or "")
    if not _is_safe_segment(clean):
        raise ValueError("unsafe store path segment")
    return clean


def _safe_version_segment(value: str) -> str:
    clean = str(value or "")
    if not _is_safe_version_segment(clean):
        raise ValueError("unsafe plugin version path segment")
    return clean


def _is_safe_segment(value: str) -> bool:
    clean = str(value or "")
    if (
        not clean
        or clean in {".", ".."}
        or "\x00" in clean
        or any(char in clean for char in "/\\:")
        or clean[-1] in {".", " "}
        or clean.casefold().split(".", 1)[0] in _WINDOWS_RESERVED_SEGMENTS
    ):
        return False
    # Store identity segments deliberately use the same ASCII intersection as
    # PluginId, avoiding Unicode/case-folded path aliases across platforms.
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", clean))


def _is_safe_version_segment(value: str) -> bool:
    clean = str(value or "")
    if not _VERSION_RE.fullmatch(clean):
        return False
    if clean in {".", ".."} or clean[-1] in {".", " "}:
        return False
    if clean.casefold().split(".", 1)[0] in _WINDOWS_RESERVED_SEGMENTS:
        return False
    return True


def _lexists(path: Path) -> bool:
    """Return true for broken symlinks as well as ordinary filesystem entries."""

    return path.exists() or path.is_symlink()


def _ensure_no_symlink_components(root: Path, path: Path) -> None:
    root_absolute = Path(root).expanduser().absolute()
    path_absolute = Path(path).expanduser().absolute()
    try:
        relative = path_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise PluginStoreRecordError(
            f"path escapes plugin store root: {path_absolute}"
        ) from exc
    current = root_absolute
    if current.is_symlink():
        raise PluginStoreRecordError(f"plugin store root is a symlink: {root_absolute}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise PluginStoreRecordError(f"plugin store path contains symlink: {current}")


def _ensure_safe_store_parent(root: Path, parent: Path) -> None:
    parent_absolute = Path(parent).expanduser().absolute()
    root_absolute = Path(root).expanduser().absolute()
    try:
        parent_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise PluginStoreRecordError(
            f"plugin store parent escapes root: {parent_absolute}"
        ) from exc
    _ensure_no_symlink_components(root_absolute, parent_absolute)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PluginStoreRecordError(f"missing JSON file: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginStoreRecordError(f"invalid JSON file: {path}") from exc
    if not isinstance(payload, dict):
        raise PluginStoreRecordError(f"JSON file must contain an object: {path}")
    return payload


def _validate_store_metadata(
    payload: Mapping[str, Any],
    *,
    path: Path,
    name: str,
    marketplace: str,
    version: str,
) -> None:
    schema_version = payload.get("schema_version")
    if schema_version != STORE_METADATA_SCHEMA_VERSION or isinstance(schema_version, bool):
        raise PluginStoreRecordError(
            f"unsupported plugin store metadata schema at {path}"
        )
    expected_id = plugin_id(name, marketplace)
    for field, expected in (
        ("id", expected_id),
        ("name", name),
        ("marketplace", marketplace),
        ("version", version),
    ):
        value = payload.get(field)
        if not isinstance(value, str) or value != expected:
            raise PluginStoreRecordError(
                f"plugin store metadata {field!r} does not match physical path at {path}"
            )
    metadata_path = payload.get("path")
    if not isinstance(metadata_path, str) or not metadata_path.strip():
        raise PluginStoreRecordError(f"plugin store metadata path is invalid at {path}")
    if not _same_lexical_path(metadata_path, path):
        raise PluginStoreRecordError(
            f"plugin store metadata path does not match physical path at {path}"
        )
    if not isinstance(payload.get("active"), bool):
        raise PluginStoreRecordError(f"plugin store metadata active flag is invalid at {path}")
    if not isinstance(payload.get("source"), Mapping):
        raise PluginStoreRecordError(f"plugin store metadata source is invalid at {path}")


def _validate_source_descriptor(
    source: Mapping[str, Any],
    *,
    expected_marketplace: str | None = None,
) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        raise PluginStoreRecordError("plugin source descriptor must be an object")
    descriptor = dict(source)
    kind = descriptor.get("source")
    if not isinstance(kind, str) or kind not in _SOURCE_REQUIRED_FIELDS:
        raise PluginStoreRecordError("plugin source descriptor has an unsupported source kind")
    for field in _SOURCE_REQUIRED_FIELDS[kind]:
        value = descriptor.get(field)
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise PluginStoreRecordError(
                f"plugin source descriptor {field!r} is required for {kind}"
            )
    for field in _SOURCE_STRING_FIELDS:
        if field in descriptor and descriptor[field] is not None and not isinstance(descriptor[field], str):
            raise PluginStoreRecordError(
                f"plugin source descriptor {field!r} must be a string"
            )
    if kind in {"directory", "file"}:
        raw_path = str(descriptor["path"])
        if not _is_cross_platform_absolute(raw_path):
            raise PluginStoreRecordError(
                f"plugin source descriptor path must be absolute: {raw_path}"
            )
    if kind == "github" and not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        str(descriptor["repo"]),
    ):
        raise PluginStoreRecordError("plugin source descriptor github repo is invalid")
    if kind == "settings" and "plugins" in descriptor and not isinstance(
        descriptor["plugins"], (Mapping, list, tuple)
    ):
        raise PluginStoreRecordError(
            "plugin source descriptor settings.plugins must be an object or array"
        )
    if "marketplace" in descriptor:
        marketplace = descriptor["marketplace"]
        if not isinstance(marketplace, str) or not _is_safe_segment(marketplace):
            raise PluginStoreRecordError("plugin source descriptor marketplace is invalid")
        if expected_marketplace is not None and marketplace != expected_marketplace:
            raise PluginStoreRecordError(
                "plugin source descriptor marketplace does not match physical path"
            )
    return descriptor


def _is_cross_platform_absolute(value: str) -> bool:
    # ``Path.is_absolute`` follows the host platform.  Also recognize drive
    # and UNC forms when validating a Windows-origin descriptor on another OS.
    text = str(value or "")
    return Path(text).expanduser().is_absolute() or bool(
        re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", text)
    )


def _same_lexical_path(left: str | Path, right: str | Path) -> bool:
    left_text = os.path.normcase(str(Path(left).expanduser().absolute()))
    right_text = os.path.normcase(str(Path(right).expanduser().absolute()))
    return left_text == right_text


def _read_active_selector(path: Path) -> dict[str, str] | None:
    if Path(path).is_symlink():
        return None
    try:
        payload = _read_json_object(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, PluginStoreRecordError):
        return None
    schema_version = payload.get("schema_version")
    if schema_version != ACTIVE_SELECTOR_SCHEMA_VERSION or isinstance(schema_version, bool):
        return None
    identifier = payload.get("id")
    version = payload.get("version")
    selector_path = payload.get("path")
    if not isinstance(identifier, str) or not isinstance(version, str) or not isinstance(selector_path, str):
        return None
    if not version or not _is_safe_version_segment(version):
        return None
    try:
        parsed = parse_plugin_id_strict(identifier)
    except ValueError:
        return None
    if parsed.constraint or not is_valid_identifier(parsed.name, parsed.marketplace):
        return None
    if not selector_path.strip() or "\x00" in selector_path:
        return None
    return {"id": plugin_id(parsed.name, parsed.marketplace), "version": version, "path": selector_path}


def _version_sort_key(value: str) -> tuple[Any, ...]:
    """SemVer-like ordering with ``local`` sorted highest.

    SemVer-like releases use a lexical fallback. MiniCode ranks the explicit
    ``local`` version above releases so an unpackaged checkout always shadows
    a released copy of the same plugin.
    """

    if value == "local":
        return (3, 0, 0, 0, 0, (1, ""))
    match = _SEMVER_RE.fullmatch(value)
    if match:
        numeric = tuple(int(part or 0) for part in match.groups()[:4])
        raw_prerelease = match.group(5) or ""
        if raw_prerelease:
            identifiers: list[tuple[int, Any]] = []
            for token in raw_prerelease.split("."):
                if token.isdigit():
                    identifiers.append((0, int(token)))
                else:
                    identifiers.append((1, token.casefold()))
            pre_key: tuple[Any, ...] = (0, tuple(identifiers))
        else:
            pre_key = (1, "")
        return (2, *numeric, pre_key)
    return (1, 0, 0, 0, 0, (0, str(value).casefold()))
