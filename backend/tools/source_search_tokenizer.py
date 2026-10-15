"""English tokenizer used by Codex's bm25 2.3.2 search handler.

The deunicode 1.6.2 tables and stop-words 0.9.0 ISO dictionary are copied
unchanged from the exact Cargo.lock dependencies (licenses alongside data).
"""
from __future__ import annotations

import json
from pathlib import Path
import regex
import snowballstemmer

_ROOT = Path(__file__).with_name("source_search_resources")
_POINTERS = (_ROOT / "deunicode-pointers.bin").read_bytes()
_MAPPING = (_ROOT / "deunicode-mapping.txt").read_bytes()
_STOPWORDS = frozenset(json.loads((_ROOT / "stopwords-en.json").read_text(encoding="utf-8")))
_STEMMER = snowballstemmer.stemmer("english")
_WORD_SEGMENTS = regex.compile(r"\b.+?\b", regex.WORD)


def _transliteration(character: str) -> str | None:
    position = ord(character) * 3
    if position >= len(_POINTERS):
        return None
    length = _POINTERS[position + 2]
    if length <= 2:
        return _POINTERS[position:position + length].decode("ascii")
    offset = int.from_bytes(_POINTERS[position:position + 2], "little")
    if offset + length > len(_MAPPING):
        return None
    return _MAPPING[offset:offset + length].decode("ascii")


def normalize(text: str) -> str:
    if text.isascii():
        return text
    pieces = [_transliteration(character) for character in text]
    for index, piece in enumerate(pieces):
        if piece is not None and len(piece) > 1 and piece.endswith(" "):
            if index == len(pieces) - 1 or (pieces[index + 1] is not None and pieces[index + 1].startswith(" ")):
                pieces[index] = piece[:-1]
    return "".join(piece if piece is not None else "[?]" for piece in pieces)


def tokenize(text: str) -> list[str]:
    words = [word for word in _WORD_SEGMENTS.findall(normalize(text).lower())
             if any(character.isalnum() for character in word) and word not in _STOPWORDS]
    return _STEMMER.stemWords(words)
