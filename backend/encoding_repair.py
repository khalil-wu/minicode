from __future__ import annotations

import re
from typing import Any


_GBK_MOJIBAKE_MARKERS = (
    0x30BD,  # katakana SO, common in mojibaked "hao"
    0x5553,
    0x5B2A,
    0x6D63,
    0x72B2,
    0x7487,
    0x8133,
    0x9225,
    0x935B,
    0x93B5,
    0x93C1,
    0x93C9,
)
_CP1252_MOJIBAKE_MARKERS = (
    0x00C2,
    0x00C3,
    0x00C4,
    0x00C5,
    0x00C6,
    0x00C8,
    0x00E2,
    0x00E4,
    0x00E5,
    0x00E6,
    0x00E8,
    0x02C6,
    0x02DC,
    0x201A,
    0x201E,
    0x2020,
    0x2022,
    0x2030,
    0x2122,
)
_ALL_MARKERS = tuple(chr(item) for item in (*_GBK_MOJIBAKE_MARKERS, *_CP1252_MOJIBAKE_MARKERS))
_SUSPICIOUS_SEQUENCES = (
    "\u6d63\u72b2",
    "\u93b5\u5b2a",
    "\u93c1\u677f",
    "\u7487\u55d7",
    "\u00e4\u00bd",
    "\u00e5\u00a5",
    "\u00e6\u2030",
    "\u00e8\u00af",
)


def _mojibake_score(value: str) -> int:
    score = 0
    for char in value:
        codepoint = ord(char)
        if char in _ALL_MARKERS:
            score += 4
        elif char == "\ufffd":
            score += 8
        elif 0x00C0 <= codepoint <= 0x017F:
            score += 1
        elif codepoint in {0x02C6, 0x02DC, 0x201A, 0x201E, 0x2020, 0x2022, 0x2030, 0x2122}:
            score += 1
    for sequence in _SUSPICIOUS_SEQUENCES:
        if sequence in value:
            score += 8
    return score


def _repair_whole_text(value: str, original_score: int | None = None) -> str:
    score = _mojibake_score(value) if original_score is None else original_score
    if score <= 0:
        return value

    best = value
    best_score = score
    for encoding in ("gbk", "cp1252", "latin1"):
        try:
            candidate = value.encode(encoding).decode("utf-8")
        except UnicodeError:
            continue
        if not candidate or "\x00" in candidate:
            continue
        candidate_score = _mojibake_score(candidate)
        if candidate_score < best_score:
            best = candidate
            best_score = candidate_score

    return best if best_score + 1 < score else value


def repair_mojibake_text(value: str) -> str:
    """Repair common legacy UTF-8-as-GBK/cp1252 mojibake at persistence boundaries."""
    if not value:
        return value
    original_score = _mojibake_score(value)
    if original_score <= 0:
        return value

    repaired = _repair_whole_text(value, original_score)
    if repaired != value:
        return repaired

    parts = re.split(r"([\\/:\s]+)", value)
    if len(parts) <= 1:
        return value
    repaired_parts = [
        part if index % 2 else _repair_whole_text(part)
        for index, part in enumerate(parts)
    ]
    repaired = "".join(repaired_parts)
    return repaired if _mojibake_score(repaired) < original_score else value


def repair_mojibake_payload(value: Any) -> Any:
    """
    Recursively repair mojibake in strings, lists, and dicts.

    Skip repair for payloads with encoding_version: "utf-8-v1" marker,
    which indicates the data was written with explicit UTF-8 encoding.
    """
    if isinstance(value, str):
        return repair_mojibake_text(value)
    if isinstance(value, list):
        return [repair_mojibake_payload(item) for item in value]
    if isinstance(value, dict):
        # Skip repair if this is a versioned payload written with explicit UTF-8
        if value.get("encoding_version") == "utf-8-v1":
            return value
        return {key: repair_mojibake_payload(item) for key, item in value.items()}
    return value

