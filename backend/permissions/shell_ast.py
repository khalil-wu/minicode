"""Literal command extraction from POSIX shell scripts.

The dangerous-command rules must see the commands a script actually runs, not
the surface text. A string matcher cannot tell ``rm -rf /`` inside a brace
group, a ``for`` body, a pipeline tail, or a ``"re"set`` concatenation from
ordinary prose, and it treats a trailing ``&`` or a quote as part of the
target. Parsing with tree-sitter-bash and walking every ``command`` node gives
each rule a resolved argv to work on.

Only the statically known words are returned. Words that undergo expansion
(``$x``, ``*``, ``~``, ``$(...)``) are dropped, so the result identifies
dangerous literal commands but must never be used to prove a script safe.
Callers keep their string-level checks as the fail-closed floor when the
parser is unavailable or the script does not parse.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

try:
    import tree_sitter as _ts
    import tree_sitter_bash as _ts_bash

    _AVAILABLE = True
except Exception:  # pragma: no cover - exercised only without the wheels
    _ts = None
    _ts_bash = None
    _AVAILABLE = False

MAX_WRAPPER_DEPTH = 8

#: Stands in for a word whose runtime value depends on expansion (``$x``,
#: ``$(...)``, ``*``, ``~``). Rules see where such a word sits without ever
#: trusting its spelling.
DYNAMIC_WORD = chr(0) + "dynamic"

_POSIX_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
# Characters that make a bare word subject to shell expansion or escape removal.
_UNSAFE_WORD_CHARS = frozenset("{}*?[]\\~^#$`")

_BACKSLASH_PAIR = re.compile(r"\\(.)", re.DOTALL)

_parser_lock = threading.Lock()
_parser: Any = None


def is_available() -> bool:
    return _AVAILABLE


def _get_parser() -> Any:
    global _parser
    if _parser is None:
        _parser = _ts.Parser(_ts.Language(_ts_bash.language()))
    return _parser


@dataclass(slots=True)
class LiteralShell:
    """Statically known commands of a script."""

    commands: list[list[str]] = field(default_factory=list)
    #: Command nodes whose name could not be resolved from source spelling.
    dynamic_command_names: int = 0
    #: Some level of the script (outer or a wrapper payload) chains two or
    #: more statements with ``;``, ``&&``, ``||`` or a newline, so a rule
    #: that only fires inside compounds applies.
    compound: bool = False


@lru_cache(maxsize=512)
def parse_literal_commands(script: str) -> LiteralShell | None:
    """Return every literal command in *script*, or ``None`` when unparseable.

    Shell wrappers (``bash -c``, ``sudo``, ``env``, ``trap``) are unwrapped so
    their payload commands are reported alongside the outer ones. Nesting is
    capped; a script that nests deeper is reported as unparseable so callers
    fall back to their conservative path. The result is shared between the
    permission gate, execution gate and side-effect classifier of one command
    and must not be mutated.
    """
    return _parse_literal_commands(script, 0)


def _parse_literal_commands(script: str, _depth: int) -> LiteralShell | None:
    if not _AVAILABLE or not script or not script.strip():
        return None
    if _depth > MAX_WRAPPER_DEPTH:
        return None
    source = script.encode("utf-8", errors="surrogateescape")
    with _parser_lock:
        tree = _get_parser().parse(source)
    root = tree.root_node
    if root.has_error:
        return None

    result = LiteralShell(compound=_is_compound(root))
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "command":
            argv = _literal_command(node, source)
            if argv is None:
                result.dynamic_command_names += 1
            else:
                result.commands.append(argv)
                nested = _unwrap(argv, _depth)
                if nested is None:
                    continue
                if nested is _UNPARSEABLE:
                    return None
                result.commands.extend(nested.commands)
                result.dynamic_command_names += nested.dynamic_command_names
                result.compound = result.compound or nested.compound
        stack.extend(node.children)
    return result


_UNPARSEABLE = LiteralShell()


def _is_compound(root: Any) -> bool:
    if root.named_child_count >= 2:
        return True
    stack = list(root.children)
    while stack:
        node = stack.pop()
        if node.type == "list":
            return True
        stack.extend(node.children)
    return False


def _unwrap(argv: list[str], depth: int) -> LiteralShell | None:
    """Return the commands hidden behind a wrapper argv, if any."""
    if not argv:
        return None
    name = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in _POSIX_SHELLS:
        for index, flag in enumerate(argv[1:], start=1):
            if flag in {"-c", "-lc", "-ic", "-ec", "-xc"} and index + 1 < len(argv):
                payload = argv[index + 1]
                return None if payload == DYNAMIC_WORD else _nested(payload, depth)
        return None
    if name == "sudo":
        return _nested_argv(_strip_sudo(argv[1:]), depth)
    if name == "env":
        return _nested_argv(_strip_env(argv[1:]), depth)
    if name == "trap":
        action = argv[2:3] if argv[1:2] == ["--"] else argv[1:2]
        if action and action[0] != DYNAMIC_WORD and not action[0].startswith("-"):
            return _nested(action[0], depth)
        return None
    return None


def _nested(script: str, depth: int) -> LiteralShell:
    parsed = _parse_literal_commands(script, depth + 1)
    return _UNPARSEABLE if parsed is None else parsed


def _nested_argv(argv: list[str], depth: int) -> LiteralShell | None:
    if not argv:
        return None
    if depth + 1 > MAX_WRAPPER_DEPTH:
        return _UNPARSEABLE
    inner = LiteralShell(commands=[argv])
    nested = _unwrap(argv, depth + 1)
    if nested is _UNPARSEABLE:
        return _UNPARSEABLE
    if nested is not None:
        inner.commands.extend(nested.commands)
        inner.dynamic_command_names += nested.dynamic_command_names
    return inner


def _strip_sudo(args: list[str]) -> list[str]:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] == "--":
            index += 1
            break
        # Options that take a separate value.
        if args[index] in {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-T", "-U"}:
            index += 2
            continue
        index += 1
    return args[index:]


def _strip_env(args: list[str]) -> list[str]:
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == "--":
            index += 1
            break
        if argument in {"-i", "--ignore-environment"}:
            index += 1
            continue
        name, sep, _ = argument.partition("=")
        if sep and name and not name.startswith("-"):
            index += 1
            continue
        break
    return args[index:]


def _literal_command(node: Any, source: bytes) -> list[str] | None:
    words: list[str] = []
    found_name = False
    for child in node.named_children:
        if child.type == "command_name":
            inner = child.named_children[0] if child.named_children else None
            name = _literal_word(inner, source) if inner is not None else None
            if name is None:
                return None
            words.append(name)
            found_name = True
        elif found_name:
            word = _literal_word(child, source)
            words.append(DYNAMIC_WORD if word is None else word)
    return words if found_name else None


def _literal_word(node: Any, source: bytes) -> str | None:
    kind = node.type
    if kind in {"word", "number"}:
        if node.named_children:
            return None
        text = _text(node, source)
        # ``\;`` and friends: a backslash quotes the next character, so the
        # runtime word is known. Any other unsafe character stays dynamic.
        unescaped = _BACKSLASH_PAIR.sub(lambda match: match.group(1), text)
        remainder = _BACKSLASH_PAIR.sub("", text)
        if text.startswith("=") or any(ch in _UNSAFE_WORD_CHARS for ch in remainder):
            return None
        return unescaped
    if kind == "string":
        for part in node.named_children:
            if part.type != "string_content":
                return None
        raw = _text(node, source)
        if not (raw.startswith('"') and raw.endswith('"')):
            return None
        body = raw[1:-1]
        # Double quotes suppress globbing but not escape removal.
        for index, ch in enumerate(body[:-1]):
            if ch == "\\" and body[index + 1] in '$`"\\\n':
                return None
        return body
    if kind == "raw_string":
        raw = _text(node, source)
        if raw.startswith("'") and raw.endswith("'"):
            return raw[1:-1]
        return None
    if kind == "concatenation":
        if _text(node, source) == "{}":
            # Bare ``{}`` is not a brace expansion; it is the placeholder that
            # ``find -exec`` and ``xargs -I`` substitute.
            return "{}"
        parts: list[str] = []
        for part in node.named_children:
            word = _literal_word(part, source)
            if word is None:
                return None
            parts.append(word)
        joined = "".join(parts)
        return joined or None
    return None


def _text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="surrogateescape")
