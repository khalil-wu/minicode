"""HTML sanitizer for web_fetch — strips noise, preserves readable text."""
from __future__ import annotations

import re

_STRIP_ELEMENTS = re.compile(
    r"<(script|style|svg|canvas|noscript|template|header|nav|footer|aside)"
    r"[\s>].*?</\1\s*>",
    re.S | re.I,
)

_SELF_CLOSING_NOISE = re.compile(
    r"<(path|circle|rect|line|polygon|polyline|ellipse|use|symbol|defs|clipPath|mask|pattern|image|animate|set)\b[^>]*/?>"
    r"|</(path|circle|rect|line|polygon|polyline|ellipse|use|symbol|defs|clipPath|mask|pattern|image|animate|set)>",
    re.I,
)

_SVG_PATH_COORDS = re.compile(
    r"[MmLlHhVvCcSsQqTtAaZz]\s*[\d\s.,eE+\-]{10,}"
)

_INLINE_STYLE = re.compile(r'\s*style="[^"]*"', re.I)
_CLASS_ATTR = re.compile(r'\s*class="[^"]*"', re.I)
_DATA_ATTR = re.compile(r'\s*data-[a-z\-]+="[^"]*"', re.I)


def sanitize_html(html: str) -> str:
    """Strip noise from HTML, return clean readable text.

    Removes: script, style, svg, canvas, noscript, template,
    header, nav, footer, aside, SVG path coordinates, inline styles,
    CSS classes, data attributes.
    """
    text = _STRIP_ELEMENTS.sub("", html)
    text = _SELF_CLOSING_NOISE.sub("", text)
    text = _SVG_PATH_COORDS.sub("", text)
    text = _INLINE_STYLE.sub("", text)
    text = _CLASS_ATTR.sub("", text)
    text = _DATA_ATTR.sub("", text)

    text = re.sub(r"<br\s*/?\s*>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6]|tr|article|section)>", "\n", text, flags=re.I)
    text = re.sub(
        r"<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        r"\2 [\1]",
        text,
        flags=re.S | re.I,
    )
    text = re.sub(r"<[^>]+>", "", text)

    for entity, char in [
        ("nbsp", " "), ("amp", "&"), ("lt", "<"),
        ("gt", ">"), ("quot", '"'), ("#39", "'"),
    ]:
        text = text.replace(f"&{entity};", char)

    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text.strip()


def assess_extraction(cleaned: str, raw_length: int) -> str:
    """Return extraction_status: ok, partial, or failed."""
    if not cleaned or len(cleaned) < 20:
        return "failed"
    ratio = len(cleaned) / max(raw_length, 1)
    if ratio < 0.01:
        return "partial"
    return "ok"
