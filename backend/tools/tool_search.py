"""Deferred tool discovery bridge.

Core tools stay directly visible to the model. Optional or connector tools are
searched and invoked through this small bridge so the live registry remains the
single source of truth.
"""

from __future__ import annotations

import json
import math
import re
import html
from dataclasses import dataclass, field
from typing import Any

from backend.tools.catalog import BRIDGE_TOOL_NAMES, tool_spec_for
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec


TOKEN_RE = re.compile(r"[A-Za-z0-9_\-\.\u4e00-\u9fff]+")
SELECT_PREFIX_RE = re.compile(r"^\s*select\s*:\s*(?P<names>.+?)\s*$", re.IGNORECASE)
DEFAULT_DEFERRED_TOOL_PROMPT_LIMIT = 80
DEFAULT_DEFERRED_CATALOG_SCOPE = "default"


@dataclass
class DeferredToolEntry:
    name: str
    description: str
    permission: str
    read_only: bool
    catalog_text: str = ""
    tokens: list[str] = field(default_factory=list)


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in TOKEN_RE.findall(text.lower()):
        tokens.extend(part for part in re.split(r"[_\-.]+", raw) if part)
    return tokens


def _bool_arg(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


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
    def __init__(
        self,
        registry: Any,
        *,
        toolset_policy: Any | None = None,
        permission_checker: Any | None = None,
        permission_context: Any | None = None,
        scope: str = DEFAULT_DEFERRED_CATALOG_SCOPE,
    ) -> None:
        self.registry = registry
        self.toolset_policy = toolset_policy
        self.permission_checker = permission_checker
        self.permission_context = permission_context
        self.scope = str(scope or DEFAULT_DEFERRED_CATALOG_SCOPE).strip() or DEFAULT_DEFERRED_CATALOG_SCOPE
        self._entries_cache: list[DeferredToolEntry] | None = None

    def entries(self) -> list[DeferredToolEntry]:
        if self._entries_cache is None:
            self._entries_cache = self._build_entries()
        return list(self._entries_cache)

    def _build_entries(self) -> list[DeferredToolEntry]:
        # Derive from the registry's ToolSchemaView (single source of truth) when
        # available, falling back to per-tool spec computation otherwise.
        build_views = getattr(self.registry, "build_schema_views", None)
        if callable(build_views):
            entries: list[DeferredToolEntry] = []
            for view in build_views(
                toolset_policy=self.toolset_policy,
                permission_checker=self.permission_checker,
                permission_context=self.permission_context,
                materialize_schema=False,
            ):
                if view.name in BRIDGE_TOOL_NAMES:
                    continue
                if (
                    view.exposure != "deferred"
                    or view.direct
                    or not bool(getattr(view, "schema_available", False))
                ):
                    continue
                meta = view.runtime_metadata or {}
                if not deferred_catalog_scope_allows(meta, self.scope):
                    continue
                catalog_text = str(getattr(view, "catalog_text", "") or "")
                entry = DeferredToolEntry(
                    name=str(view.name),
                    description=str(getattr(view, "short_description", "") or catalog_text or view.name),
                    permission=str(meta.get("permission") or "auto"),
                    read_only=bool(meta.get("read_only", False)),
                    catalog_text=catalog_text,
                )
                hint = (view.search_hint or "").strip()
                entry.tokens = _tokenize(catalog_text + (" " + hint if hint else ""))
                entries.append(entry)
            return entries

        # Legacy fallback (registry without build_schema_views).
        entries = []
        for tool in self.registry.get_tools():
            if tool.name in BRIDGE_TOOL_NAMES:
                continue
            spec = tool_spec_for(tool.name, self.registry)
            if self.toolset_policy is not None and not self.toolset_policy.is_available(spec):
                continue
            if spec.exposure != "deferred":
                continue
            meta = tool.to_runtime_metadata() if hasattr(tool, "to_runtime_metadata") else {}
            if not deferred_catalog_scope_allows(meta, self.scope):
                continue
            description = str(getattr(tool, "description", "") or "")
            catalog_text = " ".join(
                str(part)
                for part in (
                    tool.name,
                    description,
                    getattr(tool, "search_hint", "") or "",
                    getattr(spec, "capability", "") or "",
                    " ".join(getattr(spec, "required_args", ()) or ()),
                )
                if str(part).strip()
            )
            entry = DeferredToolEntry(
                name=str(tool.name),
                description=description,
                permission=tool.permission.value,
                read_only=bool(tool.read_only),
                catalog_text=catalog_text,
            )
            entry.tokens = _tokenize(catalog_text)
            entries.append(entry)
        return entries

    def search(self, query: str, limit: int) -> list[DeferredToolEntry]:
        catalog = self.entries()
        selected = self._select_entries(query, catalog, limit)
        if selected is not None:
            return selected

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

    def _select_entries(
        self,
        query: str,
        catalog: list[DeferredToolEntry],
        limit: int,
    ) -> list[DeferredToolEntry] | None:
        """Return exact select: matches, or None when query is not selection.

        Mirrors cc's ToolSearch "select:Read,Edit" contract. Exact selection is
        deliberately forgiving on case and comma/space separators so a model can
        recover from seeing a tool name in a previous turn without paying a BM25
        round trip.
        """
        names: list[str]
        match = SELECT_PREFIX_RE.match(query)
        if match:
            raw_names = match.group("names")
            names = [part.strip() for part in re.split(r"[,\n]+", raw_names) if part.strip()]
        else:
            names = [query.strip()]

        if not names:
            return [] if match else None

        by_exact = {entry.name: entry for entry in catalog}
        by_lower = {entry.name.lower(): entry for entry in catalog}
        selected: list[DeferredToolEntry] = []
        seen: set[str] = set()
        for name in names:
            entry = by_exact.get(name) or by_lower.get(name.lower())
            if entry is None:
                if not match:
                    return None
                continue
            if entry.name in seen:
                continue
            selected.append(entry)
            seen.add(entry.name)
            if len(selected) >= limit:
                break

        if selected:
            return selected
        return [] if match else None


def build_deferred_tools_prompt_block(
    registry: Any,
    *,
    toolset_policy: Any | None = None,
    permission_checker: Any | None = None,
    permission_context: Any | None = None,
    scope: str = DEFAULT_DEFERRED_CATALOG_SCOPE,
    limit: int = DEFAULT_DEFERRED_TOOL_PROMPT_LIMIT,
) -> str:
    """Return a cc-style lightweight directory of deferred tool names.

    The model needs the names to use ``select:tool_name`` without a keyword
    discovery round trip, but it must not pay for full JSON schemas until a
    deferred tool is actually selected. This block intentionally contains names
    only; search hints, descriptions, and schemas stay behind tool_search and
    tool_describe.
    """
    catalog = DeferredToolCatalog(
        registry,
        toolset_policy=toolset_policy or _toolset_policy_for_context(permission_context),
        permission_checker=permission_checker,
        permission_context=permission_context,
        scope=scope,
    )
    names = sorted({entry.name for entry in catalog.entries() if str(entry.name or "").strip()})
    if not names:
        return ""
    safe_limit = max(1, min(int(limit or DEFAULT_DEFERRED_TOOL_PROMPT_LIMIT), 200))
    shown = names[:safe_limit]
    lines = [html.escape(name, quote=False) for name in shown]
    if len(names) > len(shown):
        lines.append(
            f"... {len(names) - len(shown)} more deferred tools; use tool_search keywords."
        )
    return (
        f'<available_deferred_tools total="{len(names)}">\n'
        + "\n".join(lines)
        + "\n</available_deferred_tools>"
    )


def deferred_catalog_scope_allows(metadata: dict[str, Any], scope: str) -> bool:
    raw = metadata.get("deferred_catalog_scopes")
    if isinstance(raw, str):
        scopes = {part.strip() for part in raw.split(",") if part.strip()}
    elif isinstance(raw, (list, tuple, set, frozenset)):
        scopes = {str(part).strip() for part in raw if str(part).strip()}
    else:
        scopes = {DEFAULT_DEFERRED_CATALOG_SCOPE}
    return "*" in scopes or scope in scopes


def _toolset_policy_for_context(permission_context: Any | None) -> Any | None:
    try:
        from backend.tools.subagent_context import (
            is_subagent_permission_context,
            subagent_toolset_policy,
        )
    except Exception:
        return None
    if is_subagent_permission_context(permission_context):
        return subagent_toolset_policy()
    return None


class ToolSearchTool(BaseTool):
    name = "tool_search"
    read_only = True
    permission = PermissionLevel.AUTO
    result_kind = "generic"
    activity_kind = "genericTool"
    display_label = "Find tools"
    description = (
        "Search deferred tools that are not directly listed in the current tool set. "
        "When to use: the directly listed tools do not cover a needed capability such as browser/desktop control, document work, connector actions, optional MCP tools, or specialized workflows. "
        "Use 'select:tool_name' for exact tools. Results include full schemas by default so matching tools can be invoked with tool_call; if a result lacks a schema, use tool_describe before tool_call. Use include_schemas=false only for lightweight diagnostics."
    )

    def model_description(self) -> str:
        return (
            "Search deferred tools not directly listed this turn; results include schemas for tool_call."
        )

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        )

    def __init__(self, registry: Any | None = None) -> None:
        self._registry = registry

    def update_index(self, tools: list[BaseTool], registry: Any | None = None) -> None:
        if registry is not None:
            self._registry = registry

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="tool.discovery",
            toolset="core",
            exposure="core",
            required_args=("query",),
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Capability keywords or exact names, e.g. 'browser click' or 'select:tool_a,tool_b'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matches to return. Default 5.",
                    },
                    "include_schemas": {
                        "type": "boolean",
                        "description": "Include full schemas in the result. Default true; set false only for lightweight diagnostics.",
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
        include_schemas = True
        if "include_schemas" in args:
            include_schemas = _bool_arg(args.get("include_schemas"))
        if SELECT_PREFIX_RE.match(query):
            include_schemas = True

        catalog = DeferredToolCatalog(
            self._registry,
            toolset_policy=_toolset_policy_for_context(getattr(context, "permission", None)),
            permission_checker=getattr(context, "permission_checker", None),
            permission_context=getattr(context, "permission", None),
        )
        matches = catalog.search(query, limit)
        total = len(catalog.entries())
        permission_checker = getattr(context, "permission_checker", None)
        permission_context = getattr(context, "permission", None)
        payload = {
            "query": query,
            "total_available": total,
            "matches": [
                self._match_payload(
                    entry,
                    include_schema=include_schemas,
                    permission_checker=permission_checker,
                    permission_context=permission_context,
                )
                for entry in matches
            ],
        }
        return ToolResult(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            display_summary=f"Found {len(matches)} deferred tools",
            result_kind="generic",
        )

    def _match_payload(
        self,
        entry: DeferredToolEntry,
        *,
        include_schema: bool,
        permission_checker: Any | None,
        permission_context: Any | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": entry.name,
            "permission": entry.permission,
            "read_only": entry.read_only,
        }
        if not include_schema:
            payload["description"] = entry.description[:400]
            return payload
        load_schema = getattr(self._registry, "get_tool_schema", None)
        if not callable(load_schema):
            payload["description"] = entry.description[:400]
            return payload
        schema = load_schema(
            entry.name,
            toolset_policy=_toolset_policy_for_context(permission_context),
            permission_checker=permission_checker,
            permission_context=permission_context,
            require_deferred=True,
        )
        if schema is not None:
            payload["schema"] = schema
        else:
            payload["description"] = entry.description[:400]
        return payload


class ToolDescribeTool(BaseTool):
    name = "tool_describe"
    read_only = True
    permission = PermissionLevel.AUTO
    description = (
        "Load the full JSON schema for one deferred tool returned by tool_search. "
        "This schema is required before tool_call unless tool_search already returned it. "
        "Do not infer arguments from the short search result."
    )

    def model_description(self) -> str:
        return "Load the full schema for one deferred tool before tool_call."

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="tool.describe",
            toolset="core",
            exposure="core",
            required_args=("name",),
        )

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

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            },
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        name = str(args.get("name") or "").strip()
        if not name:
            return self._error_result("name is required")
        permission_checker = getattr(context, "permission_checker", None)
        permission_context = getattr(context, "permission", None)
        entry = DeferredToolCatalog(
            self._registry,
            toolset_policy=_toolset_policy_for_context(permission_context),
            permission_checker=permission_checker,
            permission_context=permission_context,
        ).get(name)
        if entry is None:
            return self._error_result(f"'{name}' is not an available deferred tool")
        load_schema = getattr(self._registry, "get_tool_schema", None)
        if not callable(load_schema):
            return self._error_result("Tool registry cannot load deferred schemas")
        schema = load_schema(
            entry.name,
            toolset_policy=_toolset_policy_for_context(permission_context),
            permission_checker=permission_checker,
            permission_context=permission_context,
            require_deferred=True,
        )
        if schema is None:
            return self._error_result(f"'{name}' is not an available deferred tool")
        return ToolResult(
            content=json.dumps(schema, ensure_ascii=False, indent=2),
            display_summary=f"Loaded schema for {name}",
            result_kind="generic",
        )

class ToolCallTool(BaseTool):
    name = "tool_call"
    result_kind = "generic"
    activity_kind = "genericTool"
    display_label = "Call tool"
    read_only = False
    permission = PermissionLevel.AUTO
    description = (
        "Invoke a deferred tool by exact name with arguments matching the schema returned by tool_search or tool_describe. "
        "Use only after selecting a deferred tool and loading its schema. "
        "Do not use this for directly listed tools; call those tools directly."
    )

    def model_description(self) -> str:
        return "Invoke a deferred tool by exact name with arguments matching its loaded schema."

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="tool.call",
            toolset="core",
            exposure="core",
            required_args=("name", "arguments"),
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

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["name", "arguments"],
            },
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        return self._error_result("tool_call must be unwrapped by the agent runtime before execution")
