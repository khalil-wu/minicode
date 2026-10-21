"""Codex's deferred BM25 discovery and native tool-search result contract."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import math
from typing import Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.catalog import BRIDGE_TOOL_NAMES
from backend.tools.contracts import ToolSpec
from backend.tools.source_search_tokenizer import tokenize as _tokenize

TOOL_SEARCH_DEFAULT_LIMIT = 8
MAX_TOOL_SEARCH_SOURCE_DESCRIPTION_BYTES = 512 * 1024
DEFAULT_DEFERRED_CATALOG_SCOPE = "default"


@dataclass
class DeferredToolEntry:
    name: str
    description: str
    tokens: list[str] = field(default_factory=list)
    code_mode_available: bool = True


def _bm25_score(query_tokens: list[str], doc_tokens: list[str], doc_freq: dict[str, int],
                avg_dl: float, doc_count: int) -> float:
    # bm25 2.3.2 EmbedderBuilder + Scorer, with the English fitted corpus.
    tf = Counter(doc_tokens)
    avg_dl = avg_dl if avg_dl > 0 else 256.0
    score = 0.0
    for token in query_tokens:
        freq = tf[token]
        if freq:
            df = doc_freq[token]
            idf = math.log(1 + (doc_count - df + 0.5) / (df + 0.5))
            score += idf * freq * 2.2 / (freq + 1.2 * (0.25 + 0.75 * len(doc_tokens) / avg_dl))
    return score


class DeferredToolCatalog:
    def __init__(self, registry: Any, *, toolset_policy: Any | None = None,
                 permission_checker: Any | None = None, permission_context: Any | None = None,
                 scope: str = DEFAULT_DEFERRED_CATALOG_SCOPE) -> None:
        self.registry = registry
        self.toolset_policy = toolset_policy
        self.permission_checker = permission_checker
        self.permission_context = permission_context
        self.scope = scope
        self._entries_cache: list[DeferredToolEntry] | None = None
        self._doc_freq: dict[str, int] = {}
        self._avg_dl = 0.0

    def entries(self) -> list[DeferredToolEntry]:
        if self._entries_cache is None:
            self._entries_cache = self._build_entries()
            for entry in self._entries_cache:
                for token in set(entry.tokens):
                    self._doc_freq[token] = self._doc_freq.get(token, 0) + 1
            if self._entries_cache:
                self._avg_dl = sum(len(entry.tokens) for entry in self._entries_cache) / len(self._entries_cache)
        return list(self._entries_cache)

    def _build_entries(self) -> list[DeferredToolEntry]:
        entries = []
        for view in self.registry.build_schema_views(toolset_policy=self.toolset_policy,
                permission_checker=self.permission_checker, permission_context=self.permission_context,
                materialize_schema=False):
            if (view.name in BRIDGE_TOOL_NAMES or view.exposure not in {"deferred", "deferred_model_only"}
                    or not view.schema_available or not deferred_catalog_scope_allows(view.runtime_metadata, self.scope)):
                continue
            entries.append(DeferredToolEntry(name=view.name, description=view.short_description,
                tokens=_tokenize(view.catalog_text), code_mode_available=view.code_mode_available))
        return entries

    def search(self, query: str, limit: int, *, nested: bool = False) -> list[DeferredToolEntry]:
        entries = self.entries()
        query_tokens = _tokenize(query)
        scored = [(_bm25_score(query_tokens, entry.tokens, self._doc_freq, self._avg_dl, len(entries)), entry)
                  for entry in entries if not nested or entry.code_mode_available]
        scored.sort(key=lambda item: -item[0])
        return [entry for score, entry in scored if score > 0][:limit]

    def get(self, name: str) -> DeferredToolEntry | None:
        return next((entry for entry in self.entries() if entry.name == name), None)


def deferred_catalog_scope_allows(metadata: dict[str, Any], scope: str) -> bool:
    raw = metadata.get("deferred_catalog_scopes")
    if isinstance(raw, str):
        scopes = {part.strip() for part in raw.split(",") if part.strip()}
    elif isinstance(raw, (list, tuple, set, frozenset)):
        scopes = set(raw)
    else:
        scopes = {DEFAULT_DEFERRED_CATALOG_SCOPE}
    return "*" in scopes or scope in scopes


def _toolset_policy_for_context(permission_context: Any | None, metadata: dict[str, Any] | None = None) -> Any:
    from backend.tools.toolset_runtime import resolve_context_toolset_policy
    return resolve_context_toolset_policy(permission_context, metadata)


def _loadable_tools(registry: Any, entries: list[DeferredToolEntry], *, policy: Any,
                    checker: Any, permission: Any) -> list[dict[str, Any]]:
    namespaces: dict[str, dict[str, Any]] = {}
    for entry in entries:
        schema = registry.get_tool_schema(entry.name, toolset_policy=policy,
            permission_checker=checker, permission_context=permission)
        function = schema["function"]
        runtime = registry.get_tool(entry.name)
        from backend.mcp.registry import MCPToolProxy

        namespace_name = runtime.model_namespace if isinstance(runtime, MCPToolProxy) else "functions"
        name = runtime.model_tool_name if isinstance(runtime, MCPToolProxy) else entry.name
        namespace = namespaces.setdefault(namespace_name, {"type": "namespace", "name": namespace_name,
            "description": f"Tools in the {namespace_name} namespace.", "tools": []})
        if "_minicode_freeform" in schema:
            native = schema["_minicode_freeform"]
            tool = {"type": "custom", "name": name, "description": native["description"],
                    "format": deepcopy(native["format"]), "defer_loading": True}
        else:
            tool = {"type": "function", "name": name, "description": function["description"],
                    "parameters": deepcopy(function["parameters"]), "strict": bool(function.get("strict", False)),
                    "defer_loading": True}
        namespace["tools"].append(tool)
    return list(namespaces.values())


class ToolSearchTool(BaseTool):
    name = "tool_search"
    read_only = True
    permission = PermissionLevel.AUTO
    result_kind = "generic"
    activity_kind = "genericTool"
    display_label = "Find tools"
    projection_visibility = "debug"
    description = "Searches over deferred tool metadata with BM25 and exposes matching tools for the next model call."

    def __init__(self, registry: Any | None = None) -> None:
        self._registry = registry
        self._catalog_key: Any = None
        self._catalog: DeferredToolCatalog | None = None

    def model_description(self) -> str:
        return self._description({})

    def model_description_for_policy(self, policy: Any) -> str:
        sources: dict[str, str | None] = {}
        if self._registry is not None:
            for runtime in self._registry.get_tools():
                spec = self._registry.get_tool_spec(runtime.name)
                if spec.exposure not in {"deferred", "deferred_model_only"} or not policy.is_available(spec):
                    continue
                source = getattr(runtime, "server_name", None)
                if source is not None:
                    sources.setdefault(source, None)
        return self._description(sources)

    @staticmethod
    def _description(sources: dict[str, str | None]) -> str:
        reserved = sum(2 + len(name.encode("utf-8")) for name in sources) + max(0, len(sources) - 1)
        budget = max(0, MAX_TOOL_SEARCH_SOURCE_DESCRIPTION_BYTES - reserved)
        lines: list[str] = []
        used = 0
        for name, description in sorted(sources.items()):
            line = "- " + name
            if used + len(line.encode("utf-8")) + bool(lines) > MAX_TOOL_SEARCH_SOURCE_DESCRIPTION_BYTES:
                continue
            if description is not None and budget >= 2:
                bounded = description.encode("utf-8")[:budget - 2].decode("utf-8", errors="ignore")
                line += ": " + bounded
                budget -= 2 + len(bounded.encode("utf-8"))
            used += len(line.encode("utf-8")) + bool(lines)
            lines.append(line)
        listing = "\n".join(lines) if lines else "None currently enabled."
        return ("# Tool discovery\n\nSearches over deferred tool metadata with BM25 and exposes matching tools for the next model call."
                "\n\nYou have access to tools from the following sources:\n" + listing + "\n"
                "Some of the tools may not have been provided to you upfront, and you should use this tool (`tool_search`) to search for the required tools. "
                "For MCP tool discovery, always use `tool_search` instead of `list_mcp_resources` or `list_mcp_resource_templates`.")

    def model_schema(self) -> ToolSchema:
        return self.get_schema()

    def get_schema(self) -> ToolSchema:
        return ToolSchema(self.name, self.model_description(), {"type": "object", "properties": {
            "query": {"type": "string", "description": "Search query for deferred tools."},
            "limit": {"type": "number", "description": "Maximum number of tools to return. Defaults to 8."}},
            "required": ["query"], "additionalProperties": False})

    def get_spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, capability="tool.discovery", toolset="core", exposure="core", required_args=("query",))

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        registry = context.tool_registry if context is not None and context.tool_registry is not None else self._registry
        query = args["query"].strip()
        if not query:
            return self._error_result("query must not be empty")
        limit = args.get("limit", TOOL_SEARCH_DEFAULT_LIMIT)
        if type(limit) is not int or limit < 0:
            return self._error_result("failed to parse tool_search arguments: limit must be a nonnegative integer")
        if limit == 0:
            return self._error_result("limit must be greater than zero")
        permission = context.permission if context is not None else None
        checker = context.permission_checker if context is not None else None
        policy = _toolset_policy_for_context(permission, context.metadata if context is not None else None)
        key = (id(registry), registry.version, policy.cache_key(), asdict(permission) if permission is not None else None,
               checker.policy_snapshot() if checker is not None else None)
        if self._catalog is None or key != self._catalog_key:
            self._catalog = DeferredToolCatalog(registry, toolset_policy=policy, permission_checker=checker, permission_context=permission)
            self._catalog_key = key
        nested = context is not None and context.source_for_call(context.tool_call_id).kind == "code_mode"
        entries = self._catalog.search(query, limit, nested=nested)
        if nested:
            from backend.tools.schema import code_mode_parameters, code_mode_tool_name
            tools = []
            for entry in entries:
                schema = registry.get_tool_schema(entry.name, toolset_policy=policy,
                    permission_checker=checker, permission_context=permission)
                function = schema["function"]
                parameters = code_mode_parameters(function["parameters"])
                output_schema = schema.get("_minicode_output_schema")
                output = f" -> Promise<{code_mode_parameters(output_schema)}>" if output_schema is not None else ""
                public_name = code_mode_tool_name(schema)
                description = function["description"] + f"\n\nDeclaration: tools.{public_name}({parameters}){output}"
                tools.append({"name": public_name, "description": description})
            payload = {"tools": tools}
            return ToolResult(json.dumps(payload, ensure_ascii=False), runtime_metadata={"code_mode_result": payload})
        tools = _loadable_tools(registry, entries, policy=policy, checker=checker, permission=permission)
        state = context.metadata.get("_agent_state") if context is not None else None
        if state is not None:
            state.loaded_deferred_tools.update(entry.name for entry in entries)
        return ToolResult(json.dumps(tools, ensure_ascii=False), display_summary=f"Found {len(entries)} tools",
                          tool_search_output=tools)
