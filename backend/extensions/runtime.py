"""Runtime and API for MiniCode executable extensions.

This module is deliberately host-neutral.  A websocket/session host can bind
actions and registries after loading, while tests and small integrations can
use :class:`ExtensionRunner.invoke_tool` directly. The lifecycle is:

``factory -> registrations -> bind -> events/tools -> session_shutdown -> stale``

No extension callback is allowed to retain a live capability after the runner
has been replaced.  Every API/context operation checks the generation guard.
"""

from __future__ import annotations

import copy
import inspect
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .types import (
    Extension,
    ExtensionCommand,
    ExtensionError,
    ExtensionFlag,
    ExtensionMode,
    ExtensionProvider,
    ExtensionRegistrationError,
    ExtensionShortcut,
    ExtensionStaleError,
    ExtensionToolDefinition,
    LoadExtensionsResult,
    ToolCallDecision,
    ToolCallEvent,
    ToolResultEvent,
    ToolResultPatch,
)

logger = logging.getLogger(__name__)


DEFAULT_STALE_MESSAGE = (
    "This extension context is stale after session replacement or reload. "
    "Do not use a captured extension API/context after shutdown or reload."
)


class _NoOpExtensionUI:
    """No-op UI surface for print/json modes.

    Extensions are allowed to call ``ctx.ui.notify`` without first branching
    on the mode; ``ctx.has_ui`` remains false so dialog-capable code can still
    opt out.  Returning a stable object is safer than exposing ``None`` and
    turning a harmless notification into an AttributeError.
    """

    async def select(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return None

    async def confirm(self, *args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        return False

    async def input(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return None

    def notify(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("set_") or name.startswith("get_"):
            return lambda *args, **kwargs: None
        raise AttributeError(name)


_NO_OP_UI = _NoOpExtensionUI()


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _call_with_signature(
    func: Callable[..., Any],
    values: Mapping[str, Any],
    positional_fallback: Sequence[Any] = (),
) -> Any:
    """Call an extension callback once using names from its signature.

    Extension callbacks may receive a fixed positional signature. Python
    extensions also commonly use ``(params, ctx)`` or
    keyword-only callbacks.  Signature-based binding supports both without the
    dangerous "retry after TypeError" pattern (which could execute a side
    effect twice when the callback itself raises ``TypeError``).
    """

    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return func(*positional_fallback)

    parameters = list(signature.parameters.values())
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return func(**dict(values))

    kwargs: dict[str, Any] = {}
    positional: list[Any] = []
    fallback_iter = iter(positional_fallback)
    for parameter in parameters:
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            positional.extend(fallback_iter)
            continue
        if parameter.name in values:
            value = values[parameter.name]
        else:
            aliases = {
                "args": "params",
                "input": "params",
                "event": "event",
                "context": "ctx",
                "execution_context": "tool_context",
                "tool_context": "tool_context",
                "call_id": "tool_call_id",
                "id": "tool_call_id",
                "signal": "signal",
                "update": "on_update",
                "callback": "on_update",
                "names": "tool_names",
                "tool_names": "names",
                "value": "level",
                "level": "value",
                "text": "content",
            }
            source_name = aliases.get(parameter.name)
            if source_name is not None and source_name in values:
                value = values[source_name]
            elif parameter.default is not inspect.Parameter.empty:
                continue
            else:
                try:
                    value = next(fallback_iter)
                except StopIteration as exc:
                    raise TypeError(
                        f"Cannot bind extension callback parameter '{parameter.name}'"
                    ) from exc

        if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        else:
            kwargs[parameter.name] = value

    # A callback with only positional-only parameters cannot receive kwargs.
    if all(
        parameter.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL}
        for parameter in parameters
    ):
        return func(*positional)
    return func(*positional, **kwargs)


def _normalise_content(value: Any) -> str:
    """Project extension content blocks into MiniCode's compact text result."""
    from backend.agent.content_projection import normalise_content

    return normalise_content(value)


def _as_tool_result(value: Any) -> Any:
    """Return a host ``ToolResult`` when available, otherwise a tiny fallback."""

    try:
        from backend.tools.base import ToolResult
    except Exception:  # pragma: no cover - import is available in MiniCode
        ToolResult = None  # type: ignore[assignment]

    if ToolResult is not None and isinstance(value, ToolResult):
        return value
    if isinstance(value, Mapping):
        content = _normalise_content(value.get("content", value.get("result", "")))
        kwargs = {
            "is_error": bool(value.get("is_error", False)),
            "status": value.get("status"),
            "display_summary": value.get("display_summary"),
            "details": value.get("details"),
        }
        # ToolResult intentionally has a narrow constructor; only pass fields
        # that are part of the current host contract.
        if ToolResult is not None:
            allowed = {
                key: val
                for key, val in kwargs.items()
                if key in {"is_error", "status", "display_summary"}
            }
            return ToolResult(content=content, **allowed)
        return {"content": content, **kwargs}
    if ToolResult is not None:
        return ToolResult(content=_normalise_content(value))
    return {"content": _normalise_content(value), "is_error": False}


class ExtensionEventBus:
    """Small async event bus exposed as ``api.events``."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[..., Any]]] = {}
        self._closed = False

    def on(self, event: str, handler: Callable[..., Any]) -> Callable[[], None]:
        if self._closed:
            raise ExtensionStaleError(DEFAULT_STALE_MESSAGE)
        if not isinstance(event, str) or not event.strip() or not callable(handler):
            raise ExtensionRegistrationError(
                "events.on requires a non-empty event and callable handler"
            )
        self._handlers.setdefault(event, []).append(handler)

        def unsubscribe() -> None:
            handlers = self._handlers.get(event, [])
            try:
                handlers.remove(handler)
            except ValueError:
                return
            if not handlers:
                self._handlers.pop(event, None)

        return unsubscribe

    async def emit(self, event: str, payload: Any = None) -> list[Any]:
        if self._closed:
            raise ExtensionStaleError(DEFAULT_STALE_MESSAGE)
        result: list[Any] = []
        for handler in tuple(self._handlers.get(event, ())):
            result.append(
                await _maybe_await(
                    _call_with_signature(
                        handler, {"event": payload, "payload": payload}, (payload,)
                    )
                )
            )
        return result

    def close(self) -> None:
        self._closed = True
        self._handlers.clear()


class _BoundEventBus:
    """Generation-guarded view exposed to one extension factory."""

    def __init__(
        self, runtime: "ExtensionRuntime", event_bus: ExtensionEventBus, owner: str = ""
    ) -> None:
        self._runtime = runtime
        self._event_bus = event_bus
        self._owner = owner

    def on(self, event: str, handler: Callable[..., Any]) -> Callable[[], None]:
        self._runtime.assert_active()
        unsubscribe = self._event_bus.on(event, handler)
        self._runtime.track_event_subscription(self._owner, unsubscribe)

        def guarded_unsubscribe() -> None:
            self._runtime.assert_active()
            unsubscribe()

        return guarded_unsubscribe

    async def emit(self, event: str, payload: Any = None) -> list[Any]:
        self._runtime.assert_active()
        return await self._event_bus.emit(event, payload)


class ExtensionRuntime:
    """Shared action/provider state for one loaded extension generation."""

    def __init__(
        self,
        *,
        generation: int = 0,
        actions: Mapping[str, Callable[..., Any]] | None = None,
    ) -> None:
        self.generation = int(generation)
        self.flag_values: dict[str, bool | str] = {}
        self.pending_provider_registrations: list[ExtensionProvider] = []
        self.provider_diagnostics: list[dict[str, str]] = []
        self._provider_sink: Any | None = None
        self._event_unsubscribers: dict[str, list[Callable[[], None]]] = {}
        self._event_bus: ExtensionEventBus | None = None
        self._actions: dict[str, Callable[..., Any]] = dict(actions or {})
        self._active = True
        self._stale_message: str | None = None

    @property
    def active(self) -> bool:
        return self._active and self._stale_message is None

    def assert_active(self) -> None:
        if not self.active:
            raise ExtensionStaleError(self._stale_message or DEFAULT_STALE_MESSAGE)

    def invalidate(self, message: str | None = None) -> None:
        if self._stale_message is None:
            self._stale_message = message or DEFAULT_STALE_MESSAGE
            self.cleanup_event_subscriptions()
            # Invalidation only makes captured APIs stale. Provider state is
            # owned by the complete ModelRuntime generation and is retired by
            # the session composition root; deleting by provider name here can
            # race a freshly published generation with the same registration.
        self._active = False

    def bind_actions(
        self,
        actions: Mapping[str, Callable[..., Any]] | None = None,
        **kwargs: Callable[..., Any],
    ) -> None:
        self.assert_active()
        self._actions.update(dict(actions or {}))
        self._actions.update(kwargs)

    def action(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.assert_active()
        callback = self._actions.get(name)
        if callback is None:
            raise RuntimeError(f"Extension runtime action '{name}' is not bound")
        return _call_with_signature(callback, kwargs, args)

    def bind_provider_sink(self, sink: Any | None) -> None:
        self.assert_active()
        self._provider_sink = sink
        if sink is None:
            return
        pending = tuple(self.pending_provider_registrations)
        self.pending_provider_registrations.clear()
        for registration in pending:
            try:
                self._apply_provider_registration(registration)
            except Exception as exc:
                self.provider_diagnostics.append(
                    {
                        "path": registration.extension_path,
                        "error": str(exc),
                        "event": "register_provider",
                    }
                )
                logger.exception(
                    "failed to flush extension provider registration %s",
                    registration.name,
                )

    def _apply_provider_registration(self, registration: ExtensionProvider) -> None:
        sink = self._provider_sink
        if sink is None:
            self.pending_provider_registrations.append(registration)
            return
        sink.register_provider(registration.name, registration.config)

    def register_provider(
        self,
        name: str,
        config: Mapping[str, Any],
        extension_path: str = "<unknown>",
    ) -> None:
        self.assert_active()
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ExtensionRegistrationError("provider name must be non-empty")
        if not isinstance(config, Mapping):
            raise ExtensionRegistrationError("provider config must be an object")
        registration = ExtensionProvider(clean_name, dict(config), extension_path)
        if self._provider_sink is None:
            # MiniCode keeps registration call order and ModelRuntime merges each
            # defined value over the previous registration. Replacing the
            # queued item here loses intentional partial re-registrations.
            self.pending_provider_registrations.append(registration)
            return
        self._apply_provider_registration(registration)

    def unregister_provider(self, name: str, extension_path: str = "<unknown>") -> None:
        self.assert_active()
        del extension_path
        clean_name = str(name or "").strip()
        self.pending_provider_registrations = [
            item
            for item in self.pending_provider_registrations
            if item.name != clean_name
        ]
        sink = self._provider_sink
        unregister = (
            getattr(sink, "unregister_provider", None) if sink is not None else None
        )
        if unregister is not None:
            _call_with_signature(unregister, {"name": clean_name}, (clean_name,))

    def unregister_owner(self, extension_path: str) -> None:
        # Helper for callers that discard a factory before the generation is
        # bound. Once bound, provider ownership is the whole
        # ModelRuntime generation rather than an individual extension path.
        self.pending_provider_registrations = [
            item
            for item in self.pending_provider_registrations
            if item.extension_path != extension_path
        ]

    def track_event_subscription(
        self, owner: str, unsubscribe: Callable[[], None]
    ) -> None:
        self._event_unsubscribers.setdefault(owner, []).append(unsubscribe)

    def cleanup_event_subscriptions(self) -> None:
        for unsubscribers in tuple(self._event_unsubscribers.values()):
            for unsubscribe in unsubscribers:
                try:
                    unsubscribe()
                except Exception:
                    logger.exception("failed to remove extension event subscription")
        self._event_unsubscribers.clear()

    # Action/context methods are thin and guarded by the generation check.
    # Unknown actions fail closed instead of becoming a
    # silent no-op that can make an extension believe it sent a message.
    def send_message(
        self, message: Any, options: Mapping[str, Any] | None = None
    ) -> Any:
        return self.action("send_message", message=message, options=options)

    def send_user_message(
        self, content: Any, options: Mapping[str, Any] | None = None
    ) -> Any:
        return self.action("send_user_message", content=content, options=options)

    def append_entry(self, custom_type: str, data: Any = None) -> Any:
        return self.action("append_entry", custom_type=custom_type, data=data)

    def set_session_name(self, name: str) -> Any:
        return self.action("set_session_name", name=name)

    def set_label(self, entry_id: str, label: str | None = None) -> Any:
        return self.action("set_label", entry_id=entry_id, label=label)

    def get_session_name(self) -> Any:
        return self.action("get_session_name")

    def get_active_tools(self) -> list[str]:
        return list(self.action("get_active_tools"))

    def get_all_tools(self) -> Any:
        return self.action("get_all_tools")

    def set_active_tools(self, names: Sequence[str]) -> Any:
        return self.action("set_active_tools", tool_names=list(names))

    def get_commands(self) -> Any:
        return self.action("get_commands")

    async def set_model(self, model: Any) -> Any:
        return await _maybe_await(self.action("set_model", model=model))

    def get_thinking_level(self) -> Any:
        return self.action("get_thinking_level")

    def set_thinking_level(self, level: Any) -> Any:
        return self.action("set_thinking_level", level=level)


class ExtensionContext:
    """Lazy, generation-guarded context passed to extension callbacks."""

    def __init__(
        self,
        runner: "ExtensionRunner",
        *,
        signal: Any = None,
        tool_context: Any = None,
        mode: ExtensionMode = "print",
    ) -> None:
        self._runner = runner
        self._signal = signal
        self._tool_context = tool_context
        self._mode = mode
        self._event_system_prompt: str | None = None

    def _assert(self) -> None:
        self._runner.assert_active()

    @property
    def ui(self) -> Any:
        self._assert()
        return self._runner.ui_context or _NO_OP_UI

    @property
    def mode(self) -> ExtensionMode:
        self._assert()
        return self._mode

    @property
    def has_ui(self) -> bool:
        self._assert()
        return self._runner.ui_context is not None

    @property
    def cwd(self) -> str:
        self._assert()
        return self._runner.cwd

    @property
    def model(self) -> Any:
        self._assert()
        return self._runner.context_action("model")

    @property
    def thinking_level(self) -> Any:
        self._assert()
        return self._runner.runtime.get_thinking_level()

    @property
    def session_manager(self) -> Any:
        self._assert()
        return self._runner.context_action("session_manager")

    @property
    def model_registry(self) -> Any:
        self._assert()
        return self._runner.context_action("model_registry")

    @property
    def signal(self) -> Any:
        self._assert()
        return self._signal

    @property
    def tool_context(self) -> Any:
        self._assert()
        return self._tool_context

    def is_idle(self) -> bool:
        self._assert()
        return bool(self._runner.context_action("is_idle", default=True))

    def is_project_trusted(self) -> bool:
        self._assert()
        return bool(self._runner.context_action("is_project_trusted", default=False))

    def abort(self) -> Any:
        self._assert()
        return self._runner.context_action("abort")

    def has_pending_messages(self) -> bool:
        self._assert()
        return bool(self._runner.context_action("has_pending_messages", default=False))

    def shutdown(self) -> Any:
        self._assert()
        return self._runner.request_shutdown()

    def get_context_usage(self) -> Any:
        self._assert()
        return self._runner.context_action("get_context_usage")

    def compact(self, options: Mapping[str, Any] | None = None) -> Any:
        self._assert()
        return self._runner.context_action("compact", options=options)

    def get_system_prompt(self) -> str:
        self._assert()
        if self._event_system_prompt is not None:
            return self._event_system_prompt
        return str(self._runner.context_action("get_system_prompt", default=""))

    def _set_event_system_prompt(self, prompt: str | None) -> None:
        self._event_system_prompt = prompt

    def __getattr__(self, name: str) -> Any:
        # Keep the context extensible without exposing private runner state.
        self._assert()
        if name in {
            "get_system_prompt_options",
            "wait_for_idle",
            "new_session",
            "fork",
            "navigate_tree",
            "switch_session",
            "reload",
        }:
            raise AttributeError(name)
        action = self._runner._context_actions.get(name)
        if action is None:
            raise AttributeError(name)
        if not callable(action):
            return action

        def guarded(*args: Any, **kwargs: Any) -> Any:
            self._assert()
            return _call_with_signature(action, kwargs, args)

        return guarded


class ExtensionCommandContext(ExtensionContext):
    """Command-only context for session replacement and reload actions."""

    def get_system_prompt_options(self) -> Any:
        self._assert()
        return self._runner.context_action("get_system_prompt_options", default={})

    async def wait_for_idle(self) -> Any:
        self._assert()
        return await _maybe_await(self._runner.context_action("wait_for_idle"))

    async def reload(self) -> Any:
        self._assert()
        return await _maybe_await(self._runner.context_action("reload"))


class ExtensionAPI:
    """Factory-facing registration/action API."""

    def __init__(self, runner: "ExtensionRunner", extension: Extension) -> None:
        self._runner = runner
        self._extension = extension
        self.events = _BoundEventBus(runner.runtime, runner.event_bus, extension.path)

    def _assert(self) -> None:
        self._runner.assert_active()

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        self._assert()
        if not isinstance(event, str) or not event.strip() or not callable(handler):
            raise ExtensionRegistrationError(
                "on requires a non-empty event and callable handler"
            )
        self._extension.add_handler(event.strip(), handler)

    def register_tool(
        self, tool: ExtensionToolDefinition | Mapping[str, Any], **kwargs: Any
    ) -> None:
        self._assert()
        if kwargs:
            if isinstance(tool, Mapping):
                merged = dict(tool)
                merged.update(kwargs)
                tool = merged
            else:
                merged = {**asdict(tool), **kwargs}
                tool = merged
        definition = ExtensionToolDefinition.from_value(tool)
        self._extension.tools[definition.name] = definition
        self._runner.refresh_tools()

    def register_command(
        self, name: str, options: Mapping[str, Any] | Callable[..., Any], **kwargs: Any
    ) -> None:
        self._assert()
        clean_name = str(name or "").strip().lstrip("/")
        if not clean_name:
            raise ExtensionRegistrationError("command name must be non-empty")
        if callable(options):
            payload: dict[str, Any] = {"handler": options}
        elif isinstance(options, Mapping):
            payload = dict(options)
        else:
            raise ExtensionRegistrationError(
                "register_command options must be a mapping or callable"
            )
        payload.update(kwargs)
        handler = payload.get("handler")
        if not callable(handler):
            raise ExtensionRegistrationError(
                f"command '{clean_name}' requires a callable handler"
            )
        self._extension.commands[clean_name] = ExtensionCommand(
            name=clean_name,
            handler=handler,
            description=(
                str(payload["description"])
                if payload.get("description") is not None
                else None
            ),
            get_argument_completions=payload.get("get_argument_completions"),
            extension_path=self._extension.path,
        )

    def register_shortcut(
        self,
        shortcut: str,
        options: Mapping[str, Any] | Callable[..., Any],
        **kwargs: Any,
    ) -> None:
        self._assert()
        clean = str(shortcut or "").strip().lower()
        if not clean:
            raise ExtensionRegistrationError("shortcut must be non-empty")
        payload = {"handler": options} if callable(options) else dict(options)
        payload.update(kwargs)
        if not callable(payload.get("handler")):
            raise ExtensionRegistrationError(
                f"shortcut '{clean}' requires a callable handler"
            )
        self._extension.shortcuts[clean] = ExtensionShortcut(
            shortcut=clean,
            handler=payload["handler"],
            description=(
                str(payload["description"])
                if payload.get("description") is not None
                else None
            ),
            extension_path=self._extension.path,
        )

    def register_flag(
        self, name: str, options: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> None:
        self._assert()
        clean = str(name or "").strip()
        payload = dict(options or {})
        payload.update(kwargs)
        flag_type = str(payload.get("type") or "boolean").lower()
        if flag_type not in {"boolean", "string"}:
            raise ExtensionRegistrationError("flag type must be 'boolean' or 'string'")
        if (
            flag_type == "boolean"
            and payload.get("default") is not None
            and not isinstance(payload["default"], bool)
        ):
            raise ExtensionRegistrationError("boolean flag default must be bool")
        if (
            flag_type == "string"
            and payload.get("default") is not None
            and not isinstance(payload["default"], str)
        ):
            raise ExtensionRegistrationError("string flag default must be str")
        if not clean:
            raise ExtensionRegistrationError("flag name must be non-empty")
        flag = ExtensionFlag(
            name=clean,
            type=flag_type,  # type: ignore[arg-type]
            default=payload.get("default"),
            description=(
                str(payload["description"])
                if payload.get("description") is not None
                else None
            ),
            extension_path=self._extension.path,
        )
        self._extension.flags[clean] = flag
        if flag.default is not None and clean not in self._runner.runtime.flag_values:
            self._runner.runtime.flag_values[clean] = flag.default

    def get_flag(self, name: str) -> bool | str | None:
        self._assert()
        if str(name) not in self._extension.flags:
            return None
        return self._runner.runtime.flag_values.get(str(name))

    def register_provider(
        self,
        provider: str,
        config: Mapping[str, Any],
    ) -> None:
        self._assert()
        if not isinstance(provider, str) or not provider.strip():
            raise ExtensionRegistrationError("provider id must be a non-empty string")
        if not isinstance(config, Mapping):
            raise ExtensionRegistrationError("provider config must be an object")
        name = provider.strip()
        registration = ExtensionProvider(
            name=name,
            config=dict(config),
            extension_path=self._extension.path,
        )
        self._extension.providers[name] = registration
        self._runner.runtime.register_provider(
            name, registration.config, self._extension.path
        )

    def unregister_provider(self, name: str) -> None:
        self._assert()
        clean = str(name or "").strip()
        self._extension.providers.pop(clean, None)
        self._runner.runtime.unregister_provider(clean, self._extension.path)

    def register_message_renderer(
        self, custom_type: str, renderer: Callable[..., Any]
    ) -> None:
        self._assert()
        if not str(custom_type).strip() or not callable(renderer):
            raise ExtensionRegistrationError(
                "register_message_renderer requires a type and callable"
            )
        self._extension.message_renderers[str(custom_type)] = renderer

    def register_entry_renderer(
        self, custom_type: str, renderer: Callable[..., Any]
    ) -> None:
        self._assert()
        if not str(custom_type).strip() or not callable(renderer):
            raise ExtensionRegistrationError(
                "register_entry_renderer requires a type and callable"
            )
        self._extension.entry_renderers[str(custom_type)] = renderer

    # Action methods -----------------------------------------------------
    def send_message(
        self, message: Any, options: Mapping[str, Any] | None = None
    ) -> Any:
        self._assert()
        return self._runner.runtime.send_message(message, options)

    def send_user_message(
        self, content: Any, options: Mapping[str, Any] | None = None
    ) -> Any:
        self._assert()
        return self._runner.runtime.send_user_message(content, options)

    def append_entry(self, custom_type: str, data: Any = None) -> Any:
        self._assert()
        return self._runner.runtime.append_entry(custom_type, data)

    def set_session_name(self, name: str) -> Any:
        self._assert()
        return self._runner.runtime.set_session_name(name)

    def set_label(self, entry_id: str, label: str | None = None) -> Any:
        self._assert()
        return self._runner.runtime.set_label(entry_id, label)

    def get_session_name(self) -> Any:
        self._assert()
        return self._runner.runtime.get_session_name()

    def get_active_tools(self) -> list[str]:
        self._assert()
        return self._runner.runtime.get_active_tools()

    def get_all_tools(self) -> Any:
        self._assert()
        return self._runner.runtime.get_all_tools()

    def set_active_tools(self, names: Sequence[str]) -> Any:
        self._assert()
        return self._runner.runtime.set_active_tools(names)

    def get_commands(self) -> Any:
        self._assert()
        return self._runner.runtime.get_commands()

    async def set_model(self, model: Any) -> Any:
        self._assert()
        return await self._runner.runtime.set_model(model)

    def get_thinking_level(self) -> Any:
        self._assert()
        return self._runner.runtime.get_thinking_level()

    def set_thinking_level(self, level: Any) -> Any:
        self._assert()
        return self._runner.runtime.set_thinking_level(level)

    def exec(
        self,
        command: str,
        args: Sequence[str] = (),
        options: Mapping[str, Any] | None = None,
    ) -> Any:
        self._assert()
        options = dict(options or {})
        if self._runner.runtime._actions.get("exec") is None:
            raise PermissionError(
                "extension exec requires a MiniCode host action"
            )
        return self._runner.runtime.action(
            "exec", command=command, args=list(args), options=options
        )

    events: Any


try:  # Keep module importable for light-weight tooling outside MiniCode.
    from backend.tools.base import BaseTool
except Exception:  # pragma: no cover

    class BaseTool:  # type: ignore[no-redef]
        pass


class ExtensionToolAdapter(BaseTool):
    """Adapt an extension tool definition to MiniCode's ``BaseTool`` contract."""

    def __init__(
        self, definition: ExtensionToolDefinition, runner: "ExtensionRunner"
    ) -> None:
        self.definition = definition
        self._runner = runner
        self.name = definition.name
        self.description = definition.description
        from backend.tools.base import PermissionLevel

        self.read_only = definition.read_only
        self.destructive = definition.destructive
        self.open_world = definition.open_world
        self.mutates_workspace = definition.mutates_workspace
        self.mutates_external_state = definition.mutates_external_state
        self.side_effect_kind = definition.side_effect_kind
        # Mirror the MCP tool mapping (mcp/registry.py): tools whose declared
        # semantics mutate state escalate to CONFIRM instead of a blanket
        # AUTO, so a changed extension tool re-earns approval.
        if self.get_side_effect_kind() != "none":
            self.permission = PermissionLevel.CONFIRM
        else:
            self.permission = PermissionLevel.AUTO
        self.idempotent = definition.idempotent
        self.timeout_seconds = definition.timeout_seconds
        self.streams_output = definition.streams_output
        self.execution_mode = definition.execution_mode
        self.display_label = definition.label or definition.name
        self.result_kind = str(definition.metadata.get("result_kind") or "") or None
        self.activity_kind = str(definition.metadata.get("activity_kind") or "") or None
        self.search_hint = str(definition.metadata.get("search_hint") or "")
        self.always_load = bool(definition.metadata.get("always_load", False))
        self.should_defer = bool(definition.metadata.get("should_defer", False))
        self.deferred_catalog_scopes = tuple(
            definition.metadata.get("deferred_catalog_scopes") or ("default",)
        )

    def get_schema(self) -> Any:
        from backend.tools.base import ToolSchema

        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=dict(self.definition.parameters),
            strict=bool(self.definition.metadata.get("strict", False)),
        )

    def to_runtime_metadata(self) -> dict[str, Any]:
        try:
            metadata = dict(super().to_runtime_metadata())
        except (AttributeError, TypeError):  # pragma: no cover - fallback BaseTool
            metadata = {}
        metadata.update(dict(self.definition.metadata))
        metadata.update(
            {
                "extension_path": self._runner.extension_for_tool(self.name),
                "extension": True,
                "read_only": self.is_read_only(),
                "destructive": self.destructive,
                "open_world": self.open_world,
                "mutates_workspace": self.mutates_workspace,
                "mutates_external_state": self.mutates_external_state,
                "side_effect_kind": self.get_side_effect_kind(),
                "idempotent": self.is_idempotent(),
                "timeout_seconds": self.timeout_seconds,
                "streams_output": self.streams_output,
                "execution_mode": self.execution_mode,
            }
        )
        metadata.update(self.to_projection_metadata())
        return metadata

    def model_description(self) -> str:
        return self.description

    def model_schema(self) -> Any:
        return None

    def validate_input(self, args: dict[str, Any] | None = None) -> str:
        return ""

    def to_projection_metadata(self) -> dict[str, Any]:
        result = {}
        if self.result_kind:
            result["result_kind"] = self.result_kind
        if self.activity_kind:
            result["activity_kind"] = self.activity_kind
        if self.display_label:
            result["display_label"] = self.display_label
        return result

    def is_read_only(self, args: dict[str, Any] | None = None) -> bool:
        return (
            self.read_only
            and not self.destructive
            and not self.open_world
            and not self.mutates_workspace
            and not self.mutates_external_state
        )

    def is_concurrency_safe(self, args: dict[str, Any] | None = None) -> bool:
        if self.execution_mode == "parallel":
            return True
        if self.execution_mode == "sequential":
            return False
        return super().is_concurrency_safe(args)

    def prepare_arguments(self, args: dict[str, Any]) -> dict[str, Any]:
        prepare = self.definition.prepare_arguments
        if prepare is None:
            return args
        value = _call_with_signature(
            prepare,
            {"args": args, "input": args, "params": args},
            (args,),
        )
        if inspect.isawaitable(value):
            raise TypeError("extension prepare_arguments must be synchronous")
        if not isinstance(value, Mapping):
            raise TypeError("extension prepare_arguments must return an object")
        return dict(value)

    def get_side_effect_kind(self, args: dict[str, Any] | None = None) -> str:
        if self.destructive:
            return "destructive"
        if self.mutates_external_state or self.open_world:
            return "external"
        if self.mutates_workspace:
            return "workspace"
        if self.side_effect_kind:
            return self.side_effect_kind
        return "none"

    def is_idempotent(self, args: dict[str, Any] | None = None) -> bool:
        if self.idempotent is not None:
            return bool(self.idempotent)
        return self.get_side_effect_kind(args) == "none"

    async def execute(self, args: dict[str, Any], context: Any = None) -> Any:
        tool_call_id = ""
        signal = None
        if context is not None:
            metadata = getattr(context, "metadata", {}) or {}
            tool_call_id = str(
                getattr(context, "tool_call_id", "")
                or metadata.get("tool_call_id")
                or metadata.get("tool_call_id_hint")
                or ""
            )
            signal = getattr(context, "cancel_event", None)
        on_update = None
        if context is not None:
            stream_callback = getattr(context, "stream_callback", None)
            if callable(stream_callback):

                async def _on_update(value: Any, *args: Any, **kwargs: Any) -> None:
                    del args, kwargs
                    text = _normalise_content(value)
                    if not text:
                        return
                    callback_result = stream_callback(text, "stdout", tool_call_id)
                    if inspect.isawaitable(callback_result):
                        await callback_result

                on_update = _on_update
        return await self._runner.invoke_tool(
            tool_call_id=tool_call_id,
            tool_name=self.name,
            params=dict(args or {}),
            signal=signal,
            on_update=on_update,
            tool_context=context,
            raw_definition=self.definition,
        )


class ExtensionRunner:
    """Own loaded extensions and execute their registrations/events."""

    def __init__(
        self,
        extensions: Sequence[Extension] = (),
        runtime: ExtensionRuntime | None = None,
        *,
        cwd: str | Path | None = None,
        event_bus: ExtensionEventBus | None = None,
        mode: ExtensionMode = "print",
        ui_context: Any = None,
        context_actions: Mapping[str, Callable[..., Any]] | None = None,
        error_listener: Callable[[ExtensionError], Any] | None = None,
    ) -> None:
        self.extensions = list(extensions)
        self.runtime = runtime or ExtensionRuntime()
        self.cwd = str(Path(cwd or Path.cwd()).expanduser().resolve())
        self.event_bus = (
            event_bus
            or getattr(self.runtime, "_event_bus", None)
            or ExtensionEventBus()
        )
        self.runtime._event_bus = self.event_bus
        self.mode = mode
        self.ui_context = ui_context
        self._context_actions: dict[str, Callable[..., Any]] = dict(
            context_actions or {}
        )
        self._error_listener = error_listener
        self.errors: list[ExtensionError] = []
        self.diagnostics: list[dict[str, str]] = []
        self._active = True
        self._startup_emitted = False
        self._shutdown_started = False
        self._shutdown_requested = False
        self._tool_registry: Any | None = None
        self._registered_tool_adapters: dict[str, ExtensionToolAdapter] = {}
        self._replaced_tools: dict[str, Any] = {}
        self._replaced_tool_owners: dict[str, str] = {}
        self._tool_bind_override_existing = False
        self._command_registry: Any | None = None
        self._registered_commands: dict[str, str] = {}
        self._command_wrappers: dict[str, Callable[..., Any]] = {}
        self._command_registry_scope = "*"
        self._command_registry_token: object | None = None
        self._provider_sink: Any | None = None
        self.events = self.event_bus

    @property
    def generation(self) -> int:
        return self.runtime.generation

    @property
    def active(self) -> bool:
        return self._active and self.runtime.active

    def assert_active(self) -> None:
        if not self._active:
            raise ExtensionStaleError(DEFAULT_STALE_MESSAGE)
        self.runtime.assert_active()

    def invalidate(self, message: str | None = None) -> None:
        # MiniCode replaces the complete tool/command registry with the new session
        # runtime.  MiniCode binds into host registries, so invalidation must
        # explicitly remove those wrappers before the old API becomes stale;
        # otherwise a dead generation remains user-invokable after reload.
        self.detach_tool_registry()
        self.detach_command_registry()
        self._active = False
        self.runtime.invalidate(message)

    def bind_actions(
        self,
        actions: Mapping[str, Callable[..., Any]] | None = None,
        **kwargs: Callable[..., Any],
    ) -> None:
        self.assert_active()
        self.runtime.bind_actions(actions, **kwargs)

    def context_action(self, name: str, default: Any = None, **kwargs: Any) -> Any:
        self.assert_active()
        callback = self._context_actions.get(name)
        if callback is None:
            return default
        return _call_with_signature(callback, kwargs, ())

    def bind_context_actions(
        self,
        actions: Mapping[str, Callable[..., Any]] | None = None,
        **kwargs: Callable[..., Any],
    ) -> None:
        self.assert_active()
        self._context_actions.update(dict(actions or {}))
        self._context_actions.update(kwargs)

    def bind_ui_context(
        self,
        ui_context: Any = None,
        *,
        mode: ExtensionMode | None = None,
    ) -> None:
        """Swap the host UI capability without replacing the runner."""

        self.assert_active()
        self.ui_context = ui_context
        if mode is not None:
            self.mode = mode

    def bind_provider_sink(self, sink: Any | None) -> None:
        self.assert_active()
        self._provider_sink = sink
        self.runtime.bind_provider_sink(sink)

    def bind_tool_registry(
        self,
        registry: Any,
        *,
        include_conflicts: bool | None = None,
        override_existing: bool | None = None,
    ) -> list[str]:
        """Register extension tools into an existing CapabilityRegistry.

        MiniCode builds the base registry first. Extension tools cannot shadow
        a built-in capability unless the caller explicitly opts into replacement.
        """

        self.assert_active()
        if override_existing is None:
            override_existing = (
                False if include_conflicts is None else bool(include_conflicts)
            )
        if self._tool_registry is not None:
            self.detach_tool_registry()
        self._tool_registry = registry
        self._tool_bind_override_existing = bool(override_existing)
        added: list[str] = []
        getter = getattr(registry, "get_tool", None)
        register = getattr(registry, "register", None)
        if register is None:
            raise TypeError("tool registry does not expose register(tool)")
        for definition in self.get_registered_tools():
            name = definition.name
            existing = None
            if getter is not None:
                try:
                    existing = getter(name)
                except Exception:
                    existing = None
            if existing is not None and not override_existing:
                self.diagnostics.append(
                    {
                        "type": "warning",
                        "path": self.extension_for_tool(name),
                        "message": f"tool conflict for '{name}'; host tool wins",
                    }
                )
                continue
            if existing is not None:
                self._replaced_tools[name] = existing
                get_owner = getattr(registry, "get_tool_owner", None)
                if callable(get_owner):
                    prior_owner = get_owner(name)
                    if prior_owner:
                        self._replaced_tool_owners[name] = str(prior_owner)
                self.diagnostics.append(
                    {
                        "type": "warning",
                        "path": self.extension_for_tool(name),
                        "message": f"tool conflict for '{name}'; extension tool wins",
                    }
                )
            adapter = ExtensionToolAdapter(definition, self)
            register(adapter, replace=bool(override_existing), owner="extension")
            self._registered_tool_adapters[name] = adapter
            added.append(name)
        return added

    def detach_tool_registry(self) -> None:
        registry = self._tool_registry
        if registry is None:
            return
        unregister = getattr(registry, "unregister", None)
        register = getattr(registry, "register", None)
        for name, adapter in list(self._registered_tool_adapters.items()):
            getter = getattr(registry, "get_tool", None)
            current = getter(name) if getter is not None else adapter
            if current is adapter:
                try:
                    if unregister is not None:
                        unregister(name)
                    replaced = self._replaced_tools.get(name)
                    if replaced is not None and register is not None:
                        register(
                            replaced,
                            replace=True,
                            owner=self._replaced_tool_owners.get(name),
                        )
                except Exception:
                    logger.exception("failed to detach extension tool %s", name)
        self._registered_tool_adapters.clear()
        self._replaced_tools.clear()
        self._replaced_tool_owners.clear()
        self._tool_registry = None

    def bind_command_registry(
        self, registry: Any, *, scope_id: str = "*"
    ) -> list[str]:
        self.assert_active()
        self._command_registry = registry
        self._command_registry_scope = str(scope_id or "*").strip() or "*"
        added: list[str] = []
        registrations: dict[
            str, tuple[Callable[..., Any], Mapping[str, Any]]
        ] = {}
        for command in self.get_commands():
            invocation = command.name

            async def command_wrapper(
                _handler_obj: Any,
                args: Any = "",
                attachments: Any = None,
                _command: ExtensionCommand = command,
            ) -> Any:
                _ = attachments
                self.assert_active()
                ctx = self.create_command_context()
                try:
                    value = _call_with_signature(
                        _command.handler,
                        {"args": args, "ctx": ctx, "context": ctx},
                        (args, ctx),
                    )
                    await _maybe_await(value)
                except ExtensionStaleError:
                    raise
                except Exception as exc:
                    self.record_error("command", _command.extension_path, exc)
                return True

            metadata = {
                "name": invocation,
                "command": invocation,
                "label": f"/{invocation}",
                "description": str(command.description or ""),
                "type": "local",
                "source": "extension",
                "extension_path": command.extension_path,
                "enabled": True,
                "availability": {
                    "kind": "always",
                    "scope": (
                        "session"
                        if self._command_registry_scope == "*"
                        else "conversation"
                    ),
                },
            }
            registrations[invocation] = (command_wrapper, metadata)
            self._command_wrappers[invocation] = command_wrapper
            self._registered_commands[invocation] = command.extension_path
            added.append(invocation)

        replace = getattr(registry, "replace_extension_slash_handlers", None)
        if not callable(replace):
            raise TypeError(
                "command registry does not expose replace_extension_slash_handlers"
            )
        self._command_registry_token = replace(
            self._command_registry_scope,
            registrations,
        )
        return added

    def detach_command_registry(self) -> None:
        registry = self._command_registry
        clear = (
            getattr(registry, "clear_extension_slash_handlers", None)
            if registry is not None
            else None
        )
        if callable(clear):
            try:
                clear(
                    self._command_registry_scope,
                    token=self._command_registry_token,
                )
            except Exception:
                logger.exception(
                    "failed to detach extension command scope %s",
                    self._command_registry_scope,
                )
        self._registered_commands.clear()
        self._command_wrappers.clear()
        self._command_registry = None
        self._command_registry_scope = "*"
        self._command_registry_token = None

    def refresh_tools(self) -> None:
        # A full host may own registry reconstruction.
        # Lightweight integrations that bind an existing registry still need
        # late register_tool() calls to become visible immediately.
        callback = self.runtime._actions.get("refresh_tools")
        if callback is not None:
            try:
                _call_with_signature(callback, {}, ())
            except Exception as exc:
                self.record_error("refresh_tools", "<runtime>", exc)
            return
        registry = self._tool_registry
        if registry is not None:
            override_existing = self._tool_bind_override_existing
            self.detach_tool_registry()
            self.bind_tool_registry(
                registry,
                override_existing=override_existing,
            )

    def extension_for_tool(self, name: str) -> str:
        for extension in self.extensions:
            if name in extension.tools:
                return extension.path
        return ""

    def source_info_for_tool(self, name: str) -> dict[str, Any] | None:
        for extension in self.extensions:
            if name in extension.tools:
                return extension.source.to_source_info()
        return None

    def has_handlers(self, event: str) -> bool:
        return any(self._handlers_for(event))

    def _handlers_for(self, event: str) -> list[tuple[Extension, Callable[..., Any]]]:
        result: list[tuple[Extension, Callable[..., Any]]] = []
        for extension in self.extensions:
            for handler in extension.handlers.get(event, ()):
                result.append((extension, handler))
        return result

    def record_error(
        self, event: str, extension_path: str, error: BaseException
    ) -> ExtensionError:
        item = ExtensionError(
            extension_path=extension_path,
            event=event,
            error=str(error),
            stack=(__import__("traceback").format_exc() if error else None),
        )
        self.errors.append(item)
        if self._error_listener is not None:
            try:
                self._error_listener(item)
            except Exception:
                logger.exception("extension error listener failed")
        return item

    def create_context(
        self, *, signal: Any = None, tool_context: Any = None
    ) -> ExtensionContext:
        self.assert_active()
        return ExtensionContext(
            self, signal=signal, tool_context=tool_context, mode=self.mode
        )

    def create_command_context(
        self, *, signal: Any = None, tool_context: Any = None
    ) -> ExtensionCommandContext:
        self.assert_active()
        return ExtensionCommandContext(
            self, signal=signal, tool_context=tool_context, mode=self.mode
        )

    async def emit(
        self,
        event: str | Mapping[str, Any] | Any,
        payload: Any = None,
        *,
        context: ExtensionContext | None = None,
    ) -> list[Any]:
        self.assert_active()
        if isinstance(event, str):
            event_name = event
            event_payload = payload
        elif isinstance(event, Mapping):
            event_name = str(event.get("type") or event.get("event") or "")
            event_payload = event
        else:
            event_name = str(getattr(event, "type", "") or "")
            event_payload = event
        if not event_name:
            raise ValueError("extension event requires a type")
        ctx = context or self.create_context()
        results: list[Any] = []
        for extension, handler in self._handlers_for(event_name):
            try:
                value = _call_with_signature(
                    handler,
                    {"event": event_payload, "ctx": ctx, "context": ctx},
                    (event_payload, ctx),
                )
                results.append(await _maybe_await(value))
            except ExtensionStaleError:
                raise
            except Exception as exc:
                self.record_error(event_name, extension.path, exc)
        return results

    async def emit_before_agent_start(
        self,
        prompt: str,
        images: Any = None,
        system_prompt: str = "",
        system_prompt_options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Run ordered ``before_agent_start`` aggregation.

        The generic event bus returns one value per handler. This specialized
        path folds results in extension order: custom messages are
        accumulated and a returned system prompt replaces the current one for
        subsequent handlers. Keeping that fold here means hosts can consume a
        stable result without knowing how extensions were loaded.
        """

        self.assert_active()
        current_system_prompt = str(system_prompt or "")
        system_prompt_modified = False
        messages: list[Any] = []
        context = self.create_context()
        event_options = dict(system_prompt_options or {})
        for extension, handler in self._handlers_for("before_agent_start"):
            context._set_event_system_prompt(current_system_prompt)
            event = {
                "type": "before_agent_start",
                "prompt": str(prompt or ""),
                "images": images,
                "system_prompt": current_system_prompt,
                "system_prompt_options": event_options,
            }
            try:
                value = await _maybe_await(
                    _call_with_signature(handler, {"event": event, "ctx": context, "context": context}, (event, context))
                )
            except Exception as exc:
                self.record_error("before_agent_start", extension.path, exc)
                continue
            if not isinstance(value, Mapping):
                continue
            if "message" in value and value.get("message") is not None:
                messages.append(value.get("message"))
            raw_messages = value.get("messages")
            if isinstance(raw_messages, (list, tuple)):
                messages.extend(raw_messages)
            if "system_prompt" in value:
                raw_prompt = value.get("system_prompt")
                if raw_prompt is not None:
                    current_system_prompt = str(raw_prompt)
                    system_prompt_modified = True
        if not messages and not system_prompt_modified:
            return None
        return {
            "messages": messages or None,
            "system_prompt": current_system_prompt if system_prompt_modified else None,
        }

    async def emit_context(self, messages: Sequence[Any]) -> list[Any]:
        """Apply the sequential ``context`` transform before provider I/O."""

        self.assert_active()
        current = copy.deepcopy(list(messages))
        context = self.create_context()
        for extension, handler in self._handlers_for("context"):
            handler_messages = copy.deepcopy(current)
            event = {"type": "context", "messages": handler_messages}
            try:
                value = await _maybe_await(
                    _call_with_signature(handler, {"event": event, "ctx": context, "context": context}, (event, context))
                )
            except ExtensionStaleError:
                raise
            except Exception as exc:
                self.record_error("context", extension.path, exc)
                continue
            if isinstance(value, Mapping) and isinstance(value.get("messages"), (list, tuple)):
                current = copy.deepcopy(list(value["messages"]))
            elif isinstance(value, (list, tuple)):
                current = copy.deepcopy(list(value))
            else:
                current = handler_messages
        return current

    async def emit_before_provider_request(self, payload: Any) -> Any:
        """Apply the replaceable ``before_provider_request`` payload hook."""

        self.assert_active()
        current = payload
        context = self.create_context()
        for extension, handler in self._handlers_for("before_provider_request"):
            event = {"type": "before_provider_request", "payload": current}
            try:
                value = await _maybe_await(
                    _call_with_signature(handler, {"event": event, "ctx": context, "context": context}, (event, context))
                )
            except ExtensionStaleError:
                raise
            except Exception as exc:
                self.record_error("before_provider_request", extension.path, exc)
                continue
            if value is not None:
                current = value
        return current

    async def emit_before_provider_headers(
        self, headers: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Run the in-place provider header hook."""

        self.assert_active()
        # Expose one mutable header object to the hook chain. Preserve the
        # caller's dict identity so mutations cross the adapter wire boundary;
        # copying here would make a correct in-place handler a silent no-op.
        current = headers if isinstance(headers, dict) else dict(headers)
        context = self.create_context()
        for extension, handler in self._handlers_for("before_provider_headers"):
            event = {"type": "before_provider_headers", "headers": current}
            try:
                await _maybe_await(
                    _call_with_signature(handler, {"event": event, "ctx": context, "context": context}, (event, context))
                )
            except ExtensionStaleError:
                raise
            except Exception as exc:
                self.record_error("before_provider_headers", extension.path, exc)
        return current

    async def emit_after_provider_response(
        self, status: int, headers: Mapping[str, Any] | None = None
    ) -> list[Any]:
        """Notify extensions after a provider response is received."""

        return await self.emit(
            {
                "type": "after_provider_response",
                "status": int(status),
                "headers": dict(headers or {}),
            }
        )

    async def emit_tool_call(
        self, event: ToolCallEvent, *, context: ExtensionContext | None = None
    ) -> ToolCallDecision | None:
        self.assert_active()
        ctx = context or self.create_context()
        decision: ToolCallDecision | None = None
        for extension, handler in self._handlers_for("tool_call"):
            try:
                value = await _maybe_await(
                    _call_with_signature(
                        handler,
                        {"event": event, "ctx": ctx, "context": ctx},
                        (event, ctx),
                    )
                )
                if value is None:
                    continue
                if isinstance(value, ToolCallDecision):
                    candidate = value
                elif isinstance(value, Mapping):
                    candidate = ToolCallDecision(
                        bool(value.get("block", False)), value.get("reason")
                    )
                else:
                    raise ExtensionRegistrationError(
                        "tool_call handlers must return a mapping or ToolCallDecision"
                    )
                decision = candidate
                if candidate.block:
                    return candidate
            except Exception as exc:
                # A pre-tool hook is a security boundary.  Fail closed and
                # make the owning extension visible in diagnostics.
                self.record_error("tool_call", extension.path, exc)
                return ToolCallDecision(
                    True,
                    f"Extension '{extension.path}' failed before tool execution: {exc}",
                )
        return decision

    async def emit_tool_result(
        self, event: ToolResultEvent, *, context: ExtensionContext | None = None
    ) -> ToolResultPatch | None:
        self.assert_active()
        if isinstance(event, Mapping):
            event = ToolResultEvent(
                tool_call_id=str(
                    event.get("tool_call_id", event.get("id", ""))
                    or ""
                ),
                tool_name=str(
                    event.get("tool_name", event.get("name", ""))
                    or ""
                ),
                input=dict(event.get("input", event.get("args", {})) or {}),
                content=event.get("content", ""),
                details=event.get("details"),
                is_error=bool(event.get("is_error", False)),
                usage=event.get("usage"),
            )
        ctx = context or self.create_context()
        modified = False
        for extension, handler in self._handlers_for("tool_result"):
            try:
                before_state = (
                    event.content,
                    event.details,
                    event.is_error,
                    event.usage,
                )
                value = await _maybe_await(
                    _call_with_signature(
                        handler,
                        {"event": event, "ctx": ctx, "context": ctx},
                        (event, ctx),
                    )
                )
                patch = ToolResultPatch.from_value(value)
                if patch is None:
                    continue
                if patch.has_content:
                    event.content = patch.content
                    modified = True
                if patch.has_details:
                    event.details = patch.details
                    modified = True
                if patch.is_error is not None:
                    event.is_error = bool(patch.is_error)
                    modified = True
                if patch.has_usage:
                    event.usage = patch.usage
                    modified = True
                if (
                    event.content,
                    event.details,
                    event.is_error,
                    event.usage,
                ) != before_state:
                    # Preserve the ergonomic in-place mutation form as well as
                    # the documented returned patch form.
                    modified = True
            except Exception as exc:
                self.record_error("tool_result", extension.path, exc)
        if not modified:
            return None
        return ToolResultPatch(
            content=event.content,
            details=event.details,
            is_error=event.is_error,
            usage=event.usage,
            has_content=True,
            has_details=True,
            has_usage=True,
        )

    async def invoke_tool(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        params: dict[str, Any],
        signal: Any = None,
        on_update: Callable[..., Any] | None = None,
        tool_context: Any = None,
        raw_definition: ExtensionToolDefinition | None = None,
    ) -> Any:
        self.assert_active()
        definition = raw_definition
        if definition is None:
            for extension in self.extensions:
                definition = extension.tools.get(tool_name)
                if definition is not None:
                    break
        if definition is None:
            raise KeyError(f"extension tool not found: {tool_name}")
        event = ToolCallEvent(
            tool_call_id=tool_call_id, tool_name=tool_name, input=dict(params or {})
        )
        ctx = self.create_context(signal=signal, tool_context=tool_context)
        decision = await self.emit_tool_call(event, context=ctx)
        if decision is not None and decision.block:
            result = _as_tool_result(
                {
                    "content": decision.reason
                    or f"Tool '{tool_name}' was blocked by an extension.",
                    "is_error": True,
                    "status": "blocked",
                }
            )
        else:
            values = {
                "tool_call_id": tool_call_id,
                "params": event.input,
                "signal": signal,
                "on_update": on_update,
                "ctx": ctx,
                "tool_context": tool_context,
            }
            raw = _call_with_signature(
                definition.execute,
                values,
                (tool_call_id, event.input, signal, on_update, ctx),
            )
            result = _as_tool_result(await _maybe_await(raw))

        # Build the extension result event while retaining MiniCode's compact
        # ToolResult object for the host.
        content = getattr(
            result,
            "content",
            result.get("content", "") if isinstance(result, Mapping) else result,
        )
        is_error = bool(
            getattr(
                result,
                "is_error",
                result.get("is_error", False) if isinstance(result, Mapping) else False,
            )
        )
        details = getattr(
            result,
            "details",
            result.get("details") if isinstance(result, Mapping) else None,
        )
        usage = getattr(
            result,
            "usage",
            result.get("usage") if isinstance(result, Mapping) else None,
        )
        result_event = ToolResultEvent(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            input=event.input,
            content=content,
            details=details,
            is_error=is_error,
            usage=usage,
        )
        patch = await self.emit_tool_result(result_event, context=ctx)
        if patch is not None:
            try:
                from backend.tools.base import ToolResult

                if isinstance(result, ToolResult):
                    result.content = _normalise_content(result_event.content)
                    result.is_error = bool(result_event.is_error)
                    if (
                        hasattr(result, "display_summary")
                        and result_event.details is not None
                    ):
                        # Keep details opaque; hosts that support a details
                        # field can consume it from the event patch.
                        pass
                    return result
            except Exception as exc:
                logger.warning("Extension tool-result patch failed; preserving original result: %s", exc, exc_info=True)
                pass
            return _as_tool_result(
                {
                    "content": result_event.content,
                    "is_error": result_event.is_error,
                    "details": result_event.details,
                }
            )
        return result

    async def before_tool_call(
        self,
        tool_call_id: str,
        tool_name: str,
        params: Mapping[str, Any] | None = None,
        *,
        signal: Any = None,
        tool_context: Any = None,
    ) -> ToolCallDecision | None:
        """Agent-hook shaped pre-execution entry point.

        A host agent can assign this method to its ``before_tool_call`` hook
        without adopting the extension tool adapter.  The argument dictionary
        is shared with handlers, so in-place argument mutation is
        preserved.
        """

        context = self.create_context(signal=signal, tool_context=tool_context)
        input_payload = params if isinstance(params, dict) else dict(params or {})
        return await self.emit_tool_call(
            ToolCallEvent(
                tool_call_id=str(tool_call_id or ""),
                tool_name=str(tool_name or ""),
                input=input_payload,
            ),
            context=context,
        )

    async def after_tool_call(
        self,
        tool_call_id: str,
        tool_name: str,
        params: Mapping[str, Any] | None,
        result: Any,
        *,
        is_error: bool | None = None,
        usage: Any = None,
        details: Any = None,
        signal: Any = None,
        tool_context: Any = None,
    ) -> ToolResultPatch | None:
        """Agent-hook shaped post-execution entry point."""

        resolved_error = bool(
            is_error
            if is_error is not None
            else getattr(
                result,
                "is_error",
                result.get("is_error", False) if isinstance(result, Mapping) else False,
            )
        )
        content = getattr(
            result,
            "content",
            result.get("content", result) if isinstance(result, Mapping) else result,
        )
        resolved_details = (
            details
            if details is not None
            else getattr(
                result,
                "details",
                result.get("details") if isinstance(result, Mapping) else None,
            )
        )
        resolved_usage = (
            usage
            if usage is not None
            else getattr(
                result,
                "usage",
                result.get("usage") if isinstance(result, Mapping) else None,
            )
        )
        context = self.create_context(signal=signal, tool_context=tool_context)
        return await self.emit_tool_result(
            ToolResultEvent(
                tool_call_id=str(tool_call_id or ""),
                tool_name=str(tool_name or ""),
                input=dict(params or {}),
                content=content,
                details=resolved_details,
                is_error=resolved_error,
                usage=resolved_usage,
            ),
            context=context,
        )

    def get_registered_tools(self) -> list[ExtensionToolDefinition]:
        result: list[ExtensionToolDefinition] = []
        seen: set[str] = set()
        for extension in self.extensions:
            for name, definition in extension.tools.items():
                if name in seen:
                    continue
                seen.add(name)
                result.append(definition)
        return result

    def get_commands(self) -> list[ExtensionCommand]:
        result: list[ExtensionCommand] = []
        counts: dict[str, int] = {}
        for extension in self.extensions:
            for command in extension.commands.values():
                result.append(command)
                counts[command.name] = counts.get(command.name, 0) + 1
        seen: dict[str, int] = {}
        taken_invocation_names: set[str] = set()
        resolved: list[ExtensionCommand] = []
        for command in result:
            occurrence = seen.get(command.name, 0) + 1
            seen[command.name] = occurrence
            invocation = (
                command.name
                if counts[command.name] == 1
                else f"{command.name}:{occurrence}"
            )
            if invocation in taken_invocation_names:
                suffix = occurrence
                while invocation in taken_invocation_names:
                    suffix += 1
                    invocation = f"{command.name}:{suffix}"
            taken_invocation_names.add(invocation)
            resolved.append(
                ExtensionCommand(
                    name=invocation,
                    handler=command.handler,
                    description=command.description,
                    get_argument_completions=command.get_argument_completions,
                    extension_path=command.extension_path,
                )
            )
        return resolved

    def get_message_renderer(self, custom_type: str) -> Callable[..., Any] | None:
        self.assert_active()
        for extension in self.extensions:
            renderer = extension.message_renderers.get(str(custom_type))
            if renderer is not None:
                return renderer
        return None

    def get_entry_renderer(self, custom_type: str) -> Callable[..., Any] | None:
        self.assert_active()
        for extension in self.extensions:
            renderer = extension.entry_renderers.get(str(custom_type))
            if renderer is not None:
                return renderer
        return None

    def get_flags(self) -> dict[str, ExtensionFlag]:
        result: dict[str, ExtensionFlag] = {}
        for extension in self.extensions:
            for name, flag in extension.flags.items():
                result.setdefault(name, flag)
        return result

    def get_shortcuts(
        self, reserved: set[str] | None = None
    ) -> dict[str, ExtensionShortcut]:
        result: dict[str, ExtensionShortcut] = {}
        reserved = {str(item).lower() for item in (reserved or set())}
        for extension in self.extensions:
            for key, shortcut in extension.shortcuts.items():
                normalized = key.lower()
                if normalized in reserved:
                    self.diagnostics.append(
                        {
                            "type": "warning",
                            "path": extension.path,
                            "message": f"shortcut '{key}' conflicts with a reserved binding",
                        }
                    )
                    continue
                if normalized in result:
                    self.diagnostics.append(
                        {
                            "type": "warning",
                            "path": extension.path,
                            "message": f"shortcut '{key}' overridden by later extension",
                        }
                    )
                result[normalized] = shortcut
        return result

    async def invoke_shortcut(self, shortcut: str) -> Any:
        """Invoke a registered shortcut with a fresh guarded context."""

        self.assert_active()
        item = self.get_shortcuts().get(str(shortcut).lower())
        if item is None:
            return None
        ctx = self.create_context()
        value = _call_with_signature(
            item.handler,
            {"ctx": ctx, "context": ctx},
            (ctx,),
        )
        return await _maybe_await(value)

    def request_shutdown(self) -> None:
        self._shutdown_requested = True
        callback = self.runtime._actions.get("shutdown")
        if callback is not None:
            try:
                _call_with_signature(callback, {}, ())
            except Exception as exc:
                self.record_error("shutdown", "<runtime>", exc)

    async def shutdown(
        self, reason: str = "quit", *, target_session_file: str | None = None
    ) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        try:
            if self.active and self.has_handlers("session_shutdown"):
                await self.emit(
                    {
                        "type": "session_shutdown",
                        "reason": reason,
                        "target_session_file": target_session_file,
                    }
                )
        finally:
            # Providers are owned by this generation. Remove them before
            # invalidating the runner so a reload/session shutdown cannot
            # leave stale models in the host registry.
            for extension in tuple(self.extensions):
                for provider in tuple(extension.providers.values()):
                    try:
                        self.runtime.unregister_provider(
                            provider.name, provider.extension_path
                        )
                    except Exception:
                        logger.exception(
                            "failed to unregister extension provider %s",
                            provider.name,
                        )
            # Session shutdown is a lifecycle fence. Cancellation or a host
            # failure cannot leave callable stale APIs or registry bindings.
            self.detach_tool_registry()
            self.detach_command_registry()
            self.runtime.cleanup_event_subscriptions()
            self.invalidate(f"{DEFAULT_STALE_MESSAGE} (reason={reason})")

    async def startup(self, reason: str = "startup") -> None:
        """Emit session_start once for this extension generation."""

        self.assert_active()
        if self._startup_emitted:
            return
        self._startup_emitted = True
        if self.has_handlers("session_start"):
            await self.emit({"type": "session_start", "reason": reason})

    async def reload(
        self, loader: Any, paths: Sequence[str], **kwargs: Any
    ) -> LoadExtensionsResult:
        """Close this generation and load a fresh one through ``loader``."""

        await self.shutdown("reload")
        clear = getattr(loader, "clear_cache", None)
        if clear is not None:
            clear()
        return await loader.load(paths, **kwargs)


__all__ = [
    "DEFAULT_STALE_MESSAGE",
    "ExtensionAPI",
    "ExtensionCommandContext",
    "ExtensionContext",
    "ExtensionEventBus",
    "ExtensionRunner",
    "ExtensionRuntime",
    "ExtensionToolAdapter",
]
