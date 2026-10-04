"""HTML sanitizer for web_fetch — strips noise, preserves readable text."""
from __future__ import annotations

import re
from html.parser import HTMLParser

_NOISE_ELEMENTS = {"script", "style", "svg", "canvas", "noscript", "template", "header", "nav", "footer", "aside"}
_BLOCK_ELEMENTS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "article", "section", "pre", "blockquote"}


class _ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0
        self._links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _NOISE_ELEMENTS:
            self._ignored_depth += 1
        if self._ignored_depth:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag == "a":
            self._links.append(dict(attrs).get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in _NOISE_ELEMENTS and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "a" and self._links:
            href = self._links.pop()
            if href:
                self.parts.append(f" [{href}]")
        elif tag in _BLOCK_ELEMENTS:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def sanitize_html(html: str) -> str:
    """Strip noise from HTML, return clean readable text.

    Ignore noise containers and attributes, decode HTML entities and retain
    text/link structure. HTML tokenization owns quotes and nested markup.
    """
    parser = _ReadableHTMLParser()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")

    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text.strip()


def assess_extraction(cleaned: str, raw_length: int) -> str:
    """Return extraction_status: ok, partial, or failed."""
    if not cleaned:
        return "failed"
    ratio = len(cleaned) / max(raw_length, 1)
    if ratio < 0.01:
        return "partial"
    return "ok"
