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
        self._in_title = False
        self.has_body_text = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in _NOISE_ELEMENTS:
            self._ignored_depth += 1
        if self._ignored_depth:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag == "a":
            self._links.append(dict(attrs).get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
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
            if not self._in_title and data.strip():
                self.has_body_text = True


def sanitize_html(html: str) -> str:
    """Strip noise from HTML, return clean readable text.

    Ignore noise containers and attributes, decode HTML entities and retain
    text/link structure. HTML tokenization owns quotes and nested markup.
    """
    return sanitize_html_with_status(html)[0]


def sanitize_html_with_status(html: str) -> tuple[str, str, str, bool]:
    """Extract readable text and distinguish a page shell from its body."""
    parser = _ReadableHTMLParser()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")

    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    text = text.strip()
    status = assess_extraction(text, len(html))
    if text and not parser.has_body_text:
        return text, "partial", "Only the page title was available; the page body was not fetched.", False
    limitation = "Only part of the page's readable content was available." if status == "partial" else ""
    return text, status, limitation, parser.has_body_text


def assess_extraction(cleaned: str, raw_length: int) -> str:
    """Return extraction_status: ok, partial, or failed."""
    if not cleaned:
        return "failed"
    ratio = len(cleaned) / max(raw_length, 1)
    if ratio < 0.01:
        return "partial"
    return "ok"
