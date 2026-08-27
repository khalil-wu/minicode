"""Pure plugin dependency graph operations.

The implementation follows Claude's two-phase semantics: installation walks a
transitive closure with cycle/cross-marketplace checks; loading performs a
fixed-point demotion without mutating persisted settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .identity import (
    PluginId,
    has_explicit_marketplace,
    parse_plugin_id,
    plugin_id,
    version_satisfies,
)


@dataclass(frozen=True)
class DependencyError:
    reason: str
    dependency: str = ""
    required_by: str = ""
    chain: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class DependencyResolution:
    ok: bool
    closure: tuple[PluginId, ...] = ()
    error: DependencyError | None = None


@dataclass(frozen=True)
class DependencyReference:
    """A dependency's canonical identity plus its optional version range.

    Claude's manifest grammar keeps the stable ``name@marketplace`` identity
    separate from an optional trailing ``@<range>`` selector.  Older
    MiniCode code returned only the identity, which made a dependency such as
    ``tooling@official@^2`` silently accept an incompatible installed copy.
    Keeping the selector explicit lets install-time and load-time resolution
    share one source of truth without changing the public ``qualify_dependency``
    return type.
    """

    identity: str
    constraint: str = ""


def parse_dependency_reference(raw: Any, declaring_id: str) -> DependencyReference:
    text = str(raw or "").strip()
    if not text:
        return DependencyReference("")
    parsed = parse_plugin_id(text)
    constraint = str(parsed.constraint or "").strip()
    if has_explicit_marketplace(text):
        return DependencyReference(parsed.id, constraint)
    declaring = parse_plugin_id(declaring_id)
    if declaring.marketplace and declaring.marketplace.casefold() != "inline":
        return DependencyReference(plugin_id(text, declaring.marketplace), constraint)
    # ``inline`` is a synthetic marketplace.  Bare dependencies remain
    # name-only so they can match an enabled plugin from any real marketplace.
    return DependencyReference(text, constraint)


def qualify_dependency(raw: Any, declaring_id: str) -> str:
    return parse_dependency_reference(raw, declaring_id).identity


async def resolve_dependency_closure(
    root_id: str,
    lookup: Callable[[str], Any],
    already_enabled: Iterable[str] = (),
    allowed_cross_marketplaces: Iterable[str] = (),
) -> DependencyResolution:
    """Resolve a dependency closure from an async or sync lookup callback."""

    import inspect

    root_reference = parse_dependency_reference(root_id, root_id)
    root = root_reference.identity or plugin_id(root_id)
    if not root:
        return DependencyResolution(False, error=DependencyError("invalid-root", message="root plugin id is empty"))
    root_marketplace = parse_plugin_id(root).marketplace.casefold()
    enabled = {plugin_id(item).casefold() for item in already_enabled if plugin_id(item)}
    allowed = {str(item).strip().casefold() for item in allowed_cross_marketplaces}
    closure: list[str] = []
    visited: set[str] = set()
    stack: list[str] = []

    async def call(value: str) -> Any:
        result = lookup(value)
        if inspect.isawaitable(result):
            return await result
        return result

    async def walk(
        current: str,
        required_by: str,
        constraint: str = "",
    ) -> DependencyError | None:
        current = plugin_id(current) or current
        if current != root and current.casefold() in enabled and not constraint:
            return None
        marketplace = parse_plugin_id(current).marketplace.casefold()
        if marketplace != root_marketplace and marketplace not in allowed:
            return DependencyError(
                "cross-marketplace", dependency=current, required_by=required_by,
                message=f"dependency {current} crosses marketplace boundary",
            )
        if current in stack:
            return DependencyError("cycle", dependency=current, required_by=required_by, chain=tuple(stack + [current]))
        if current in visited:
            return None
        visited.add(current)
        entry = await call(current)
        if entry is None:
            return DependencyError("not-found", dependency=current, required_by=required_by, message=f"missing plugin {current}")
        selected = _select_dependency_entry(entry, constraint)
        if selected is None:
            return DependencyError(
                "version-mismatch",
                dependency=current,
                required_by=required_by,
                message=f"plugin {current} does not satisfy {constraint}",
            )
        entry = selected
        if isinstance(entry, Mapping):
            dependencies = entry.get("dependencies") or []
        else:
            dependencies = getattr(entry, "dependencies", []) or []
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        stack.append(current)
        for raw_dep in dependencies if isinstance(dependencies, Iterable) else ():
            reference = parse_dependency_reference(raw_dep, current)
            if not reference.identity:
                continue
            error = await walk(reference.identity, current, reference.constraint)
            if error:
                return error
        stack.pop()
        closure.append(current)
        return None

    error = await walk(root, root)
    if error:
        return DependencyResolution(False, error=error)
    return DependencyResolution(True, closure=tuple(closure))


def _select_dependency_entry(entry: Any, constraint: str) -> Any | None:
    """Select a lookup result that satisfies a dependency range.

    Lookup adapters in the wild return either one manifest mapping or a small
    candidate list.  Supporting both keeps the resolver pure and makes it
    usable by local marketplaces and remote catalogs alike.  A constrained
    dependency fails closed when no version is advertised.
    """

    if not constraint:
        return entry
    candidates: list[Any] = []
    if isinstance(entry, Mapping):
        for key in ("versions", "candidates", "plugins"):
            raw = entry.get(key)
            if isinstance(raw, (list, tuple)):
                candidates.extend(raw)
        if not candidates:
            candidates.append(entry)
    elif isinstance(entry, (list, tuple)):
        candidates.extend(entry)
    else:
        candidates.append(entry)
    matching = [
        candidate
        for candidate in candidates
        if version_satisfies(_entry_version(candidate), constraint)
    ]
    if not matching:
        return None
    # Prefer the highest advertised version when multiple candidates satisfy.
    # ``packaging`` handles prereleases and build metadata; lexical fallback is
    # deterministic for vendor versions it cannot parse.
    try:
        from packaging.version import Version

        return max(matching, key=lambda item: Version(_entry_version(item).lstrip("vV")))
    except Exception:
        return max(matching, key=lambda item: _entry_version(item))


def _entry_version(entry: Any) -> str:
    if isinstance(entry, Mapping):
        return str(entry.get("version") or entry.get("installed_version") or "").strip()
    return str(getattr(entry, "version", "") or getattr(entry, "installed_version", "") or "").strip()


def verify_and_demote(plugins: Sequence[Mapping[str, Any] | Any]) -> tuple[set[str], list[DependencyError]]:
    """Return enabled IDs that must be demoted due to missing/disabled deps."""

    def get(item: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
        return item.get(key, default) if isinstance(item, Mapping) else getattr(item, key, default)

    ids: dict[str, str] = {}
    entries_by_id: dict[str, list[Mapping[str, Any] | Any]] = {}
    enabled: set[str] = set()
    by_name: dict[str, set[str]] = {}
    for item in plugins:
        raw_id = str(get(item, "id", "") or "").strip()
        name = str(get(item, "name", "") or "").strip()
        identity = plugin_id(raw_id or name, str(get(item, "marketplace", "local") or "local"))
        if not identity:
            continue
        key = identity.casefold()
        ids[key] = identity
        entries_by_id.setdefault(key, []).append(item)
        if bool(get(item, "enabled", False)):
            enabled.add(key)
        by_name.setdefault(parse_plugin_id(identity).name.casefold(), set()).add(key)

    errors: list[DependencyError] = []
    changed = True
    while changed:
        changed = False
        for item in plugins:
            identity = plugin_id(str(get(item, "id", "") or get(item, "name", "") or ""), str(get(item, "marketplace", "local") or "local"))
            key = identity.casefold()
            if key not in enabled:
                continue
            dependencies = get(item, "dependencies", ()) or ()
            if isinstance(dependencies, str):
                dependencies = [dependencies]
            version = str(get(item, "version", "") or "")
            constraints = get(item, "constraints", ())
            declaring_marketplace = parse_plugin_id(identity).marketplace.casefold()
            # A record can carry a managed constraint separately; validation
            # happens here so a loaded incompatible version is demoted.
            if constraints and not version_satisfies(version, constraints):
                enabled.remove(key)
                errors.append(DependencyError("version-mismatch", dependency=identity, message=f"{identity} does not satisfy managed version constraint"))
                changed = True
                continue
            for raw_dep in dependencies if isinstance(dependencies, Iterable) else ():
                reference = parse_dependency_reference(raw_dep, identity)
                dep = reference.identity
                dep_key = dep.casefold()
                candidate_keys: set[str] = {dep_key}
                if not has_explicit_marketplace(raw_dep) and declaring_marketplace == "inline":
                    candidate_keys = by_name.get(str(raw_dep).strip().casefold(), set())
                satisfied = False
                for candidate_key in candidate_keys & enabled:
                    candidates = entries_by_id.get(candidate_key, ())
                    if not reference.constraint or any(
                        version_satisfies(_entry_version(candidate), reference.constraint)
                        for candidate in candidates
                    ):
                        satisfied = True
                        break
                if not satisfied:
                    enabled.remove(key)
                    errors.append(DependencyError(
                        "dependency-unsatisfied" if not reference.constraint else "version-mismatch",
                        dependency=dep,
                        required_by=identity,
                        message=f"{identity} requires {dep}",
                    ))
                    changed = True
                    break

    demoted = {ids[key] for key in ids if key not in enabled and any(
        plugin_id(str(get(item, "id", "") or get(item, "name", "") or ""), str(get(item, "marketplace", "local") or "local")).casefold() == key
        and bool(get(item, "enabled", False)) for item in plugins
    )}
    return demoted, errors


def find_reverse_dependents(plugin_identity: str, plugins: Sequence[Mapping[str, Any] | Any]) -> list[str]:
    target = plugin_id(plugin_identity)
    target_name = parse_plugin_id(target).name.casefold()
    result: list[str] = []
    for item in plugins:
        get = item.get if isinstance(item, Mapping) else lambda key, default=None: getattr(item, key, default)
        if not bool(get("enabled", False)):
            continue
        identity = plugin_id(str(get("id", "") or get("name", "") or ""), str(get("marketplace", "local") or "local"))
        if identity.casefold() == target.casefold():
            continue
        deps = get("dependencies", ()) or ()
        if isinstance(deps, str):
            deps = [deps]
        for raw in deps:
            reference = parse_dependency_reference(raw, identity)
            dep = reference.identity
            if dep.casefold() == target.casefold() or (not has_explicit_marketplace(raw) and parse_plugin_id(dep).name.casefold() == target_name):
                result.append(str(get("name", "") or identity))
                break
    return sorted(set(result), key=str.casefold)
