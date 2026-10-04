"""Incremental removal of leaked provider reasoning control tokens."""

from __future__ import annotations

import re
from backend.memory.citations import parse_memory_citation


_VISIBLE_BOUNDARY_RE = re.compile(r"[<`~\\\n]")
_REASONING_TAG_AT_START_RE = re.compile(
    r"^<\s*(/?)\s*(thinking|reasoning|internal|think)(?=[\s/>])[^>]*>",
    re.IGNORECASE,
)
_REASONING_CLOSE_RE = re.compile(
    r"<\s*/\s*(?:thinking|reasoning|internal|think)(?=[\s>])[^>]*>",
    re.IGNORECASE,
)
_SPECIAL_TOKEN_AT_START_RE = re.compile(r"^<\|[^|>]*\|>")
_REASONING_CONTROL_PREFIXES = (
    "<think",
    "</think",
    "<thinking",
    "</thinking",
    "<reasoning",
    "</reasoning",
    "<internal",
    "</internal",
    "<|",
    "<minicode-memory-citation>",
)
_MEMORY_CITATION_OPEN = "<minicode-memory-citation>"
_MEMORY_CITATION_CLOSE = "</minicode-memory-citation>"


class ThinkingStreamSanitizer:
    """Remove reasoning tags without leaking tags split across chunks."""

    def __init__(self, *, hide_memory_citations: bool = True) -> None:
        self._pending = ""
        self._inside_reasoning = False
        self._inside_memory_citation = False
        self._memory_citation_body = ""
        self.citations: list[str] = []
        # The Chat adapter must leave citations for the agent's usage recorder.
        self._hide_memory_citations = hide_memory_citations
        self._fence = ""
        self._fence_closing = False
        self._inline_ticks = 0
        self._line_indent: int | None = 0
        self._escaped = False

    def _looks_like_control_prefix(self, value: str) -> bool:
        lowered = value.lower()
        if self._hide_memory_citations and _MEMORY_CITATION_OPEN.startswith(lowered):
            return True
        if re.fullmatch(r"<\|[^|>]*\|?", value):
            return True
        lowered = re.sub(r"^<\s*(/?)\s*", r"<\1", lowered)
        for prefix in _REASONING_CONTROL_PREFIXES:
            if prefix in {"<|", _MEMORY_CITATION_OPEN}:
                continue
            if prefix.startswith(lowered):
                return True
            if not lowered.startswith(prefix):
                continue
            remainder = lowered[len(prefix):]
            # Once the candidate tag name is complete, only whitespace,
            # slash, or the closing angle can introduce a real control tag.
            # This prevents ordinary prose such as ``<internal-api>`` and
            # ``<think-tank>`` from holding the rest of the answer forever.
            if not remainder or remainder[0] in " \t\r\n/>":
                return True
        return False

    @staticmethod
    def _closing_prefix_length(value: str) -> int:
        marker = value.rfind("<")
        if marker < 0:
            return 0
        suffix = value[marker:]
        lowered = re.sub(r"^<\s*/?\s*", "</", suffix.lower())
        closing_prefixes = ("</think", "</thinking", "</reasoning", "</internal")
        for prefix in closing_prefixes:
            if prefix.startswith(lowered):
                return len(suffix)
            if lowered.startswith(prefix) and lowered[len(prefix):len(prefix)+1] in {" ", "\t", "\r", "\n", "/"}:
                return len(suffix)
        return 0

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._pending += chunk
        return self._drain()

    def _emit_visible(self, visible: list[str], text: str) -> None:
        visible.append(text)
        if text == "\n":
            if self._fence_closing:
                self._fence = ""
            self._fence_closing = False
            self._line_indent = 0
        else:
            if text.strip(" \t\r"):
                self._fence_closing = False
            if self._line_indent is not None:
                self._line_indent = (
                    self._line_indent + len(text) if not text.strip(" ") else None
                )
        self._escaped = not self._escaped if text == "\\" else False

    def _drain(self, *, final: bool = False) -> str:
        visible: list[str] = []

        while self._pending:
            if self._inside_memory_citation:
                closing_index = self._pending.find(_MEMORY_CITATION_CLOSE)
                if closing_index >= 0:
                    self._memory_citation_body += self._pending[:closing_index]
                    if parse_memory_citation([self._memory_citation_body]) is not None:
                        self.citations.append(self._memory_citation_body)
                    else:
                        visible.append(_MEMORY_CITATION_OPEN + self._memory_citation_body + _MEMORY_CITATION_CLOSE)
                    self._memory_citation_body = ""
                    self._pending = self._pending[
                        closing_index + len(_MEMORY_CITATION_CLOSE):
                    ]
                    self._inside_memory_citation = False
                    continue
                keep = 0
                max_length = min(len(self._pending), len(_MEMORY_CITATION_CLOSE))
                for length in range(max_length, 0, -1):
                    if _MEMORY_CITATION_CLOSE.startswith(self._pending[-length:]):
                        keep = length
                        break
                if keep:
                    self._memory_citation_body += self._pending[:-keep]
                    self._pending = self._pending[-keep:]
                else:
                    self._memory_citation_body += self._pending
                    self._pending = ""
                break

            if self._inside_reasoning:
                closing = _REASONING_CLOSE_RE.search(self._pending)
                if closing is None:
                    keep = self._closing_prefix_length(self._pending)
                    self._pending = self._pending[-keep:] if keep else ""
                    break
                self._pending = self._pending[closing.end():]
                self._inside_reasoning = False
                continue

            boundary = _VISIBLE_BOUNDARY_RE.search(self._pending)
            if boundary is None:
                self._emit_visible(visible, self._pending)
                self._pending = ""
                break
            marker_index = boundary.start()
            if marker_index > 0:
                self._emit_visible(visible, self._pending[:marker_index])
                self._pending = self._pending[marker_index:]

            marker = self._pending[0]
            if marker in "`~" and not (self._escaped and not self._fence and not self._inline_ticks):
                length = len(self._pending) - len(self._pending.lstrip(marker))
                if length == len(self._pending) and not final:
                    # A delimiter run may be split across provider chunks.
                    break
                at_line_start = self._line_indent is not None and self._line_indent <= 3
                delimiter = self._pending[:length]
                closes_fence = bool(
                    self._fence and at_line_start and marker == self._fence[0]
                    and length >= len(self._fence)
                )
                if not self._fence:
                    if self._inline_ticks:
                        if marker == "`" and length == self._inline_ticks:
                            self._inline_ticks = 0
                    elif at_line_start and length >= 3:
                        self._fence = delimiter
                    elif marker == "`":
                        self._inline_ticks = length
                self._emit_visible(visible, delimiter)
                if closes_fence:
                    # A closing fence accepts only trailing whitespace to EOL.
                    self._fence_closing = True
                self._pending = self._pending[length:]
                continue

            if marker != "<" or self._fence or self._inline_ticks or self._escaped:
                self._emit_visible(visible, marker)
                self._pending = self._pending[1:]
                continue

            tag = _REASONING_TAG_AT_START_RE.match(self._pending)
            if tag is not None:
                self._inside_reasoning = not bool(tag.group(1))
                self._pending = self._pending[tag.end():]
                continue

            special = _SPECIAL_TOKEN_AT_START_RE.match(self._pending)
            if special is not None:
                self._pending = self._pending[special.end():]
                continue

            if self._hide_memory_citations and self._pending.startswith(_MEMORY_CITATION_OPEN):
                self._pending = self._pending[len(_MEMORY_CITATION_OPEN):]
                self._inside_memory_citation = True
                self._memory_citation_body = ""
                continue

            if self._looks_like_control_prefix(self._pending):
                # The chunk may end inside a control tag; wait for more text.
                # Known tag attributes may be long. A character-count cutoff
                # would leak a valid opener split before its closing angle.
                break

            self._emit_visible(visible, "<")
            self._pending = self._pending[1:]

        return "".join(visible)

    def finish(self) -> str:
        """Release text held while waiting for a tag that never completed."""
        visible = self._drain(final=True)
        if self._inside_memory_citation:
            tail = _MEMORY_CITATION_OPEN + self._memory_citation_body + self._pending
        else:
            tail = "" if self._inside_reasoning else self._pending
        self._pending = ""
        self._inside_reasoning = False
        self._inside_memory_citation = False
        self._memory_citation_body = ""
        self._fence = ""
        self._fence_closing = False
        self._inline_ticks = 0
        self._line_indent = 0
        self._escaped = False
        return visible + tail


def scrub_thinking_tags(text: str) -> str:
    """Remove reasoning tags and special tokens from completed model text."""
    if not text or "<" not in text:
        return text
    sanitizer = ThinkingStreamSanitizer()
    return sanitizer.feed(text) + sanitizer.finish()
