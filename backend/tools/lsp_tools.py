"""
LSP-enhanced code navigation tools.

These tools wrap the lightweight LSP client to provide precise, language-server-
backed code navigation. When a language server is available for the target file's
language, the LSP is used for precise results. When no server is available, the
tools fail with an actionable install hint; the existing AST tools remain
available separately for symbol-name search.

Tools provided:
  - lsp_go_to_definition: Jump to symbol definition using LSP
  - lsp_find_references: Find all references to a symbol using LSP
  - lsp_hover: Get hover/type information for a symbol using LSP
  - lsp_document_symbols: List all symbols in a document using LSP

These augment (not replace) the existing ast_tools.py GoToDefinition / FindReferences
tools.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from backend.lsp.client import LSPLocation, get_lsp_manager
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import (
    BaseTool,
    PermissionLevel,
    ToolResult,
    ToolSchema,
)
from backend.tools.contracts import ToolSpec

logger = logging.getLogger(__name__)

_MAX_LOCATIONS = 30
_MAX_HOVER_CHARS = 4_000
_MAX_SYMBOLS = 100
_PROJECT_MARKERS = (".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod", ".vscode")


class LSPGoToDefinitionTool(BaseTool):
    """Jump to a symbol's definition using a language server."""

    name = "lsp_go_to_definition"
    description = (
        "Use a Language Server (pyright, tsserver, gopls, etc.) to find the precise "
        "definition location of a symbol at the given file:line:character. "
        "More accurate than AST-based go_to_definition when a language server is installed."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    open_world = False
    timeout_seconds = 30.0
    result_kind = "code"
    activity_kind = "genericTool"
    panel_hint = "editor"
    display_label = "LSP Go to Definition"
    max_result_chars = 8_000

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="code.lsp.definition",
            toolset="code",
            exposure="deferred",
            required_args=("file_path", "line", "character"),
            arg_roles={"file_path": "resource", "line": "control", "character": "control", "line_base": "control"},
            repair_policy={"file_path": "resource_resolver"},
            empty_args_policy="block",
            blocked_guidance="Missing file_path, line, or character.",
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the source file.",
                    },
                    "line": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Line number. Interpreted as 0-based unless line_base is 1.",
                    },
                    "character": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "0-based character offset in the line.",
                    },
                    "line_base": {
                        "type": "integer",
                        "enum": [0, 1],
                        "description": "Set to 1 when passing a human/editor 1-based line number. Defaults to 0.",
                    },
                    "workspace_root": {
                        "type": "string",
                        "description": "Workspace root for the language server. Defaults to the file's directory.",
                    },
                },
                "required": ["file_path", "line", "character"],
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        file_path = str(args.get("file_path") or "").strip()
        if not file_path:
            return self._error_result("Missing file_path")

        position, error = _parse_position(args)
        if error:
            return self._error_result(error)
        line, character = position
        workspace_root = _workspace_root_for(file_path, args.get("workspace_root"))

        manager = get_lsp_manager()
        if not manager.is_available(file_path):
            return ToolResult(
                content=f"No language server available for {Path(file_path).suffix} files. "
                        f"Install pyright (pip install pyright), typescript-language-server (npm i -g typescript-language-server), "
                        f"gopls, rust-analyzer, or clangd as needed.",
                is_error=True,
                display_summary="LSP not available",
            )

        client = await manager.get_client(file_path, workspace_root)
        if client is None:
            return ToolResult(
                content="Failed to start language server.",
                is_error=True,
                display_summary="LSP failed to start",
            )

        try:
            locations = await client.definition(file_path, line, character)
        except Exception as exc:
            logger.debug("LSP definition failed: %s", exc)
            return self._error_result(f"LSP definition failed: {exc}")

        if not locations:
            return ToolResult(
                content="No definition found at the given position.",
                display_summary="No definition found",
            )

        return ToolResult(
            content=_format_locations("Definition", locations[:_MAX_LOCATIONS]),
            display_summary=f"Definition: {_short_path(locations[0])}" if locations else "",
        )


class LSPFindReferencesTool(BaseTool):
    """Find all references to a symbol using a language server."""

    name = "lsp_find_references"
    description = (
        "Use a Language Server to find all references to the symbol at the given "
        "file:line:character position. Returns a list of file:line:char locations. "
        "More accurate than AST-based find_references."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    open_world = False
    timeout_seconds = 30.0
    result_kind = "code"
    activity_kind = "genericTool"
    panel_hint = "editor"
    display_label = "LSP Find References"
    max_result_chars = 12_000

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="code.lsp.references",
            toolset="code",
            exposure="deferred",
            required_args=("file_path", "line", "character"),
            arg_roles={"file_path": "resource", "line": "control", "character": "control", "line_base": "control"},
            repair_policy={"file_path": "resource_resolver"},
            empty_args_policy="block",
            blocked_guidance="Missing file_path, line, or character.",
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the source file."},
                    "line": {"type": "integer", "minimum": 0, "description": "Line number. Interpreted as 0-based unless line_base is 1."},
                    "character": {"type": "integer", "minimum": 0, "description": "0-based character offset."},
                    "line_base": {
                        "type": "integer",
                        "enum": [0, 1],
                        "description": "Set to 1 when passing a human/editor 1-based line number. Defaults to 0.",
                    },
                    "workspace_root": {"type": "string", "description": "Workspace root. Defaults to auto-detected project root."},
                },
                "required": ["file_path", "line", "character"],
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        file_path = str(args.get("file_path") or "").strip()
        if not file_path:
            return self._error_result("Missing file_path")

        position, error = _parse_position(args)
        if error:
            return self._error_result(error)
        line, character = position
        workspace_root = _workspace_root_for(file_path, args.get("workspace_root"))

        manager = get_lsp_manager()
        if not manager.is_available(file_path):
            return ToolResult(
                content=f"No language server available for {Path(file_path).suffix} files.",
                is_error=True,
                display_summary="LSP not available",
            )

        client = await manager.get_client(file_path, workspace_root)
        if client is None:
            return ToolResult(
                content="Failed to start language server.",
                is_error=True,
                display_summary="LSP failed to start",
            )

        try:
            locations = await client.references(file_path, line, character)
        except Exception as exc:
            logger.debug("LSP references failed: %s", exc)
            return self._error_result(f"LSP references failed: {exc}")

        if not locations:
            return ToolResult(
                content="No references found at the given position.",
                display_summary="No references found",
            )

        return ToolResult(
            content=_format_locations("References", locations[:_MAX_LOCATIONS]),
            display_summary=f"References: {len(locations)} found",
        )


class LSPHoverTool(BaseTool):
    """Get hover/type information for a symbol using a language server."""

    name = "lsp_hover"
    description = (
        "Use a Language Server to get hover information (type signature, documentation) "
        "for the symbol at the given file:line:character position."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    open_world = False
    timeout_seconds = 20.0
    result_kind = "code"
    activity_kind = "genericTool"
    panel_hint = "editor"
    display_label = "LSP Hover"
    max_result_chars = 6_000

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="code.lsp.hover",
            toolset="code",
            exposure="deferred",
            required_args=("file_path", "line", "character"),
            arg_roles={"file_path": "resource", "line": "control", "character": "control", "line_base": "control"},
            repair_policy={"file_path": "resource_resolver"},
            empty_args_policy="block",
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the source file."},
                    "line": {"type": "integer", "minimum": 0, "description": "Line number. Interpreted as 0-based unless line_base is 1."},
                    "character": {"type": "integer", "minimum": 0, "description": "0-based character offset."},
                    "line_base": {
                        "type": "integer",
                        "enum": [0, 1],
                        "description": "Set to 1 when passing a human/editor 1-based line number. Defaults to 0.",
                    },
                    "workspace_root": {"type": "string", "description": "Workspace root."},
                },
                "required": ["file_path", "line", "character"],
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        file_path = str(args.get("file_path") or "").strip()
        if not file_path:
            return self._error_result("Missing file_path")

        position, error = _parse_position(args)
        if error:
            return self._error_result(error)
        line, character = position
        workspace_root = _workspace_root_for(file_path, args.get("workspace_root"))

        manager = get_lsp_manager()
        if not manager.is_available(file_path):
            return ToolResult(
                content=f"No language server available for {Path(file_path).suffix} files.",
                is_error=True,
                display_summary="LSP not available",
            )

        client = await manager.get_client(file_path, workspace_root)
        if client is None:
            return ToolResult(
                content="Failed to start language server.",
                is_error=True,
                display_summary="LSP failed to start",
            )

        try:
            hover = await client.hover(file_path, line, character)
        except Exception as exc:
            logger.debug("LSP hover failed: %s", exc)
            return self._error_result(f"LSP hover failed: {exc}")

        if not hover or not hover.contents:
            return ToolResult(
                content="No hover information available at the given position.",
                display_summary="No hover info",
            )

        contents = hover.contents[:_MAX_HOVER_CHARS]
        return ToolResult(
            content=f"Hover at {file_path}:{line + 1}:{character + 1}:\n\n{contents}",
            display_summary="Hover info",
        )


class LSPDocumentSymbolsTool(BaseTool):
    """List all symbols in a document using a language server."""

    name = "lsp_document_symbols"
    description = (
        "Use a Language Server to list all symbols (functions, classes, variables) "
        "in a source file. Returns a structured outline with names, kinds, and line numbers."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    open_world = False
    timeout_seconds = 20.0
    result_kind = "code"
    activity_kind = "genericTool"
    panel_hint = "editor"
    display_label = "LSP Document Symbols"
    max_result_chars = 10_000

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="code.lsp.symbols",
            toolset="code",
            exposure="deferred",
            required_args=("file_path",),
            arg_roles={"file_path": "resource"},
            repair_policy={"file_path": "resource_resolver"},
            empty_args_policy="block",
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the source file."},
                    "workspace_root": {"type": "string", "description": "Workspace root."},
                },
                "required": ["file_path"],
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        file_path = str(args.get("file_path") or "").strip()
        if not file_path:
            return self._error_result("Missing file_path")

        workspace_root = _workspace_root_for(file_path, args.get("workspace_root"))

        manager = get_lsp_manager()
        if not manager.is_available(file_path):
            return ToolResult(
                content=f"No language server available for {Path(file_path).suffix} files.",
                is_error=True,
                display_summary="LSP not available",
            )

        client = await manager.get_client(file_path, workspace_root)
        if client is None:
            return ToolResult(
                content="Failed to start language server.",
                is_error=True,
                display_summary="LSP failed to start",
            )

        try:
            symbols = await client.document_symbols(file_path)
        except Exception as exc:
            logger.debug("LSP document symbols failed: %s", exc)
            return self._error_result(f"LSP document symbols failed: {exc}")

        if not symbols:
            return ToolResult(
                content="No symbols found in the document.",
                display_summary="No symbols",
            )

        lines = [f"Document symbols for {file_path}:", ""]
        for sym in symbols[:_MAX_SYMBOLS]:
            lines.append(f"  {_symbol_kind_name(sym.kind)} {sym.name}  (line {sym.line + 1})")
            for child in sym.children[:10]:
                lines.append(f"    {_symbol_kind_name(child.kind)} {child.name}  (line {child.line + 1})")

        return ToolResult(
            content="\n".join(lines),
            display_summary=f"Symbols: {len(symbols)} top-level",
        )


# ── Helpers ─────────────────────────────────────────────────────────

_SYMBOL_KINDS = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
    6: "Method", 7: "Property", 8: "Field", 9: "Constructor", 10: "Enum",
    11: "Interface", 12: "Function", 13: "Variable", 14: "Constant",
    15: "String", 16: "Number", 17: "Boolean", 18: "Array", 19: "Object",
    20: "Key", 21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
    25: "Operator", 26: "TypeParameter",
}


def _symbol_kind_name(kind: int) -> str:
    return _SYMBOL_KINDS.get(kind, "Symbol")


def _parse_position(args: dict[str, Any]) -> tuple[tuple[int, int], str]:
    try:
        line = int(args.get("line"))
        character = int(args.get("character"))
    except (TypeError, ValueError):
        return (0, 0), "line and character must be integers"
    if line < 0 or character < 0:
        return (0, 0), "line and character must be non-negative"

    try:
        line_base = int(args.get("line_base", 0))
    except (TypeError, ValueError):
        return (0, 0), "line_base must be 0 or 1"
    if line_base not in {0, 1}:
        return (0, 0), "line_base must be 0 or 1"
    if line_base == 1:
        if line < 1:
            return (0, 0), "line must be at least 1 when line_base is 1"
        line -= 1
    return (line, character), ""


def _workspace_root_for(file_path: str, explicit_root: Any) -> str:
    workspace_root = str(explicit_root or "").strip()
    if workspace_root:
        return workspace_root
    path = Path(file_path)
    workspace_root = str(path.parent)
    for parent in path.resolve().parents:
        if any((parent / marker).exists() for marker in _PROJECT_MARKERS):
            return str(parent)
    return workspace_root


def _format_locations(title: str, locations: list[LSPLocation]) -> str:
    lines = [f"{title} ({len(locations)} location(s)):", ""]
    for i, loc in enumerate(locations, 1):
        lines.append(f"{i}. {loc.to_display()}")
    return "\n".join(lines)


def _short_path(loc: LSPLocation) -> str:
    path = Path(loc.file)
    return f"{path.name}:{loc.line + 1}"
