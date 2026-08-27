"""Project MiniCode agent events into the executable-extension lifecycle.

MiniCode keeps a rich transport/UI event stream, so this observer projects
stable message and tool boundaries without making the extension runtime aware
of websocket details.  Tool authorization remains owned by the existing
``tool_call``/``tool_result`` hooks; this module only emits observational
``tool_execution_*`` lifecycle events.
"""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger(__name__)
_EXTENSION_CLONE_MAX_DEPTH = 64


def _now_ms() -> int:
    return int(time.time() * 1000)


def _text_content(value: Any) -> list[dict[str, str]]:
    text = str(value or "")
    return [{"type": "text", "text": text}] if text else []


def _extension_usage(value: Any) -> dict[str, Any]:
    """Return bounded usage for extension lifecycle messages."""

    source = value if isinstance(value, Mapping) else {}

    def nonnegative_number(raw: Any) -> int | float:
        if isinstance(raw, bool):
            return 0
        try:
            number = float(raw or 0)
        except (TypeError, ValueError, OverflowError):
            return 0
        if number != number or number in (float("inf"), float("-inf")):
            return 0
        if number.is_integer():
            return max(0, int(number))
        return max(0.0, number)

    def first(*keys: str) -> int | float:
        for key in keys:
            if key in source:
                return nonnegative_number(source.get(key))
        return 0

    raw_cost = source.get("cost")
    cost = raw_cost if isinstance(raw_cost, Mapping) else {}
    return {
        "input": first("input", "input_tokens"),
        "output": first("output", "output_tokens"),
        "cache_read": first("cache_read", "cache_read_input_tokens"),
        "cache_write": first("cache_write", "cache_creation_input_tokens"),
        "total_tokens": first("total_tokens", "total_tokens"),
        "cost": {
            "input": nonnegative_number(cost.get("input", source.get("cost_input", 0))),
            "output": nonnegative_number(cost.get("output", source.get("cost_output", 0))),
            "cache_read": nonnegative_number(
                cost.get("cache_read", source.get("cost_cache_read", 0))
            ),
            "cache_write": nonnegative_number(
                cost.get("cache_write", source.get("cost_cache_write", 0))
            ),
            "total": nonnegative_number(
                cost.get("total", source.get("cost_usd", 0))
            ),
        },
    }


def _image_content(value: Any) -> dict[str, Any] | None:
    """Normalize a MiniCode attachment descriptor for lifecycle messages."""

    if isinstance(value, Mapping):
        data = value.get("data", value.get("image_data", ""))
        mime = value.get("mime_type", value.get("media_type", "image/png"))
    else:
        data = getattr(value, "data", getattr(value, "image_data", ""))
        mime = getattr(value, "mime_type", getattr(value, "media_type", "image/png"))
    if not isinstance(data, str) or not data:
        return None
    return {"type": "image", "data": data, "mime_type": str(mime or "image/png")}


def _clone(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception as exc:
        # Extension handlers must never receive a mutable object owned by the
        # agent loop.  Some provider SDK objects cannot be deep-copied; fall
        # back to a recursively detached JSON-like projection instead of
        # returning the original reference (which defeats the isolation
        # boundary).
        logger.debug("Deep-copying extension payload failed: %s", exc)
        return _clone_fallback(value, seen=set(), depth=0)


def _clone_fallback(value: Any, *, seen: set[int], depth: int) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, bytes)):
        return value
    if depth >= _EXTENSION_CLONE_MAX_DEPTH:
        return None
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return None
        seen.add(identity)
        try:
            result: dict[Any, Any] = {}
            for key, item in value.items():
                cloned_key = _clone_fallback(key, seen=seen, depth=depth + 1)
                try:
                    hash(cloned_key)
                except Exception:
                    try:
                        cloned_key = str(key)
                    except Exception:
                        continue
                result[cloned_key] = _clone_fallback(
                    item,
                    seen=seen,
                    depth=depth + 1,
                )
            return result
        finally:
            seen.discard(identity)
    if isinstance(value, (list, tuple, set, frozenset)):
        identity = id(value)
        if identity in seen:
            return None
        seen.add(identity)
        try:
            items = [
                _clone_fallback(item, seen=seen, depth=depth + 1)
                for item in value
            ]
            return (
                tuple(items)
                if isinstance(value, (tuple, set, frozenset))
                else items
            )
        finally:
            seen.discard(identity)
    try:
        return repr(value)
    except Exception:
        return None


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        return "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, Mapping) and item.get("type") == "text"
        )
    return ""


def _extension_stop_reason(
    *,
    status: str = "completed",
    finish_reason: str = "",
    has_tools: bool = False,
) -> str:
    normalized_status = str(status or "completed").strip().lower()
    normalized_finish = str(finish_reason or "").strip()
    if normalized_status == "failed" or normalized_finish in {"error", "failed"}:
        return "error"
    if normalized_status == "cancelled" or normalized_finish in {
        "aborted",
        "cancelled",
        "interrupted",
        "user_interrupted",
    }:
        return "aborted"
    if normalized_finish in {
        "length",
        "max_tokens",
        "max_output_tokens",
        "max_turn_tokens",
        "budget_exceeded",
    }:
        return "length"
    if has_tools or normalized_finish in {"tool_use", "tool_calls"}:
        return "tool_use"
    if normalized_status == "partial":
        return "aborted"
    return "stop"


@dataclass(slots=True)
class ExtensionLifecycleObserver:
    """Project one MiniCode query into its extension lifecycle vocabulary."""

    runner: Any | None
    user_message: str
    metadata: Mapping[str, Any] | None = None
    images: Sequence[Any] | None = None
    _started: bool = False
    _finished: bool = False
    _turn_index: int = 0
    _messages: list[dict[str, Any]] = field(default_factory=list)
    _current_assistant: dict[str, Any] | None = None
    _assistant_item_started: bool = False
    _assistant_message_ended: bool = False
    _pending_message_end: dict[str, Any] | None = None
    _pending_tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    _announced_pending_tool_calls: set[str] = field(default_factory=set)
    _started_tool_calls: set[str] = field(default_factory=set)
    _finished_tool_calls: set[str] = field(default_factory=set)
    _tool_results: list[dict[str, Any]] = field(default_factory=list)
    _turn_had_tool: bool = False
    _assistant_text: str = ""
    _thinking_blocks: dict[int, dict[str, Any]] = field(default_factory=dict)
    _thinking_block_order: dict[int, int] = field(default_factory=dict)
    _assistant_tool_blocks: list[dict[str, Any]] = field(default_factory=list)
    _tool_block_indices: dict[str, int] = field(default_factory=dict)
    _next_content_order: int = 0
    _last_done_data: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return bool(
            self.runner is not None
            and bool(getattr(self.runner, "active", True))
            and not self._finished
        )

    async def _emit(self, event: Mapping[str, Any]) -> list[Any]:
        if not self.active:
            return []
        emit = getattr(self.runner, "emit", None)
        if not callable(emit):
            return []
        try:
            result = await emit(dict(event))
        except Exception as exc:
            # Extension callbacks are isolated by ExtensionRunner.  A stale
            # generation or a host adapter failure must not stop the turn.
            logger.debug("Extension lifecycle event failed: %s", exc, exc_info=True)
            return []
        # The native runner returns an array.  Treating an accidental string as
        # a generic Sequence would expose one-character handler results and
        # make lifecycle aggregation silently corrupt the message boundary.
        return list(result or []) if isinstance(result, (list, tuple)) else []

    def _mutable_metadata(self) -> dict[str, Any]:
        current = self.metadata
        if isinstance(current, dict):
            return current
        copied = dict(current or {})
        self.metadata = copied
        return copied

    def _runtime_value(self, *keys: str, default: str = "") -> str:
        metadata = self.metadata if isinstance(self.metadata, Mapping) else {}
        runtime = metadata.get("_subagent_parent_runtime")
        runtime_mapping = runtime if isinstance(runtime, Mapping) else {}
        for key in keys:
            value = metadata.get(key)
            if value is None or value == "":
                value = runtime_mapping.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return default

    def _assistant_defaults(self, item: Mapping[str, Any] | None = None) -> dict[str, Any]:
        source = item if isinstance(item, Mapping) else {}
        api = str(source.get("api") or "").strip() or self._runtime_value(
            "api", "wire_api", default="minicode"
        )
        provider = str(source.get("provider") or "").strip() or self._runtime_value(
            "provider", "provider_id", default="minicode"
        )
        model = str(source.get("model") or "").strip() or self._runtime_value(
            "model", "model_id", default="unknown"
        )
        usage = source.get("usage")
        if not isinstance(usage, Mapping):
            usage = self._last_done_data.get("usage")
        return {
            "api": api,
            "provider": provider,
            "model": model,
            "usage": _extension_usage(usage),
        }

    def _rebuild_assistant_content(self) -> None:
        if self._current_assistant is None:
            return
        blocks: list[tuple[int, int, dict[str, Any]]] = []
        if self._assistant_text:
            blocks.append((0, -1, {"type": "text", "text": self._assistant_text}))
        for index, block in self._thinking_blocks.items():
            blocks.append(
                (
                    index,
                    self._thinking_block_order.get(index, index),
                    block,
                )
            )
        for position, block in enumerate(self._assistant_tool_blocks):
            tool_id = str(block.get("id") or "")
            blocks.append(
                (
                    self._tool_block_indices.get(tool_id, position + 1),
                    position,
                    block,
                )
            )
        blocks.sort(key=lambda item: (item[0], item[1]))
        self._current_assistant["content"] = [block for _, _, block in blocks]
        if self._pending_message_end is not None:
            self._pending_message_end = self._current_assistant

    def _reset_assistant_content_state(self) -> None:
        self._assistant_text = ""
        self._thinking_blocks.clear()
        self._thinking_block_order.clear()
        self._assistant_tool_blocks.clear()
        self._tool_block_indices.clear()
        self._next_content_order = 0

    def _sync_assistant_content_state(self, item: Mapping[str, Any]) -> None:
        self._reset_assistant_content_state()
        raw_text = item.get("text")
        if not isinstance(raw_text, str):
            raw_text = _message_text(item)
        self._assistant_text = raw_text
        raw_content = item.get("content")
        if isinstance(raw_content, Sequence) and not isinstance(
            raw_content, (str, bytes, bytearray)
        ):
            for index, raw_block in enumerate(raw_content):
                if not isinstance(raw_block, Mapping):
                    continue
                block_type = str(raw_block.get("type") or "")
                if block_type == "thinking":
                    thinking = str(
                        raw_block.get("thinking")
                        or raw_block.get("text")
                        or raw_block.get("content")
                        or ""
                    )
                    self._thinking_blocks[index] = {
                        "type": "thinking",
                        "thinking": thinking,
                    }
                    self._thinking_block_order[index] = self._next_content_order
                    self._next_content_order += 1
        for position, block in enumerate(self._tool_blocks(item)):
            tool_id = str(block.get("id") or "")
            if not tool_id:
                continue
            self._assistant_tool_blocks.append(block)
            self._tool_block_indices[tool_id] = position + (1 if self._assistant_text else 0)
        self._rebuild_assistant_content()

    def _record_thinking_event(self, data: Mapping[str, Any]) -> dict[str, Any]:
        raw_index = data.get("content_index", data.get("content_index", 0))
        if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
            index = 0
        else:
            index = raw_index
        block = self._thinking_blocks.setdefault(
            index,
            {"type": "thinking", "thinking": ""},
        )
        if index not in self._thinking_block_order:
            self._thinking_block_order[index] = self._next_content_order
            self._next_content_order += 1
        lifecycle = str(data.get("lifecycle") or "delta").strip().lower()
        if lifecycle == "end" and isinstance(data.get("content"), str):
            terminal_content = str(data.get("content") or "")
            current = str(block.get("thinking") or "")
            # Providers differ: some end frames carry the complete block,
            # while MiniCode's normalized end frame is empty or carries only
            # the final fragment. Preserve both forms without duplicating a
            # full repeated payload.
            if terminal_content and not terminal_content.startswith(current):
                block["thinking"] = current + terminal_content
            elif terminal_content:
                block["thinking"] = terminal_content
        elif lifecycle != "start":
            block["thinking"] = str(block.get("thinking") or "") + str(
                data.get("content") or data.get("delta") or ""
            )
        self._rebuild_assistant_content()
        return {
            "type": "thinking_delta",
            "content_index": index,
            "delta": str(data.get("content") or data.get("delta") or ""),
        }

    def _register_tool_block(
        self, block: Mapping[str, Any], *, content_index: int | None = None
    ) -> None:
        tool_id = str(block.get("id") or "").strip()
        if not tool_id:
            return
        for existing in self._assistant_tool_blocks:
            if str(existing.get("id") or "") == tool_id:
                existing.update(_clone(dict(block)))
                if content_index is not None:
                    self._tool_block_indices[tool_id] = content_index
                self._rebuild_assistant_content()
                return
        self._assistant_tool_blocks.append(_clone(dict(block)))
        self._tool_block_indices[tool_id] = (
            content_index
            if isinstance(content_index, int) and content_index >= 0
            else len(self._assistant_tool_blocks) + (1 if self._assistant_text else 0) - 1
        )
        self._rebuild_assistant_content()

    async def start(self) -> None:
        if self._started or self.runner is None:
            self._started = True
            return
        self._started = True
        metadata = self._mutable_metadata()
        before_agent_start: dict[str, Any] = {
            "type": "before_agent_start",
            "prompt": str(self.user_message or ""),
            "system_prompt": str(metadata.get("system_prompt") or ""),
            "system_prompt_options": _clone(
                metadata.get("system_prompt_options")
                if isinstance(metadata.get("system_prompt_options"), Mapping)
                else {}
            ),
        }
        if self.images:
            before_agent_start["images"] = _clone(list(self.images))
        # Fold before_agent_start handler results instead of exposing the
        # raw per-handler return list. Prefer the runner's explicit aggregate
        # method and retain a generic-event fallback for lightweight hosts.
        before_handler = getattr(self.runner, "emit_before_agent_start", None)
        before_result: Any = None
        if callable(before_handler):
            try:
                before_result = await before_handler(
                    str(self.user_message or ""),
                    _clone(list(self.images)) if self.images else None,
                    str(metadata.get("system_prompt") or ""),
                    (
                        dict(metadata.get("system_prompt_options"))
                        if isinstance(metadata.get("system_prompt_options"), Mapping)
                        else {}
                    ),
                )
            except Exception as exc:
                logger.debug(
                    "before_agent_start aggregation failed: %s",
                    exc,
                    exc_info=True,
                )
        else:
            results = await self._emit(before_agent_start)
            for value in results:
                if isinstance(value, Mapping):
                    before_result = value
        if isinstance(before_result, Mapping):
            # Keep this turn-local result in the metadata shared with the loop
            # bootstrap. The context builder applies it exactly once before
            # the first provider request.
            if before_result.get("system_prompt") is not None:
                metadata["_extension_system_prompt"] = str(
                    before_result.get("system_prompt") or ""
                )
            elif before_result.get("system_prompt") is not None:
                metadata["_extension_system_prompt"] = str(
                    before_result.get("system_prompt") or ""
                )
            raw_messages = before_result.get("messages")
            if isinstance(raw_messages, Mapping):
                raw_messages = [raw_messages]
            elif before_result.get("message") is not None and not raw_messages:
                raw_messages = [before_result.get("message")]
            if isinstance(raw_messages, (list, tuple)) and raw_messages:
                metadata["_extension_before_agent_messages"] = _clone(
                    list(raw_messages)
                )
        await self._emit({"type": "agent_start"})
        await self._start_turn()
        user = {
            "role": "user",
            "content": _text_content(self.user_message)
            + [
                image
                for raw_image in (self.images or ())
                if (image := _image_content(raw_image)) is not None
            ],
            "timestamp": _now_ms(),
        }
        self._messages.append(user)
        await self._emit({"type": "message_start", "message": _clone(user)})
        await self._emit({"type": "message_end", "message": _clone(user)})

    async def _start_turn(self) -> None:
        await self._emit(
            {
                "type": "turn_start",
                "turn_index": self._turn_index,
                "timestamp": _now_ms(),
            }
        )

    def _assistant_message(self, item: Mapping[str, Any]) -> dict[str, Any]:
        text = item.get("text")
        if not isinstance(text, str):
            text = _message_text(item)
        tool_blocks = ExtensionLifecycleObserver._tool_blocks(item)
        defaults = self._assistant_defaults(item)
        message: dict[str, Any] = {
            "role": "assistant",
            "content": _text_content(text) + tool_blocks,
            "api": defaults["api"],
            "provider": defaults["provider"],
            "model": defaults["model"],
            "usage": defaults["usage"],
            # Partial assistant messages still carry a valid
            # StopReason placeholder; extensions commonly inspect the field
            # before message_end.
            "stop_reason": "stop",
            "timestamp": _now_ms(),
        }
        raw_content = item.get("content")
        if isinstance(raw_content, Sequence) and not isinstance(
            raw_content, (str, bytes, bytearray)
        ):
            native_blocks = [
                _clone(block)
                for block in raw_content
                if isinstance(block, Mapping)
                and str(block.get("type") or "") in {"text", "thinking", "tool_call"}
            ]
            if native_blocks:
                message["content"] = native_blocks
        status = str(item.get("status") or "").strip().lower()
        if status in {"completed", "partial", "cancelled", "failed"}:
            message["stop_reason"] = _extension_stop_reason(
                status=status,
                finish_reason=str(item.get("finish_reason") or ""),
                has_tools=bool(tool_blocks),
            )
        return message

    def _tool_content_index(self, tool_id: str) -> int:
        content = (
            self._current_assistant.get("content", [])
            if isinstance(self._current_assistant, Mapping)
            else []
        )
        if isinstance(content, Sequence) and not isinstance(
            content, (str, bytes, bytearray)
        ):
            for index, block in enumerate(content):
                if (
                    isinstance(block, Mapping)
                    and block.get("type") == "tool_call"
                    and str(block.get("id") or "") == tool_id
                ):
                    return index
            base_index = len(content)
        else:
            base_index = 0
        pending_ids = list(self._pending_tool_calls)
        try:
            return base_index + pending_ids.index(tool_id)
        except ValueError:
            return base_index

    @staticmethod
    def _tool_blocks(
        item: Mapping[str, Any], raw_calls_override: Any = None
    ) -> list[dict[str, Any]]:
        raw_calls = (
            raw_calls_override
            if raw_calls_override is not None
            else item.get("tool_calls")
        )
        if not isinstance(raw_calls, Sequence) or isinstance(
            raw_calls, (str, bytes, bytearray)
        ):
            raw_content = item.get("content")
            raw_calls = (
                raw_content
                if isinstance(raw_content, Sequence)
                and not isinstance(raw_content, (str, bytes, bytearray))
                else ()
            )
        blocks: list[dict[str, Any]] = []
        for raw in raw_calls:
            if not isinstance(raw, Mapping):
                continue
            block_type = str(raw.get("type") or "tool_call")
            if block_type != "tool_call":
                continue
            tool_id = str(raw.get("id") or raw.get("tool_call_id") or "").strip()
            if not tool_id:
                continue
            blocks.append(
                {
                    "type": "tool_call",
                    "id": tool_id,
                    "name": str(raw.get("name") or raw.get("tool_name") or ""),
                    "arguments": _clone(
                        raw.get("arguments", raw.get("args", raw.get("input", {})))
                    ),
                }
            )
        return blocks

    async def _flush_pending_message_end(self) -> None:
        message = self._pending_message_end
        if message is None:
            return
        self._pending_message_end = None
        results = await self._emit(
            {"type": "message_end", "message": _clone(message)}
        )
        # message_end handlers may replace the finalized message.
        # Keep that replacement in the bridge's history for the later
        # turn_end/agent_end payloads while preserving the original role.
        replacement: Mapping[str, Any] | None = None
        for value in results:
            candidate = value.get("message") if isinstance(value, Mapping) else value
            if isinstance(candidate, Mapping):
                replacement = candidate
        if replacement is not None and replacement.get("role") == message.get("role"):
            message = dict(replacement)
            if message.get("role") == "assistant":
                defaults = self._assistant_defaults(message)
                for key, value in defaults.items():
                    if key not in message:
                        message[key] = value
                message.setdefault("stop_reason", "stop")
        self._current_assistant = message
        self._messages.append(message)
        self._assistant_message_ended = True

    async def _end_turn(self) -> None:
        await self._flush_pending_message_end()
        message = self._current_assistant
        if message is None:
            if self._tool_results:
                message = {
                    "role": "assistant",
                    "content": [],
                    **self._assistant_defaults(),
                    "timestamp": _now_ms(),
                    "stop_reason": "stop",
                }
            else:
                return
        await self._emit(
            {
                "type": "turn_end",
                "turn_index": self._turn_index,
                "message": _clone(message),
                "tool_results": _clone(self._tool_results),
            }
        )
        self._turn_index += 1
        self._current_assistant = None
        self._reset_assistant_content_state()
        self._assistant_item_started = False
        self._assistant_message_ended = False
        self._tool_results.clear()
        self._turn_had_tool = False
        self._pending_tool_calls.clear()
        self._announced_pending_tool_calls.clear()
        self._started_tool_calls.clear()
        self._finished_tool_calls.clear()

    async def _begin_assistant_message(
        self, item: Mapping[str, Any], *, placeholder: bool = False
    ) -> None:
        if self._current_assistant is not None and self._assistant_message_ended:
            await self._end_turn()
            await self._start_turn()
        elif self._current_assistant is not None:
            if placeholder or not self._assistant_item_started:
                self._update_assistant_text(str(item.get("text") or ""))
                if not placeholder:
                    self._assistant_item_started = True
                return
            await self._flush_pending_message_end()
        if self._current_assistant is not None:
            await self._end_turn()
            await self._start_turn()
        self._current_assistant = self._assistant_message(item)
        self._sync_assistant_content_state(item)
        self._assistant_item_started = not placeholder
        self._assistant_message_ended = False
        self._pending_message_end = self._current_assistant
        await self._emit(
            {"type": "message_start", "message": _clone(self._current_assistant)}
        )

    def _update_assistant_text(self, text: str) -> None:
        if self._current_assistant is None:
            return
        self._assistant_text = str(text or "")
        self._rebuild_assistant_content()

    async def _message_update(self, event: Mapping[str, Any]) -> None:
        delta = str(event.get("delta") or "")
        if self._current_assistant is None:
            await self._begin_assistant_message({}, placeholder=True)
        current = _message_text(self._current_assistant or {})
        self._update_assistant_text(current + delta)
        assistant_event = {
            "type": "text_delta",
            "delta": delta,
        }
        await self._emit(
            {
                "type": "message_update",
                "message": _clone(self._current_assistant or {}),
                "assistant_message_event": assistant_event,
            }
        )

    async def _assistant_update(self, assistant_event: Mapping[str, Any]) -> None:
        if self._current_assistant is None:
            await self._begin_assistant_message({}, placeholder=True)
        await self._emit(
            {
                "type": "message_update",
                "message": _clone(self._current_assistant or {}),
                "assistant_message_event": _clone(dict(assistant_event)),
            }
        )

    async def _start_tool(self, tool_id: str, tool_name: str, args: Any) -> None:
        if not tool_id or tool_id in self._started_tool_calls:
            return
        self._started_tool_calls.add(tool_id)
        self._pending_tool_calls[tool_id] = {
            "id": tool_id,
            "name": tool_name,
            "args": _clone(args if isinstance(args, Mapping) else {}),
        }
        self._turn_had_tool = True
        await self._emit(
            {
                "type": "tool_execution_start",
                "tool_call_id": tool_id,
                "tool_name": tool_name,
                "args": _clone(args if isinstance(args, Mapping) else {}),
            }
        )

    async def _finalize_tool_only_assistant_message(
        self,
        *,
        fallback_id: str,
        fallback_name: str,
        fallback_args: Any,
    ) -> None:
        """Finalize the single assistant message before tool execution.

        MiniCode intentionally does not expose a tool-only provider response
        as a visible ``item.completed`` answer. The provider projection sends
        the complete batch through the existing hidden stream-event envelope,
        so the observer can reconstruct assistant content without creating
        a fake user-visible answer item.
        """

        if self._current_assistant is None:
            await self._begin_assistant_message({}, placeholder=True)
        calls = list(self._pending_tool_calls.values())
        if not any(str(call.get("id") or "") == fallback_id for call in calls):
            calls.append(
                {
                    "id": fallback_id,
                    "name": fallback_name,
                    "args": _clone(
                        fallback_args if isinstance(fallback_args, Mapping) else {}
                    ),
                }
            )

        existing_ids = {
            str(block.get("id") or "")
            for block in self._assistant_tool_blocks
            if isinstance(block, Mapping)
        }
        for call in calls:
            call_id = str(call.get("id") or "").strip()
            if not call_id or call_id in existing_ids:
                continue
            call_name = str(call.get("name") or "").strip()
            call_args = call.get("args", {})
            self._register_tool_block(
                {
                    "type": "tool_call",
                    "id": call_id,
                    "name": call_name,
                    "arguments": _clone(
                        call_args if isinstance(call_args, Mapping) else {}
                    ),
                }
            )
            existing_ids.add(call_id)
            await self._assistant_update(
                {
                    "type": "toolcall_end",
                    "content_index": self._tool_content_index(call_id),
                }
            )
        self._current_assistant["stop_reason"] = "tool_use"
        self._pending_message_end = self._current_assistant
        await self._flush_pending_message_end()

    async def _tool_only_assistant(self, event: Mapping[str, Any]) -> None:
        calls = self._tool_blocks({"tool_calls": event.get("tool_calls")})
        if not calls:
            return
        for block in calls:
            self._pending_tool_calls[block["id"]] = {
                "id": block["id"],
                "name": block["name"],
                "args": _clone(block.get("arguments") or {}),
            }
            if block["id"] not in self._announced_pending_tool_calls:
                self._announced_pending_tool_calls.add(block["id"])
                await self._assistant_update(
                    {
                        "type": "toolcall_start",
                        "content_index": self._tool_content_index(block["id"]),
                        "id": block["id"],
                        "tool_name": block["name"],
                    }
                )
        first = calls[0]
        await self._finalize_tool_only_assistant_message(
            fallback_id=first["id"],
            fallback_name=first["name"],
            fallback_args=first.get("arguments") or {},
        )

    async def _tool_call(self, event: Mapping[str, Any]) -> None:
        tool_id = str(event.get("id") or event.get("tool_call_id") or "").strip()
        tool_name = str(event.get("name") or event.get("tool_name") or "").strip()
        args = event.get("args", event.get("input", {}))
        if not tool_id:
            return
        if str(event.get("status") or "running").lower() == "pending":
            previous = self._pending_tool_calls.get(tool_id)
            self._pending_tool_calls[tool_id] = {
                "id": tool_id,
                "name": tool_name,
                "args": _clone(args if isinstance(args, Mapping) else {}),
            }
            if tool_id not in self._announced_pending_tool_calls:
                self._announced_pending_tool_calls.add(tool_id)
                await self._assistant_update(
                    {
                        "type": "toolcall_start",
                        "content_index": self._tool_content_index(tool_id),
                        "id": tool_id,
                        "tool_name": tool_name,
                    }
                )
            elif previous is not None and previous.get("args") != args:
                await self._assistant_update(
                    {
                        "type": "toolcall_delta",
                        "content_index": self._tool_content_index(tool_id),
                        "delta": _clone(args),
                    }
                )
            return
        if self._current_assistant is None:
            await self._begin_assistant_message({}, placeholder=True)
        if self._assistant_message_ended:
            # The complete assistant item already carried all tool_call blocks
            # and emitted its single message_end. Tool execution starts
            # after that boundary; never reopen or re-end the message when a
            # parallel/sequential batch yields its remaining calls.
            await self._start_tool(tool_id, tool_name, args)
            return
        if tool_id not in self._announced_pending_tool_calls:
            self._announced_pending_tool_calls.add(tool_id)
            await self._assistant_update(
                {
                    "type": "toolcall_start",
                    "content_index": self._tool_content_index(tool_id),
                    "id": tool_id,
                    "tool_name": tool_name,
                }
            )
        await self._finalize_tool_only_assistant_message(
            fallback_id=tool_id,
            fallback_name=tool_name,
            fallback_args=args,
        )
        await self._start_tool(tool_id, tool_name, args)

    async def _tool_update(self, event: Mapping[str, Any]) -> None:
        tool_id = str(event.get("id") or event.get("tool_call_id") or "").strip()
        if not tool_id:
            return
        call = self._pending_tool_calls.get(tool_id, {})
        tool_name = str(event.get("tool_name") or call.get("name") or "").strip()
        args = event.get("args", call.get("args", {}))
        output = event.get("output", event.get("partial_result", ""))
        partial_result = (
            output
            if isinstance(output, Mapping)
            else {"content": _text_content(output), "is_error": False}
        )
        await self._emit(
            {
                "type": "tool_execution_update",
                "tool_call_id": tool_id,
                "tool_name": tool_name,
                "args": _clone(args if isinstance(args, Mapping) else {}),
                "partial_result": _clone(partial_result),
            }
        )

    async def _tool_result(self, event: Mapping[str, Any]) -> None:
        tool_id = str(event.get("id") or event.get("tool_call_id") or "").strip()
        if not tool_id or tool_id in self._finished_tool_calls:
            return
        call = self._pending_tool_calls.get(tool_id, {})
        tool_name = str(
            event.get("name") or event.get("tool_name") or call.get("name") or ""
        ).strip()
        args = call.get("args", {})
        if tool_id not in self._started_tool_calls:
            await self._flush_pending_message_end()
            await self._start_tool(tool_id, tool_name, args)
        is_error = bool(event.get("is_error", event.get("is_error", False)))
        summary = event.get("summary")
        if summary is None:
            summary = event.get("content_preview", event.get("content", ""))
        result = {
            "content": _text_content(summary),
            "details": _clone(event.get("details") or {}),
            "is_error": is_error,
        }
        await self._emit(
            {
                "type": "tool_execution_end",
                "tool_call_id": tool_id,
                "tool_name": tool_name,
                "result": _clone(result),
                "is_error": is_error,
            }
        )
        tool_message = {
            "role": "tool_result",
            "tool_call_id": tool_id,
            "tool_name": tool_name,
            "content": _text_content(summary),
            "details": _clone(event.get("details") or {}),
            "is_error": is_error,
            "timestamp": _now_ms(),
        }
        await self._emit({"type": "message_start", "message": _clone(tool_message)})
        await self._emit({"type": "message_end", "message": _clone(tool_message)})
        self._messages.append(tool_message)
        self._tool_results.append(tool_message)
        self._finished_tool_calls.add(tool_id)

    async def observe(self, event: Any) -> None:
        if not self.active:
            return
        event_type = str(getattr(event, "type", "") or "")
        data = getattr(event, "data", {})
        if not isinstance(data, Mapping):
            data = {}
        if event_type == "item.started":
            item = data.get("item")
            if isinstance(item, Mapping) and item.get("type") == "agent_message":
                await self._begin_assistant_message(item)
        elif event_type == "agent_message.delta":
            await self._message_update(data)
        elif event_type == "thinking_delta":
            assistant_event = self._record_thinking_event(data)
            await self._assistant_update(
                assistant_event
            )
        elif event_type == "item.completed":
            item = data.get("item")
            if isinstance(item, Mapping) and item.get("type") == "agent_message":
                if self._current_assistant is None:
                    await self._begin_assistant_message(item)
                self._update_assistant_text(str(item.get("text") or ""))
                if self._current_assistant is not None:
                    tool_blocks = self._tool_blocks(item, data.get("tool_calls"))
                    if tool_blocks:
                        for block in tool_blocks:
                            self._register_tool_block(block)
                            self._announced_pending_tool_calls.add(block["id"])
                    status = str(item.get("status") or "completed").lower()
                    self._current_assistant["stop_reason"] = _extension_stop_reason(
                        status=status,
                        finish_reason=str(data.get("finish_reason") or ""),
                        has_tools=bool(tool_blocks),
                    )
                    self._pending_message_end = self._current_assistant
                    if tool_blocks:
                        for block in tool_blocks:
                            await self._assistant_update(
                                {
                                    "type": "toolcall_end",
                                    "content_index": self._tool_content_index(
                                        str(block.get("id") or "")
                                    ),
                                    "id": block["id"],
                                }
                            )
                    await self._assistant_update(
                        {
                            "type": "done",
                            "reason": self._current_assistant["stop_reason"],
                        }
                    )
                    if tool_blocks:
                        await self._flush_pending_message_end()
        elif event_type == "done":
            self._last_done_data = _clone(dict(data))
            if self._current_assistant is not None:
                usage = data.get("usage")
                if isinstance(usage, Mapping):
                    self._current_assistant["usage"] = _extension_usage(usage)
                provider_raw = data.get("provider_raw", data.get("provider_raw"))
                if isinstance(provider_raw, Mapping):
                    request_summary = provider_raw.get("request_summary")
                    if isinstance(request_summary, Mapping):
                        for key, aliases in (
                            ("api", ("wire_api", "api")),
                            ("provider", ("provider", "provider_id")),
                            ("model", ("model", "model_id", "response_model")),
                        ):
                            if not self._current_assistant.get(key):
                                for alias in aliases:
                                    candidate = str(request_summary.get(alias) or "").strip()
                                    if candidate:
                                        self._current_assistant[key] = candidate
                                        break
                reason = str(data.get("reason") or "").strip()
                status = str(data.get("status") or "completed").strip().lower()
                self._current_assistant["stop_reason"] = _extension_stop_reason(
                    status=status,
                    finish_reason=reason,
                    has_tools=any(
                        isinstance(block, Mapping)
                        and block.get("type") == "tool_call"
                        for block in self._current_assistant.get("content", [])
                    ),
                )
                self._pending_message_end = self._current_assistant
        elif event_type == "stream_event":
            if str(data.get("provider") or "") == "agent-loop":
                payload = data.get("data")
                if (
                    str(data.get("event_type") or "") == "tool_only_assistant"
                    and isinstance(payload, Mapping)
                ):
                    await self._tool_only_assistant(payload)
        elif event_type == "tool_call":
            await self._tool_call(data)
        elif event_type == "tool_output_delta":
            await self._tool_update(data)
        elif event_type == "tool_result":
            await self._tool_result(data)

    async def finish(self, *, status: str = "completed", reason: str = "") -> None:
        if self._finished:
            return
        if self.runner is None or not self._started:
            self._finished = True
            return
        try:
            if self._current_assistant is not None and status != "completed":
                current_content = self._current_assistant.get("content", [])
                has_tools = bool(
                    isinstance(current_content, Sequence)
                    and not isinstance(current_content, (str, bytes, bytearray))
                    and any(
                        isinstance(block, Mapping)
                        and block.get("type") == "tool_call"
                        for block in current_content
                    )
                )
                if not has_tools or not self._assistant_message_ended:
                    self._current_assistant["stop_reason"] = _extension_stop_reason(
                        status=status,
                        finish_reason=reason,
                        has_tools=has_tools,
                    )
                    if self._pending_message_end is not None:
                        self._pending_message_end = self._current_assistant
            if self._current_assistant is not None and not self._current_assistant.get(
                "stop_reason"
            ):
                self._current_assistant["stop_reason"] = _extension_stop_reason(
                    status=status,
                    finish_reason=reason,
                    has_tools=any(
                        isinstance(block, Mapping)
                        and block.get("type") == "tool_call"
                        for block in self._current_assistant.get("content", [])
                    ),
                )
                self._pending_message_end = self._current_assistant
            await self._flush_pending_message_end()
            if self._current_assistant is None and status not in {"completed"}:
                failure_text = str(reason or status or "Agent run stopped")
                failure = {
                    "role": "assistant",
                    "content": _text_content(failure_text),
                    "timestamp": _now_ms(),
                    "stop_reason": _extension_stop_reason(
                        status=status,
                        finish_reason=reason,
                    ),
                }
                self._current_assistant = failure
                await self._emit(
                    {"type": "message_start", "message": _clone(failure)}
                )
                self._pending_message_end = failure
                await self._flush_pending_message_end()
            await self._end_turn()
            await self._emit({"type": "agent_end", "messages": _clone(self._messages)})
            await self._emit({"type": "agent_settled"})
        finally:
            self._finished = True


def lifecycle_observer_factory(
    *,
    runner: Any | None,
    user_message: str,
    metadata: Mapping[str, Any] | None = None,
    images: Sequence[Any] | None = None,
) -> ExtensionLifecycleObserver:
    """Create the MiniCode extension observer for one agent query."""
    return ExtensionLifecycleObserver(
        runner=runner,
        user_message=user_message,
        metadata=metadata,
        images=images,
    )


__all__ = ["ExtensionLifecycleObserver", "lifecycle_observer_factory"]
