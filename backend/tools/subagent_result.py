from __future__ import annotations

from backend.tools.base import MAX_TOOL_RESULT_BYTES


PER_TASK_OUTPUT_CAP = MAX_TOOL_RESULT_BYTES


def _truncate_parallel_output(content: str, max_bytes: int) -> tuple[str, bool]:
    """Cap each parallel task's output.

    The byte ceiling aligns with the shared 2000-line/50-KiB tool-output
    boundary used by Pi's tool projections; applying it per task while results
    stream in parallel is MiniCode's own extension.
    """

    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content, False
    prefix = encoded[:max_bytes]
    while prefix:
        try:
            rendered = prefix.decode("utf-8")
            break
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    else:
        rendered = ""
    omitted = len(encoded) - len(prefix)
    return (
        f"{rendered}\n\n[Output truncated: {omitted} bytes omitted. "
        "Full output preserved in tool details.]",
        True,
    )


def _requested_cap(max_chars: int | None) -> int:
    if max_chars is None:
        return PER_TASK_OUTPUT_CAP
    try:
        requested = int(max_chars)
    except (TypeError, ValueError):
        return PER_TASK_OUTPUT_CAP
    return min(PER_TASK_OUTPUT_CAP, requested) if requested > 0 else PER_TASK_OUTPUT_CAP


def compact_subagent_result(content: str, *, max_chars: int | None = None) -> tuple[str, bool]:
    text = str(content or "").strip()
    if not text:
        return "", False
    return _truncate_parallel_output(text, _requested_cap(max_chars))


def full_subagent_result(content: str, *, max_chars: int | None = None) -> tuple[str, bool]:
    text = str(content or "").strip()
    return _truncate_parallel_output(text, _requested_cap(max_chars))


__all__ = ["PER_TASK_OUTPUT_CAP", "compact_subagent_result", "full_subagent_result"]
