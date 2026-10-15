"""Source's UTF-8 prefix/suffix evidence cap; authorization records stay whole."""
from __future__ import annotations


def source_token_estimate(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4


def truncate_guardian_text(text: str, max_tokens: int) -> str:
    raw = text.encode("utf-8")
    maximum = max_tokens * 4
    if len(raw) <= maximum:
        return text
    omitted = (len(raw) - maximum + 3) // 4
    marker = f'<truncated omitted_approx_tokens="{omitted}" />'
    available = maximum - len(marker.encode("utf-8"))
    if available <= 0:
        return marker
    prefix = raw[:available // 2].decode("utf-8", errors="ignore")
    suffix = raw[-(available - available // 2):].decode("utf-8", errors="ignore")
    return prefix + marker + suffix
