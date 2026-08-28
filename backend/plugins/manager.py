"""Unified MiniCode plugin manager used by all runtime consumers."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from filelock import FileLock

from .identity import normalize_plugin_id, parse_plugin_id, plugin_id
from .layout import PLUGIN_MANIFEST_DIRECTORY, MARKETPLACE_MANIFEST_FILENAME
from .materializer import (
    MaterializationError,
    MaterializedSource,
    is_safe_marketplace_name,
    materialize_source,
    parse_marketplace_source,
    recover_materialization_artifacts,
)


def _effective_marketplace_policy(policy: Any | None) -> Any:
    """Return one current policy snapshot; ``None`` never means unrestricted."""

    if policy is not None:
        return policy
    from backend.plugins.policy import _plugin_policy_from_stack

    return _plugin_policy_from_stack()


@dataclass(frozen=True)
class PluginSnapshot:
    payload: Mapping[str, Any]

    @property
    def plugins(self) -> tuple[Mapping[str, Any], ...]:
        values = self.payload.get("plugins", ())
        return tuple(item for item in values if isinstance(item, Mapping))

    @property
    def enabled_plugins(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(item for item in self.plugins if bool(item.get("enabled")))

    @property
    def fingerprint(self) -> str:
        return str(self.payload.get("fingerprint") or "")

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


class PluginManager:
    """Single runtime inventory and lifecycle entry point."""

    def __init__(
        self,
        *,
        plugin_roots: Iterable[Path] | None = None,
        config_stack: Any | None = None,
    ) -> None:
        self.plugin_roots = tuple(Path(path) for path in plugin_roots) if plugin_roots is not None else None
        self.config_stack = config_stack
        self._snapshot: PluginSnapshot | None = None

    def snapshot(self, *, force_refresh: bool = False) -> PluginSnapshot:
        if self._snapshot is None or force_refresh:
            # Import lazily: the compatibility service imports this manager's
            # identity/dependency primitives and must not form an import cycle.
            from backend.services.plugin_settings_service import get_plugin_snapshot

            payload = get_plugin_snapshot(
                self.plugin_roots,
                config_stack=self.config_stack,
            )
            self._snapshot = PluginSnapshot(payload)
        return self._snapshot

    def enabled(self) -> tuple[Mapping[str, Any], ...]:
        return self.snapshot().enabled_plugins

    def resolve(self, mention: str, *, require_enabled: bool = True) -> Mapping[str, Any] | None:
        requested = parse_plugin_id(mention)
        requested_id = normalize_plugin_id(mention)
        candidates: list[Mapping[str, Any]] = []
        for item in self.snapshot().plugins:
            if require_enabled and not bool(item.get("enabled")):
                continue
            identity = plugin_id(
                str(item.get("id") or item.get("name") or ""),
                str(item.get("marketplace") or "local"),
            )
            if normalize_plugin_id(identity) == requested_id or (
                "@" not in str(mention)
                and str(item.get("name") or "").casefold() == requested.name.casefold()
            ):
                candidates.append(item)
        if len(candidates) != 1:
            return None
        return candidates[0]

    def enabled_roots(self) -> tuple[Path, ...]:
        roots: list[Path] = []
        seen: set[str] = set()
        for item in self.enabled():
            raw = str(item.get("path") or "").strip()
            if not raw:
                continue
            try:
                path = Path(raw).resolve()
            except OSError:
                path = Path(raw).absolute()
            key = os.path.normcase(str(path))
            if key not in seen and path.is_dir():
                seen.add(key)
                roots.append(path)
        return tuple(roots)

    def manifests(self) -> tuple[tuple[Mapping[str, Any], Path], ...]:
        result: list[tuple[Mapping[str, Any], Path]] = []
        for item in self.enabled():
            root = Path(str(item.get("path") or ""))
            for raw in item.get("manifest_paths", ()):
                path = Path(str(raw))
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(payload, Mapping):
                    result.append((payload, root))
        return tuple(result)

    def reconcile(self) -> dict[str, Any]:
        """Validate the materialized inventory without changing settings."""
        snapshot = self.snapshot(force_refresh=True)
        errors: list[dict[str, Any]] = []
        for item in snapshot.plugins:
            if item.get("load_errors"):
                errors.append({
                    "id": item.get("id", ""),
                    "errors": list(item.get("load_errors") or []),
                })
            path = Path(str(item.get("path") or ""))
            if not path.is_dir():
                errors.append({"id": item.get("id", ""), "errors": ["plugin path is missing"]})
        return {
            "ok": not errors,
            "plugins": len(snapshot.plugins),
            "enabled": len(snapshot.enabled_plugins),
            "errors": errors,
            "dependency_errors": list(snapshot.get("dependency_errors", ()) or ()),
        }

    async def install_local(
        self,
        source_path: str | Path,
        *,
        overwrite: bool = False,
        marketplace: str = "local",
        settings_file: Path | None = None,
        config_change_hook: Any | None = None,
    ) -> dict[str, Any]:
        from backend.config import SETTINGS_FILE
        from backend.hooks.runtime import run_config_change_hook
        from backend.plugins.policy import _plugin_policy_from_stack
        from backend.services.plugin_settings_service import import_plugin_from_path

        policy = _plugin_policy_from_stack(self.config_stack)

        result = await import_plugin_from_path(
            source_path,
            overwrite=overwrite,
            marketplace=marketplace,
            settings_file=settings_file or SETTINGS_FILE,
            config_change_hook=config_change_hook or run_config_change_hook,
            _policy=policy,
        )
        self._snapshot = None
        return result

    async def install_marketplace_plugin(
        self,
        marketplace: str,
        plugin_name: str,
        *,
        overwrite: bool = False,
        settings_file: Path | None = None,
        config_change_hook: Any | None = None,
        refresh: bool = True,
    ) -> dict[str, Any]:
        """Install one plugin from a registered marketplace.

        Marketplace discovery/materialization is performed before the plugin
        import boundary.  This preserves MiniCode's declared-vs-materialized
        state model and ensures a remote source can never be treated as an
        enabled plugin merely because it appears in a catalog.
        """

        from backend.config import SETTINGS_FILE
        from backend.hooks.runtime import run_config_change_hook
        from backend.services.plugin_settings_service import import_plugin_from_path

        registry = MarketplaceRegistry()
        from backend.plugins.policy import _plugin_policy_from_stack

        policy = _plugin_policy_from_stack(self.config_stack)
        if refresh:
            await asyncio.to_thread(registry.refresh, marketplace, policy=policy)
        plugin, path = await asyncio.to_thread(
            registry.materialize_plugin,
            marketplace,
            plugin_name,
            policy=policy,
        )
        marketplace_record = await asyncio.to_thread(
            registry.get,
            marketplace,
            policy=policy,
        )
        if not isinstance(marketplace_record, Mapping):
            raise MaterializationError("marketplace disappeared during install")
        result = await import_plugin_from_path(
            path,
            overwrite=overwrite,
            marketplace=marketplace,
            settings_file=settings_file or SETTINGS_FILE,
            config_change_hook=config_change_hook or run_config_change_hook,
            _policy=policy,
            _trusted_marketplace=True,
            _marketplace_source_descriptor=(
                marketplace_record.get("source")
                if isinstance(marketplace_record.get("source"), Mapping)
                else None
            ),
        )
        imported = result.get("imported") if isinstance(result, Mapping) else None
        if isinstance(imported, Mapping):
            result = {
                **result,
                "imported": {
                    **dict(imported),
                    "marketplace_plugin": plugin,
                    "marketplace_source": registry.get(marketplace, policy=policy),
                },
            }
        self._snapshot = None
        return result

    async def update_local(
        self,
        source_path: str | Path,
        *,
        marketplace: str = "local",
        settings_file: Path | None = None,
        config_change_hook: Any | None = None,
    ) -> dict[str, Any]:
        return await self.install_local(
            source_path,
            overwrite=True,
            marketplace=marketplace,
            settings_file=settings_file,
            config_change_hook=config_change_hook,
        )

    async def uninstall(
        self,
        identity: str,
        *,
        settings_file: Path | None = None,
        config_change_hook: Any | None = None,
    ) -> dict[str, Any]:
        from backend.config import SETTINGS_FILE
        from backend.hooks.runtime import run_config_change_hook
        from backend.services.plugin_settings_service import remove_plugin

        result = await remove_plugin(
            identity,
            settings_file=settings_file or SETTINGS_FILE,
            config_change_hook=config_change_hook or run_config_change_hook,
        )
        self._snapshot = None
        return result


def get_plugin_manager(*, config_stack: Any | None = None) -> PluginManager:
    """Construct a turn-scoped manager; callers own its snapshot lifetime."""

    return PluginManager(config_stack=config_stack)


def _registry_path() -> Path:
    explicit = os.environ.get("MINICODE_PLUGIN_REGISTRY", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    from backend.config import STATE_ROOT
    return STATE_ROOT / "extensions" / "plugins" / "marketplaces.json"


def _source_fingerprint(source: Mapping[str, Any]) -> str:
    return json.dumps(dict(source), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _read_marketplace_plugins(root: Path, marketplace: str) -> list[dict[str, Any]]:
    """Read a MiniCode marketplace manifest without executing plugins."""

    candidates = tuple(root.joinpath(*layout) for layout in _MARKETPLACE_MANIFEST_LAYOUTS)
    manifest_path = next((path for path in candidates if path.is_file()), None)
    if manifest_path is None:
        raise MaterializationError(f"marketplace manifest not found under {root}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"invalid marketplace manifest: {manifest_path}") from exc
    if not isinstance(payload, Mapping):
        raise MaterializationError("marketplace manifest must be an object")
    declared_name = str(payload.get("name") or "").strip()
    if not declared_name or not is_safe_marketplace_name(declared_name):
        raise MaterializationError("marketplace manifest must declare a safe name")
    if declared_name.casefold() != str(marketplace or "").strip().casefold():
        raise MaterializationError(
            f"marketplace manifest name '{declared_name}' does not match registered marketplace '{marketplace}'"
        )
    raw_plugins = payload.get("plugins")
    if not isinstance(raw_plugins, list):
        raise MaterializationError("marketplace manifest plugins must be an array")
    # Local plugin paths are owned by the marketplace root rather than the
    # manifest metadata directory. Keep that root explicit through install.
    marketplace_root = _marketplace_root_for_manifest(manifest_path, root)

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_plugins:
        if not isinstance(raw, Mapping):
            continue
        plugin_name = str(raw.get("name") or raw.get("id") or "").strip()
        if not plugin_name or not is_safe_marketplace_name(plugin_name):
            continue
        key = plugin_name.casefold()
        if key in seen:
            continue
        seen.add(key)
        item = dict(raw)
        item["name"] = plugin_name
        item["id"] = plugin_id(plugin_name, marketplace)

        # MiniCode deserializes policy enums as part of the root manifest.  An
        # unknown policy is therefore a root validation failure, not a plugin
        # that can coexist in a ready snapshot.
        item["policy"] = _normalize_marketplace_plugin_policy(raw.get("policy"))

        source = item.get("source")
        if isinstance(source, str):
            source = {"source": "directory", "path": source}
        if isinstance(source, Mapping):
            try:
                _validate_marketplace_plugin_source_path(source, marketplace_root, manifest_path)
                parsed_source = parse_marketplace_source(source, base_dir=marketplace_root)
                item["source"] = {**dict(source), **parsed_source.to_dict()}
            except MaterializationError as exc:
                item["load_error"] = str(exc)
        if "path" in item and isinstance(item["path"], str):
            try:
                path = _resolve_local_plugin_path(
                    item["path"], marketplace_root, manifest_path
                )
                item["path"] = str(path)
                item["resolved_path"] = str(path)
            except MaterializationError as exc:
                item["load_error"] = str(exc)
        # A local source object is the canonical catalog representation.  A
        # top-level path is retained only for old MiniCode catalogs; normalize it
        # into the same resolved field so the installer cannot reinterpret it.
        source_payload = item.get("source")
        if isinstance(source_payload, Mapping) and str(source_payload.get("source") or "").casefold() in {"local", "directory"}:
            raw_local_path = str(source_payload.get("path") or "")
            if raw_local_path:
                try:
                    resolved = _resolve_local_plugin_path(raw_local_path, marketplace_root, manifest_path)
                    item["resolved_path"] = str(resolved)
                    item["path"] = str(resolved)
                except MaterializationError as exc:
                    item["load_error"] = str(exc)
        result.append(item)
    return result


_MARKETPLACE_MANIFEST_LAYOUTS: tuple[tuple[str, ...], ...] = (
    (PLUGIN_MANIFEST_DIRECTORY, MARKETPLACE_MANIFEST_FILENAME),
)


def _marketplace_root_for_manifest(manifest_path: Path, materialized_root: Path) -> Path:
    """Return the repository root represented by a marketplace manifest."""

    path = manifest_path.resolve()
    for layout in _MARKETPLACE_MANIFEST_LAYOUTS:
        if tuple(path.parts[-len(layout):]) == layout:
            root = path
            for _ in layout:
                root = root.parent
            return root.resolve()
    return materialized_root.resolve()


def _resolve_local_plugin_path(raw_path: str, marketplace_root: Path, manifest_path: Path) -> Path:
    value = str(raw_path or "").strip()
    if not value:
        raise MaterializationError("local plugin source path must not be empty")
    candidate_raw = Path(value).expanduser()
    if candidate_raw.is_absolute():
        candidate = candidate_raw.resolve()
    else:
        if any(part == ".." for part in candidate_raw.parts):
            raise MaterializationError("local plugin source path must stay within the marketplace root")
        if value not in {".", "./"} and not value.startswith("./"):
            raise MaterializationError("local plugin source path must start with './'")
        candidate = (marketplace_root / value.removeprefix("./")).resolve()
    try:
        candidate.relative_to(marketplace_root.resolve())
    except ValueError as exc:
        raise MaterializationError("local plugin source path must stay within the marketplace root") from exc
    return candidate


def _validate_marketplace_plugin_source_path(
    source: Mapping[str, Any], marketplace_root: Path, manifest_path: Path
) -> None:
    kind = str(source.get("source") or source.get("source_type") or "").strip().casefold()
    if kind in {"local", "directory", "file"}:
        raw_path = str(source.get("path") or "")
        if kind == "file":
            raise MaterializationError("local marketplace plugin source must be a directory")
        _resolve_local_plugin_path(raw_path, marketplace_root, manifest_path)
    elif kind == "git-subdir":
        subpath = str(source.get("path") or "").strip().removeprefix("./")
        if not subpath or any(part in {"..", ""} for part in Path(subpath).parts):
            raise MaterializationError("git plugin source path must stay within the repository root")


def _normalize_marketplace_plugin_policy(raw_policy: Any) -> dict[str, Any]:
    """Normalize MiniCode marketplace policy fields and fail closed."""

    policy = raw_policy if isinstance(raw_policy, Mapping) else {}
    installation = str(policy.get("installation") or "AVAILABLE").strip().upper()
    authentication = str(policy.get("authentication") or "ON_INSTALL").strip().upper()
    if installation not in {"NOT_AVAILABLE", "AVAILABLE", "INSTALLED_BY_DEFAULT"}:
        raise MaterializationError(f"unsupported marketplace installation policy: {installation}")
    if authentication not in {"ON_INSTALL", "ON_USE"}:
        raise MaterializationError(f"unsupported marketplace authentication policy: {authentication}")
    products_raw = policy.get("products")
    products: list[str] | None
    if products_raw is None:
        products = None
    elif isinstance(products_raw, list):
        products = []
        for value in products_raw:
            product = str(value or "").strip().upper()
            if product != "MINICODE":
                raise MaterializationError(f"unsupported marketplace product: {product or '<empty>'}")
            if product not in products:
                products.append(product)
    else:
        raise MaterializationError("marketplace policy products must be an array")
    return {
        "installation": installation,
        "authentication": authentication,
        "products": products,
    }


def _marketplace_root_for_catalog(root: Path) -> Path:
    """Resolve the repository root for a materialized catalog directory."""

    for layout in _MARKETPLACE_MANIFEST_LAYOUTS:
        manifest = root.joinpath(*layout)
        if manifest.is_file():
            return _marketplace_root_for_manifest(manifest, root)
    return root.resolve()


def _current_product() -> str:
    """Return MiniCode's sole plugin host product identity."""

    return "MINICODE"


def _assert_marketplace_plugin_installable(plugin: Mapping[str, Any], *, product: str) -> None:
    policy = plugin.get("policy") if isinstance(plugin.get("policy"), Mapping) else {}
    # Catalogs persisted before policy normalization are still handled through
    # the same fail-closed parser.
    normalized = _normalize_marketplace_plugin_policy(policy)
    if normalized["installation"] == "NOT_AVAILABLE":
        raise MaterializationError(
            f"plugin '{plugin.get('name') or plugin.get('id') or ''}' is not available for install"
        )
    products = normalized["products"]
    if products is not None and (not products or product.upper() not in products):
        raise MaterializationError(
            f"plugin '{plugin.get('name') or plugin.get('id') or ''}' is not available for product {product.upper()}"
        )


def _is_verified_external_materialization(
    candidate: Path,
    *,
    cache_root: Path,
    declared_source: Any,
) -> bool:
    """Allow only our own provenance-bearing remote materializations."""

    try:
        candidate_resolved = candidate.resolve()
        cache_resolved = cache_root.resolve()
        candidate_resolved.relative_to(cache_resolved)
    except (OSError, ValueError):
        return False
    if not candidate_resolved.is_dir() or not isinstance(declared_source, Mapping):
        return False
    provenance_path = candidate_resolved / ".marketplace-source.json"
    try:
        payload = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    recorded = payload.get("source") if isinstance(payload, Mapping) else None
    if not isinstance(recorded, Mapping):
        return False
    try:
        declared = parse_marketplace_source(declared_source).to_dict()
        recorded_normalized = parse_marketplace_source(recorded).to_dict()
    except MaterializationError:
        return False
    return _source_fingerprint(declared) == _source_fingerprint(recorded_normalized)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


class MarketplaceRegistry:
    """Small durable marketplace registry modeled after MiniCode's store.

    Registry operations are metadata-only unless a caller explicitly asks to
    materialize a local directory/zip.  Remote URLs are recorded with source
    provenance and never executed implicitly during discovery.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or _registry_path()).expanduser()
        self._read_error: str = ""
        # MiniCode serializes marketplace sync across processes.  Registry
        # instances are short-lived here, so the lock must be path-based.
        lock_path = (self.path.parent / ".marketplaces.sync.lock").absolute()
        self._mutation_lock = FileLock(str(lock_path), timeout=60)

    def list(self, *, policy: Any | None = None) -> list[dict[str, Any]]:
        effective_policy = _effective_marketplace_policy(policy)
        effective_policy.assert_policy_valid()
        from backend.plugins.policy import PluginSettingsError

        payload = self._raw_records()
        result: list[dict[str, Any]] = []
        for item in payload:
            source = item.get("source") if isinstance(item.get("source"), Mapping) else {}
            try:
                effective_policy.assert_source_allowed(source)
            except PluginSettingsError:
                # MiniCode's list projection hides ordinary marketplaces that are
                # blocked by the current managed policy.  Policy compilation
                # errors were checked above and therefore cannot be swallowed.
                continue
            result.append(dict(item))
        return result

    def _raw_records(self) -> list[dict[str, Any]]:
        payload = self._read()
        return [dict(item) for item in payload if isinstance(item, Mapping)]

    def _get_raw(self, name: str) -> dict[str, Any] | None:
        key = str(name or "").strip().casefold()
        return next((item for item in self._raw_records() if str(item.get("name") or "").casefold() == key), None)

    def get(self, name: str, *, policy: Any | None = None) -> dict[str, Any] | None:
        key = str(name or "").strip().casefold()
        return next(
            (
                item
                for item in self.list(policy=policy)
                if str(item.get("name") or "").casefold() == key
            ),
            None,
        )

    def add(self, name: str, source: Mapping[str, Any], *, policy: Any | None = None) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._mutation_lock:
            return self._add_locked(name, source, policy=policy)

    def _add_locked(
        self,
        name: str,
        source: Mapping[str, Any],
        *,
        policy: Any | None = None,
    ) -> dict[str, Any]:
        clean_name = str(name or "").strip()
        if not clean_name or not is_safe_marketplace_name(clean_name):
            raise ValueError("marketplace name is required")
        effective_policy = _effective_marketplace_policy(policy)
        effective_policy.assert_policy_valid()
        descriptor = dict(source)
        if not descriptor.get("source"):
            raise ValueError("marketplace source is required")
        try:
            parsed_source = parse_marketplace_source(descriptor)
        except MaterializationError as exc:
            raise ValueError(str(exc)) from exc
        effective_policy.assert_source_allowed(parsed_source.to_dict())
        existing = self._get_raw(clean_name)
        if self._read_error:
            raise ValueError(f"Marketplace registry is corrupt: {self._read_error}")
        if existing is not None:
            old_source = existing.get("source") if isinstance(existing.get("source"), Mapping) else {}
            try:
                old_canonical = parse_marketplace_source(old_source).to_dict()
            except MaterializationError:
                old_canonical = dict(old_source)
            if _source_fingerprint(old_canonical) != _source_fingerprint(parsed_source.to_dict()):
                raise ValueError(
                    f"Marketplace '{clean_name}' is already registered with a different source"
                )
        record = {
            "name": clean_name,
            # Keep the caller's fields for UI round-tripping, but persist the
            # parser-normalized descriptor as the trusted runtime source.
            "source": {**descriptor, **parsed_source.to_dict()},
            "declared_source": descriptor,
            "provenance": {
                "registered_at": __import__("time").time(),
                "parser": "minicode-native-source-parser",
            },
            "status": "registered",
            "plugins": [],
        }
        records = [item for item in self._raw_records() if str(item.get("name") or "").casefold() != clean_name.casefold()]
        records.append(record)
        self._write(records)
        return record

    def remove(
        self,
        name: str,
        *,
        policy: Any | None = None,
    ) -> dict[str, Any] | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._mutation_lock:
            return self._remove_locked(name, policy=policy)

    def _remove_locked(
        self,
        name: str,
        *,
        policy: Any | None = None,
    ) -> dict[str, Any] | None:
        effective_policy = _effective_marketplace_policy(policy)
        effective_policy.assert_policy_valid()
        target = self._get_raw(name)
        if self._read_error:
            raise ValueError(f"Marketplace registry is corrupt: {self._read_error}")
        if target is None:
            return None
        target_name = str(target.get("name") or name).casefold()
        records = [
            item
            for item in self._raw_records()
            if str(item.get("name") or "").casefold() != target_name
        ]
        self._write(records)
        return target

    def refresh(self, name: str, *, policy: Any | None = None) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._mutation_lock:
            return self._refresh_locked(name, policy=policy)

    def _refresh_locked(self, name: str, *, policy: Any | None = None) -> dict[str, Any]:
        effective_policy = _effective_marketplace_policy(policy)
        effective_policy.assert_policy_valid()
        target = self._get_raw(name)
        if self._read_error:
            raise ValueError(f"Marketplace registry is corrupt: {self._read_error}")
        if target is None:
            raise KeyError(name)
        source = target.get("source") if isinstance(target.get("source"), Mapping) else {}
        parsed = parse_marketplace_source(source)
        effective_policy.assert_source_allowed(parsed.to_dict())
        marketplace_name = str(target.get("name") or name)
        materialized_root = self._materialized_root(marketplace_name)
        expected_record = _source_fingerprint(target)
        committed: dict[str, dict[str, Any]] = {}

        def validate_staged(root: Path) -> None:
            # Root-level validation must finish before the active cache moves.
            # The returned plugin paths point into staging and are deliberately
            # discarded; they are rebased by re-reading the activated root.
            _read_marketplace_plugins(root, marketplace_name)

        def persist_activated(materialized: MaterializedSource) -> None:
            records = self._raw_records()
            if self._read_error:
                raise ValueError(f"Marketplace registry is corrupt: {self._read_error}")
            current = next(
                (
                    item
                    for item in records
                    if str(item.get("name") or "").casefold() == marketplace_name.casefold()
                ),
                None,
            )
            if current is None or _source_fingerprint(current) != expected_record:
                raise MaterializationError(
                    f"marketplace '{marketplace_name}' changed while refresh was in flight"
                )

            # Re-read after activation so every resolved local path is rooted
            # at the durable destination rather than the temporary stage.
            plugins = _read_marketplace_plugins(materialized.path, marketplace_name)
            refreshed = {
                **current,
                "source": {
                    **dict(source),
                    "materialized_path": str(materialized.path),
                },
                "materialized_path": str(materialized.path),
                "provenance": {
                    **dict(current.get("provenance") or {}),
                    "materialized": materialized.to_dict(),
                },
                "plugins": plugins,
                "status": "ready",
                "error": "",
                "refreshed_at": __import__("time").time(),
            }
            next_records = [
                refreshed
                if str(item.get("name") or "").casefold() == marketplace_name.casefold()
                else item
                for item in records
            ]
            self._write(next_records)
            committed["record"] = refreshed

        materialize_source(
            parsed,
            materialized_root,
            validate=validate_staged,
            after_activate=persist_activated,
        )
        if "record" not in committed:  # pragma: no cover - callback contract guard
            raise MaterializationError("marketplace activation did not commit registry metadata")
        return committed["record"]

    def resolve_plugin(
        self,
        marketplace: str,
        plugin_name: str,
        *,
        policy: Any | None = None,
    ) -> dict[str, Any] | None:
        """Resolve one catalog entry by stable name, rejecting ambiguity."""

        target = self.get(marketplace, policy=policy)
        if target is None:
            return None
        requested = str(plugin_name or "").strip().casefold()
        matches = [
            dict(item)
            for item in target.get("plugins", [])
            if isinstance(item, Mapping)
            and str(item.get("name") or item.get("id") or "").strip().casefold() == requested
        ]
        return matches[0] if len(matches) == 1 else None

    def materialize_plugin(
        self,
        marketplace: str,
        plugin_name: str,
        *,
        policy: Any | None = None,
    ) -> tuple[dict[str, Any], Path]:
        """Return a validated plugin root from a refreshed marketplace."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._mutation_lock:
            return self._materialize_plugin_locked(
                marketplace,
                plugin_name,
                policy=policy,
            )

    def _materialize_plugin_locked(
        self,
        marketplace: str,
        plugin_name: str,
        *,
        policy: Any | None = None,
    ) -> tuple[dict[str, Any], Path]:

        effective_policy = _effective_marketplace_policy(policy)
        effective_policy.assert_policy_valid()
        plugin = self.resolve_plugin(marketplace, plugin_name, policy=effective_policy)
        if plugin is None:
            raise KeyError(f"plugin {plugin_name!r} was not found in marketplace {marketplace!r}")
        target = self.get(marketplace, policy=effective_policy)
        assert target is not None
        root = Path(str(target.get("materialized_path") or ""))
        if not root.is_dir():
            raise MaterializationError("marketplace is not materialized; refresh it first")
        if plugin.get("load_error"):
            raise MaterializationError(str(plugin["load_error"]))
        _assert_marketplace_plugin_installable(plugin, product=_current_product())
        raw_path = str(plugin.get("path") or "").strip()
        if raw_path:
            # Catalog paths are normalized during refresh.  Resolve again here
            # because the registry is durable and may have been edited between
            # refresh and install.
            candidate = Path(raw_path).expanduser().resolve()
        else:
            source = plugin.get("source") if isinstance(plugin.get("source"), Mapping) else {}
            parsed = parse_marketplace_source(source, base_dir=_marketplace_root_for_catalog(root))
            candidate_root = self._plugin_materialized_root(marketplace, plugin_name)
            candidate = materialize_source(parsed, candidate_root).path
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            # A separately materialized remote plugin is allowed only when its
            # destination is inside our own cache and its provenance records
            # the exact catalog source.  An arbitrary absolute catalog path is
            # never trusted merely because it happens to be a directory.
            if not _is_verified_external_materialization(
                candidate,
                cache_root=self.path.parent / ".marketplaces",
                declared_source=plugin.get("source"),
            ):
                raise MaterializationError("plugin source path escapes marketplace root")
        if not candidate.is_dir():
            raise MaterializationError(f"plugin source directory does not exist: {candidate}")
        return plugin, candidate

    def reconcile(self, *, policy: Any | None = None) -> dict[str, Any]:
        """Validate declared marketplace state without executing plugin code."""

        effective_policy = _effective_marketplace_policy(policy)
        effective_policy.assert_policy_valid()
        errors: list[dict[str, Any]] = []
        records = self.list(policy=effective_policy)
        for record in records:
            name = str(record.get("name") or "")
            source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
            try:
                parse_marketplace_source(source)
            except MaterializationError as exc:
                errors.append({"marketplace": name, "errors": [str(exc)]})
                continue
            materialized = str(record.get("materialized_path") or "")
            materialized_path = Path(materialized) if materialized else None
            if materialized_path is not None:
                try:
                    recover_materialization_artifacts(materialized_path)
                except OSError as exc:
                    errors.append({
                        "marketplace": name,
                        "errors": [f"materialized cache recovery failed: {exc}"],
                    })
            if materialized and not Path(materialized).is_dir():
                errors.append({"marketplace": name, "errors": ["materialized path is missing"]})
            elif materialized_path is not None:
                try:
                    manifest_candidates = [
                        materialized_path.joinpath(*layout)
                        for layout in _MARKETPLACE_MANIFEST_LAYOUTS
                    ]
                    manifest = next((path for path in manifest_candidates if path.is_file()), None)
                    if manifest is not None:
                        payload = json.loads(manifest.read_text(encoding="utf-8"))
                        declared = str(payload.get("name") or "").strip() if isinstance(payload, Mapping) else ""
                        if declared.casefold() != name.casefold():
                            errors.append({
                                "marketplace": name,
                                "errors": ["marketplace manifest name does not match registry name"],
                            })
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    errors.append({"marketplace": name, "errors": ["marketplace manifest is unreadable"]})
            for plugin in record.get("plugins", ()):
                if not isinstance(plugin, Mapping):
                    errors.append({"marketplace": name, "errors": ["invalid plugin catalog entry"]})
                    continue
                if plugin.get("load_error"):
                    errors.append({
                        "marketplace": name,
                        "plugin": plugin.get("name", ""),
                        "errors": [str(plugin["load_error"])],
                    })
                try:
                    _normalize_marketplace_plugin_policy(plugin.get("policy"))
                except MaterializationError as exc:
                    errors.append({
                        "marketplace": name,
                        "plugin": plugin.get("name", ""),
                        "errors": [str(exc)],
                    })
                raw_plugin_path = str(plugin.get("path") or "").strip()
                if raw_plugin_path and materialized_path is not None and materialized_path.is_dir():
                    candidate = Path(raw_plugin_path)
                    if not candidate.is_absolute():
                        candidate = (materialized_path / candidate).resolve()
                    if not _is_relative_to(candidate, materialized_path) and not _is_verified_external_materialization(
                        candidate,
                        cache_root=self.path.parent / ".marketplaces",
                        declared_source=plugin.get("source"),
                    ):
                        errors.append({
                            "marketplace": name,
                            "plugin": plugin.get("name", ""),
                            "errors": ["plugin source path escapes marketplace root"],
                        })
        return {
            "ok": not errors,
            "marketplaces": len(records),
            "errors": errors,
        }

    def _materialized_root(self, name: str) -> Path:
        safe_name = "".join(char if char.isalnum() or char in "._-" else "_" for char in str(name or "")).strip(".")
        if not safe_name or safe_name in {".", ".."}:
            raise MaterializationError("unsafe marketplace materialization name")
        root = self.path.parent / ".marketplaces" / safe_name
        root.parent.mkdir(parents=True, exist_ok=True)
        return root

    def _plugin_materialized_root(self, marketplace: str, plugin_name: str) -> Path:
        if not is_safe_marketplace_name(marketplace) or not is_safe_marketplace_name(plugin_name):
            raise MaterializationError("unsafe marketplace plugin cache identity")
        root = self.path.parent / ".marketplaces" / ".plugins" / marketplace / plugin_name
        root.parent.mkdir(parents=True, exist_ok=True)
        return root

    def _read(self) -> list[Any]:
        self._read_error = ""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            if self.path.exists():
                self._read_error = "invalid JSON"
            return []
        if not isinstance(payload, list):
            self._read_error = "registry root must be an array"
            return []
        valid: list[Any] = []
        for item in payload:
            if not isinstance(item, Mapping) or not str(item.get("name") or "").strip() or not isinstance(item.get("source"), Mapping):
                self._read_error = "registry contains an invalid marketplace record"
                continue
            valid.append(item)
        return valid

    def _write(self, records: list[Mapping[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(records, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
