from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from backend.atomic_io import canonical_file_path_key, file_mutation_locks
from backend.config import (
    SETTINGS_FILE,
    _SETTINGS_WRITE_LOCK,
    _load_settings_json,
    _write_settings_json as _config_write_settings_json,
)
from backend.feature_flags import feature_enabled
from backend.hooks.runtime import raise_if_config_change_blocked
from backend.plugins.dependencies import find_reverse_dependents, verify_and_demote
from backend.plugins.identity import (
    has_explicit_marketplace,
    normalize_plugin_id,
    parse_plugin_id,
    parse_plugin_id_strict,
    plugin_id as canonical_plugin_id,
    is_valid_identifier,
    version_satisfies,
)
from backend.plugins.layout import PLUGIN_MANIFEST_DIRECTORY, plugin_manifest_path

ConfigChangeHook = Callable[..., Awaitable[Any]]
logger = logging.getLogger(__name__)


def _write_settings_json(data: dict[str, Any]) -> None:
    """Write the user settings layer through the shared atomic transaction.

    Keep this narrow compatibility seam in the plugin service: embedders and
    older tests inject the settings reader/writer here to isolate plugin
    lifecycle operations. The default implementation still delegates to the
    config layer's durable writer, so production callers retain its lock,
    atomic replace, and permission behavior.
    """

    _config_write_settings_json(data)


def _update_settings_json(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Apply one serialized settings update while preserving test injection."""

    with _SETTINGS_WRITE_LOCK:
        with file_mutation_locks([SETTINGS_FILE]):
            settings_data = _load_settings_json()
            mutator(settings_data)
            _write_settings_json(settings_data)
            return settings_data


class PluginSettingsError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _marketplace_policy_rule_error(rule: Any) -> str:
    """Validate one managed marketplace policy rule before matching it.

    A malformed rule is a policy compilation error, not a non-match.  This
    keeps one bad allow/block entry from being bypassed by a later valid entry.
    """

    if not isinstance(rule, Mapping):
        return "rule must be an object"
    kind = str(rule.get("source") or "")
    accepted = {
        "url",
        "github",
        "git",
        "npm",
        "file",
        "directory",
        "hostPattern",
        "pathPattern",
        "settings",
    }
    if kind not in accepted:
        return f"unsupported source type: {kind or '<missing>'}"
    required = {
        "url": "url",
        "github": "repo",
        "git": "url",
        "npm": "package",
        "file": "path",
        "directory": "path",
        "hostPattern": "hostPattern",
        "pathPattern": "pathPattern",
        "settings": "name",
    }[kind]
    value = rule.get(required)
    if not isinstance(value, str) or not value.strip():
        return f"requires a non-empty {required} string"
    if kind == "settings" and not isinstance(rule.get("plugins"), list):
        return "requires a plugins array"
    for field_name in ("ref", "path"):
        if field_name in rule:
            field_value = rule.get(field_name)
            if not isinstance(field_value, str) or not field_value.strip():
                return f"{field_name} must be a non-empty string"
    if kind in {"hostPattern", "pathPattern"}:
        try:
            re.compile(value)
        except re.error as exc:
            return f"invalid regular expression: {exc}"
    return ""


@dataclass(frozen=True)
class ManagedPluginPolicy:
    """MiniCode plugin policy projected from one config stack snapshot."""

    enabled_plugins: Mapping[str, Any]
    strict_known_marketplaces: tuple[Mapping[str, Any], ...] | None
    blocked_marketplaces: tuple[Mapping[str, Any], ...]
    marketplace_requirements: Mapping[str, Any]
    trust_message: str = ""
    source: str = ""
    fingerprint: str = ""
    policy_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        errors = [str(item) for item in self.policy_errors if str(item)]
        for field_name, rules in (
            ("strict_known_marketplaces", self.strict_known_marketplaces),
            ("blocked_marketplaces", self.blocked_marketplaces),
        ):
            if rules is None:
                continue
            if not isinstance(rules, (tuple, list)):
                errors.append(f"{field_name} must be an array")
                continue
            for index, rule in enumerate(rules):
                error = _marketplace_policy_rule_error(rule)
                if error:
                    errors.append(f"{field_name}[{index}] {error}")
        object.__setattr__(self, "policy_errors", tuple(dict.fromkeys(errors)))

    def assert_policy_valid(self) -> None:
        if not self.policy_errors:
            return
        details = "; ".join(str(item) for item in self.policy_errors if str(item))
        raise PluginSettingsError(
            self._message(
                "Managed plugin policy is invalid and all plugin marketplace operations are disabled"
                + (f": {details}" if details else "")
            ),
            status_code=503,
        )

    def managed_state(self, plugin_name: str, version: str = "") -> bool | None:
        """Return the effective managed enablement for one canonical id.

        MiniCode's managed ``enabled_plugins`` accepts booleans or arrays of
        version constraints. Arrays are enabled selections, but an installed
        version must satisfy every constraint; an unknown version is treated
        as unresolved and therefore disabled at runtime (fail closed).
        """
        if self.policy_errors:
            return False
        requested = parse_plugin_id(plugin_name)
        requested_id = normalize_plugin_id(plugin_name)
        requested_name = normalize_plugin_name(requested.name)
        candidates: list[tuple[str, Any]] = []
        for raw_id, state in self.enabled_plugins.items():
            try:
                policy = parse_plugin_id_strict(str(raw_id))
            except ValueError:
                continue
            policy_id = normalize_plugin_id(str(raw_id))
            if policy_id == requested_id:
                candidates.append((str(raw_id), state))
            elif not has_explicit_marketplace(plugin_name) and normalize_plugin_name(policy.name) == requested_name:
                candidates.append((str(raw_id), state))
        if not has_explicit_marketplace(plugin_name):
            # Bare names are only safe when the policy itself is unambiguous.
            identities = {normalize_plugin_id(raw_id) for raw_id, _state in candidates}
            if len(identities) > 1:
                return None
        matched: list[bool] = []
        for _raw_id, state in candidates:
            if not isinstance(state, (bool, list, tuple)):
                continue
            if isinstance(state, bool):
                matched.append(state)
                continue
            if isinstance(state, (list, tuple)):
                matched.append(version_satisfies(version, state))
        if False in matched:
            return False
        return True if True in matched else None

    def managed_constraint(self, plugin_name: str) -> tuple[str, ...] | None:
        requested_id = normalize_plugin_id(plugin_name)
        requested_name = normalize_plugin_name(parse_plugin_id(plugin_name).name)
        explicit = has_explicit_marketplace(plugin_name)
        candidate_ids: set[str] = set()
        constraints: list[str] = []
        for raw_id, state in self.enabled_plugins.items():
            try:
                parsed = parse_plugin_id_strict(str(raw_id))
            except ValueError:
                continue
            raw_identity = normalize_plugin_id(str(raw_id))
            if raw_identity != requested_id and (explicit or normalize_plugin_name(parsed.name) != requested_name):
                continue
            candidate_ids.add(raw_identity)
            if isinstance(state, (list, tuple)):
                constraints.extend(str(item).strip() for item in state if str(item).strip())
        if not explicit and len(candidate_ids) > 1:
            return None
        return tuple(dict.fromkeys(constraints)) or None

    def assert_plugin_mutable(self, plugin_name: str) -> None:
        self.assert_policy_valid()
        if self.managed_state(plugin_name) is None:
            return
        raise PluginSettingsError(
            self._message(
                f"Plugin '{plugin_name}' is locked by managed plugin enablement policy"
            ),
            status_code=403,
        )

    def assert_plugin_installable(
        self,
        plugin_name: str,
        *,
        trusted_marketplace: bool = False,
    ) -> None:
        self.assert_policy_valid()
        state = self.managed_state(plugin_name)
        if state is True and trusted_marketplace:
            return
        if state is not False and state is None:
            return
        raise PluginSettingsError(
            self._message(
                f"Plugin '{plugin_name}' is locked by managed plugin enablement policy"
                if state is True
                else f"Plugin '{plugin_name}' is disabled by managed plugin enablement policy"
            ),
            status_code=403,
        )

    def assert_source_allowed(self, source: Mapping[str, Any]) -> None:
        self.assert_policy_valid()
        if any(_source_matches_blocked(source, blocked) for blocked in self.blocked_marketplaces):
            raise PluginSettingsError(
                self._message(
                    f"Plugin source {_format_marketplace_source(source)} is blocked by managed policy"
                ),
                status_code=403,
            )
        if self.strict_known_marketplaces is not None and not any(
            _source_matches_allowed(source, allowed)
            for allowed in self.strict_known_marketplaces
        ):
            raise PluginSettingsError(
                self._message(
                    f"Plugin source {_format_marketplace_source(source)} is not in strict_known_marketplaces"
                ),
                status_code=403,
            )
        if not _source_allowed_by_marketplace_requirements(
            source, self.marketplace_requirements
        ):
            raise PluginSettingsError(
                self._message(
                    f"Plugin source {_format_marketplace_source(source)} is not allowed by MiniCode marketplace requirements"
                ),
                status_code=403,
            )

    def _message(self, message: str) -> str:
        details = self.trust_message.strip()
        source = f" Source: {self.source}." if self.source else ""
        trust = f" {details}" if details else ""
        return f"{message}.{source}{trust}".strip()


def _plugin_policy_from_stack(config_stack: Any | None = None) -> ManagedPluginPolicy:
    if config_stack is None:
        from backend.config import load_config_layer_stack

        config_stack = load_config_layer_stack()
    requirements = config_stack.requirements
    source = requirements.source_for("enabled_plugins")
    if source is None:
        source = requirements.source_for("marketplaces")
    return ManagedPluginPolicy(
        enabled_plugins=dict(requirements.enabled_plugins),
        strict_known_marketplaces=requirements.strict_known_marketplaces,
        blocked_marketplaces=requirements.blocked_marketplaces,
        marketplace_requirements=dict(requirements.marketplace_requirements),
        trust_message=requirements.plugin_trust_message,
        source=str(source or ""),
        fingerprint=str(getattr(config_stack, "fingerprint", "")),
        policy_errors=tuple(
            str(item)
            for item in (getattr(config_stack, "managed_policy_errors", ()) or ())
            if str(item)
        ),
    )


def _source_matches_allowed(
    source: Mapping[str, Any], allowed: Mapping[str, Any]
) -> bool:
    # Normalize and validate the actual descriptor before evaluating any
    # pattern rule.  Falling back to the raw mapping on parser failure made a
    # malformed source look like an ordinary non-match and could bypass a
    # managed allow/block policy.
    source = _canonical_source_descriptor(source)
    allowed_kind = str(allowed.get("source") or "")
    if allowed_kind == "hostPattern":
        host = _marketplace_source_host(source)
        try:
            return bool(host and re.search(str(allowed.get("hostPattern") or ""), host))
        except re.error:
            return False
    if allowed_kind == "pathPattern":
        if str(source.get("source") or "") not in {"file", "directory"}:
            return False
        try:
            return bool(re.search(
                str(allowed.get("pathPattern") or ""),
                str(source.get("path") or ""),
            ))
        except re.error:
            return False
    allowed = _canonical_source_descriptor(allowed)
    if _sources_equal(source, allowed):
        return True
    # Treat GitHub shorthand and its normalized Git URL as one allowlist
    # identity so a raw URL cannot evade an approved repository entry.
    source_kind = str(source.get("source") or "")
    allowed_kind = str(allowed.get("source") or "")
    if {source_kind, allowed_kind} != {"github", "git"}:
        return False
    github = source if source_kind == "github" else allowed
    git = source if source_kind == "git" else allowed
    if _github_repo_from_git_url(str(git.get("url") or "")) != github.get("repo"):
        return False
    return _blocked_constraint_matches(allowed.get("ref"), source.get("ref")) and _blocked_constraint_matches(
        allowed.get("path"), source.get("path")
    )


def _source_matches_blocked(
    source: Mapping[str, Any], blocked: Mapping[str, Any]
) -> bool:
    if str(blocked.get("source") or "") in {"hostPattern", "pathPattern"}:
        return _source_matches_allowed(source, blocked)
    source = _canonical_source_descriptor(source)
    blocked = _canonical_source_descriptor(blocked)
    source_kind = str(source.get("source") or "")
    blocked_kind = str(blocked.get("source") or "")
    if source_kind == blocked_kind:
        if source_kind in {"github", "git"}:
            identity_field = "repo" if source_kind == "github" else "url"
            if source.get(identity_field) != blocked.get(identity_field):
                return False
            return _blocked_constraint_matches(blocked.get("ref"), source.get("ref")) and (
                _blocked_constraint_matches(blocked.get("path"), source.get("path"))
            )
        if source_kind == "settings":
            return source.get("name") == blocked.get("name")
        return _sources_equal(source, blocked)
    if {source_kind, blocked_kind} == {"github", "git"}:
        github = source if source_kind == "github" else blocked
        git = source if source_kind == "git" else blocked
        if _github_repo_from_git_url(str(git.get("url") or "")) != github.get("repo"):
            return False
        blocked_source = blocked
        other_source = source
        return _blocked_constraint_matches(
            blocked_source.get("ref"), other_source.get("ref")
        ) and _blocked_constraint_matches(
            blocked_source.get("path"), other_source.get("path")
        )
    return False


def _blocked_constraint_matches(blocked: Any, actual: Any) -> bool:
    return not blocked or blocked == actual


def _sources_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    kind = str(left.get("source") or "")
    if kind != str(right.get("source") or ""):
        return False
    fields = {
        "url": ("url",),
        "github": ("repo", "ref", "path"),
        "git": ("url", "ref", "path"),
        "npm": ("package",),
        "file": ("path",),
        "directory": ("path",),
        "settings": ("name", "plugins"),
    }.get(kind)
    if fields is None:
        return False
    for field_name in fields:
        left_value = left.get(field_name)
        right_value = right.get(field_name)
        if field_name == "path" and kind in {"file", "directory"}:
            if not _paths_equal(left_value, right_value):
                return False
        elif (left_value or None) != (right_value or None):
            return False
    return True


def _paths_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _marketplace_source_host(source: Mapping[str, Any]) -> str:
    kind = str(source.get("source") or "")
    if kind == "github":
        return "github.com"
    if kind not in {"git", "url"}:
        return ""
    value = str(source.get("url") or "")
    ssh_match = re.match(r"^[^@]+@([^:]+):", value)
    if ssh_match:
        return ssh_match.group(1).lower()
    try:
        return str(urlsplit(value).hostname or "").lower()
    except ValueError:
        return ""


def _github_repo_from_git_url(value: str) -> str:
    match = re.match(
        r"^(?:git@github\.com:|https?://github\.com/)([^/]+/[^/]+?)(?:\.git)?$",
        value,
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _source_allowed_by_marketplace_requirements(
    source: Mapping[str, Any], requirements: Mapping[str, Any]
) -> bool:
    if requirements.get("restrict_to_allowed_sources") is not True:
        return True
    allowed_sources = requirements.get("allowed_sources")
    if not isinstance(allowed_sources, Mapping):
        return False
    actual = _canonical_source_descriptor(source)
    for rule in allowed_sources.values():
        if not isinstance(rule, Mapping):
            return False
        kind = str(rule.get("source") or "")
        if kind == "host_pattern":
            pattern = rule.get("host_pattern")
            if not isinstance(pattern, str) or not pattern.strip():
                return False
            try:
                compiled = re.compile(pattern)
            except re.error:
                return False
            if compiled.search(_marketplace_source_host(actual)):
                return True
            continue
        if kind == "local":
            path = rule.get("path")
            if not isinstance(path, str) or not Path(path).expanduser().is_absolute():
                return False
            if actual.get("source") == "directory" and _paths_equal(actual.get("path"), path):
                return True
            continue
        if kind == "git":
            url = rule.get("url")
            if not isinstance(url, str) or not url.strip():
                return False
            ref = rule.get("ref")
            if ref is not None and (not isinstance(ref, str) or not ref.strip()):
                return False
            try:
                from backend.plugins.materializer import parse_marketplace_source

                expected = parse_marketplace_source({"source": "git", "url": url, **(
                    {"ref": ref} if ref is not None else {}
                )}).to_dict()
            except Exception:
                return False
            if actual.get("source") != "git" or actual.get("url") != expected.get("url"):
                continue
            if ref is not None and actual.get("ref") != expected.get("ref"):
                continue
            expected_path = rule.get("path")
            if expected_path is not None and actual.get("path") != expected_path:
                continue
            return True
        return False
    return False


def _canonical_source_descriptor(source: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize source aliases before any managed rule is evaluated."""

    if not isinstance(source, Mapping):
        raise PluginSettingsError(
            "Marketplace source descriptor must be an object",
            status_code=400,
        )
    try:
        from backend.plugins.materializer import parse_marketplace_source

        return parse_marketplace_source(source).to_dict()
    except Exception as exc:
        raise PluginSettingsError(
            f"Invalid marketplace source descriptor: {exc}",
            status_code=400,
        ) from exc


def _format_marketplace_source(source: Mapping[str, Any]) -> str:
    kind = str(source.get("source") or "unknown")
    value = (
        source.get("path")
        or source.get("url")
        or source.get("repo")
        or source.get("package")
        or source.get("name")
        or ""
    )
    return f"{kind}:{value}"


def normalize_plugin_name(name: str) -> str:
    return str(name or "").strip().casefold()


def plugin_id_for_name(name: str, marketplace: str = "local") -> str:
    return canonical_plugin_id(name, marketplace)


_PLUGIN_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
}
_PLUGIN_EXCLUDED_FILES = {".DS_Store", "Thumbs.db"}
_PLUGIN_MANIFEST_FIELDS = {
    "name", "version", "description", "keywords", "skills", "hooks",
    "mcp_servers", "apps", "interface", "dependencies", "marketplace",
    "source", "commands", "prompts", "extensions",
}
_MAX_PLUGIN_PACKAGE_BYTES = 50 * 1024 * 1024
_MAX_PLUGIN_PACKAGE_FILE_BYTES = 5 * 1024 * 1024
_MAX_PLUGIN_ARCHIVE_ENTRIES = 1000


def plugin_install_root() -> Path:
    from backend.config import STATE_ROOT
    return STATE_ROOT / "extensions" / "plugins" / "installed"


def _canonical_configured_plugin_id(value: Any) -> str:
    """Normalize a MiniCode config key to ``<name>@<marketplace>``."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = parse_plugin_id_strict(raw)
    except ValueError:
        return ""
    return canonical_plugin_id(parsed.id)


def _coerce_plugin_enablement_state(value: Any) -> bool | tuple[str, ...] | None:
    """Return the only state shapes accepted by MiniCode plugin settings."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple)):
        constraints = tuple(
            str(item).strip()
            for item in value
            if isinstance(item, str) and str(item).strip()
        )
        # An array containing non-string values is malformed.  Failing closed
        # is safer than silently treating it as an unconstrained enablement.
        if len(constraints) != len(value):
            return None
        return constraints
    return None


def _collect_configured_plugin_states(
    effective_config: Mapping[str, Any] | None,
    settings_data: Mapping[str, Any] | None,
) -> tuple[dict[str, bool | tuple[str, ...]], bool, dict[str, str]]:
    """Project MiniCode user configuration onto one canonical map.

    The map is deliberately the only user enablement input consumed by the
    inventory.  A plugin that merely exists under a scanned root is *not*
    enabled.

    Returns ``(states, has_explicit_config, invalid_entries)``.  The latter is
    kept separate so a malformed optional entry can be surfaced on the
    matching inventory item without making unrelated plugins executable.
    """

    states: dict[str, bool | tuple[str, ...]] = {}
    invalid: dict[str, str] = {}
    explicit = False

    def add_state(raw_id: Any, raw_state: Any, *, overwrite: bool = True) -> None:
        nonlocal explicit
        canonical = _canonical_configured_plugin_id(raw_id)
        key_text = str(raw_id or "").strip()
        if not canonical:
            if key_text:
                invalid[key_text] = "invalid plugin id"
            return
        state = _coerce_plugin_enablement_state(raw_state)
        if state is None:
            invalid[canonical] = "enabled state must be a boolean or string array"
            # An explicitly malformed entry is still an explicit deny.  This
            # prevents a typo from falling back to physical auto-enable.
            if overwrite or canonical not in states:
                states[canonical] = False
            explicit = True
            return
        if overwrite or canonical not in states:
            states[canonical] = state
        explicit = True

    def add_plugin_entries(raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        nonlocal explicit
        saw_plugin_table = False
        for raw_id, raw_entry in raw.items():
            if not isinstance(raw_entry, Mapping) or "enabled" not in raw_entry:
                continue
            saw_plugin_table = True
            add_state(raw_id, raw_entry.get("enabled"))
        if saw_plugin_table:
            explicit = True

    effective = effective_config if isinstance(effective_config, Mapping) else {}
    add_plugin_entries(effective.get("plugins"))

    # Explicitly injected settings snapshots are used by isolated callers and
    # tests. The already-composed config stack remains authoritative.
    if isinstance(settings_data, Mapping):
        before = dict(states)
        add_plugin_entries(settings_data.get("plugins"))
        for key, value in before.items():
            states[key] = value

    return states, explicit, invalid


def _configured_plugin_state(
    plugin_id: str,
    plugin_name: str,
    version: str,
    states: Mapping[str, bool | tuple[str, ...]],
) -> tuple[bool | None, tuple[str, ...]]:
    """Resolve one inventory item against the canonical user selection map."""

    target_id = normalize_plugin_id(plugin_id)
    target_name = normalize_plugin_name(plugin_name)
    candidates: list[tuple[str, bool | tuple[str, ...]]] = []
    for raw_id, state in states.items():
        if normalize_plugin_id(raw_id) == target_id:
            candidates.append((raw_id, state))
            continue
        parsed = parse_plugin_id(raw_id)
        if normalize_plugin_name(parsed.name) == target_name and not has_explicit_marketplace(plugin_name):
            candidates.append((raw_id, state))
    if not candidates:
        return None, ()
    # A bare-name match is only safe when it resolves to one canonical id.
    identities = {normalize_plugin_id(raw_id) for raw_id, _state in candidates}
    if len(identities) > 1:
        return None, ()
    state = candidates[-1][1]
    if isinstance(state, bool):
        return state, ()
    constraints = tuple(state)
    return version_satisfies(version, constraints), constraints


def _persist_user_plugin_enablement(
    settings_data: dict[str, Any],
    plugin_id: str,
    enabled: bool,
) -> None:
    """Persist canonical MiniCode plugin state in the user layer."""

    canonical = _canonical_configured_plugin_id(plugin_id)
    if not canonical:
        raise PluginSettingsError(f"Invalid plugin identity: {plugin_id}", status_code=400)
    settings_data.pop("enabledPlugins", None)
    settings_data.pop("enabled_plugins", None)
    raw = settings_data.get("plugins")
    plugins = dict(raw) if isinstance(raw, Mapping) else {}
    plugins.pop("disabled", None)
    existing: dict[str, Any] = {}
    for raw_id in list(plugins):
        if _canonical_configured_plugin_id(raw_id).casefold() != canonical.casefold():
            continue
        raw_entry = plugins.pop(raw_id)
        if isinstance(raw_entry, Mapping):
            existing.update(dict(raw_entry))
    existing["enabled"] = bool(enabled)
    plugins[canonical] = existing
    settings_data["plugins"] = plugins


def _clear_user_plugin_enablement(settings_data: dict[str, Any], plugin_id: str) -> None:
    """Remove a user selection when a plugin is uninstalled."""

    canonical = _canonical_configured_plugin_id(plugin_id)
    settings_data.pop("enabledPlugins", None)
    settings_data.pop("enabled_plugins", None)
    raw = settings_data.get("plugins")
    if isinstance(raw, Mapping):
        plugins = dict(raw)
        plugins.pop("disabled", None)
        for raw_id in list(plugins):
            if _canonical_configured_plugin_id(raw_id).casefold() == canonical.casefold():
                plugins.pop(raw_id, None)
        if plugins:
            settings_data["plugins"] = plugins
        else:
            settings_data.pop("plugins", None)


def plugin_name_from_directory(plugin_dir: Path) -> str:
    plugin_dir = Path(plugin_dir)
    name = plugin_name_from_manifest(plugin_manifest_path(plugin_dir))
    if name:
        return name
    return plugin_dir.name


def plugin_name_from_manifest(manifest_path: Path) -> str:
    manifest_path = Path(manifest_path)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    plugin_dir = plugin_directory_for_manifest(manifest_path)
    if isinstance(raw, Mapping):
        name = str(raw.get("name") or "").strip()
        if name:
            return name
    return plugin_dir.name


def plugin_directory_for_manifest(manifest_path: Path) -> Path:
    manifest_path = Path(manifest_path)
    if manifest_path.parent.name == PLUGIN_MANIFEST_DIRECTORY:
        return manifest_path.parent.parent
    return manifest_path.parent


def _read_plugin_manifest(manifest_path: Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _primary_plugin_manifest(plugin_dir: Path) -> Path | None:
    """Return the canonical MiniCode plugin manifest."""

    for manifest in _plugin_manifest_paths(Path(plugin_dir)):
        if manifest.is_file():
            return manifest
    return None


def _looks_like_versioned_store(plugin_dir: Path) -> bool:
    """Recognize ``marketplace/name/version`` stores without trusting names."""
    parts = [part for part in plugin_dir.resolve().parts if part not in {"", "."}]
    if len(parts) < 3:
        return False
    candidate = parts[-1]
    return candidate == "local" or bool(
        re.fullmatch(r"v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?", candidate)
    )


def _validate_versioned_store_path(
    plugin_dir: Path,
    plugin_roots: Iterable[Path],
) -> str:
    """Validate a store-backed plugin before exposing its manifest."""
    if not _looks_like_versioned_store(plugin_dir):
        return ""
    resolved = plugin_dir.resolve() if plugin_dir.exists() else plugin_dir.absolute()
    for raw_root in plugin_roots:
        root = Path(raw_root).expanduser()
        try:
            root_resolved = root.resolve()
            relative = resolved.relative_to(root_resolved)
        except (OSError, ValueError):
            continue
        if len(relative.parts) < 3:
            continue
        if root.name.casefold() != "store" and not (plugin_dir / ".plugin-store.json").is_file():
            continue
        try:
            from backend.plugins.store import PluginStore
            records = PluginStore(root).list()
        except Exception as exc:
            return f"invalid plugin store record: {exc}"
        for record in records:
            if record.path.absolute() == resolved:
                if not record.active:
                    return "plugin store record is not the active version"
                return ""
        return "plugin store record failed integrity validation"
    return ""

def _plugin_marketplace_for_manifest(
    manifest_path: Path,
    raw_manifest: Mapping[str, Any] | None,
    plugin_roots: Iterable[Path],
) -> str:
    # Marketplace identity comes from the registry/store path, never from
    # self-authored manifest metadata.
    plugin_dir = plugin_directory_for_manifest(manifest_path)
    try:
        resolved = plugin_dir.resolve()
    except OSError:
        resolved = plugin_dir.absolute()
    # Versioned stores use <root>/<marketplace>/<plugin>/<version>; cache
    # layouts use <root>/cache/<owner>/<plugin>/<version>.
    for raw_root in plugin_roots:
        root = Path(raw_root).expanduser()
        try:
            rel = resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        parts = rel.parts
        if len(parts) == 1 and "@" in parts[0]:
            projected = parse_plugin_id(parts[0])
            manifest_name = str((raw_manifest or {}).get("name") or "").strip()
            if (
                projected.name == manifest_name
                and is_valid_identifier(projected.name, projected.marketplace)
            ):
                return projected.marketplace
        if len(parts) >= 3 and parts[-1] and _looks_like_versioned_store(resolved):
            return str(parts[-3])
        if len(parts) >= 4 and parts[0].casefold() == "cache":
            return str(parts[1])
    return "local"


def _manifest_source_descriptor(
    plugin_dir: Path,
    marketplace: str,
    raw_manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    # A manifest is untrusted plugin content and cannot self-attest a trusted
    # Git/GitHub/NPM provenance.  A versioned store record is written by the
    # installer boundary and may carry the verified source descriptor.
    if _looks_like_versioned_store(plugin_dir):
        try:
            from backend.plugins.store import PluginStore
            store_root = plugin_dir.parents[2]
            record = next(
                (item for item in PluginStore(store_root).list() if item.path.absolute() == plugin_dir.absolute()),
                None,
            )
            if record is not None and isinstance(record.source, Mapping) and record.source.get("source"):
                return dict(record.source)
        except Exception:
            # A malformed store must never make its self-authored metadata
            # authoritative.  Fall through to an untrusted directory source;
            # the caller will mark the record disabled via integrity checks.
            pass
    return {
        "source": "directory",
        "path": str(plugin_dir),
        "marketplace": marketplace,
    }


def _filesystem_path_key(value: Any) -> str:
    return os.path.normcase(os.path.normpath(str(value or "")))


def _prefer_active_store_entries(
    inventory: Mapping[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Collapse flat compatibility projections to one active record per id."""
    selected: dict[str, tuple[int, str, dict[str, Any]]] = {}
    for entry in inventory.values():
        identity = normalize_plugin_id(str(entry.get("id") or entry.get("name") or ""))
        if not identity:
            identity = f"<invalid>:{_filesystem_path_key(entry.get('path'))}"
        provenance = entry.get("provenance") if isinstance(entry.get("provenance"), Mapping) else {}
        rank = 2 if str(provenance.get("store") or "") == "versioned" else 1
        path = str(entry.get("path") or "")
        previous = selected.get(identity)
        if previous is None or (rank, path.casefold()) > (previous[0], previous[1].casefold()):
            selected[identity] = (rank, path, entry)
    return {
        f"{identity}::{_filesystem_path_key(entry.get('path'))}": entry
        for identity, (_rank, _path, entry) in selected.items()
    }


def get_plugin_settings(
    plugin_roots: Iterable[Path] | None = None,
    *,
    config_stack: Any | None = None,
    policy: ManagedPluginPolicy | None = None,
) -> dict[str, Any]:
    from backend.commands.plugins import (
        _iter_plugin_manifests,
        default_plugin_roots,
    )

    # Resolve one config-layer snapshot up front.  Every enablement decision
    # below is derived from this object plus the explicitly injected writable
    # settings data; no consumer is allowed to re-read a second settings file
    # after inventory discovery.
    resolved_stack = config_stack
    if resolved_stack is None:
        try:
            from backend.config import load_config_layer_stack

            resolved_stack = load_config_layer_stack()
        except Exception:
            # Inventory/listing remains useful in a degraded installation, but
            # the fail-closed default below still prevents physical auto-load.
            resolved_stack = None
    effective_policy = policy or _plugin_policy_from_stack(resolved_stack)
    roots = list(plugin_roots) if plugin_roots is not None else list(default_plugin_roots())
    settings_data = _load_settings_json()
    effective_config: Mapping[str, Any] = {}
    if resolved_stack is not None:
        try:
            candidate_config = resolved_stack.effective_config()
            if isinstance(candidate_config, Mapping):
                effective_config = candidate_config
        except Exception:
            effective_config = {}
    configured_states, configured_explicit, invalid_config = _collect_configured_plugin_states(
        effective_config,
        settings_data,
    )
    inventory: dict[str, dict[str, Any]] = {}

    for manifest_path in _iter_plugin_manifests(roots):
        plugin_dir = plugin_directory_for_manifest(manifest_path)
        raw_manifest = _read_plugin_manifest(manifest_path)
        name = plugin_name_from_manifest(manifest_path) or plugin_dir.name
        store_validation_error = _validate_versioned_store_path(plugin_dir, roots)
        marketplace = _plugin_marketplace_for_manifest(
            manifest_path,
            raw_manifest,
            roots,
        )
        plugin_id = plugin_id_for_name(name, marketplace)
        identity_valid = bool(plugin_id)
        # A path is provenance, not identity.  Keep separate installations of
        # the same name from different marketplaces visible and deterministic.
        path_key = str(plugin_dir.resolve()) if plugin_dir.exists() else str(plugin_dir.absolute())
        key = f"{plugin_id.casefold()}::{_filesystem_path_key(path_key)}"
        entry = inventory.setdefault(
            key,
            {
                "name": name,
                "id": plugin_id,
                "identity_valid": identity_valid,
                "marketplace": marketplace,
                "source_kind": "directory",
                "source_descriptor": {"source": "directory", "path": path_key},
                "provenance": {
                    "manifest_path": str(manifest_path),
                    "root": str(plugin_dir),
                    "store": "versioned" if _looks_like_versioned_store(plugin_dir) else "legacy",
                },
                "displayName": "",
                "description": "",
                "shortDescription": "",
                "longDescription": "",
                "developerName": "",
                "category": "",
                "capabilities": [],
                "version": "",
                "installed_version": "",
                "active_version": "",
                "dependencies": [],
                "constraints": (),
                "load_errors": [],
                "trust": {"allowed": True, "reason": ""},
                "websiteUrl": "",
                "iconUrl": "",
                "iconVariant": "",
                "brandColor": "",
                "defaultPrompt": [],
                "path": str(plugin_dir),
                "manifest_path": str(manifest_path),
                "manifest_paths": [],
                "skill_count": 0,
                "mcp_server_count": 0,
                "mcp_server_names": [],
                "app_count": 0,
                "hook_count": 0,
                "extension_count": 0,
                "runtime_support": {
                    "skills": True,
                    "mcp_servers": True,
                    "apps": False,
                    "hooks": True,
                    "extensions": True,
                },
                # A materialized directory is not an activation grant.  The
                # final state is filled from managed/user config below.
                "enabled": False,
                "enablement_source": "unconfigured",
            },
        )
        if store_validation_error:
            entry.setdefault("load_errors", []).append(store_validation_error)
            entry["store_valid"] = False
        else:
            entry.setdefault("store_valid", True)
        entry["manifest_paths"].append(str(manifest_path))
        _merge_manifest_metadata(entry, manifest_path)
        if isinstance(raw_manifest, Mapping):
            dependencies = raw_manifest.get("dependencies")
            if isinstance(dependencies, str):
                dependencies = [dependencies]
            if isinstance(dependencies, list):
                entry["dependencies"] = sorted({
                    str(item).strip() for item in [*entry.get("dependencies", []), *dependencies]
                    if isinstance(item, str) and item.strip()
                }, key=str.casefold)
            entry["source_descriptor"] = _manifest_source_descriptor(
                plugin_dir, marketplace, raw_manifest,
            )
        counts = _manifest_component_counts(manifest_path)
        entry["mcp_server_count"] += counts["mcp_server_count"]
        entry["mcp_server_names"] = sorted({
            *entry.get("mcp_server_names", []),
            *_manifest_mcp_server_names(manifest_path),
        }, key=str.casefold)
        entry["app_count"] += counts["app_count"]
        entry["hook_count"] += counts["hook_count"]
        entry["extension_count"] += counts["extension_count"]

    inventory = _prefer_active_store_entries(inventory)
    for entry in inventory.values():
        plugin_dir = Path(str(entry["path"]))
        entry["skill_count"] = _count_plugin_skills(plugin_dir)
        entry["installed_version"] = str(entry.get("version") or "")
        if str((entry.get("provenance") or {}).get("store") or "") == "versioned":
            entry["active_version"] = plugin_dir.name
        else:
            entry["active_version"] = entry["installed_version"]
        plugin_id = str(entry.get("id") or plugin_id_for_name(
            str(entry.get("name") or ""), str(entry.get("marketplace") or "local")
        ))
        managed_state = effective_policy.managed_state(
            plugin_id,
            str(entry.get("version") or ""),
        )
        configured_state: bool | None = None
        configured_constraints: tuple[str, ...] = ()
        if managed_state is not None:
            entry["enabled"] = managed_state
            entry["enablement_source"] = "managed"
        else:
            configured_state, configured_constraints = _configured_plugin_state(
                plugin_id,
                str(entry.get("name") or ""),
                str(entry.get("version") or ""),
                configured_states,
            )
            if configured_state is None:
                # Explicit configuration is required even when the plugin is
                # installed locally. This boundary prevents a cache scan from
                # launching plugin
                # Skills/MCP/Hooks by accident.
                entry["enabled"] = False
                entry["enablement_source"] = "unconfigured"
            else:
                entry["enabled"] = configured_state
                entry["enablement_source"] = "user"
        if not bool(entry.get("identity_valid", True)):
            entry["enabled"] = False
            entry["enablement_source"] = "invalid-identity"
            entry.setdefault("load_errors", []).append("invalid plugin identity")
        if entry.get("store_valid") is False:
            entry["enabled"] = False
        managed_constraints = effective_policy.managed_constraint(plugin_id) or ()
        constraints = tuple(dict.fromkeys((*configured_constraints, *managed_constraints)))
        entry["constraints"] = constraints
        if managed_constraints and not version_satisfies(
            str(entry.get("version") or ""),
            managed_constraints,
        ):
            entry["enabled"] = False
            entry["load_errors"].append(
                f"installed version {entry.get('version') or '<unknown>'} does not satisfy {', '.join(managed_constraints)}"
            )
        try:
            effective_policy.assert_source_allowed(entry.get("source_descriptor") or {})
        except PluginSettingsError as exc:
            entry["enabled"] = False
            entry["trust"] = {"allowed": False, "reason": str(exc)}
            entry["load_errors"].append(str(exc))
        entry["disabled"] = not bool(entry["enabled"])
        entry["managed"] = (
            _is_relative_to(plugin_dir, plugin_install_root())
            or _is_relative_to(plugin_dir, plugin_install_root().parent / "store")
        )
        entry["policy_managed"] = managed_state is not None
        entry["managed_enabled"] = managed_state
        entry["configured"] = configured_state is not None if managed_state is None else True
        if plugin_id.casefold() in invalid_config:
            entry.setdefault("load_errors", []).append(
                f"invalid configured plugin state: {invalid_config[plugin_id.casefold()]}"
            )

    # Load-time fixed-point dependency safety net.  Do not write settings;
    # consumers receive the same demoted snapshot for this turn.
    demoted, dependency_errors = verify_and_demote(list(inventory.values()))
    if demoted:
        for entry in inventory.values():
            if str(entry.get("id") or "").casefold() in {value.casefold() for value in demoted}:
                entry["enabled"] = False
                entry["disabled"] = True
                entry.setdefault("load_errors", []).append("dependency-unsatisfied")
    dependency_error_payload = [
        {
            "reason": error.reason,
            "dependency": error.dependency,
            "required_by": error.required_by,
            "chain": list(error.chain),
            "message": error.message,
        }
        for error in dependency_errors
    ]

    plugins = sorted(
        inventory.values(),
        key=lambda item: (not bool(item.get("enabled")), str(item.get("name", "")).casefold(), str(item.get("path", "")).casefold()),
    )
    fingerprint = _plugin_inventory_fingerprint(
        plugins,
        effective_policy,
        configured_states=configured_states,
    )
    return {
        "plugins": plugins,
        "disabled": sorted(
            {
                str(entry.get("id") or entry.get("name") or "")
                for entry in plugins
                if not bool(entry.get("enabled"))
                and str(entry.get("id") or entry.get("name") or "")
            },
            key=str.casefold,
        ),
        "configured_plugins": {
            str(key): value for key, value in sorted(configured_states.items(), key=lambda item: item[0].casefold())
        },
        "configured_explicit": configured_explicit,
        "feature_enabled": feature_enabled("plugin_lifecycle_api", True),
        "policy": {
            "configured": bool(
                configured_explicit
                or effective_policy.enabled_plugins
                or effective_policy.strict_known_marketplaces is not None
                or effective_policy.blocked_marketplaces
                or effective_policy.marketplace_requirements
            ),
            "source": effective_policy.source,
            "fingerprint": effective_policy.fingerprint,
            "trust_message": effective_policy.trust_message,
        },
        "dependency_errors": dependency_error_payload,
        "snapshot_version": 2,
        "fingerprint": fingerprint,
    }


def _plugin_inventory_fingerprint(
    plugins: Iterable[Mapping[str, Any]],
    policy: ManagedPluginPolicy,
    *,
    configured_states: Mapping[str, Any] | None = None,
) -> str:
    material: list[dict[str, Any]] = []
    for entry in plugins:
        manifests: list[dict[str, Any]] = []
        for raw_path in entry.get("manifest_paths", ()) if isinstance(entry, Mapping) else ():
            path = Path(str(raw_path))
            try:
                stat = path.stat()
                manifests.append({"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
            except OSError:
                manifests.append({"path": str(path), "missing": True})
        material.append({
            "id": entry.get("id", ""),
            "path": entry.get("path", ""),
            "version": entry.get("version", ""),
            "enabled": bool(entry.get("enabled")),
            "constraints": list(entry.get("constraints", ()) or ()),
            "dependencies": list(entry.get("dependencies", ()) or ()),
            "manifests": manifests,
        })
    payload = {
        "policy": policy.fingerprint,
        "configured_plugins": {
            str(key): value
            for key, value in sorted(
                (configured_states or {}).items(),
                key=lambda item: str(item[0]).casefold(),
            )
        },
        "plugins": sorted(material, key=lambda item: (str(item["id"]).casefold(), str(item["path"]).casefold())),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def get_plugin_snapshot(
    plugin_roots: Iterable[Path] | None = None,
    *,
    config_stack: Any | None = None,
    policy: ManagedPluginPolicy | None = None,
) -> dict[str, Any]:
    """Canonical effective inventory consumed by runtime subsystems.

    Kept as a named API so callers cannot accidentally fall back to the old
    ``disabled`` list or perform a second plugin-root scan.  The payload is
    structurally the same as ``get_plugin_settings`` for frontend and API
    compatibility.
    """

    if plugin_roots is None and config_stack is None and policy is None:
        # A number of integrations monkeypatch the legacy zero-argument
        # function.  Preserve that injection seam while keeping the named
        # snapshot API canonical for real callers.
        return get_plugin_settings()
    return get_plugin_settings(plugin_roots, config_stack=config_stack, policy=policy)


async def update_plugin_enabled(
    name: str,
    enabled: bool,
    *,
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
) -> dict[str, Any]:
    policy = _plugin_policy_from_stack()
    clean_name = str(name or "").strip()
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)
    if not clean_name:
        raise PluginSettingsError("Plugin name is required", status_code=400)
    if not isinstance(enabled, bool):
        raise PluginSettingsError("Plugin enabled must be a boolean", status_code=400)
    known_plugins = get_plugin_settings(policy=policy)
    target = _resolve_plugin_inventory_item(
        clean_name,
        known_plugins.get("plugins", []),
    )
    plugin_id = str(target.get("id") or plugin_id_for_name(
        str(target.get("name") or ""), str(target.get("marketplace") or "local")
    ))
    policy.assert_plugin_mutable(plugin_id)

    hook_result = await config_change_hook(source="plugins", file_path=str(settings_file))
    try:
        raise_if_config_change_blocked(
            hook_result,
            source="plugins",
            file_path=str(settings_file),
        )
    except Exception as exc:
        raise PluginSettingsError(str(exc), status_code=409) from exc
    def apply_enablement(settings_data: dict[str, Any]) -> None:
        _persist_user_plugin_enablement(settings_data, plugin_id, enabled)

    _update_settings_json(apply_enablement)
    return get_plugin_settings(policy=policy)


async def remove_plugin(
    name: str,
    *,
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
) -> dict[str, Any]:
    policy = _plugin_policy_from_stack()
    clean_name = str(name or "").strip()
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)
    if not clean_name:
        raise PluginSettingsError("Plugin name is required", status_code=400)
    install_root = plugin_install_root()
    store_root = plugin_install_root().parent / "store"
    inventory = get_plugin_settings([install_root, store_root], policy=policy).get("plugins", [])
    target = _resolve_plugin_inventory_item(clean_name, inventory)
    plugin_id = str(target.get("id") or plugin_id_for_name(
        str(target.get("name") or ""), str(target.get("marketplace") or "local")
    ))
    policy.assert_plugin_mutable(plugin_id)
    installed = [target]
    removal_root = install_root / ".removals" / uuid4().hex
    moved: list[tuple[Path, Path]] = []
    store_removal: Any | None = None
    reverse_dependents = find_reverse_dependents(
        plugin_id,
        get_plugin_snapshot(policy=policy).get("plugins", []),
    )
    # Gate the lifecycle mutation before staging or removing physical files.
    hook_result = await config_change_hook(source="plugins", file_path=str(settings_file))
    try:
        raise_if_config_change_blocked(
            hook_result,
            source="plugins",
            file_path=str(settings_file),
        )
    except Exception as exc:
        raise PluginSettingsError(str(exc), status_code=409) from exc
    try:
        removal_root.mkdir(parents=True, exist_ok=False)
        for index, item in enumerate(installed):
            plugin_path = Path(str(item.get("path") or ""))
            if not _is_relative_to(plugin_path, install_root) and not _is_relative_to(plugin_path, store_root):
                raise PluginSettingsError(
                    "Refusing to remove a path outside the plugin root",
                    status_code=400,
                )
            if _is_relative_to(plugin_path, install_root):
                staged = removal_root / f"{index}-{plugin_path.name}"
                plugin_path.replace(staged)
                moved.append((plugin_path, staged))

        from backend.plugins.store import PluginStore

        store_removal = PluginStore().stage_remove(plugin_id)

        def clear_enablement(settings_data: dict[str, Any]) -> None:
            _clear_user_plugin_enablement(settings_data, plugin_id)

        _update_settings_json(clear_enablement)
    except Exception:
        if store_removal is not None:
            from backend.plugins.store import PluginStore

            PluginStore.rollback_remove(store_removal)
        for original, staged in reversed(moved):
            if staged.exists() and not original.exists():
                staged.replace(original)
        raise
    finally:
        if removal_root.exists() and not moved:
            _remove_within_root(removal_root, install_root)

    _remove_within_root(removal_root, install_root)
    if store_removal is not None:
        from backend.plugins.store import PluginStore

        PluginStore.commit_remove(store_removal)
    return {
        **get_plugin_settings(policy=policy),
        "removed": {
            "name": str(target.get("name") or clean_name),
            "id": plugin_id,
            "reverse_dependents": reverse_dependents,
        },
    }


async def import_plugin_from_path(
    source_path: str | Path,
    *,
    overwrite: bool = False,
    marketplace: str = "local",
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
    _policy: ManagedPluginPolicy | None = None,
    _trusted_marketplace: bool = False,
    _marketplace_source_descriptor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    policy = _policy or _plugin_policy_from_stack()
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)
    if _trusted_marketplace and not isinstance(_marketplace_source_descriptor, Mapping):
        raise PluginSettingsError(
            "Trusted marketplace installs require the registered source provenance",
            status_code=403,
        )

    source = Path(str(source_path or "")).expanduser()
    if not str(source).strip():
        raise PluginSettingsError("Plugin source path is required", status_code=400)
    if source.is_file():
        return await import_plugin_package(
            source,
            overwrite=overwrite,
            marketplace=marketplace,
            settings_file=settings_file,
            config_change_hook=config_change_hook,
            _policy=policy,
            _trusted_marketplace=_trusted_marketplace,
            _marketplace_source_descriptor=_marketplace_source_descriptor,
        )
    source_text = str(source_path or "").strip()
    if _looks_like_remote_plugin_source(source_text):
        from backend.plugins.materializer import (
            MaterializationError,
            materialize_source,
            parse_marketplace_source,
        )

        try:
            parsed_source = parse_marketplace_source(source_text)
            source_descriptor = parsed_source.to_dict()
            source_descriptor["marketplace"] = str(marketplace or "local").strip() or "local"
            policy.assert_source_allowed(source_descriptor)
            staging_root = plugin_install_root().parent / ".remote-imports"
            staging_root.mkdir(parents=True, exist_ok=True)
            destination = staging_root / uuid4().hex
            materialized = materialize_source(parsed_source, destination)
            try:
                return await _install_plugin_directory(
                    materialized.path,
                    overwrite=overwrite,
                    settings_file=settings_file,
                    config_change_hook=config_change_hook,
                    import_kind="remote",
                    policy=policy,
                    trusted_marketplace=_trusted_marketplace,
                    marketplace_source_descriptor=_marketplace_source_descriptor,
                    source_descriptor={
                        **source_descriptor,
                        "provenance": materialized.to_dict(),
                    },
                )
            finally:
                if destination.exists():
                    _remove_within_root(destination, staging_root)
        except MaterializationError as exc:
            raise PluginSettingsError(str(exc), status_code=400) from exc
    if not source.is_dir():
        raise PluginSettingsError("Plugin source path must be an existing directory or .zip file", status_code=400)
    source_descriptor = {
        "source": "directory",
        "path": str(source.resolve()),
        "marketplace": str(marketplace or "local").strip() or "local",
    }
    policy.assert_source_allowed(
        _marketplace_source_descriptor
        if _trusted_marketplace and isinstance(_marketplace_source_descriptor, Mapping)
        else source_descriptor
    )
    return await _install_plugin_directory(
        source,
        overwrite=overwrite,
        settings_file=settings_file,
        config_change_hook=config_change_hook,
        import_kind="directory",
        policy=policy,
        trusted_marketplace=_trusted_marketplace,
        marketplace_source_descriptor=_marketplace_source_descriptor,
        source_descriptor=source_descriptor,
    )


async def import_plugin_package(
    package_path: str | Path,
    *,
    overwrite: bool = False,
    marketplace: str = "local",
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
    _policy: ManagedPluginPolicy | None = None,
    _trusted_marketplace: bool = False,
    _marketplace_source_descriptor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    policy = _policy or _plugin_policy_from_stack()
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)
    if _trusted_marketplace and not isinstance(_marketplace_source_descriptor, Mapping):
        raise PluginSettingsError(
            "Trusted marketplace installs require the registered source provenance",
            status_code=403,
        )

    package = Path(str(package_path or "")).expanduser()
    if not str(package).strip():
        raise PluginSettingsError("Plugin package path is required", status_code=400)
    if not package.is_file():
        raise PluginSettingsError("Plugin package path must be an existing file", status_code=400)
    if package.suffix.lower() != ".zip":
        raise PluginSettingsError("Plugin package must be a .zip file", status_code=400)

    source_descriptor = {
        "source": "file",
        "path": str(package.resolve()),
        "marketplace": str(marketplace or "local").strip() or "local",
    }
    policy.assert_source_allowed(
        _marketplace_source_descriptor
        if _trusted_marketplace and isinstance(_marketplace_source_descriptor, Mapping)
        else source_descriptor
    )
    policy.assert_plugin_installable(
        plugin_id_for_name(_plugin_name_from_package(package), marketplace),
        trusted_marketplace=_trusted_marketplace,
    )

    install_root = plugin_install_root()
    install_root.mkdir(parents=True, exist_ok=True)
    tmp_root = install_root / ".package-imports"
    tmp_root.mkdir(parents=True, exist_ok=True)
    token = _safe_plugin_folder_name(package.stem) or "plugin-package"
    tmp_extract = tmp_root / f"{token}.{os.getpid()}.tmp"
    if tmp_extract.exists():
        _remove_within_root(tmp_extract, install_root)
    tmp_extract.mkdir(parents=True)
    try:
        _extract_plugin_package(package, tmp_extract)
        source = _normalized_extracted_plugin_dir(tmp_extract)
        return await _install_plugin_directory(
            source,
            overwrite=overwrite,
            settings_file=settings_file,
            config_change_hook=config_change_hook,
            import_kind="package",
            package_path=package,
            policy=policy,
            trusted_marketplace=_trusted_marketplace,
            marketplace_source_descriptor=_marketplace_source_descriptor,
            source_descriptor=source_descriptor,
        )
    finally:
        if tmp_extract.exists():
            _remove_within_root(tmp_extract, install_root)


async def _install_plugin_directory(
    source: Path,
    *,
    overwrite: bool,
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
    import_kind: str,
    package_path: Path | None = None,
    policy: ManagedPluginPolicy | None = None,
    source_descriptor: Mapping[str, Any] | None = None,
    trusted_marketplace: bool = False,
    marketplace_source_descriptor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not _is_plugin_directory(source):
        raise PluginSettingsError(
            "Plugin directory must contain .minicode-plugin/plugin.json",
            status_code=400,
        )
    linked_paths = _plugin_symlink_paths(source)
    if linked_paths:
        raise PluginSettingsError(
            "Plugin directories cannot contain symbolic links: " + ", ".join(linked_paths[:3]),
            status_code=400,
        )

    plugin_name = plugin_name_from_directory(source)
    folder_name = _safe_plugin_folder_name(plugin_name or source.name)
    if not folder_name:
        raise PluginSettingsError("Plugin name could not be resolved", status_code=400)
    effective_policy = policy or _plugin_policy_from_stack()
    if trusted_marketplace and not isinstance(marketplace_source_descriptor, Mapping):
        raise PluginSettingsError(
            "Trusted marketplace installs require the registered source provenance",
            status_code=403,
        )
    policy_source = (
        marketplace_source_descriptor
        if trusted_marketplace and isinstance(marketplace_source_descriptor, Mapping)
        else source_descriptor
        or {"source": "directory", "path": str(source.resolve())}
    )
    effective_policy.assert_source_allowed(policy_source)
    manifest_path = _primary_plugin_manifest(source)
    manifest_payload = _read_plugin_manifest(manifest_path) if manifest_path is not None else None
    marketplace = str((source_descriptor or {}).get("marketplace") or "local").strip() or "local"
    try:
        parse_plugin_id_strict(plugin_name, marketplace)
    except ValueError as exc:
        raise PluginSettingsError(f"Invalid plugin identity: {exc}", status_code=400) from exc
    plugin_id = plugin_id_for_name(plugin_name, marketplace)
    effective_policy.assert_plugin_installable(
        plugin_id,
        trusted_marketplace=trusted_marketplace,
    )
    validation = validate_plugin_directory(source)
    if not validation.get("ok"):
        first_error = str(
            (validation.get("errors") or ["Plugin validation failed"])[0]
        )
        raise PluginSettingsError(first_error, status_code=400)

    install_root = plugin_install_root()
    install_root.mkdir(parents=True, exist_ok=True)
    destination_folder = (
        folder_name
        if marketplace.casefold() == "local"
        else _safe_plugin_folder_name(f"{plugin_name}@{marketplace}")
    )
    destination = install_root / destination_folder
    source_resolved = source.resolve()
    install_root_resolved = install_root.resolve()
    destination_resolved = destination.resolve() if destination.exists() else (install_root_resolved / folder_name)
    if not _is_relative_to(destination_resolved, install_root_resolved):
        raise PluginSettingsError("Plugin destination escapes the plugin root", status_code=400)
    if _same_path(source_resolved, destination_resolved):
        return {
            **get_plugin_settings(policy=effective_policy),
            "imported": {
                "name": plugin_name,
                "id": plugin_id,
                "path": str(destination),
                "already_installed": True,
            },
        }
    if _is_relative_to(install_root_resolved, source_resolved):
        raise PluginSettingsError("Cannot import a plugin directory into itself", status_code=400)
    if destination.exists() and not overwrite:
        raise PluginSettingsError(f"Plugin '{plugin_name}' is already installed", status_code=409)

    token = uuid4().hex
    tmp_destination = install_root / f".{folder_name}.{token}.tmp"
    backup_destination = install_root / f".{folder_name}.{token}.backup"
    destination_replaced = False
    backup_created = False
    # Run the hook before any destination replacement or settings write.  A
    # blocked ConfigChange must leave both the compatibility projection and
    # the versioned store untouched.
    hook_result = await config_change_hook(source="plugins", file_path=str(settings_file))
    try:
        raise_if_config_change_blocked(
            hook_result,
            source="plugins",
            file_path=str(settings_file),
        )
    except Exception as exc:
        raise PluginSettingsError(str(exc), status_code=409) from exc
    try:
        shutil.copytree(
            source,
            tmp_destination,
            ignore=shutil.ignore_patterns(
                ".git",
                "__pycache__",
                ".pytest_cache",
                "node_modules",
                "dist",
                "build",
            ),
        )
        if destination.exists():
            if not overwrite:
                raise PluginSettingsError(
                    f"Plugin '{plugin_name}' is already installed",
                    status_code=409,
                )
            destination.replace(backup_destination)
            backup_created = True
        tmp_destination.replace(destination)
        destination_replaced = True

        # An explicit install is an explicit activation. Persist that positive
        # selection before exposing the materialized directory to consumers.
        def activate_plugin(settings_data: dict[str, Any]) -> None:
            _persist_user_plugin_enablement(settings_data, plugin_id, True)

        _update_settings_json(activate_plugin)

    except Exception:
        if destination_replaced and destination.exists():
            _remove_within_root(destination, install_root)
        if backup_created and backup_destination.exists():
            backup_destination.replace(destination)
        if tmp_destination.exists():
            _remove_within_root(tmp_destination, install_root)
        raise
    if backup_destination.exists():
        _remove_within_root(backup_destination, install_root)

    # Materialize a versioned provenance copy as the canonical store.  The
    # flat destination remains as a compatibility projection for existing
    # clients; runtime discovery can select the active version from the store
    # without exposing historical versions.
    store_record: dict[str, Any] | None = None
    try:
        from backend.plugins.store import PluginStore

        stored = PluginStore().materialize(
            source,
            name=plugin_name,
            marketplace=marketplace,
            version=str((manifest_payload or {}).get("version") or "local"),
            source=policy_source,
            activate=True,
            overwrite=overwrite,
        )
        store_record = stored.to_dict()
    except Exception as exc:
        # A read-only/legacy home must not make a validated flat import fail;
        # expose the degradation explicitly so callers can reconcile later.
        logger.warning("Failed to materialize versioned plugin store for %s: %s", plugin_id, exc)

    # cc surfaces declared-but-missing plugin dependencies instead of letting
    # an install silently proceed without them (installedPluginsManager tracks
    # the dependency closure). Report them so the UI and caller can reconcile.
    declared_dependencies = (manifest_payload or {}).get("dependencies")
    if isinstance(declared_dependencies, str):
        declared_dependencies = [declared_dependencies]
    missing_dependencies: list[str] = []
    if isinstance(declared_dependencies, list):
        installed_ids = {
            str(entry.get("id") or entry.get("name") or "").strip()
            for entry in get_plugin_settings(policy=effective_policy).get("plugins", [])
            if isinstance(entry, dict)
        }
        for dependency in declared_dependencies:
            token = str(dependency or "").strip()
            if token and token not in installed_ids:
                missing_dependencies.append(token)

    return {
        **get_plugin_settings(policy=effective_policy),
        "imported": {
            "name": plugin_name,
            "id": plugin_id,
            "path": str(destination),
            "already_installed": False,
            "kind": import_kind,
            **({"package_path": str(package_path)} if package_path is not None else {}),
            **({"store": store_record} if store_record is not None else {"store_error": "versioned store unavailable"}),
            **({"missing_dependencies": missing_dependencies} if missing_dependencies else {}),
        },
    }


def validate_plugin_directory(source_path: str | Path) -> dict[str, Any]:
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)

    source = _resolve_plugin_source_directory(source_path)
    manifest_paths = _plugin_manifest_paths(source)
    warnings: list[str] = []
    errors: list[str] = []
    manifests: list[dict[str, Any]] = []

    if not manifest_paths:
        errors.append("Plugin must contain .minicode-plugin/plugin.json.")

    for manifest_path in manifest_paths:
        manifest_result = _validate_plugin_manifest(manifest_path)
        manifests.append(manifest_result["manifest"])
        warnings.extend(manifest_result["warnings"])
        errors.extend(manifest_result["errors"])

    skill_count = _count_plugin_skills(source)
    component_counts = _manifest_component_counts(manifest_paths[0]) if manifest_paths else {
        "mcp_server_count": 0,
        "app_count": 0,
        "hook_count": 0,
        "extension_count": 0,
    }
    if skill_count <= 0 and not any(component_counts.values()):
        warnings.append(
            "Plugin manifest does not expose skills, MCP servers, apps, hooks, or extensions."
        )

    linked_paths = _plugin_symlink_paths(source)
    if linked_paths:
        errors.append(
            "Plugin directories cannot contain symbolic links: "
            + ", ".join(linked_paths[:3])
        )

    files, excluded = _collect_packable_plugin_files(source)
    if not files:
        errors.append("Plugin directory has no packageable files.")

    metadata = _plugin_metadata(source, manifest_paths)
    plugin_name = metadata.get("name") or plugin_name_from_directory(source)
    plugin = {
        "name": plugin_name,
        "version": metadata.get("version", ""),
        "description": metadata.get("description", ""),
        "path": str(source),
        "manifest_paths": [str(path) for path in manifest_paths],
        "skill_count": skill_count,
        **component_counts,
        "file_count": len(files),
        "total_bytes": sum(size for _path, _rel, size in files),
    }
    return {
        "ok": not errors,
        "plugin": plugin,
        "manifests": manifests,
        "warnings": warnings,
        "errors": errors,
        "excluded": excluded[:80],
    }


def load_enabled_plugin_hook_sources(
    *,
    config_stack: Any | None = None,
) -> list[dict[str, Any]]:
    """Return source-aware hook declarations for enabled MiniCode plugins."""
    sources: list[dict[str, Any]] = []
    seen_manifests: set[str] = set()
    for plugin in get_plugin_snapshot(config_stack=config_stack).get("plugins", []):
        if not isinstance(plugin, Mapping) or not bool(plugin.get("enabled")):
            continue
        plugin_root = Path(str(plugin.get("path") or "")).resolve()
        plugin_id = str(plugin.get("id") or plugin.get("name") or plugin_root.name).strip()
        plugin_data_root = plugin_install_root().parent / "data" / _safe_plugin_data_segment(plugin_id)
        for raw_manifest in plugin.get("manifest_paths", []):
            manifest_path = Path(str(raw_manifest))
            key = _filesystem_path_key(manifest_path)
            if key in seen_manifests:
                continue
            seen_manifests.add(key)
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(manifest, Mapping):
                continue
            payload = _declared_component_payload(
                plugin_root,
                manifest.get("hooks"),
                "hooks/hooks.json",
                "hooks",
            )
            for hook_map in _iter_hook_maps(payload):
                annotated: dict[str, Any] = {}
                for event_name, groups in hook_map.items():
                    if not isinstance(groups, list):
                        continue
                    copied_groups: list[Any] = []
                    for group in groups:
                        if not isinstance(group, Mapping):
                            continue
                        copied = dict(group)
                        handlers = copied.get("hooks")
                        if isinstance(handlers, list):
                            copied["hooks"] = [
                                {**dict(handler), "_plugin_root": str(plugin_root)}
                                if isinstance(handler, Mapping)
                                else handler
                                for handler in handlers
                            ]
                        copied_groups.append(copied)
                    if copied_groups:
                        annotated[str(event_name)] = copied_groups
                if annotated:
                    sources.append(
                        {
                            "hooks": annotated,
                            "source_path": str(manifest_path.resolve()),
                            "plugin_id": plugin_id,
                            "plugin_root": str(plugin_root),
                            "plugin_data_root": str(plugin_data_root),
                            "managed": bool(plugin.get("policy_managed")),
                        }
                    )
    return sources


def load_enabled_plugin_extension_sources(
    *,
    config_stack: Any | None = None,
) -> list[dict[str, Any]]:
    """Resolve enabled plugin extension declarations without executing code.

    Resolve package resources before handing executable extension paths to the
    loader. Active components come from the enabled snapshot rather than a
    scan of every materialized directory. This two-step boundary means this
    function returns lexical, root-contained paths plus provenance; the
    extension trust policy performs the final pre-import symlink/root check.

    Invalid declarations are returned as explicit error records so a broken
    plugin cannot disappear silently from diagnostics.
    """

    sources: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    snapshot = get_plugin_snapshot(config_stack=config_stack)
    for plugin in snapshot.get("plugins", []):
        if not isinstance(plugin, Mapping) or not bool(plugin.get("enabled")):
            continue
        raw_root = str(plugin.get("path") or "").strip()
        plugin_id = str(
            plugin.get("id") or plugin.get("name") or Path(raw_root).name
        ).strip()
        marketplace = str(plugin.get("marketplace") or "local").strip() or "local"
        managed = bool(plugin.get("policy_managed"))
        base = {
            "plugin_id": plugin_id,
            "marketplace": marketplace,
            "managed": managed,
            "scope": "managed" if managed else "user",
            "origin": "plugin",
            "plugin_fingerprint": str(snapshot.get("fingerprint") or ""),
        }
        if not raw_root:
            sources.append({**base, "error": "enabled plugin has no materialized root"})
            continue
        plugin_root = Path(raw_root).expanduser().absolute()
        try:
            resolved_root = plugin_root.resolve(strict=True)
        except OSError as exc:
            sources.append(
                {
                    **base,
                    "plugin_root": str(plugin_root),
                    "error": f"plugin root is unavailable: {exc}",
                }
            )
            continue
        if not resolved_root.is_dir():
            sources.append(
                {
                    **base,
                    "plugin_root": str(plugin_root),
                    "error": "plugin root is not a directory",
                }
            )
            continue
        base["plugin_root"] = str(plugin_root)

        for raw_manifest in plugin.get("manifest_paths", ()):
            manifest_path = Path(str(raw_manifest)).expanduser().absolute()
            try:
                manifest_resolved = manifest_path.resolve(strict=True)
                manifest_resolved.relative_to(resolved_root)
            except (OSError, ValueError) as exc:
                sources.append(
                    {
                        **base,
                        "source_path": str(manifest_path),
                        "error": f"plugin manifest is outside its materialized root: {exc}",
                    }
                )
                continue
            try:
                manifest = json.loads(manifest_resolved.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                sources.append(
                    {
                        **base,
                        "source_path": str(manifest_path),
                        "error": f"plugin manifest could not be read: {exc}",
                    }
                )
                continue
            if not isinstance(manifest, Mapping):
                sources.append(
                    {
                        **base,
                        "source_path": str(manifest_path),
                        "error": "plugin manifest must be an object",
                    }
                )
                continue
            declared = manifest.get("extensions")
            raw_paths = [declared] if isinstance(declared, str) else declared
            if declared is None:
                continue
            if not isinstance(raw_paths, list) or any(
                not isinstance(item, str) or not item.strip() for item in raw_paths
            ):
                sources.append(
                    {
                        **base,
                        "source_path": str(manifest_path),
                        "error": "extensions must be a relative path string or string array",
                    }
                )
                continue
            for raw_path in raw_paths:
                candidate = _declared_plugin_component_path(plugin_root, raw_path)
                if candidate is None:
                    sources.append(
                        {
                            **base,
                            "source_path": str(manifest_path),
                            "declaration": raw_path,
                            "error": "extension path is absolute or escapes the plugin root",
                        }
                    )
                    continue
                key = (plugin_id.casefold(), canonical_file_path_key(candidate))
                if key in seen:
                    continue
                seen.add(key)
                if not candidate.exists():
                    sources.append(
                        {
                            **base,
                            "source_path": str(manifest_path),
                            "path": str(candidate),
                            "declaration": raw_path,
                            "error": "declared extension path does not exist",
                        }
                    )
                    continue
                if candidate.is_file() and candidate.suffix.casefold() not in {
                    ".py",
                    ".pyw",
                }:
                    sources.append(
                        {
                            **base,
                            "source_path": str(manifest_path),
                            "path": str(candidate),
                            "declaration": raw_path,
                            "error": "declared extension file must use .py or .pyw",
                        }
                    )
                    continue
                if not candidate.is_file() and not candidate.is_dir():
                    sources.append(
                        {
                            **base,
                            "source_path": str(manifest_path),
                            "path": str(candidate),
                            "declaration": raw_path,
                            "error": "declared extension path is not a file or directory",
                        }
                    )
                    continue
                sources.append(
                    {
                        **base,
                        "source_path": str(manifest_path),
                        "path": str(candidate),
                        "declaration": raw_path,
                    }
                )
    return sources



def _iter_hook_maps(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, list):
        for item in payload:
            yield from _iter_hook_maps(item)
        return
    if not isinstance(payload, Mapping):
        return
    hooks = payload.get("hooks")
    yield hooks if isinstance(hooks, Mapping) else payload


def resolve_enabled_plugin_mentions(
    mentions: Iterable[Mapping[str, Any]],
    *,
    connected_mcp_servers: Iterable[str] = (),
    config_stack: Any | None = None,
) -> list[dict[str, Any]]:
    """Resolve structured ``plugin://`` mentions against enabled local plugins.

    Client-supplied display metadata is ignored, disabled or missing plugins
    are dropped, and only MCP
    servers that are actually connected for this session are advertised.
    """
    inventory_by_id: dict[str, Mapping[str, Any]] = {}
    inventory_by_name: dict[str, list[Mapping[str, Any]]] = {}
    for item in get_plugin_snapshot(config_stack=config_stack).get("plugins", []):
        if not isinstance(item, Mapping) or not bool(item.get("enabled")):
            continue
        raw_id = str(item.get("id") or item.get("name") or "").strip()
        identity = plugin_id_for_name(raw_id, str(item.get("marketplace") or "local"))
        key = normalize_plugin_id(identity)
        name_key = normalize_plugin_name(str(item.get("name") or parse_plugin_id(identity).name))
        if key:
            inventory_by_id[key] = item
        if name_key:
            inventory_by_name.setdefault(name_key, []).append(item)
    connected = {str(name).strip() for name in connected_mcp_servers if str(name).strip()}
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for mention in mentions:
        if not isinstance(mention, Mapping):
            continue
        config_name = str(
            mention.get("config_name")
            or mention.get("configName")
            or mention.get("name")
            or ""
        ).strip()
        path = str(mention.get("path") or "").strip()
        if path.startswith("plugin://"):
            config_name = path.removeprefix("plugin://").strip()
        key = normalize_plugin_id(config_name)
        plugin = inventory_by_id.get(key)
        if plugin is None and "@" not in config_name:
            candidates = inventory_by_name.get(normalize_plugin_name(config_name), [])
            plugin = candidates[0] if len(candidates) == 1 else None
        if plugin is None or key in seen:
            continue
        resolved_key = normalize_plugin_id(str(plugin.get("id") or plugin.get("name") or config_name))
        if resolved_key in seen:
            continue
        seen.add(resolved_key)
        declared_servers = {
            str(name).strip()
            for name in plugin.get("mcp_server_names", [])
            if str(name).strip()
        }
        resolved.append({
            "config_name": str(plugin.get("name") or config_name),
            "display_name": str(plugin.get("displayName") or plugin.get("name") or config_name),
            "description": str(plugin.get("shortDescription") or plugin.get("description") or ""),
            "has_skills": int(plugin.get("skill_count") or 0) > 0,
            "mcp_server_names": sorted(declared_servers & connected, key=str.casefold),
            # MiniCode does not expose plugin apps in the Agent runtime yet, so
            # do not claim they are available merely because the manifest lists them.
            "available_apps": [],
        })
    return resolved


def package_plugin_directory(source_path: str | Path, output_dir: str | Path | None = None) -> dict[str, Any]:
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)

    source = _resolve_plugin_source_directory(source_path)
    validation = validate_plugin_directory(source)
    if not validation.get("ok"):
        first_error = str((validation.get("errors") or ["Plugin validation failed"])[0])
        raise PluginSettingsError(first_error, status_code=400)

    files, excluded = _collect_packable_plugin_files(source)
    if not files:
        raise PluginSettingsError("Plugin directory has no packageable files", status_code=400)

    plugin = validation.get("plugin") if isinstance(validation.get("plugin"), Mapping) else {}
    plugin_name = str(plugin.get("name") or plugin_name_from_directory(source)).strip()
    version = str(plugin.get("version") or "dev").strip()
    filename = _plugin_package_filename(plugin_name, version)
    output_root = Path(str(output_dir)).expanduser() if output_dir else plugin_install_root().parent / "plugin-packages"
    output_root.mkdir(parents=True, exist_ok=True)
    package_path = output_root / filename
    tmp_path = output_root / f".{filename}.{uuid4().hex}.tmp"

    with zipfile.ZipFile(tmp_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path, rel_path, _size in files:
            archive.write(file_path, rel_path)
    tmp_path.replace(package_path)

    return {
        "ok": True,
        "package": {
            "name": filename,
            "path": str(package_path),
            "file_count": len(files),
            "total_bytes": sum(size for _path, _rel, size in files),
        },
        "validation": {
            **validation,
            "excluded": excluded[:80],
        },
    }


def _resolve_plugin_inventory_item(
    requested: str,
    inventory: Any,
) -> Mapping[str, Any]:
    normalized = normalize_plugin_id(requested)
    requested_name = normalize_plugin_name(parse_plugin_id(requested).name)
    candidates = [
        item
        for item in inventory
        if isinstance(item, Mapping)
        and (
            normalize_plugin_id(str(item.get("id") or item.get("name") or ""), str(item.get("marketplace") or "local")) == normalized
            or (
                "@" not in str(requested)
                and normalize_plugin_name(str(item.get("name") or "")) == requested_name
            )
        )
    ] if isinstance(inventory, Iterable) else []
    if not candidates:
        raise PluginSettingsError(f"Plugin '{requested}' was not found", status_code=404)
    if len(candidates) > 1:
        choices = ", ".join(
            sorted(str(item.get("id") or item.get("name") or "") for item in candidates)
        )
        raise PluginSettingsError(
            f"Plugin name '{requested}' is ambiguous; use one of: {choices}",
            status_code=409,
        )
    return candidates[0]


def _merge_manifest_metadata(entry: dict[str, Any], manifest_path: Path) -> None:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(raw, Mapping):
        return
    if not entry.get("description"):
        entry["description"] = str(raw.get("description") or "").strip()
    if not entry.get("version"):
        entry["version"] = str(raw.get("version") or "").strip()
    if not entry.get("dependencies"):
        dependencies = raw.get("dependencies")
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        if isinstance(dependencies, list):
            entry["dependencies"] = sorted({
                str(item).strip() for item in dependencies
                if isinstance(item, str) and item.strip()
            }, key=str.casefold)
    interface = raw.get("interface") if isinstance(raw.get("interface"), Mapping) else {}
    scalar_fields = {
        "displayName": "displayName",
        "shortDescription": "shortDescription",
        "longDescription": "longDescription",
        "developerName": "developerName",
        "category": "category",
        "brandColor": "brandColor",
    }
    for target, source in scalar_fields.items():
        if not entry.get(target):
            entry[target] = str(interface.get(source) or "").strip()
    if not entry.get("websiteUrl"):
        website_url = str(interface.get("websiteUrl") or interface.get("websiteURL") or "").strip()
        if website_url.startswith(("https://", "http://")):
            entry["websiteUrl"] = website_url
    if not entry.get("capabilities"):
        capabilities = interface.get("capabilities")
        if isinstance(capabilities, list):
            entry["capabilities"] = [str(item).strip() for item in capabilities if str(item).strip()]
    if not entry.get("defaultPrompt"):
        default_prompt = interface.get("defaultPrompt")
        if isinstance(default_prompt, str) and default_prompt.strip():
            entry["defaultPrompt"] = [default_prompt.strip()]
        elif isinstance(default_prompt, list):
            entry["defaultPrompt"] = [str(item).strip() for item in default_prompt if str(item).strip()]
    if not entry.get("iconVariant"):
        for field, variant in (("logo", "logo"), ("composerIcon", "composer"), ("logoDark", "logo-dark")):
            if _resolve_manifest_asset(manifest_path, interface.get(field)) is not None:
                entry["iconVariant"] = variant
                break


def _resolve_manifest_asset(manifest_path: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    plugin_dir = plugin_directory_for_manifest(manifest_path)
    try:
        candidate = (plugin_dir / value).resolve()
        candidate.relative_to(plugin_dir.resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def resolve_plugin_asset(plugin_path: str | Path, variant: str) -> Path | None:
    """Resolve one official interface asset from an installed plugin."""
    try:
        requested = Path(plugin_path).expanduser().resolve()
    except OSError:
        return None
    installed = {
        Path(str(entry.get("path") or "")).expanduser().resolve()
        for entry in get_plugin_settings().get("plugins", [])
        if str(entry.get("path") or "").strip()
    }
    if requested not in installed:
        return None
    manifest = _primary_plugin_manifest(requested)
    if manifest is None:
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    interface = payload.get("interface") if isinstance(payload, Mapping) else None
    if not isinstance(interface, Mapping):
        return None
    field = {"composer": "composerIcon", "logo": "logo", "logo-dark": "logoDark"}.get(variant)
    return _resolve_manifest_asset(manifest, interface.get(field)) if field else None


def _resolve_plugin_source_directory(source_path: str | Path) -> Path:
    source_text = str(source_path or "").strip()
    if not source_text:
        raise PluginSettingsError("Plugin source path is required", status_code=400)
    source = Path(source_text).expanduser()
    if not source.is_dir():
        raise PluginSettingsError("Plugin source path must be an existing directory", status_code=400)
    try:
        return source.resolve()
    except OSError:
        return source.absolute()


def _looks_like_remote_plugin_source(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text.startswith(("http://", "https://", "git@", "ssh://", "npm:")):
        return True
    # Accept GitHub shorthand (owner/repo or owner/repo#ref).
    if "/" in text and not text.startswith(("./", "../", ".\\", "..\\", "/")):
        parts = text.split("/", 2)
        if len(parts) == 2 and all(parts) and all(
            all(char.isalnum() or char in "-_.@#" for char in part)
            for part in parts
        ):
            return True
    return False


def _plugin_manifest_paths(plugin_dir: Path) -> list[Path]:
    manifest = plugin_manifest_path(plugin_dir)
    return [manifest] if manifest.is_file() else []


def _plugin_metadata(plugin_dir: Path, manifest_paths: list[Path]) -> dict[str, str]:
    metadata = {
        "name": plugin_dir.name,
        "version": "",
        "description": "",
    }
    for manifest_path in manifest_paths:
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(raw, Mapping):
            continue
        if not metadata["name"] or metadata["name"] == plugin_dir.name:
            name = str(raw.get("name") or "").strip()
            if name:
                metadata["name"] = name
        if not metadata["version"]:
            metadata["version"] = str(raw.get("version") or "").strip()
        if not metadata["description"]:
            metadata["description"] = str(raw.get("description") or "").strip()
    return metadata


def _validate_plugin_manifest(manifest_path: Path) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    rel_manifest = str(manifest_path)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "manifest": {"path": rel_manifest},
            "warnings": warnings,
            "errors": [f"{rel_manifest}: invalid JSON at line {exc.lineno} column {exc.colno}"],
        }
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "manifest": {"path": rel_manifest},
            "warnings": warnings,
            "errors": [f"{rel_manifest}: could not be read: {exc}"],
        }

    if not isinstance(raw, Mapping):
        return {
            "manifest": {"path": rel_manifest},
            "warnings": warnings,
            "errors": [f"{rel_manifest}: manifest must be a JSON object."],
        }
    plugin_name = str(raw.get("name") or "").strip()
    if not plugin_name:
        errors.append(f"{rel_manifest}: manifest name is required.")
    elif not is_valid_identifier(
        plugin_name,
        str(raw.get("marketplace") or "local").strip() or "local",
    ):
        errors.append(
            f"{rel_manifest}: name/marketplace contains unsafe characters; use a canonical plugin id."
        )
    else:
        try:
            parse_plugin_id_strict(
                plugin_name,
                str(raw.get("marketplace") or "local").strip() or "local",
            )
        except ValueError as exc:
            errors.append(f"{rel_manifest}: invalid plugin identity: {exc}")
    unsupported = sorted(str(key) for key in raw.keys() if str(key) not in _PLUGIN_MANIFEST_FIELDS)
    if unsupported:
        errors.append(
            f"{rel_manifest}: unsupported manifest fields: {', '.join(unsupported)}."
        )
    skills = raw.get("skills")
    if skills is not None and not (
        isinstance(skills, str)
        or isinstance(skills, list) and all(isinstance(item, str) for item in skills)
    ):
        errors.append(f"{rel_manifest}: skills must be a relative path string or string array.")
    hooks = raw.get("hooks")
    if hooks is not None and not (
        isinstance(hooks, (str, Mapping))
        or isinstance(hooks, list) and all(isinstance(item, (str, Mapping)) for item in hooks)
    ):
        errors.append(f"{rel_manifest}: hooks must be a path, path array, object, or object array.")
    extensions = raw.get("extensions")
    if extensions is not None and not (
        isinstance(extensions, str)
        or isinstance(extensions, list)
        and all(isinstance(item, str) and item.strip() for item in extensions)
    ):
        errors.append(
            f"{rel_manifest}: extensions must be a relative path string or string array."
        )
    if isinstance(extensions, str):
        extensions = [extensions]
    if isinstance(extensions, list):
        plugin_root = plugin_directory_for_manifest(manifest_path)
        for extension_path in extensions:
            if not str(extension_path).strip() or _declared_plugin_component_path(
                plugin_root, str(extension_path)
            ) is None:
                errors.append(
                    f"{rel_manifest}: extension path escapes the plugin root: {extension_path!r}."
                )
    apps = raw.get("apps")
    if apps is not None and not isinstance(apps, str):
        errors.append(f"{rel_manifest}: apps must be a relative path string.")
    mcp_servers = raw.get("mcp_servers")
    if mcp_servers is not None and not isinstance(mcp_servers, (str, Mapping)):
        errors.append(f"{rel_manifest}: mcp_servers must be a relative path string or object.")
    dependencies = raw.get("dependencies")
    if dependencies is not None and not (
        isinstance(dependencies, str)
        or isinstance(dependencies, list)
        and all(isinstance(item, str) and item.strip() for item in dependencies)
    ):
        errors.append(f"{rel_manifest}: dependencies must be a plugin id string or string array.")
    if isinstance(dependencies, str) and not dependencies.strip():
        errors.append(f"{rel_manifest}: dependencies must not contain an empty id.")
    marketplace = raw.get("marketplace")
    if marketplace is not None and (
        not isinstance(marketplace, str) or not marketplace.strip()
    ):
        errors.append(f"{rel_manifest}: marketplace must be a non-empty string.")
    source = raw.get("source")
    if source is not None and not isinstance(source, Mapping):
        errors.append(f"{rel_manifest}: source must be an object.")
    interface = raw.get("interface")
    if interface is not None and not isinstance(interface, Mapping):
        errors.append(f"{rel_manifest}: interface must be an object.")

    return {
        "manifest": {
            "path": rel_manifest,
            "plugin_name": plugin_name,
            **_manifest_component_counts(manifest_path),
        },
        "warnings": warnings,
        "errors": errors,
    }


def _collect_packable_plugin_files(plugin_dir: Path) -> tuple[list[tuple[Path, str, int]], list[str]]:
    files: list[tuple[Path, str, int]] = []
    excluded: list[str] = []
    total_bytes = 0
    for root, dirnames, filenames in os.walk(plugin_dir):
        root_path = Path(root)
        kept_dirs: list[str] = []
        for dirname in dirnames:
            rel = _relative_plugin_path(root_path / dirname, plugin_dir)
            if dirname in _PLUGIN_EXCLUDED_DIRS or dirname.startswith(".") and dirname != PLUGIN_MANIFEST_DIRECTORY:
                excluded.append(rel)
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            file_path = root_path / filename
            rel = _relative_plugin_path(file_path, plugin_dir)
            if filename in _PLUGIN_EXCLUDED_FILES or filename.endswith((".pyc", ".pyo", ".tmp", ".log")):
                excluded.append(rel)
                continue
            try:
                size = file_path.stat().st_size
            except OSError:
                excluded.append(rel)
                continue
            if size > _MAX_PLUGIN_PACKAGE_FILE_BYTES:
                excluded.append(rel)
                continue
            if total_bytes + size > _MAX_PLUGIN_PACKAGE_BYTES:
                excluded.append(rel)
                continue
            files.append((file_path, rel, size))
            total_bytes += size
    files.sort(key=lambda item: item[1].lower())
    return files, sorted(excluded)


def _plugin_symlink_paths(plugin_dir: Path, *, limit: int = 20) -> list[str]:
    """Return in-tree links without ever traversing their targets.

    Plugin packages must be self-contained. Following a directory-import link
    would otherwise silently copy files from outside the selected plugin, and
    package creation would archive those external files under a trusted name.
    """
    found: list[str] = []
    for root, dirnames, filenames in os.walk(plugin_dir, followlinks=False):
        root_path = Path(root)
        for name in [*dirnames, *filenames]:
            candidate = root_path / name
            if not candidate.is_symlink():
                continue
            found.append(_relative_plugin_path(candidate, plugin_dir))
            if len(found) >= limit:
                return found
    return found


def _extract_plugin_package(package_path: Path, destination: Path) -> None:
    try:
        archive = zipfile.ZipFile(package_path)
    except zipfile.BadZipFile as exc:
        raise PluginSettingsError("Plugin package is not a valid zip file", status_code=400) from exc
    with archive:
        for info, rel in _validated_plugin_package_infos(archive):
            target = destination / rel
            try:
                target.resolve().relative_to(destination.resolve())
            except ValueError as exc:
                raise PluginSettingsError(f"Plugin package escapes destination: {info.filename}", status_code=400) from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _plugin_name_from_package(package_path: Path) -> str:
    try:
        archive = zipfile.ZipFile(package_path)
    except zipfile.BadZipFile as exc:
        raise PluginSettingsError("Plugin package is not a valid zip file", status_code=400) from exc
    with archive:
        candidates = [
            (info, rel)
            for info, rel in _validated_plugin_package_infos(archive)
            if Path(rel).parts[-2:] == (PLUGIN_MANIFEST_DIRECTORY, "plugin.json")
        ]
        if not candidates:
            raise PluginSettingsError(
                "Plugin package must contain .minicode-plugin/plugin.json",
                status_code=400,
            )
        candidates.sort(key=lambda item: (len(Path(item[1]).parts), item[1].casefold()))
        try:
            raw = json.loads(archive.read(candidates[0][0]).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PluginSettingsError(
                "Plugin package manifest is not valid UTF-8 JSON",
                status_code=400,
            ) from exc
        name = str(raw.get("name") or "").strip() if isinstance(raw, Mapping) else ""
        if not name:
            raise PluginSettingsError(
                "Plugin package manifest name is required",
                status_code=400,
            )
        return name


def _validated_plugin_package_infos(
    archive: zipfile.ZipFile,
) -> list[tuple[zipfile.ZipInfo, str]]:
    infos = [info for info in archive.infolist() if not info.is_dir()]
    if not infos:
        raise PluginSettingsError("Plugin package is empty", status_code=400)
    if len(infos) > _MAX_PLUGIN_ARCHIVE_ENTRIES:
        raise PluginSettingsError("Plugin package has too many files", status_code=400)
    validated: list[tuple[zipfile.ZipInfo, str]] = []
    total_bytes = 0
    for info in infos:
        rel = _safe_zip_member_path(info.filename)
        if rel is None:
            raise PluginSettingsError(
                f"Plugin package contains unsafe path: {info.filename}",
                status_code=400,
            )
        if Path(rel).parts[0] in _PLUGIN_EXCLUDED_DIRS:
            continue
        if info.file_size > _MAX_PLUGIN_PACKAGE_FILE_BYTES:
            raise PluginSettingsError(
                f"Plugin package file is too large: {rel}",
                status_code=400,
            )
        total_bytes += info.file_size
        if total_bytes > _MAX_PLUGIN_PACKAGE_BYTES:
            raise PluginSettingsError("Plugin package is too large", status_code=400)
        validated.append((info, rel))
    return validated


def _safe_zip_member_path(name: str) -> str | None:
    raw = str(name or "").replace("\\", "/")
    if raw.startswith("/") or raw.startswith("~"):
        return None
    normalized = raw.strip("/")
    if not normalized:
        return None
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return None
    if re.match(r"^[A-Za-z]:", parts[0]):
        return None
    return "/".join(parts)


def _safe_plugin_data_segment(plugin_identity: str) -> str:
    parsed = parse_plugin_id(plugin_identity)
    name = parsed.name or "plugin"
    marketplace = parsed.marketplace or "local"
    # Keep the segment stable across platforms and independent of user input.
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in f"{name}-{marketplace}")


def _normalized_extracted_plugin_dir(extract_root: Path) -> Path:
    if _is_plugin_directory(extract_root):
        return extract_root
    children = [child for child in extract_root.iterdir() if child.is_dir()]
    if len(children) == 1 and _is_plugin_directory(children[0]):
        return children[0]
    raise PluginSettingsError(
            "Plugin package must contain .minicode-plugin/plugin.json at the root",
            status_code=400,
        )


def _relative_plugin_path(path: Path, root: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = path.absolute().relative_to(root.absolute())
    return rel.as_posix()


def _plugin_package_filename(plugin_name: str, version: str) -> str:
    base = _safe_plugin_folder_name(plugin_name) or "plugin"
    version_part = _safe_plugin_folder_name(version) or "dev"
    return f"{base}-{version_part}.zip"


def _count_plugin_skills(plugin_dir: Path) -> int:
    names: set[str] = set()
    skills_dirs: list[Path] = []
    manifest_path = _primary_plugin_manifest(plugin_dir)
    try:
        if manifest_path is None:
            raise FileNotFoundError(str(plugin_dir))
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        configured = raw.get("skills") if isinstance(raw, Mapping) else None
        configured_paths = [configured] if isinstance(configured, str) else configured if isinstance(configured, list) else []
        for configured_path in configured_paths:
            if not isinstance(configured_path, str) or not configured_path.strip():
                continue
            candidate = (plugin_dir / configured_path).resolve()
            candidate.relative_to(plugin_dir.resolve())
            skills_dirs.append(candidate)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    if not skills_dirs:
        skills_dirs.append(plugin_dir / "skills")
    for skills_dir in skills_dirs:
        if not skills_dir.is_dir():
            continue
        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").is_file():
                names.add(skill_dir.name)
    return len(names)


def _manifest_component_counts(manifest_path: Path) -> dict[str, int]:
    try:
        raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        raw = {}
    if not isinstance(raw, Mapping):
        raw = {}
    plugin_dir = plugin_directory_for_manifest(Path(manifest_path))
    mcp_servers = _declared_component_payload(
        plugin_dir,
        raw.get("mcp_servers"),
        ".mcp.json",
        "servers",
    )
    apps = _declared_component_payload(plugin_dir, raw.get("apps"), ".app.json", "apps")
    hooks = _declared_component_payload(plugin_dir, raw.get("hooks"), "hooks/hooks.json", "hooks")
    extensions = raw.get("extensions")
    extension_paths = (
        [extensions]
        if isinstance(extensions, str) and extensions.strip()
        else [
            item
            for item in extensions
            if isinstance(item, str) and item.strip()
        ]
        if isinstance(extensions, list)
        else []
    )
    return {
        "mcp_server_count": len(mcp_servers) if isinstance(mcp_servers, Mapping) else int(bool(mcp_servers)),
        "app_count": len(apps) if isinstance(apps, Mapping) else int(bool(apps)),
        "hook_count": _count_hook_declarations(hooks),
        "extension_count": len(extension_paths),
    }


def _manifest_mcp_server_names(manifest_path: Path) -> list[str]:
    try:
        raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(raw, Mapping):
        return []
    plugin_dir = plugin_directory_for_manifest(Path(manifest_path))
    servers = _declared_component_payload(
        plugin_dir,
        raw.get("mcp_servers"),
        ".mcp.json",
        "servers",
    )
    if not isinstance(servers, Mapping):
        return []
    return sorted(
        {str(name).strip() for name in servers if str(name).strip()},
        key=str.casefold,
    )


def _declared_component_payload(
    plugin_dir: Path,
    declaration: Any,
    default_path: str,
    container_key: str,
) -> Any:
    if isinstance(declaration, Mapping):
        return declaration.get(container_key, declaration)
    inline_payloads: list[Any] = []
    if isinstance(declaration, list) and container_key == "hooks":
        paths = []
        for item in declaration:
            if isinstance(item, str) and item.strip():
                paths.append(item)
            elif isinstance(item, Mapping):
                inline_payloads.append(item.get(container_key, item))
    else:
        paths = [declaration] if isinstance(declaration, str) and declaration.strip() else [default_path]
    payloads: list[Any] = []
    for raw_path in paths:
        candidate = (plugin_dir / str(raw_path)).resolve()
        try:
            candidate.relative_to(plugin_dir.resolve())
        except ValueError:
            continue
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(payload, Mapping):
            payloads.append(payload.get(container_key, payload))
    payloads = [*inline_payloads, *payloads]
    if len(payloads) == 1:
        return payloads[0]
    return payloads


def _count_hook_declarations(payload: Any) -> int:
    if isinstance(payload, list):
        return sum(_count_hook_declarations(item) for item in payload)
    if not isinstance(payload, Mapping):
        return 0
    hooks = payload.get("hooks") if isinstance(payload.get("hooks"), Mapping) else payload
    total = 0
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            handlers = entry.get("hooks")
            total += len(handlers) if isinstance(handlers, list) else int(bool(entry.get("command")))
    return total


def _is_plugin_directory(path: Path) -> bool:
    return plugin_manifest_path(path).is_file()


def _declared_plugin_component_path(plugin_root: Path, declaration: str) -> Path | None:
    """Return a lexical in-root component path, preserving symlink evidence."""

    raw = str(declaration or "").strip()
    if (
        not raw
        or "\x00" in raw
        or raw.startswith(("/", "\\", "~"))
        or re.match(r"^[A-Za-z]:", raw)
    ):
        return None
    lexical = (plugin_root / raw).expanduser().absolute()
    try:
        lexical.resolve(strict=False).relative_to(plugin_root.resolve(strict=True))
    except (OSError, ValueError):
        return None
    return lexical


def _safe_plugin_folder_name(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name or "").strip()).strip(".-")
    return value[:80]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.absolute() == right.absolute()


def _remove_within_root(path: Path, root: Path) -> None:
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    if not _is_relative_to(path_resolved, root_resolved) or path_resolved == root_resolved:
        raise PluginSettingsError("Refusing to remove a path outside the plugin root", status_code=400)
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
