"""Unicode hardening for untrusted MCP metadata.

This module intentionally does *not* sanitize source files, attachments, MCP
tool results, resource bodies, or user-authored tool arguments. Those values
may legitimately depend on exact Unicode code points. It is used only at MCP
metadata/instruction boundaries that are projected into model-visible tool or
prompt descriptions.
"""

from __future__ import annotations

import unicodedata
from typing import Any


class UnsafeUnicodeMetadataKey(ValueError):
    """Raised when sanitizing a mapping key would change its wire contract."""


_MAX_NORMALIZATION_PASSES = 10
_REMOVED_CATEGORIES = frozenset({"Cf", "Co", "Cn"})

# U+200D is required for many ordinary emoji sequences. The other format
# controls remain removable, including bidi controls, zero-width spaces, tag
# characters, byte-order marks, and word joiners.
_PRESERVED_FORMAT_CODEPOINTS = frozenset({0x200D})


def _is_explicitly_dangerous(codepoint: int) -> bool:
    return (
        (0x200B <= codepoint <= 0x200F and codepoint != 0x200D)
        or 0x202A <= codepoint <= 0x202E
        or 0x2066 <= codepoint <= 0x2069
        or codepoint == 0xFEFF
        or 0xE000 <= codepoint <= 0xF8FF
        or 0xF0000 <= codepoint <= 0xFFFFD
        or 0x100000 <= codepoint <= 0x10FFFD
        or 0xE0000 <= codepoint <= 0xE007F
        or 0xFDD0 <= codepoint <= 0xFDEF
        or (codepoint & 0xFFFF) in {0xFFFE, 0xFFFF}
    )


def _strip_dangerous_codepoints(value: str) -> str:
    kept: list[str] = []
    for character in value:
        codepoint = ord(character)
        category = unicodedata.category(character)
        if _is_explicitly_dangerous(codepoint):
            continue
        if category in _REMOVED_CATEGORIES and codepoint not in _PRESERVED_FORMAT_CODEPOINTS:
            continue
        kept.append(character)
    return "".join(kept)


def sanitize_untrusted_unicode(value: object) -> str:
    """Normalize and remove hidden-control code points from untrusted text.

    NFKC is applied iteratively because normalization can expose code points
    that require another pass. The operation is deterministic and bounded;
    callers never receive partially sanitized text.
    """

    current = str(value or "")
    for _ in range(_MAX_NORMALIZATION_PASSES):
        sanitized = _strip_dangerous_codepoints(unicodedata.normalize("NFKC", current))
        if sanitized == current:
            return sanitized
        current = sanitized
    raise ValueError("Unicode sanitization did not converge")


def unicode_identifier_is_safe(value: object) -> bool:
    """Return whether an identifier can be preserved byte-for-byte on the wire."""

    text = str(value or "")
    return bool(text) and sanitize_untrusted_unicode(text) == text


def sanitize_untrusted_metadata(
    value: Any,
    *,
    reject_unsafe_keys: bool = False,
) -> Any:
    """Recursively sanitize model-visible metadata without renaming keys.

    Mapping keys are part of JSON-schema and MCP wire contracts. A key that
    changes under sanitization is therefore either rejected with the complete
    metadata object or dropped, never silently renamed. Use rejection for
    schemas and dropping for advisory annotations/_meta so an attacker cannot
    smuggle a privileged key such as ``readOnlyHint`` through hidden Unicode.
    """

    if isinstance(value, str):
        return sanitize_untrusted_unicode(value)
    if isinstance(value, list):
        return [
            sanitize_untrusted_metadata(item, reject_unsafe_keys=reject_unsafe_keys)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            sanitize_untrusted_metadata(item, reject_unsafe_keys=reject_unsafe_keys)
            for item in value
        )
    if isinstance(value, dict):
        sanitized: dict[Any, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                if reject_unsafe_keys:
                    raise UnsafeUnicodeMetadataKey("metadata key is not a string")
                continue
            if sanitize_untrusted_unicode(key) != key:
                if reject_unsafe_keys:
                    raise UnsafeUnicodeMetadataKey("metadata key contains unsafe Unicode")
                continue
            sanitized[key] = sanitize_untrusted_metadata(
                item,
                reject_unsafe_keys=reject_unsafe_keys,
            )
        return sanitized
    return value
