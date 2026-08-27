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
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

# ── Lazy import guard ────────────────────────────────────────────
try:
    import tree_sitter as _ts  # type: ignore[import-untyped]

    _HAS_TREE_SITTER = True
except ImportError:
    _ts = None  # type: ignore[assignment]
    _HAS_TREE_SITTER = False

if TYPE_CHECKING:
    pass

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
# Cache of Parser objects per language
_parser_cache: dict[str, Any | None] = {}


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
        lang_fn = getattr(mod, func_name)
        lang_obj = lang_fn()

        # tree-sitter >= 0.22 returns a Language directly;
        # older versions may need Language(lang_obj) wrapping.
        if not isinstance(lang_obj, _ts.Language):
            try:
                lang_obj = _ts.Language(lang_obj)
            except (TypeError, ValueError):
                pass

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
    except Exception as exc:
        logger.debug("Failed to load tree-sitter grammar '%s': %s", lang_key, exc)
        _language_cache[lang_key] = None
        return None


def get_parser(language: str) -> Any | None:
    """
    Return a configured tree-sitter Parser for *language*, or None.

    The parser is cached per language. Returns None when tree-sitter
    or the grammar package is not installed.
    """
    if not _HAS_TREE_SITTER:
        return None

    lang_key = language.lower()
    if lang_key in _parser_cache:
        return _parser_cache[lang_key]

    lang_obj = get_language(lang_key)
    if lang_obj is None:
        _parser_cache[lang_key] = None
        return None

    try:
        parser = _ts.Parser()
        # tree-sitter >= 0.22 API: parser.language = lang
        # tree-sitter <  0.22 API: parser.set_language(lang)
        try:
            parser.language = lang_obj
        except (AttributeError, TypeError):
            parser.set_language(lang_obj)

        _parser_cache[lang_key] = parser
        return parser
    except Exception as exc:
        logger.debug("Failed to create parser for '%s': %s", lang_key, exc)
        _parser_cache[lang_key] = None
        return None


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
        if hasattr(current, "children"):
            # Extend in reverse so left-most child is processed first
            stack.extend(reversed(current.children))
    return result


def _node_text(node: Any) -> str:
    """Extract the UTF-8 text of a tree-sitter node."""
    raw = node.text
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _line_of_node(node: Any) -> int:
    """Return 1-indexed line number for a node's start position."""
    if hasattr(node, "start_point"):
        return node.start_point[0] + 1
    return 1


# ── Definition node types per language ───────────────────────────
# These are the tree-sitter node types that represent definitions.
_DEFINITION_NODE_TYPES: dict[str, set[str]] = {
    "javascript": {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "variable_declarator",
        "arrow_function",
    },
    "typescript": {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "variable_declarator",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
        "arrow_function",
    },
    "go": {
        "function_declaration",
        "method_declaration",
        "type_declaration",
        "short_var_declaration",
    },
    "rust": {
        "function_item",
        "struct_item",
        "enum_item",
        "trait_item",
        "impl_item",
        "let_declaration",
    },
    "java": {
        "method_declaration",
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "field_declaration",
        "local_variable_declaration",
    },
}

# TSX is a separate grammar but declares the same constructs as TypeScript.
_DEFINITION_NODE_TYPES["tsx"] = _DEFINITION_NODE_TYPES["typescript"]

# Child field names that typically hold the defined identifier
_NAME_FIELDS = ("name", "identifier", "declarator")

# Node types for identifier/name nodes
_IDENTIFIER_TYPES = {
    "identifier",
    "property_identifier",
    "type_identifier",
    "field_identifier",
    "shorthand_property_identifier",
}


# ── Core search functions ────────────────────────────────────────

def find_definitions(source: str, name: str, language: str) -> list[tuple[int, str]]:
    """
    Find definition locations of *name* in *source* using tree-sitter.

    Args:
        source:   Full source code text.
        name:     Symbol name to search for.
        language: Tree-sitter language key (e.g. "javascript", "go").

    Returns:
        List of (line_number, line_text) tuples.  Line numbers are 1-indexed.
        Returns an empty list if tree-sitter is not available for this language.
    """
    parser = get_parser(language)
    if parser is None:
        return []

    try:
        source_bytes = source.encode("utf-8")
        tree = parser.parse(source_bytes)
    except Exception as exc:
        logger.debug("tree-sitter parse failed for language '%s': %s", language, exc)
        return []

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
        matched = _node_defines_name(node, name)
        if not matched:
            continue

        lineno = _line_of_node(node)
        if lineno in seen_lines:
            continue
        seen_lines.add(lineno)

        line_text = source_lines[lineno - 1].strip() if 0 < lineno <= len(source_lines) else ""
        results.append((lineno, line_text))

    return results


def find_references(source: str, name: str, language: str) -> list[tuple[int, str]]:
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
        Returns an empty list if tree-sitter is not available for this language.
    """
    parser = get_parser(language)
    if parser is None:
        return []

    try:
        source_bytes = source.encode("utf-8")
        tree = parser.parse(source_bytes)
    except Exception as exc:
        logger.debug("tree-sitter parse failed for language '%s': %s", language, exc)
        return []

    source_lines = source.splitlines()
    results: list[tuple[int, str]] = []
    seen_lines: set[int] = set()

    all_nodes = _walk_nodes(tree.root_node)
    for node in all_nodes:
        # Match identifier-type nodes whose text equals the target name
        if node.type not in _IDENTIFIER_TYPES:
            continue
        if _node_text(node) != name:
            continue

        lineno = _line_of_node(node)
        if lineno in seen_lines:
            continue
        seen_lines.add(lineno)

        line_text = source_lines[lineno - 1].strip() if 0 < lineno <= len(source_lines) else ""
        results.append((lineno, line_text))

    return results


def _node_defines_name(node: Any, name: str) -> bool:
    """
    Check whether a definition AST node defines the symbol *name*.

    Inspects the node's 'name' field and its immediate children for
    identifier nodes matching *name*.
    """
    # Strategy 1: check named fields
    for field in _NAME_FIELDS:
        try:
            child = node.child_by_field_name(field)
        except Exception:
            child = None
        if child is not None:
            # The field might be a declarator node — recurse one level
            if _node_text(child) == name:
                return True
            # For Go type_declaration, the name might be nested
            for sub in getattr(child, "children", []):
                if sub.type in _IDENTIFIER_TYPES and _node_text(sub) == name:
                    return True
                # One more level for wrapped declarators
                for sub2 in getattr(sub, "children", []):
                    if sub2.type in _IDENTIFIER_TYPES and _node_text(sub2) == name:
                        return True

    # Strategy 2: scan immediate children for identifier matching name
    for child in getattr(node, "children", []):
        if child.type in _IDENTIFIER_TYPES and _node_text(child) == name:
            return True
        # Handle Go's declaration spec (e.g. type Foo struct { ... })
        for sub in getattr(child, "children", []):
            if sub.type in _IDENTIFIER_TYPES and _node_text(sub) == name:
                return True

    return False
