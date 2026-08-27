from __future__ import annotations

from dataclasses import dataclass, field, replace
from collections.abc import Mapping
from typing import Any, Iterable

from backend.tools.contracts import ToolSpec


# "Every registered group", the default session selection. Naming a concrete
# group list must mean "only these groups", otherwise a caller can never exclude
# a group by omitting it; a sentinel keeps that meaning without forcing the
# policy to enumerate groups that plugins may add at runtime.
ALL_TOOLSETS = "*"

# Keep the immutable/session-owned selection separate from the per-iteration
# effective policy.  The latter includes child-profile attenuation and loaded
# deferred tools, so feeding it back as the next iteration's session policy
# would make temporary denies impossible to remove (for example when a
# teammate enters Plan mode and should gain ExitPlanMode).
ACTIVE_TOOLSET_POLICY_METADATA_KEY = "_toolset_policy"
SESSION_TOOLSET_POLICY_METADATA_KEY = "_session_toolset_policy"


@dataclass(frozen=True)
class ToolAvailabilityFilter:
    """One composable availability clause for a runtime tool surface.

    A clause is a union: a tool is accepted when its exact name, toolset, or
    capability matches.  Multiple clauses on ``ToolsetPolicy`` are an
    intersection.  Keeping clauses separate lets independent owners (for
    example a Pi session whitelist and a background-agent safety profile)
    attenuate the same surface without flattening their rules into provider-
    specific branches.
    """

    tools: frozenset[str] = field(default_factory=frozenset)
    toolsets: frozenset[str] = field(default_factory=frozenset)
    capabilities: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ToolAvailabilityFilter":
        """Restore one availability clause from durable/runtime metadata.

        The parser intentionally accepts a single string as one name rather
        than iterating its characters.  Invalid container shapes fail closed
        with ``ValueError`` so a malformed persisted capability cannot widen a
        child agent's tool surface.
        """

        if not isinstance(raw, Mapping):
            raise ValueError("availability filter must be an object")

        allowed_keys = {"tools", "toolsets", "capabilities"}
        unknown_keys = sorted(str(key) for key in raw if key not in allowed_keys)
        if unknown_keys:
            raise ValueError(
                "availability filter has unknown field(s): " + ", ".join(unknown_keys)
            )

        def _values(key: str) -> frozenset[str]:
            if key not in raw:
                return frozenset()
            value = raw[key]
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, (list, tuple, set, frozenset)):
                raise ValueError(f"availability filter {key!r} must be a string or list")
            return _strict_name_set(value, field=f"availability filter {key!r}")

        return cls(
            tools=_values("tools"),
            toolsets=_values("toolsets"),
            capabilities=_values("capabilities"),
        )

    def allows(self, spec: ToolSpec) -> bool:
        return bool(
            spec.name in self.tools
            or spec.toolset in self.toolsets
            or (spec.capability and spec.capability in self.capabilities)
        )

    def cache_key(self) -> str:
        return ";".join(
            [
                f"tools={','.join(sorted(self.tools))}",
                f"toolsets={','.join(sorted(self.toolsets))}",
                f"capabilities={','.join(sorted(self.capabilities))}",
            ]
        )

    def to_mapping(self) -> dict[str, list[str]]:
        return {
            "tools": sorted(self.tools),
            "toolsets": sorted(self.toolsets),
            "capabilities": sorted(self.capabilities),
        }


@dataclass(frozen=True)
class ToolsetPolicy:
    """Decide which registered tools are directly visible this turn.

    ToolSpec.exposure is the tool author's default. ToolsetPolicy is the
    session-level capability filter: it can include or exclude toolsets without
    scattering visibility decisions through registry, loop, and UI code.
    """

    enabled_toolsets: frozenset[str] = field(default_factory=lambda: frozenset({ALL_TOOLSETS}))
    disabled_toolsets: frozenset[str] = field(default_factory=frozenset)
    enabled_tools: frozenset[str] = field(default_factory=frozenset)
    disabled_tools: frozenset[str] = field(default_factory=frozenset)
    include_deferred_directly: bool = False
    availability_filters: tuple[ToolAvailabilityFilter, ...] = ()

    @classmethod
    def default(cls) -> "ToolsetPolicy":
        return cls()

    def to_mapping(self) -> dict[str, Any]:
        """Return the durable, provider-neutral session capability shape."""

        return {
            "enabled_toolsets": sorted(self.enabled_toolsets),
            "disabled_toolsets": sorted(self.disabled_toolsets),
            "enabled_tools": sorted(self.enabled_tools),
            "disabled_tools": sorted(self.disabled_tools),
            "include_deferred_directly": bool(self.include_deferred_directly),
            "availability_filters": [
                clause.to_mapping() for clause in self.availability_filters
            ],
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ToolsetPolicy":
        """Restore a session policy without allowing malformed data to widen it."""

        if not isinstance(raw, Mapping):
            raise ValueError("toolset policy must be an object")

        allowed_keys = {
            "enabled_toolsets",
            "disabled_toolsets",
            "enabled_tools",
            "disabled_tools",
            "include_deferred_directly",
            "availability_filters",
        }
        unknown_keys = sorted(str(key) for key in raw if key not in allowed_keys)
        if unknown_keys:
            raise ValueError(
                "toolset policy has unknown field(s): " + ", ".join(unknown_keys)
            )

        def _bool(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in {0, 1}:
                return bool(value)
            token = str(value or "").strip().lower()
            if token in {"true", "1", "yes", "on"}:
                return True
            if token in {"false", "0", "no", "off", ""}:
                return False
            raise ValueError("toolset policy boolean is invalid")

        raw_filters = raw.get("availability_filters", [])
        if not isinstance(raw_filters, (list, tuple)):
            raise ValueError("toolset policy availability_filters must be a list")
        filters = tuple(
            ToolAvailabilityFilter.from_mapping(item) for item in raw_filters
        )
        return cls(
            enabled_toolsets=(
                frozenset({ALL_TOOLSETS})
                if "enabled_toolsets" not in raw
                else _strict_name_set(
                    raw["enabled_toolsets"], field="toolset policy 'enabled_toolsets'"
                )
            ),
            disabled_toolsets=_strict_name_set(
                raw.get("disabled_toolsets", ()),
                field="toolset policy 'disabled_toolsets'",
            ),
            enabled_tools=_strict_name_set(
                raw.get("enabled_tools", ()),
                field="toolset policy 'enabled_tools'",
            ),
            disabled_tools=_strict_name_set(
                raw.get("disabled_tools", ()),
                field="toolset policy 'disabled_tools'",
            ),
            include_deferred_directly=(
                False
                if "include_deferred_directly" not in raw
                else _bool(raw["include_deferred_directly"])
            ),
            availability_filters=filters,
        )

    @classmethod
    def from_iterables(
        cls,
        *,
        enabled_toolsets: Iterable[str] | None = None,
        disabled_toolsets: Iterable[str] | None = None,
        enabled_tools: Iterable[str] | None = None,
        disabled_tools: Iterable[str] | None = None,
        include_deferred_directly: bool = False,
    ) -> "ToolsetPolicy":
        # ``None`` means "use the normal session selection: every group".  An
        # explicitly empty iterable is a real whitelist boundary used by Pi's
        # setActiveTools: only names in enabled_tools are available.  Collapsing
        # both cases made it impossible to disable every tool or select an exact
        # subset.
        resolved_enabled_toolsets = (
            frozenset({ALL_TOOLSETS})
            if enabled_toolsets is None
            else _clean_set(enabled_toolsets)
        )
        return cls(
            enabled_toolsets=resolved_enabled_toolsets,
            disabled_toolsets=_clean_set(disabled_toolsets),
            enabled_tools=_clean_set(enabled_tools),
            disabled_tools=_clean_set(disabled_tools),
            include_deferred_directly=include_deferred_directly,
        )

    def with_disabled_tools(self, values: Iterable[str]) -> "ToolsetPolicy":
        """Intersect this policy with an additional name deny-list."""

        additions = _clean_set(values)
        if not additions:
            return self
        return replace(
            self,
            disabled_tools=frozenset(self.disabled_tools) | additions,
        )

    def with_availability_filter(
        self,
        *,
        tools: Iterable[str] | None = None,
        toolsets: Iterable[str] | None = None,
        capabilities: Iterable[str] | None = None,
    ) -> "ToolsetPolicy":
        """Intersect the policy with one capability-oriented allow clause."""

        clause = ToolAvailabilityFilter(
            tools=_clean_set(tools),
            toolsets=_clean_set(toolsets),
            capabilities=_clean_set(capabilities),
        )
        if clause in self.availability_filters:
            return self
        return replace(
            self,
            availability_filters=(*self.availability_filters, clause),
        )

    def with_active_tool_selection(self, names: Iterable[str]) -> "ToolsetPolicy":
        """Intersect this policy with one explicit active-tool selection.

        Pi's ``setActiveTools`` is an exact runtime selection, and selected
        deferred tools become direct on the next model request.  The selection
        must still remain below the session/parent policy: adding the names to
        ``enabled_tools`` would turn an empty or toolset-restricted ceiling
        into an allow-list and could widen a delegated child.  An additional
        name-only availability clause preserves every existing deny, allow,
        and capability filter; enabling deferred presentation makes only the
        selected (and otherwise permitted) deferred tools direct.
        """

        selection = ToolAvailabilityFilter(tools=_clean_set(names))
        if selection in self.availability_filters and self.include_deferred_directly:
            return self
        filters = list(self.availability_filters)
        if selection not in filters:
            filters.append(selection)
        return replace(
            self,
            include_deferred_directly=True,
            availability_filters=tuple(filters),
        )

    def restricted_by(self, policy: "ToolsetPolicy") -> "ToolsetPolicy":
        """Apply only the attenuating parts of another policy.

        Session selection (enabled toolsets/tools and deferred presentation)
        stays owned by ``self``.  Child execution profiles may only remove
        capabilities, never widen the session surface.
        """

        filters = list(self.availability_filters)
        for clause in policy.availability_filters:
            if clause not in filters:
                filters.append(clause)
        return replace(
            self,
            disabled_toolsets=(
                frozenset(self.disabled_toolsets)
                | frozenset(policy.disabled_toolsets)
            ),
            disabled_tools=(
                frozenset(self.disabled_tools) | frozenset(policy.disabled_tools)
            ),
            availability_filters=tuple(filters),
        )

    def cache_key(self) -> str:
        return "|".join(
            [
                ",".join(sorted(self.enabled_toolsets)),
                ",".join(sorted(self.disabled_toolsets)),
                ",".join(sorted(self.enabled_tools)),
                ",".join(sorted(self.disabled_tools)),
                "deferred=1" if self.include_deferred_directly else "deferred=0",
                *(
                    f"availability[{index}]={clause.cache_key()}"
                    for index, clause in enumerate(self.availability_filters)
                ),
            ]
        )

    def _toolset_enabled(self, toolset: str) -> bool:
        return ALL_TOOLSETS in self.enabled_toolsets or toolset in self.enabled_toolsets

    def validate_against(self, specs: Iterable[ToolSpec]) -> None:
        """Reject a selection that names a tool or group the registry lacks.

        An unknown name in the session selection silently *shrinks* the surface —
        the observed failure was a whitelist that resolved to zero tools with no
        error at all — so it is a configuration error, not an empty selection.
        Unknown names in the attenuating directions stay tolerated: disabling or
        filtering for a capability that is not installed is already satisfied,
        and safety profiles legitimately name optional toolsets (``mcp``).
        """

        specs = list(specs)
        known_tools = {spec.name for spec in specs}
        known_toolsets = {spec.toolset for spec in specs} | {ALL_TOOLSETS}
        unknown_toolsets = sorted(self.enabled_toolsets - known_toolsets)
        if unknown_toolsets:
            raise ValueError(
                "Unknown toolset(s) in enabled_toolsets: " + ", ".join(unknown_toolsets)
            )
        unknown_tools = sorted(self.enabled_tools - known_tools)
        if unknown_tools:
            raise ValueError(
                "Unknown tool name(s) in enabled_tools: " + ", ".join(unknown_tools)
            )

    def is_available(self, spec: ToolSpec) -> bool:
        if spec.name in self.disabled_tools or spec.toolset in self.disabled_toolsets:
            return False
        if spec.exposure == "hidden":
            return False
        if spec.name not in self.enabled_tools and not self._toolset_enabled(spec.toolset):
            return False
        if any(
            not availability_filter.allows(spec)
            for availability_filter in self.availability_filters
        ):
            return False
        return True

    def is_directly_visible(self, spec: ToolSpec) -> bool:
        if not self.is_available(spec):
            return False
        if spec.name in self.enabled_tools:
            return True
        if not self.enabled_toolsets:
            return False
        # always_load forces a deferred tool into the direct list (CC parity):
        # its full schema must appear on turn 1 without a tool_search round-trip.
        if getattr(spec, "always_load", False):
            return True
        if spec.exposure == "core":
            return self._toolset_enabled(spec.toolset)
        if spec.exposure == "deferred":
            return self.include_deferred_directly and self._toolset_enabled(spec.toolset)
        return False


def _clean_set(values: Iterable[str] | None) -> frozenset[str]:
    if values is None:
        return frozenset()
    if isinstance(values, str):
        return frozenset({values.strip()} if values.strip() else set())
    return frozenset(str(value).strip() for value in values if str(value).strip())


def _strict_name_set(value: Any, *, field: str) -> frozenset[str]:
    """Parse a durable list of names without coercing corrupt values."""

    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        raise ValueError(f"{field} must be a string or list")

    names: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            raise ValueError(f"{field} entries must be strings")
        name = item.strip()
        if not name:
            raise ValueError(f"{field} entries must be non-empty")
        names.add(name)
    return frozenset(names)
