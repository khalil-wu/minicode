"""Canonical plugin identity and version-constraint helpers.

Claude and Codex use ``<name>@<marketplace>`` as the stable plugin identity.
An enabled-plugin setting may append one or more version constraints; those
constraints are selection metadata and must not become part of the identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

try:  # packaging is bundled with the desktop runtime, but keep a safe fallback
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version
except ImportError:  # pragma: no cover - exercised only in minimal runtimes
    InvalidSpecifier = InvalidVersion = ValueError  # type: ignore[assignment,misc]
    SpecifierSet = Version = None  # type: ignore[assignment,misc]

PluginId = str


@dataclass(frozen=True)
class PluginIdentifier:
    name: str
    marketplace: str
    constraint: str = ""

    @property
    def id(self) -> PluginId:
        return f"{self.name}@{self.marketplace}"


_VERSION_MARKER = re.compile(r"^(?P<base>[^@]+@[^@]+)(?:@(?P<constraint>.+))?$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SCOPED_NAME_RE = re.compile(r"^@[A-Za-z0-9._-]+/[A-Za-z0-9][A-Za-z0-9._-]*$")
_MARKETPLACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def parse_plugin_id(value: Any, marketplace: str = "local") -> PluginIdentifier:
    """Parse a bare/id/constraint mention without silently changing names.

    The first ``@`` separates name from marketplace.  A second ``@`` is
    treated as a version constraint (the syntax used by managed Claude
    settings).  Whitespace around all components is ignored.  Empty values
    use the explicit ``marketplace`` fallback, which keeps legacy local plugin
    directories addressable while making the resulting identity stable.
    """

    raw = str(value or "").strip()
    fallback_marketplace = str(marketplace or "local").strip() or "local"
    if not raw:
        return PluginIdentifier("", fallback_marketplace)

    # A version constraint can contain comparison operators and commas but
    # not an unescaped @.  Splitting at most twice preserves scoped names such
    # as ``@scope/plugin@marketplace`` reasonably: the final @ is the
    # marketplace separator when there are at least two components.
    parts = raw.split("@")
    if raw.startswith("@"):
        # npm-style scoped name: @scope/name@marketplace[@constraint].  A
        # bare scoped name (without a marketplace) remains a name and uses
        # the fallback marketplace.
        slash = raw.find("/")
        separator = raw.find("@", 1)
        if slash > 1 and separator > slash:
            name = raw[:separator]
            rest = raw[separator + 1 :].split("@")
        else:
            return PluginIdentifier(raw, fallback_marketplace)
    elif len(parts) >= 2:
        name = parts[0]
        rest = parts[1:]
    else:
        name = raw
        rest = []

    name = name.strip()
    if not rest:
        return PluginIdentifier(name, fallback_marketplace)
    selected_marketplace = rest[0].strip() or fallback_marketplace
    constraint = "@".join(part.strip() for part in rest[1:] if part.strip())
    return PluginIdentifier(name, selected_marketplace, constraint)


def parse_plugin_id_strict(value: Any, marketplace: str = "local") -> PluginIdentifier:
    raw = str(value or "").strip()
    if not raw or any(char.isspace() for char in raw) or "/" in raw or "\\" in raw:
        raise ValueError("plugin id must not contain whitespace or path separators")
    parsed = parse_plugin_id(raw, marketplace)
    if not is_valid_identifier(parsed.name, parsed.marketplace):
        raise ValueError("plugin id contains unsupported characters")
    if raw.count("@") > 2 or raw.endswith("@") or "@@" in raw:
        raise ValueError("plugin id has malformed marketplace/constraint separators")
    if parsed.constraint and not version_constraint_is_valid(parsed.constraint):
        raise ValueError("plugin id has an invalid version constraint")
    return parsed


def plugin_id(value: Any, marketplace: str = "local") -> PluginId:
    """Return the canonical ``name@marketplace`` identity."""

    parsed = parse_plugin_id(value, marketplace)
    if not is_valid_identifier(parsed.name, parsed.marketplace):
        return ""
    return parsed.id


def is_valid_identifier(name: Any, marketplace: Any = "local") -> bool:
    name_text = str(name or "").strip()
    marketplace_text = str(marketplace or "").strip()
    if not name_text or not marketplace_text:
        return False
    if name_text in {".", ".."} or marketplace_text in {".", ".."}:
        return False
    if ".." in name_text or ".." in marketplace_text:
        return False
    if "/" in marketplace_text or "\\" in marketplace_text:
        return False
    # The shared Claude/Codex plugin-id schema is deliberately narrower than
    # npm package names: scoped ``@org/name`` values are marketplace source
    # descriptors, not runtime plugin identities.
    return bool(_NAME_RE.fullmatch(name_text) and _MARKETPLACE_RE.fullmatch(marketplace_text))


def normalize_plugin_id(value: Any, marketplace: str = "local") -> str:
    """Case-folded identity used only for map/set comparisons."""

    return plugin_id(value, marketplace).casefold()


def has_explicit_marketplace(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    if raw.startswith("@"):
        slash = raw.find("/")
        return raw.find("@", max(1, slash)) > slash if slash > 1 else False
    return "@" in raw


def version_satisfies(version: Any, constraints: Any) -> bool:
    """Evaluate npm/Claude-like constraints using PEP 440 where possible.

    Supported common forms include ``^1.2.3``, ``~1.2``, ``>=1,<2``, exact
    versions and arrays of constraints.  Invalid constraints fail closed so a
    managed policy can never accidentally enable an incompatible plugin.
    """

    raw_version = str(version or "").strip()
    if not raw_version:
        return not bool(constraints)
    raw_constraints = constraints
    if raw_constraints in (None, "", [], (), set()):
        return True
    values = list(raw_constraints) if isinstance(raw_constraints, (list, tuple, set)) else [raw_constraints]
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        # npm-compatible OR clauses are common in marketplace manifests.
        if "||" in text:
            if not any(version_satisfies(raw_version, clause.strip()) for clause in text.split("||")):
                return False
            continue
        if text in {"*", "x", "X"}:
            continue
        if re.fullmatch(r"[vV]?\d+(?:\.[xX*]){1,2}", text):
            pieces = _coerce_version(text).split(".")
            actual = _coerce_version(raw_version).split(".")
            for index, piece in enumerate(pieces):
                if piece.lower() in {"x", "*"}:
                    break
                if index >= len(actual) or actual[index] != piece:
                    return False
            continue
        if _simple_constraint_match(raw_version, text):
            continue
        if SpecifierSet is None or Version is None:
            return False
        try:
            candidate = Version(_coerce_version(raw_version))
            spec = SpecifierSet(_coerce_specifier(text))
        except (InvalidSpecifier, InvalidVersion, ValueError):
            return False
        if candidate not in spec:
            return False
    return True


def version_constraint_is_valid(value: Any) -> bool:
    """Return whether a managed/plugin-manifest version selector is parseable.

    This is deliberately separate from :func:`version_satisfies`: a valid
    range need not match an arbitrary probe version.  Callers use this at
    configuration boundaries so a typo such as ``plugin@marketplace@latest``
    cannot be silently accepted and then interpreted as a permanent disable.
    """

    text = str(value or "").strip()
    if not text:
        return False
    if "||" in text:
        clauses = [part.strip() for part in text.split("||")]
        return bool(clauses) and all(version_constraint_is_valid(part) for part in clauses)
    if text in {"*", "x", "X"}:
        return True
    if re.fullmatch(r"[vV]?\d+(?:\.[xX*]){1,2}", text):
        return True
    try:
        if SpecifierSet is None or Version is None:
            return bool(
                re.fullmatch(
                    r"\s*(?:\^|~|[<>!=]=?|v?\d[0-9A-Za-z.+-]*)[^\n]*",
                    text,
                )
            )
        SpecifierSet(_coerce_specifier(text))
        return True
    except (InvalidSpecifier, InvalidVersion, ValueError, TypeError):
        return False


def _coerce_version(value: str) -> str:
    value = value.strip()
    if value.startswith(("v", "V")):
        value = value[1:]
    return value


def _coerce_specifier(value: str) -> str:
    text = value.strip()
    hyphen = re.fullmatch(r"\s*([vV]?\d+(?:\.\d+){0,2})\s+-\s+([vV]?\d+(?:\.\d+){0,2})\s*", text)
    if hyphen:
        return f">={_coerce_version(hyphen.group(1))},<={_coerce_version(hyphen.group(2))}"
    if " " in text and all(
        token.startswith((">", "<", "=", "!"))
        for token in text.split()
    ):
        text = ",".join(text.split())
    if text.startswith("^"):
        base = _coerce_version(text[1:])
        try:
            parsed = Version(base) if Version is not None else None
            if parsed is None:
                return text
            if parsed.major > 0:
                upper = f"<{parsed.major + 1}.0.0"
            elif parsed.minor > 0:
                upper = f"<0.{parsed.minor + 1}.0"
            else:
                upper = f"<0.0.{parsed.micro + 1}"
            return f">={parsed},{upper}"
        except Exception:
            return text
    if text.startswith("~"):
        base = _coerce_version(text[1:])
        try:
            parsed = Version(base) if Version is not None else None
            if parsed is None:
                return text
            return f">={parsed},<{parsed.major}.{parsed.minor + 1}.0"
        except Exception:
            return text
    # Bare versions are exact matches in plugin settings.
    partial = re.fullmatch(r"[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
    if partial:
        major, minor, patch = partial.groups()
        if minor is None:
            return f">={major}.0.0,<{int(major) + 1}.0.0"
        if patch is None:
            return f">={major}.{minor}.0,<{major}.{int(minor) + 1}.0"
        return f"=={major}.{minor}.{patch}"
    if re.fullmatch(r"[vV]?\d+(?:\.\d+){2}(?:[-+].*)?", text):
        return f"=={_coerce_version(text)}"
    return text


def _simple_constraint_match(version: str, constraint: str) -> bool:
    # Keep prerelease/build strings usable even when packaging rejects a
    # vendor-specific suffix.  This is intentionally conservative.
    normalized_version = _coerce_version(version)
    if constraint.startswith("=") and not constraint.startswith(("==", ">=", "<=", "!=")):
        return normalized_version == _coerce_version(constraint.lstrip("="))
    return False
