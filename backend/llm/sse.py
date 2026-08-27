"""Bounded parsing for provider Server-Sent Events streams."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any


_DEFAULT_MAX_LINE_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_EVENT_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_CONSECUTIVE_MALFORMED = 8
_DEFAULT_MAX_MALFORMED_BYTES = 256 * 1024
_MAX_CONFIGURED_BYTES = 64 * 1024 * 1024


def _bounded_env_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


class ProviderStreamLimitError(RuntimeError):
    """Raised when an untrusted provider stream exceeds a local safety bound."""

    provider_error_type = "protocol"
    error_type = "api"


class SSEMalformedBudget:
    """Bound malformed JSON tolerance without rejecting one isolated bad frame."""

    def __init__(self) -> None:
        self._max_consecutive = _bounded_env_int(
            "MINICODE_PROVIDER_SSE_MAX_CONSECUTIVE_MALFORMED",
            _DEFAULT_MAX_CONSECUTIVE_MALFORMED,
            minimum=1,
            maximum=1_000,
        )
        self._max_bytes = _bounded_env_int(
            "MINICODE_PROVIDER_SSE_MAX_MALFORMED_BYTES",
            _DEFAULT_MAX_MALFORMED_BYTES,
            minimum=1_024,
            maximum=_MAX_CONFIGURED_BYTES,
        )
        self._consecutive = 0
        self._bytes = 0

    def accept(self) -> None:
        self._consecutive = 0

    def reject(self, payload: str) -> None:
        self._consecutive += 1
        self._bytes += len(payload.encode("utf-8", errors="replace"))
        if (
            self._consecutive > self._max_consecutive
            or self._bytes > self._max_bytes
        ):
            raise ProviderStreamLimitError(
                "Provider SSE stream exceeded the malformed event budget"
            )


async def iter_sse_data(response: Any) -> AsyncIterator[str]:
    """Yield bounded SSE data payloads without buffering an unbounded line."""

    max_line_bytes = _bounded_env_int(
        "MINICODE_PROVIDER_SSE_MAX_LINE_BYTES",
        _DEFAULT_MAX_LINE_BYTES,
        minimum=1_024,
        maximum=_MAX_CONFIGURED_BYTES,
    )
    max_event_bytes = _bounded_env_int(
        "MINICODE_PROVIDER_SSE_MAX_EVENT_BYTES",
        _DEFAULT_MAX_EVENT_BYTES,
        minimum=1_024,
        maximum=_MAX_CONFIGURED_BYTES,
    )

    byte_iterator_factory = getattr(response, "aiter_bytes", None)
    if not callable(byte_iterator_factory):
        async for raw_line in response.aiter_lines():
            line_bytes = str(raw_line).encode("utf-8", errors="replace")
            if len(line_bytes) > max_line_bytes:
                raise ProviderStreamLimitError(
                    f"Provider SSE line exceeded {max_line_bytes} bytes"
                )
            line = str(raw_line).rstrip("\r\n")
            if not line or line.startswith(":") or line.startswith("event:"):
                continue
            if line.startswith("data:"):
                line = line[5:]
                if line.startswith(" "):
                    line = line[1:]
            payload_bytes = line.encode("utf-8", errors="replace")
            if len(payload_bytes) > max_event_bytes:
                raise ProviderStreamLimitError(
                    f"Provider SSE event exceeded {max_event_bytes} bytes"
                )
            if line:
                yield line
        return

    line_buffer = bytearray()
    event_parts: list[bytes] = []
    event_bytes = 0

    def consume_line(raw_line: bytes) -> str | None:
        nonlocal event_bytes
        line = raw_line[:-1] if raw_line.endswith(b"\r") else raw_line
        if len(line) > max_line_bytes:
            raise ProviderStreamLimitError(
                f"Provider SSE line exceeded {max_line_bytes} bytes"
            )
        if not line:
            if not event_parts:
                return None
            payload = b"\n".join(event_parts)
            event_parts.clear()
            event_bytes = 0
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProviderStreamLimitError(
                    "Provider SSE event was not valid UTF-8"
                ) from exc
        if line.startswith(b":") or line.startswith(b"event:"):
            return None
        if not line.startswith(b"data:"):
            return None
        data = line[5:]
        if data.startswith(b" "):
            data = data[1:]
        projected_size = event_bytes + len(data) + (1 if event_parts else 0)
        if projected_size > max_event_bytes:
            raise ProviderStreamLimitError(
                f"Provider SSE event exceeded {max_event_bytes} bytes"
            )
        event_parts.append(data)
        event_bytes = projected_size
        return None

    async for chunk in byte_iterator_factory():
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            chunk = bytes(chunk)
        line_buffer.extend(chunk)
        while True:
            newline = line_buffer.find(b"\n")
            if newline < 0:
                break
            raw_line = bytes(line_buffer[:newline])
            del line_buffer[: newline + 1]
            payload = consume_line(raw_line)
            if payload is not None:
                yield payload
        if len(line_buffer) > max_line_bytes:
            raise ProviderStreamLimitError(
                f"Provider SSE line exceeded {max_line_bytes} bytes"
            )

    if line_buffer:
        payload = consume_line(bytes(line_buffer))
        if payload is not None:
            yield payload
    if event_parts:
        payload = consume_line(b"")
        if payload is not None:
            yield payload
