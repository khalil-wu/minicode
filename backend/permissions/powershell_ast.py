"""Literal command extraction from PowerShell scripts.

On Windows the command tool runs its script under PowerShell, so a model may
write ``Remove-Item -Recurse C:\\``, ``rm -rf /`` (an alias), or a mix of the
two. This module lowers every ``command`` node of a PowerShell parse into an
argv-like word list the same way :mod:`backend.permissions.shell_ast` does for
POSIX shells: quoting resolved, words that only exist after evaluation
(variables, sub-expressions, script blocks) replaced by ``DYNAMIC_WORD``.

Like the POSIX extractor this identifies dangerous literal commands and must
never be used to prove a script safe: unparseable input yields ``None`` and
callers stay on their string-level floor.
"""

from __future__ import annotations

import base64
import re
import shlex
import threading
from typing import Any

from backend.permissions.shell_ast import DYNAMIC_WORD, MAX_WRAPPER_DEPTH, LiteralShell

try:
    import tree_sitter as _ts
    import tree_sitter_powershell as _ts_ps

    _AVAILABLE = True
except Exception:  # pragma: no cover - exercised only without the wheels
    _ts = None
    _ts_ps = None
    _AVAILABLE = False

_POWERSHELL_HOSTS = frozenset({"powershell", "pwsh"})
_CMD_HOSTS = frozenset({"cmd"})
_DYNAMIC_KINDS = frozenset({
    "variable",
    "sub_expression",
    "script_block_expression",
    "script_block",
    "invokation_expression",
    "member_access",
    "parenthesized_expression",
    "hashtable_expression",
    "cast_expression",
})
_CMD_SEPARATORS = re.compile(r"\s*(?:&&|\|\||&|\|)\s*")

_parser_lock = threading.Lock()
_parser: Any = None


def is_available() -> bool:
    return _AVAILABLE


def _get_parser() -> Any:
    global _parser
    if _parser is None:
        _parser = _ts.Parser(_ts.Language(_ts_ps.language()))
    return _parser


def parse_literal_commands(script: str, *, _depth: int = 0) -> LiteralShell | None:
    """Return every literal command in a PowerShell *script*, or ``None``."""
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
            argv = _lower_command(node, source)
            if argv is None:
                result.dynamic_command_names += 1
            else:
                result.commands.append(argv)
                nested = _unwrap(argv, _depth)
                if nested is _UNPARSEABLE:
                    return None
                if nested is not None:
                    result.commands.extend(nested.commands)
                    result.dynamic_command_names += nested.dynamic_command_names
                    result.compound = result.compound or nested.compound
        stack.extend(node.children)
    return result


_UNPARSEABLE = LiteralShell()


def _is_compound(root: Any) -> bool:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "statement_list" and node.named_child_count >= 2:
            statements = [
                child for child in node.named_children if child.type != "empty_statement"
            ]
            if len(statements) >= 2:
                return True
        if node.type == "pipeline_chain_tail":
            return True
        stack.extend(node.children)
    return False


def _unwrap(argv: list[str], depth: int) -> LiteralShell | None:
    if not argv:
        return None
    name = _executable_key(argv[0])
    if name in _POWERSHELL_HOSTS:
        payload = _powershell_payload(argv[1:])
        if payload is None or payload == DYNAMIC_WORD:
            return None
        nested = parse_literal_commands(payload, _depth=depth + 1)
        return _UNPARSEABLE if nested is None else nested
    if name in _CMD_HOSTS:
        payload = _cmd_payload(argv[1:])
        if payload is None or payload == DYNAMIC_WORD:
            return None
        if depth + 1 > MAX_WRAPPER_DEPTH:
            return _UNPARSEABLE
        return _lower_cmd_script(payload, depth + 1)
    return None


def _executable_key(raw: str) -> str:
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _powershell_payload(args: list[str]) -> str | None:
    index = 0
    while index < len(args):
        flag = args[index].lower()
        if flag in {"-command", "-c", "-commandwithargs"} and index + 1 < len(args):
            return " ".join(args[index + 1 :])
        if flag in {"-encodedcommand", "-e", "-ec", "-enc"} and index + 1 < len(args):
            encoded = args[index + 1]
            if encoded == DYNAMIC_WORD:
                return DYNAMIC_WORD
            try:
                return base64.b64decode(encoded, validate=True).decode("utf-16-le")
            except (ValueError, UnicodeDecodeError):
                return None
        if flag in {"-file", "-f"}:
            return None
        index += 1
    return None


def _cmd_payload(args: list[str]) -> str | None:
    for index, flag in enumerate(args):
        if flag.lower() in {"/c", "/k"} and index + 1 < len(args):
            return " ".join(args[index + 1 :])
    return None


def _lower_cmd_script(script: str, depth: int) -> LiteralShell:
    """cmd.exe has no grammar worth a parser; split on its operators."""
    result = LiteralShell()
    segments = [segment for segment in _CMD_SEPARATORS.split(script) if segment.strip()]
    result.compound = len(segments) >= 2
    for segment in segments:
        try:
            words = shlex.split(segment, posix=False)
        except ValueError:
            return _UNPARSEABLE
        words = [word[1:-1] if len(word) >= 2 and word[0] == word[-1] == '"' else word for word in words]
        words = [DYNAMIC_WORD if "%" in word or "!" in word else word for word in words]
        if not words or words[0] == DYNAMIC_WORD:
            result.dynamic_command_names += 1
            continue
        result.commands.append(words)
        nested = _unwrap(words, depth)
        if nested is _UNPARSEABLE:
            return _UNPARSEABLE
        if nested is not None:
            result.commands.extend(nested.commands)
            result.dynamic_command_names += nested.dynamic_command_names
    return result


def _lower_command(node: Any, source: bytes) -> list[str] | None:
    words: list[str] = []
    name: str | None = None
    for child in node.named_children:
        if child.type == "command_name":
            name = _text(child, source)
        elif child.type == "command_name_expr":
            name = _literal_value(child, source)
            if name is None or name == DYNAMIC_WORD:
                return None
        elif child.type == "command_elements":
            for element in child.named_children:
                if element.type == "command_argument_sep":
                    continue
                if element.type == "command_parameter":
                    words.append(_text(element, source))
                    continue
                words.extend(_lower_argument(element, source))
        elif child.type == "command_invokation_operator":
            continue
    if not name:
        return None
    return [name, *words]


def _lower_argument(node: Any, source: bytes) -> list[str]:
    kind = node.type
    if kind == "generic_token" or kind.endswith("integer_literal"):
        return [_text(node, source)]
    if kind == "expression_with_unary_operator":
        return [_text(node, source)]
    if kind == "string_literal":
        value = _literal_value(node, source)
        return [DYNAMIC_WORD if value is None else value]
    if kind in _DYNAMIC_KINDS:
        return [DYNAMIC_WORD]
    if kind in {"array_literal_expression", "array_expression", "unary_expression"} or "expression" in kind:
        if _contains_dynamic(node):
            return [DYNAMIC_WORD]
        unary = _descendants(node, "expression_with_unary_operator")
        if unary and not _descendants(node, "string_literal"):
            # Negative numeric flags such as ``git log -1``.
            return [_text(part, source) for part in unary]
        literals = [_literal_value(part, source) for part in _descendants(node, "string_literal")]
        tokens = [_text(part, source) for part in _descendants(node, "generic_token")]
        values = [value for value in literals if value is not None] + tokens
        return values or [DYNAMIC_WORD]
    return [DYNAMIC_WORD]


def _contains_dynamic(node: Any) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _DYNAMIC_KINDS:
            return True
        stack.extend(current.named_children)
    return False


def _descendants(node: Any, kind: str) -> list[Any]:
    found: list[Any] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == kind:
            found.append(current)
            continue
        stack.extend(reversed(current.named_children))
    return found


def _literal_value(node: Any, source: bytes) -> str | None:
    """Unquote a string literal; ``None`` when it needs evaluation."""
    if node.type == "string_literal":
        inner = node.named_children[0] if node.named_children else node
        return _literal_value(inner, source)
    raw = _text(node, source)
    if node.type == "verbatim_string_characters" or (raw.startswith("'") and raw.endswith("'") and len(raw) >= 2):
        return raw[1:-1].replace("''", "'")
    if node.type == "expandable_string_literal" or (raw.startswith('"') and raw.endswith('"') and len(raw) >= 2):
        body = raw[1:-1]
        if "$" in body or "`" in body:
            return None
        return body.replace('""', '"')
    if node.type == "command_name_expr":
        parts = [_literal_value(child, source) for child in node.named_children]
        if not parts or any(part is None for part in parts):
            return None
        return "".join(parts)  # type: ignore[arg-type]
    if node.type == "generic_token":
        return raw
    return None


def _text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="surrogateescape")
