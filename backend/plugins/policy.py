"""Managed plugin marketplace policy: projection, validation, and source matching."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping
from urllib.parse import urlsplit

from backend.plugins.identity import (
    has_explicit_marketplace,
    normalize_plugin_id,
    parse_plugin_id,
    parse_plugin_id_strict,
    version_satisfies,
)

class PluginSettingsError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code

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

def normalize_plugin_name(name: str) -> str:
    return str(name or "").strip().casefold()

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
