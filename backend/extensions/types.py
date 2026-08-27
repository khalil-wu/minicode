"""Public contracts for MiniCode executable extensions.

Extension state remains separate from the host agent: an extension factory
receives an API, registrations are owned by the extension instance, and event
contexts become stale when the runtime is replaced.

The module contains no host-specific imports.  This is intentional: extension
authors can import the contracts without importing the websocket/session
stack, and the loader can reject an untrusted module before executing it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias

from backend.agent.lifecycle_errors import LifecycleStaleError


ExtensionMode = Literal["tui", "rpc", "json", "print"]
ExtensionExecutionMode = Literal["sequential", "parallel"]
ExtensionScope = Literal[
    "builtin", "managed", "user", "project", "temporary", "external"
]

MaybeAwaitable: TypeAlias = Any


def _reject_noncanonical_field_spellings(
    value: Mapping[str, Any],
    canonical_fields: set[str],
    *,
    context: str,
) -> None:
    compact_names = {
        field.replace("_", "").casefold(): field for field in canonical_fields
    }
    for raw_name in value:
        name = str(raw_name)
        canonical = compact_names.get(name.replace("_", "").casefold())
        if canonical is not None and name != canonical:
            raise ExtensionRegistrationError(
                f"{context}.{name} is not supported; use {canonical}"
            )


class ExtensionFactory(Protocol):
    """Callable accepted by :class:`ExtensionLoader`.

    Factories may be synchronous or asynchronous.  The loader also accepts an
    object exposing ``setup(api)`` for small class-based extensions.
    """

    def __call__(self, api: Any) -> MaybeAwaitable: ...


class ExtensionStaleError(LifecycleStaleError):
    """Raised when an extension uses a retired lifecycle generation."""


class ExtensionTrustError(PermissionError):
    """Raised when executable extension code crosses the trust boundary."""


class ExtensionRegistrationError(ValueError):
    """Raised for malformed extension registrations."""


@dataclass(frozen=True)
class ExtensionSource:
    """Provenance attached to every loaded extension."""

    path: str
    resolved_path: str
    scope: ExtensionScope = "external"
    trusted: bool = False
    origin: str = "local"
    marketplace: str | None = None
    plugin_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "resolved_path": self.resolved_path,
            "scope": self.scope,
            "trusted": self.trusted,
            "origin": self.origin,
            "marketplace": self.marketplace,
            "plugin_id": self.plugin_id,
            "metadata": dict(self.metadata),
        }

    def to_source_info(self) -> dict[str, Any]:
        """Project provenance into the MiniCode tool source contract."""

        metadata = dict(self.metadata)
        package_source = bool(
            self.plugin_id
            or self.marketplace
            or str(metadata.get("origin") or self.origin) == "plugin"
        )
        source = str(
            metadata.get("declaration")
            or self.plugin_id
            or self.marketplace
            or self.origin
            or "local"
        )
        base_dir = str(metadata.get("plugin_root") or "").strip()
        return {
            "path": self.resolved_path or self.path,
            "source": source,
            "scope": self.scope,
            "origin": "package" if package_source else "top-level",
            **({"base_dir": base_dir} if base_dir else {}),
        }


@dataclass
class ExtensionToolDefinition:
    """MiniCode executable-extension tool registration contract.

    ``parameters`` is an OpenAI/JSON-Schema object.  ``execute`` may accept the
    five runtime arguments or the shorter ``(params, ctx)``/``(params,)``
    forms; the runtime adapts the call without invoking a callable twice.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    execute: Callable[..., Any]
    label: str | None = None
    prompt_snippet: str | None = None
    prompt_guidelines: tuple[str, ...] = ()
    read_only: bool = False
    destructive: bool = False
    open_world: bool = False
    mutates_workspace: bool = False
    mutates_external_state: bool = False
    side_effect_kind: str | None = None
    idempotent: bool | None = None
    timeout_seconds: float | None = None
    streams_output: bool = False
    # ``parallel`` opts into the concurrency-safe batch classification.
    execution_mode: ExtensionExecutionMode | None = None
    # Optional argument transform run before host JSON-schema validation.
    prepare_arguments: Callable[..., Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(
        cls, value: "ExtensionToolDefinition | Mapping[str, Any]"
    ) -> "ExtensionToolDefinition":
        if isinstance(value, cls):
            result = value
        elif isinstance(value, Mapping):
            _reject_noncanonical_field_spellings(
                value,
                {
                    "prompt_snippet",
                    "prompt_guidelines",
                    "read_only",
                    "open_world",
                    "mutates_workspace",
                    "mutates_external_state",
                    "side_effect_kind",
                    "timeout_seconds",
                    "streams_output",
                    "execution_mode",
                    "prepare_arguments",
                },
                context="register_tool",
            )
            raw_execute = value.get("execute")
            if not callable(raw_execute):
                raise ExtensionRegistrationError(
                    "register_tool requires a callable execute"
                )
            raw_params = value.get("parameters")
            if not isinstance(raw_params, Mapping):
                raise ExtensionRegistrationError(
                    "register_tool.parameters must be a JSON-Schema object"
                )
            guidelines = value.get("prompt_guidelines") or ()
            if isinstance(guidelines, str):
                guidelines = (guidelines,)
            result = cls(
                name=str(value.get("name") or ""),
                label=(str(value["label"]) if value.get("label") is not None else None),
                description=str(value.get("description") or ""),
                parameters=dict(raw_params),
                execute=raw_execute,
                prompt_snippet=(
                    str(value["prompt_snippet"])
                    if value.get("prompt_snippet") is not None
                    else None
                ),
                prompt_guidelines=tuple(
                    str(item) for item in guidelines if str(item).strip()
                ),
                read_only=bool(value.get("read_only", False)),
                destructive=bool(value.get("destructive", False)),
                open_world=bool(value.get("open_world", False)),
                mutates_workspace=bool(value.get("mutates_workspace", False)),
                mutates_external_state=bool(value.get("mutates_external_state", False)),
                side_effect_kind=(
                    str(value.get("side_effect_kind"))
                    if value.get("side_effect_kind")
                    else None
                ),
                idempotent=(
                    bool(value["idempotent"])
                    if value.get("idempotent") is not None
                    else None
                ),
                timeout_seconds=(
                    float(value.get("timeout_seconds"))
                    if value.get("timeout_seconds") is not None
                    else None
                ),
                streams_output=bool(value.get("streams_output", False)),
                execution_mode=(
                    str(value.get("execution_mode", "")).lower()
                    if value.get("execution_mode") is not None
                    else None
                ),
                prepare_arguments=(
                    value.get("prepare_arguments")
                    if callable(value.get("prepare_arguments"))
                    else None
                ),
                metadata=dict(value.get("metadata") or {}),
            )
        else:
            raise ExtensionRegistrationError(
                "register_tool expects a tool definition mapping"
            )

        result.name = result.name.strip()
        if not result.name:
            raise ExtensionRegistrationError("tool name must be non-empty")
        if any(ch.isspace() for ch in result.name) or len(result.name) > 128:
            raise ExtensionRegistrationError(
                "tool name contains whitespace or is too long"
            )
        if not result.description.strip():
            raise ExtensionRegistrationError(
                f"tool '{result.name}' requires a description"
            )
        if (
            not isinstance(result.parameters, dict)
            or result.parameters.get("type", "object") != "object"
        ):
            raise ExtensionRegistrationError(
                f"tool '{result.name}' parameters must be an object schema"
            )
        if result.execution_mode not in {None, "sequential", "parallel"}:
            raise ExtensionRegistrationError(
                f"tool '{result.name}' execution_mode must be 'sequential' or 'parallel'"
            )
        return result


@dataclass
class ExtensionCommand:
    name: str
    handler: Callable[..., Any]
    description: str | None = None
    get_argument_completions: Callable[..., Any] | None = None
    extension_path: str = ""


@dataclass
class ExtensionShortcut:
    shortcut: str
    handler: Callable[..., Any]
    description: str | None = None
    extension_path: str = ""


@dataclass
class ExtensionFlag:
    name: str
    type: Literal["boolean", "string"]
    default: bool | str | None = None
    description: str | None = None
    extension_path: str = ""


@dataclass
class ExtensionProvider:
    """Declarative provider registration owned by one extension generation."""

    name: str
    config: Mapping[str, Any]
    extension_path: str = ""


@dataclass
class ExtensionError:
    extension_path: str
    event: str
    error: str
    stack: str | None = None


@dataclass
class ToolCallEvent:
    type: Literal["tool_call"] = "tool_call"
    tool_call_id: str = ""
    tool_name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallDecision:
    block: bool = False
    reason: str | None = None


@dataclass
class ToolResultEvent:
    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str = ""
    tool_name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    content: Any = ""
    details: Any = None
    is_error: bool = False
    usage: Any = None


@dataclass(frozen=True)
class ToolResultPatch:
    content: Any = None
    details: Any = None
    is_error: bool | None = None
    usage: Any = None
    has_content: bool = False
    has_details: bool = False
    has_usage: bool = False

    @classmethod
    def from_value(cls, value: Any) -> "ToolResultPatch | None":
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ExtensionRegistrationError(
                "tool_result handlers must return a mapping or ToolResultPatch"
            )
        _reject_noncanonical_field_spellings(
            value,
            {"is_error"},
            context="tool_result",
        )
        return cls(
            content=value.get("content"),
            details=value.get("details"),
            is_error=value.get("is_error"),
            usage=value.get("usage"),
            has_content="content" in value,
            has_details="details" in value,
            has_usage="usage" in value,
        )


@dataclass
class Extension:
    path: str
    resolved_path: str
    source: ExtensionSource
    hidden: bool = False
    handlers: dict[str, list[Callable[..., Any]]] = field(default_factory=dict)
    tools: dict[str, ExtensionToolDefinition] = field(default_factory=dict)
    commands: dict[str, ExtensionCommand] = field(default_factory=dict)
    flags: dict[str, ExtensionFlag] = field(default_factory=dict)
    shortcuts: dict[str, ExtensionShortcut] = field(default_factory=dict)
    providers: dict[str, ExtensionProvider] = field(default_factory=dict)
    message_renderers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    entry_renderers: dict[str, Callable[..., Any]] = field(default_factory=dict)

    def add_handler(self, event: str, handler: Callable[..., Any]) -> None:
        self.handlers.setdefault(event, []).append(handler)


@dataclass
class LoadExtensionsResult:
    extensions: list[Extension]
    errors: list[dict[str, str]] = field(default_factory=list)
    runtime: Any | None = None
    generation: int = 0
    # The loader-created runner owns the factory-facing API references.  Hosts
    # must bind this exact runner instead of constructing a second one, or a
    # a late register_tool() call would refresh the abandoned loader runner while
    # the live ToolRegistry remained stale.
    runner: Any | None = None


class ProviderSink(Protocol):
    def register_provider(self, name: str, config: Mapping[str, Any]) -> Any: ...

    def unregister_provider(self, name: str) -> Any: ...


class ToolRegistryLike(Protocol):
    def register(self, tool: Any) -> Any: ...

    def unregister(self, name: str) -> Any: ...

    def get_tool(self, name: str) -> Any: ...


__all__ = [
    "Extension",
    "ExtensionCommand",
    "ExtensionError",
    "ExtensionExecutionMode",
    "ExtensionFactory",
    "ExtensionFlag",
    "ExtensionMode",
    "ExtensionProvider",
    "ExtensionRegistrationError",
    "ExtensionScope",
    "ExtensionShortcut",
    "ExtensionSource",
    "ExtensionStaleError",
    "ExtensionToolDefinition",
    "ExtensionTrustError",
    "LoadExtensionsResult",
    "ProviderSink",
    "ToolCallDecision",
    "ToolCallEvent",
    "ToolRegistryLike",
    "ToolResultEvent",
    "ToolResultPatch",
]
