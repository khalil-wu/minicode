from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from backend.agent.harness.contracts import ToolSpec


CORE_TOOLSET = "core"
DEFERRED_TOOLSET = "deferred"
HIDDEN_TOOLSET = "hidden"


@dataclass(frozen=True)
class ToolsetPolicy:
    """Decide which registered tools are directly visible this turn.

    ToolSpec.exposure is the tool author's default. ToolsetPolicy is the
    session-level capability filter: it can include or exclude toolsets without
    scattering visibility decisions through registry, loop, and UI code.
    """

    enabled_toolsets: frozenset[str] = field(default_factory=lambda: frozenset({CORE_TOOLSET}))
    disabled_toolsets: frozenset[str] = field(default_factory=frozenset)
    enabled_tools: frozenset[str] = field(default_factory=frozenset)
    disabled_tools: frozenset[str] = field(default_factory=frozenset)
    include_deferred_directly: bool = False

    @classmethod
    def default(cls) -> "ToolsetPolicy":
        return cls()

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
        return cls(
            enabled_toolsets=_clean_set(enabled_toolsets) or frozenset({CORE_TOOLSET}),
            disabled_toolsets=_clean_set(disabled_toolsets),
            enabled_tools=_clean_set(enabled_tools),
            disabled_tools=_clean_set(disabled_tools),
            include_deferred_directly=include_deferred_directly,
        )

    def cache_key(self) -> str:
        return "|".join(
            [
                ",".join(sorted(self.enabled_toolsets)),
                ",".join(sorted(self.disabled_toolsets)),
                ",".join(sorted(self.enabled_tools)),
                ",".join(sorted(self.disabled_tools)),
                "deferred=1" if self.include_deferred_directly else "deferred=0",
            ]
        )

    def is_directly_visible(self, spec: ToolSpec) -> bool:
        if spec.name in self.disabled_tools or spec.toolset in self.disabled_toolsets:
            return False
        if spec.exposure == "hidden":
            return False
        if spec.name in self.enabled_tools:
            return True
        # always_load forces a deferred tool into the direct list (CC parity):
        # its full schema must appear on turn 1 without a tool_search round-trip.
        if getattr(spec, "always_load", False):
            return True
        if spec.exposure == "core":
            return CORE_TOOLSET in self.enabled_toolsets or spec.toolset in self.enabled_toolsets
        if spec.exposure == "deferred":
            return self.include_deferred_directly and spec.toolset in self.enabled_toolsets
        return False


def _clean_set(values: Iterable[str] | None) -> frozenset[str]:
    if values is None:
        return frozenset()
    return frozenset(str(value).strip() for value in values if str(value).strip())


def visible_tool_specs(specs: Iterable[ToolSpec], policy: ToolsetPolicy | None = None) -> list[ToolSpec]:
    active = policy or ToolsetPolicy.default()
    return [spec for spec in specs if active.is_directly_visible(spec)]
