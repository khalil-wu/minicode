"""Deferred tool discovery bridge.

Core tools stay directly visible to the model. Optional or connector tools are
searched and invoked through this small bridge so the live registry remains the
single source of truth.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from backend.tools.catalog import BRIDGE_TOOL_NAMES, tool_spec_for
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema


TOKEN_RE = re.compile(r"[A-Za-z0-9_\-\.\u4e00-\u9fff]+")


@dataclass
class DeferredToolEntry:
    name: str
    description: str
    schema: dict[str, Any]
    permission: str
    read_only: bool
    tokens: list[str] = field(default_factory=list)


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in TOKEN_RE.findall(text.lower()):
        tokens.extend(part for part in re.split(r"[_\-.]+", raw) if part)
    return tokens


def _schema_search_text(schema: dict[str, Any]) -> str:
    function = schema.get("function") if isinstance(schema, dict) else {}
    if not isinstance(function, dict):
        return ""
    params = function.get("parameters") or {}
    properties = params.get("properties") if isinstance(params, dict) else {}
    param_names = " ".join(properties.keys()) if isinstance(properties, dict) else ""
    return f"{function.get('name', '')} {function.get('description', '')} {param_names}"


def _bm25_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    doc_freq: dict[str, int],
    avg_dl: float,
    doc_count: int,
) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    tf: dict[str, int] = {}
    for token in doc_tokens:
        tf[token] = tf.get(token, 0) + 1
    score = 0.0
    k1 = 1.5
    b = 0.75
    dl = len(doc_tokens)
    for token in query_tokens:
        freq = tf.get(token, 0)
        if not freq:
            continue
        df = doc_freq.get(token, 0)
        idf = math.log(1 + (doc_count - df + 0.5) / (df + 0.5))
        norm = freq * (k1 + 1) / (freq + k1 * (1 - b + b * dl / max(avg_dl, 1.0)))
        score += idf * norm
    return score


class DeferredToolCatalog:
    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def entries(self) -> list[DeferredToolEntry]:
        # Derive from the registry's ToolSchemaView (single source of truth) when
        # available, falling back to per-tool spec computation otherwise.
        build_views = getattr(self.registry, "build_schema_views", None)
        if callable(build_views):
            entries: list[DeferredToolEntry] = []
            for view in build_views():
                if view.name in BRIDGE_TOOL_NAMES:
                    continue
                if view.exposure != "deferred" or view.direct or view.schema is None:
                    continue
                function = view.schema.get("function") or {}
                meta = view.runtime_metadata or {}
                entry = DeferredToolEntry(
                    name=str(function.get("name") or view.name),
                    description=str(function.get("description") or ""),
                    schema=view.schema,
                    permission=str(meta.get("permission") or "auto"),
                    read_only=bool(meta.get("read_only", False)),
                )
                hint = (view.search_hint or "").strip()
                entry.tokens = _tokenize(_schema_search_text(view.schema) + (" " + hint if hint else ""))
                entries.append(entry)
            return entries

        # Legacy fallback (registry without build_schema_views).
        entries = []
        for tool in self.registry.get_tools():
            if tool.name in BRIDGE_TOOL_NAMES:
                continue
            spec = tool_spec_for(tool.name, self.registry)
            if spec.exposure != "deferred":
                continue
            schema = tool.get_schema().to_openai_tool()
            function = schema.get("function") or {}
            entry = DeferredToolEntry(
                name=str(function.get("name") or tool.name),
                description=str(function.get("description") or ""),
                schema=schema,
                permission=tool.permission.value,
                read_only=bool(tool.read_only),
            )
            entry.tokens = _tokenize(_schema_search_text(schema))
            entries.append(entry)
        return entries

    def search(self, query: str, limit: int) -> list[DeferredToolEntry]:
        catalog = self.entries()
        query_tokens = _tokenize(query)
        if not catalog or not query_tokens:
            return []

        doc_freq: dict[str, int] = {}
        for entry in catalog:
            for token in set(entry.tokens):
                doc_freq[token] = doc_freq.get(token, 0) + 1
        avg_dl = sum(len(entry.tokens) for entry in catalog) / max(len(catalog), 1)

        scored: list[tuple[float, DeferredToolEntry]] = []
        for entry in catalog:
            score = _bm25_score(query_tokens, entry.tokens, doc_freq, avg_dl, len(catalog))
            name_lower = entry.name.lower()
            query_lower = query.lower()
            if query_lower and query_lower in name_lower:
                score += 3.0
            if all(token in entry.tokens for token in query_tokens):
                score += 1.0
            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [entry for _, entry in scored[:limit]]

    def get(self, name: str) -> DeferredToolEntry | None:
        for entry in self.entries():
            if entry.name == name:
                return entry
        return None


class ToolSearchTool(BaseTool):
    name = "tool_search"
    read_only = True
    permission = PermissionLevel.AUTO
    description = (
        "Search deferred tools that are not directly listed in the current tool set.\n\n"
        "When to use: the user asks for a capability that the directly listed tools do not cover, such as "
        "browser/desktop control, document or spreadsheet work, connector-specific actions, optional MCP tools, "
        "or another specialized workflow. Search by capability keywords or exact tool names.\n\n"
        "When not to use: a direct tool already covers the request, or you only need workspace read/edit/search/"
        "command tools.\n\n"
        "Returned matches are discovery only. Until a tool's full schema is loaded with tool_describe, you do "
        "not know its parameters and must not claim it was used. Use tool_describe before tool_call unless the "
        "schema was already loaded in this turn."
    )

    def __init__(self, registry: Any | None = None) -> None:
        self._registry = registry

    def update_index(self, tools: list[BaseTool], registry: Any | None = None) -> None:
        if registry is not None:
            self._registry = registry

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Capability keywords or exact tool names, for example 'browser click', "
                            "'spreadsheet chart', 'PDF render', or 'select:tool_name'."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matches to return. Default 5.",
                    },
                },
                "required": ["query"],
            },
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        if self._registry is None:
            return self._error_result("Tool registry is not available")
        query = str(args.get("query") or "").strip()
        if not query:
            return self._error_result("query is required")
        limit = max(1, min(int(args.get("limit") or 5), 20))

        catalog = DeferredToolCatalog(self._registry)
        matches = catalog.search(query, limit)
        total = len(catalog.entries())
        payload = {
            "query": query,
            "total_available": total,
            "matches": [
                {
                    "name": entry.name,
                    "description": entry.description[:400],
                    "permission": entry.permission,
                    "read_only": entry.read_only,
                }
                for entry in matches
            ],
        }
        return ToolResult(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            display_summary=f"Found {len(matches)} deferred tools",
            result_kind="generic",
        )


class ToolDescribeTool(BaseTool):
    name = "tool_describe"
    read_only = True
    permission = PermissionLevel.AUTO
    description = (
        "Load the full JSON schema for one deferred tool returned by tool_search. "
        "This is required before tool_call unless the schema is already known from the current turn. "
        "Do not infer arguments from the short search result."
    )

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact deferred tool name."},
                },
                "required": ["name"],
            },
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        name = str(args.get("name") or "").strip()
        if not name:
            return self._error_result("name is required")
        entry = DeferredToolCatalog(self._registry).get(name)
        if entry is None:
            return self._error_result(f"'{name}' is not an available deferred tool")
        return ToolResult(
            content=json.dumps(entry.schema, ensure_ascii=False, indent=2),
            display_summary=f"Loaded schema for {name}",
            result_kind="generic",
        )


class ToolCallTool(BaseTool):
    name = "tool_call"
    read_only = False
    permission = PermissionLevel.AUTO
    description = (
        "Invoke a deferred tool by exact name with arguments matching the schema returned by tool_describe. "
        "Use only after selecting a relevant deferred tool and loading its schema. "
        "Do not use this for directly listed tools; call those tools directly."
    )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact deferred tool name."},
                    "arguments": {"type": "object", "description": "Arguments for the deferred tool."},
                },
                "required": ["name", "arguments"],
            },
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        return self._error_result("tool_call must be unwrapped by the agent runtime before execution")
