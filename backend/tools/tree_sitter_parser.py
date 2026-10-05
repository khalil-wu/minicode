"""
Tree-sitter integration for precise code analysis in non-Python languages.

Provides AST-based definition and reference finding for:
  JavaScript, TypeScript, Go, Rust, Java

Falls back gracefully to regex-based analysis when tree-sitter packages
are not installed.

Installation (optional — regex fallback works without these):
    pip install tree-sitter tree-sitter-javascript tree-sitter-typescript \\
                tree-sitter-go tree-sitter-rust tree-sitter-java

Usage:
    from backend.tools.tree_sitter_parser import get_parser, find_definitions, find_references

    parser = get_parser("javascript")       # None if not installed
    defs   = find_definitions(src, "myFunc", "javascript")
    refs   = find_references(src, "myFunc", "javascript")
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── Lazy import guard ────────────────────────────────────────────
try:
    import tree_sitter as _ts  # type: ignore[import-untyped]

    _HAS_TREE_SITTER = True
except ImportError:
    _ts = None  # type: ignore[assignment]
    _HAS_TREE_SITTER = False

# ── Extension → language mapping ─────────────────────────────────
EXTENSION_TO_LANGUAGE: dict[str, str] = {
    "js":   "javascript",
    "jsx":  "javascript",
    "mjs":  "javascript",
    "cjs":  "javascript",
    "ts":   "typescript",
    # TSX is a distinct grammar, not a TypeScript dialect: parsing a .tsx file
    # with the plain TypeScript grammar misreads JSX elements.
    "tsx":  "tsx",
    "go":   "go",
    "rs":   "rust",
    "java": "java",
}

# ── Language package registry (lazy-loaded) ──────────────────────
# Maps canonical language name → (pip package, loader callable name).
# The loader name is per-package, not a convention: tree_sitter_typescript
# ships two grammars and exposes language_typescript()/language_tsx() instead
# of the language() every other grammar package exports. Assuming language()
# here made TS/TSX raise AttributeError, get cached as unavailable, and fall
# back to regex forever.
_LANGUAGE_PACKAGES: dict[str, tuple[str, str]] = {
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx":        ("tree_sitter_typescript", "language_tsx"),
    "go":         ("tree_sitter_go",         "language"),
    "rust":       ("tree_sitter_rust",       "language"),
    "java":       ("tree_sitter_java",       "language"),
}

# Cache of already-loaded Language objects
_language_cache: dict[str, Any | None] = {}


# ── Public API ───────────────────────────────────────────────────

def is_available() -> bool:
    """Return True if the core tree-sitter package is importable."""
    return _HAS_TREE_SITTER


def get_language(language: str) -> Any | None:
    """
    Return a tree-sitter Language object for *language*, or None.

    The language grammar package is imported lazily on first call.
    Returns None when tree-sitter or the grammar package is not installed.
    """
    if not _HAS_TREE_SITTER:
        return None

    lang_key = language.lower()
    if lang_key in _language_cache:
        return _language_cache[lang_key]

    pkg_info = _LANGUAGE_PACKAGES.get(lang_key)
    if pkg_info is None:
        _language_cache[lang_key] = None
        return None

    module_name, func_name = pkg_info
    try:
        mod = __import__(module_name)
        lang_obj = _ts.Language(getattr(mod, func_name)())
        _language_cache[lang_key] = lang_obj
        return lang_obj
    except ImportError:
        logger.debug(
            "tree-sitter grammar '%s' not installed. "
            "Install with: pip install %s",
            lang_key, module_name,
        )
        _language_cache[lang_key] = None
        return None


def get_parser(language: str) -> Any | None:
    """
    Return a configured tree-sitter Parser for *language*, or None.

    Each analysis owns its parser; workspace analyses can run in parallel
    workers. The immutable grammar is cached. Returns None when tree-sitter
    or the grammar package is not installed.
    """
    if not _HAS_TREE_SITTER:
        return None

    lang_key = language.lower()
    lang_obj = get_language(lang_key)
    if lang_obj is None:
        return None

    return _ts.Parser(lang_obj)


def language_for_extension(ext: str) -> str | None:
    """
    Map a file extension (without dot) to a tree-sitter language name.

    Returns None for unsupported extensions (e.g. 'py', 'rb', 'php').
    """
    return EXTENSION_TO_LANGUAGE.get(ext.lower().lstrip("."))


# ── Tree traversal helpers ───────────────────────────────────────

def _walk_nodes(node: Any) -> list[Any]:
    """Collect all nodes in a tree-sitter tree via depth-first traversal."""
    result: list[Any] = []
    stack = [node]
    while stack:
        current = stack.pop()
        result.append(current)
        # Extend in reverse so left-most child is processed first
        stack.extend(reversed(current.children))
    return result


def _node_text(node: Any) -> str:
    """Extract the UTF-8 text of a tree-sitter node."""
    return node.text.decode("utf-8", errors="replace")


def _line_of_node(node: Any) -> int:
    """Return 1-indexed line number for a node's start position."""
    return node.start_point[0] + 1


# ── Definition node types per language ───────────────────────────
# These are the tree-sitter node types that represent definitions.
_DEFINITION_NODE_TYPES: dict[str, set[str]] = {
    "javascript": {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "variable_declarator",
    },
    "typescript": {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "variable_declarator",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
    },
    "go": {
        "function_declaration",
        "method_declaration",
        "type_spec",
        "type_alias",
        "short_var_declaration",
        "var_spec",
        "const_spec",
    },
    "rust": {
        "function_item",
        "struct_item",
        "enum_item",
        "trait_item",
        "let_declaration",
        "const_item",
        "static_item",
    },
    "java": {
        "method_declaration",
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "variable_declarator",
    },
}

# TSX is a separate grammar but declares the same constructs as TypeScript.
_DEFINITION_NODE_TYPES["tsx"] = _DEFINITION_NODE_TYPES["typescript"]

# Node types for identifier/name nodes
_IDENTIFIER_TYPES = {
    "identifier",
    "property_identifier",
    "type_identifier",
    "field_identifier",
    "shorthand_property_identifier",
    "shorthand_property_identifier_pattern",
    "shorthand_field_identifier",
}


# ── Core search functions ────────────────────────────────────────

def find_definitions(source: str, name: str, language: str) -> list[tuple[int, str]] | None:
    """
    Find definition locations of *name* in *source* using tree-sitter.

    Args:
        source:   Full source code text.
        name:     Symbol name to search for.
        language: Tree-sitter language key (e.g. "javascript", "go").

    Returns:
        List of (line_number, line_text) tuples.  Line numbers are 1-indexed.
        Returns None if the grammar is unavailable; [] means no matching definition.
    """
    parser = get_parser(language)
    if parser is None:
        return None

    tree = parser.parse(source.encode("utf-8"))

    lang_key = language.lower()
    def_types = _DEFINITION_NODE_TYPES.get(lang_key, set())
    source_lines = source.splitlines()
    results: list[tuple[int, str]] = []
    seen_lines: set[int] = set()

    all_nodes = _walk_nodes(tree.root_node)
    for node in all_nodes:
        if node.type not in def_types:
            continue

        # Look for the name inside this definition node
        matched = any(_node_text(identifier) == name for identifier in _definition_identifiers(node))
        if not matched:
            continue

        lineno = _line_of_node(node)
        if lineno in seen_lines:
            continue
        seen_lines.add(lineno)

        line_text = source_lines[lineno - 1].strip() if 0 < lineno <= len(source_lines) else ""
        results.append((lineno, line_text))

    return results


def find_references(
    source: str, name: str, language: str, *, include_definitions: bool = True,
) -> list[tuple[int, str]] | None:
    """
    Find all references to *name* in *source* using tree-sitter.

    This walks the full AST and matches identifier nodes whose text equals
    *name*. This is more precise than regex word-boundary matching because
    it understands the syntactic role of each token.

    Args:
        source:   Full source code text.
        name:     Symbol name to search for.
        language: Tree-sitter language key (e.g. "javascript", "go").

    Returns:
        List of (line_number, line_text) tuples.  Line numbers are 1-indexed.
        Returns None if the grammar is unavailable; [] means no matching reference.
    """
    parser = get_parser(language)
    if parser is None:
        return None

    tree = parser.parse(source.encode("utf-8"))

    source_lines = source.splitlines()
    results: list[tuple[int, str]] = []
    seen_lines: set[int] = set()

    all_nodes = _walk_nodes(tree.root_node)
    definition_ids = {
        identifier.id
        for node in all_nodes
        if node.type in _DEFINITION_NODE_TYPES.get(language.lower(), set())
        for identifier in _definition_identifiers(node)
    } if not include_definitions else set()
    for node in all_nodes:
        # Match identifier-type nodes whose text equals the target name
        if node.type not in _IDENTIFIER_TYPES:
            continue
        if _node_text(node) != name:
            continue
        if node.id in definition_ids:
            continue

        lineno = _line_of_node(node)
        if lineno in seen_lines:
            continue
        seen_lines.add(lineno)

        line_text = source_lines[lineno - 1].strip() if 0 < lineno <= len(source_lines) else ""
        results.append((lineno, line_text))

    return results


def _definition_identifiers(node: Any) -> list[Any]:
    """Read only declaration binding fields, never types, initializers or bodies."""
    if node.type in {"var_spec", "const_spec"}:
        return [identifier for binding in node.children_by_field_name("name") for identifier in _pattern_identifiers(binding)]
    field = "left" if node.type == "short_var_declaration" else "pattern" if node.type == "let_declaration" else "name"
    binding = node.child_by_field_name(field)
    if binding is None:
        return []
    if binding.type in _IDENTIFIER_TYPES:
        return [binding]
    return _pattern_identifiers(binding)


def _pattern_identifiers(node: Any) -> list[Any]:
    if node.type in {"identifier", "shorthand_property_identifier_pattern", "shorthand_field_identifier"}:
        return [node]
    if node.type == "pair_pattern":
        return _pattern_identifiers(node.child_by_field_name("value"))
    if node.type in {"assignment_pattern", "object_assignment_pattern"}:
        return _pattern_identifiers(node.child_by_field_name("left"))
    return [identifier for child in node.named_children for identifier in _pattern_identifiers(child)]
