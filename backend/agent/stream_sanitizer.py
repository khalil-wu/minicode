"""Incremental removal of leaked provider reasoning control tokens."""

from __future__ import annotations

import re


_THINKING_BLOCK_RE = re.compile(
    r"<(?:thinking|reasoning|internal|think)(?=[\s/>])[^>]*>.*?</(?:thinking|reasoning|internal|think)\s*>",
    re.DOTALL | re.IGNORECASE,
)
_THINKING_MARKER_RE = re.compile(
    r"</?(?:thinking|reasoning|internal|think)(?=[\s/>])[^>]*>",
    re.IGNORECASE,
)
_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|]*\|>")
_REASONING_TAG_AT_START_RE = re.compile(
    r"^<\s*(/?)\s*(thinking|reasoning|internal|think)(?=[\s/>])[^>]*>",
    re.IGNORECASE,
)
_REASONING_CLOSE_RE = re.compile(
    r"</\s*(?:thinking|reasoning|internal|think)(?=[\s>])\s*>",
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

    def __init__(self) -> None:
        self._pending = ""
        self._inside_reasoning = False
        self._inside_memory_citation = False
        self._memory_citation_body = ""
        self.citations: list[str] = []

    @staticmethod
    def _looks_like_control_prefix(value: str) -> bool:
        lowered = value.lower()
        for prefix in _REASONING_CONTROL_PREFIXES:
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
        lowered = value.lower()
        closing_prefixes = ("</think", "</thinking", "</reasoning", "</internal")
        max_length = min(len(lowered), max(len(prefix) for prefix in closing_prefixes))
        for length in range(max_length, 0, -1):
            suffix = lowered[-length:]
            if any(prefix.startswith(suffix) for prefix in closing_prefixes):
                return length
        return 0

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._pending += chunk
        visible: list[str] = []

        while self._pending:
            if self._inside_memory_citation:
                closing_index = self._pending.find(_MEMORY_CITATION_CLOSE)
                if closing_index >= 0:
                    self._memory_citation_body += self._pending[:closing_index]
                    self.citations.append(self._memory_citation_body)
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

            marker_index = self._pending.find("<")
            if marker_index < 0:
                visible.append(self._pending)
                self._pending = ""
                break
            if marker_index > 0:
                visible.append(self._pending[:marker_index])
                self._pending = self._pending[marker_index:]

            tag = _REASONING_TAG_AT_START_RE.match(self._pending)
            if tag is not None:
                self._inside_reasoning = not bool(tag.group(1))
                self._pending = self._pending[tag.end():]
                continue

            special = _SPECIAL_TOKEN_AT_START_RE.match(self._pending)
            if special is not None:
                self._pending = self._pending[special.end():]
                continue

            if self._pending.startswith(_MEMORY_CITATION_OPEN):
                self._pending = self._pending[len(_MEMORY_CITATION_OPEN):]
                self._inside_memory_citation = True
                self._memory_citation_body = ""
                continue

            if self._looks_like_control_prefix(self._pending):
                break

            visible.append("<")
            self._pending = self._pending[1:]

        return "".join(visible)

    def finish(self) -> str:
        self._pending = ""
        self._inside_reasoning = False
        self._inside_memory_citation = False
        self._memory_citation_body = ""
        return ""


def scrub_thinking_tags(text: str) -> str:
    """Remove reasoning tags and special tokens from completed model text."""
    if not text or "<" not in text:
        return text
    text = _THINKING_BLOCK_RE.sub("", text)
    text = _THINKING_MARKER_RE.sub("", text)
    text = _SPECIAL_TOKEN_RE.sub("", text)
    from backend.memory.citations import scrub_memory_citations

    return scrub_memory_citations(text)
