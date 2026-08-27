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

from backend.tools.catalog import BRIDGE_TOOL_NAMES
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
    tokens: list[str] = field(default_factory=list)


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in TOKEN_RE.findall(text.lower()):
        tokens.extend(part for part in re.split(r"[_\-.]+", raw) if part)
    return tokens


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
        # ToolSchemaView is the only authority for deferred visibility.
        build_views = getattr(self.registry, "build_schema_views", None)
        if not callable(build_views):
            raise TypeError("DeferredToolCatalog requires ToolRegistry.build_schema_views")
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
            )
            hint = (view.search_hint or "").strip()
            entry.tokens = _tokenize(catalog_text + (" " + hint if hint else ""))
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

    def directly_visible_names(self) -> set[str]:
        """Return directly visible names from the canonical schema view."""
        build_views = getattr(self.registry, "build_schema_views", None)
        if not callable(build_views):
            raise TypeError("DeferredToolCatalog requires ToolRegistry.build_schema_views")
        names: set[str] = set()
        for view in build_views(
            toolset_policy=self.toolset_policy,
            permission_checker=self.permission_checker,
            permission_context=self.permission_context,
            materialize_schema=False,
        ):
            if (
                view.name not in BRIDGE_TOOL_NAMES
                and bool(getattr(view, "direct", False))
                and getattr(view, "exposure", "") != "hidden"
            ):
                names.add(str(view.name))
        return names

    def select_names(self, query: str, limit: int) -> list[str] | None:
        """Resolve exact/bare selection, including already-direct tools.

        None means the input is a keyword query and should use BM25. An
        explicit select: with no matches returns [] so callers can preserve
        the canonical no-match response.
        """
        match = SELECT_PREFIX_RE.match(query)
        raw_names = match.group("names") if match else query.strip()
        names = [part.strip() for part in re.split(r"[,\n]+", raw_names) if part.strip()]
        if not names:
            return [] if match else None

        deferred = {entry.name for entry in self.entries()}
        direct = self.directly_visible_names()
        by_lower = {name.lower(): name for name in (*deferred, *direct)}
        selected: list[str] = []
        seen: set[str] = set()
        for requested in names:
            resolved = by_lower.get(requested.lower())
            if resolved is None or resolved in seen:
                continue
            selected.append(resolved)
            seen.add(resolved)
            if len(selected) >= limit:
                break
        if selected:
            return selected
        return [] if match else None

    def _select_entries(
        self,
        query: str,
        catalog: list[DeferredToolEntry],
        limit: int,
    ) -> list[DeferredToolEntry] | None:
        """Return exact select: matches, or None when query is not selection.

        MiniCode's ``select:name,name`` form performs exact deferred-tool
        selection. Matching is forgiving about case and comma/space separators
        so a model can reuse a tool name from a previous turn without paying a
        search round trip.
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
    only; search hints, descriptions, and schemas stay behind tool_search until
    a selected tool is activated for the next model iteration.
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
        f'<available-deferred-tools total="{len(names)}">\n'
        + "\n".join(lines)
        + "\n</available-deferred-tools>"
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


def _toolset_policy_for_context(
    permission_context: Any | None,
    metadata: dict[str, Any] | None = None,
) -> Any | None:
    from backend.tools.toolset_runtime import resolve_context_toolset_policy

    return resolve_context_toolset_policy(permission_context, metadata)


class ToolSearchTool(BaseTool):
    name = "tool_search"
    read_only = True
    permission = PermissionLevel.AUTO
    result_kind = "generic"
    activity_kind = "genericTool"
    display_label = "Find tools"
    projection_visibility = "debug"
    description = (
        "Search deferred tools that are not directly listed in the current tool set. "
        "When to use: the directly listed tools do not cover a needed capability such as browser/desktop control, document work, connector actions, optional MCP tools, or specialized workflows. "
        "Use 'select:tool_name' for exact tools. Matching tools are activated and appear as ordinary directly callable tools on the next model iteration."
    )

    def model_description(self) -> str:
        return (
            "Activate deferred tools named in "
            "<available-deferred-tools>. Until fetched, only each tool's name is "
            "known and it cannot be invoked. Use 'select:ToolName' for an exact "
            "tool. Selected tools become directly callable on the next iteration."
        )

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Maximum number of results to return. Default: 5.",
                    },
                },
                "required": ["query"],
            },
        )

    def __init__(self, registry: Any | None = None) -> None:
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
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
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
        raw_max_results = args.get("max_results", args.get("limit", 5))
        if isinstance(raw_max_results, bool):
            return self._error_result("max_results must be a positive integer")
        try:
            max_results = int(raw_max_results or 5)
        except (TypeError, ValueError):
            return self._error_result("max_results must be a positive integer")
        if max_results < 1:
            return self._error_result("max_results must be a positive integer")
        limit = min(max_results, 20)
        catalog = DeferredToolCatalog(
            self._registry,
            toolset_policy=_toolset_policy_for_context(
                getattr(context, "permission", None),
                getattr(context, "metadata", None),
            ),
            permission_checker=getattr(context, "permission_checker", None),
            permission_context=getattr(context, "permission", None),
        )
        selected = catalog.select_names(query, limit)
        deferred_entries = catalog.entries()
        deferred_names = {entry.name for entry in deferred_entries}
        if selected is not None:
            # ``select:`` may name a tool that is already directly visible.
            # That name is reported back as a harmless no-op, but it must not
            # join the deferred activation set, so the reported matches and the
            # activation list are tracked separately.
            matched_names = list(selected)
            deferred_matches = [name for name in matched_names if name in deferred_names]
        else:
            matched_names = [entry.name for entry in catalog.search(query, limit)]
            deferred_matches = list(matched_names)
        total = len(deferred_entries)
        activated: list[str] = []
        metadata = getattr(context, "metadata", None)
        state = metadata.get("_agent_state") if isinstance(metadata, dict) else None
        loaded = getattr(state, "loaded_deferred_tools", None)
        if isinstance(loaded, set):
            for name in deferred_matches:
                if name not in loaded:
                    loaded.add(name)
                    activated.append(name)
        payload = {
            "query": query,
            "matches": matched_names,
            "activated": activated,
            "total_deferred_tools": total,
        }
        return ToolResult(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            display_summary=(
                f"Activated {len(activated)} deferred tools"
                if activated
                else f"Found {len(matched_names)} tools"
            ),
            result_kind="generic",
        )
