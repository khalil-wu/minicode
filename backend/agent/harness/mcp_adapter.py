from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from backend.agent.harness.contracts import ToolSpec


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_PATH_ARG_NAMES = {"file_path", "filepath", "path"}
_URL_ARG_NAMES = {"url", "href", "link"}
_QUERY_ARG_NAMES = {"query", "q", "search_query", "pattern"}
_ARTIFACT_ARG_NAMES = {"artifact_id", "artifact_ref"}
_CONTENT_ARG_NAMES = {"content", "text", "body", "prompt", "description"}
_DOCUMENT_SOURCE_ARG_NAMES = {"source", "document", "document_id", "doc_id", "input"}


@dataclass(frozen=True)
class MCPToolSpecAdapter:
    """Classify MCP tools into the generic harness contract.

    MCP servers are dynamic and untrusted from the harness point of view. This
    adapter is intentionally conservative: tools only become deferred when their
    resource contract is clear from name, description, and schema metadata.
    Unknown tools stay hidden.
    """

    server_name: str
    tool_name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None

    @classmethod
    def from_tool_def(cls, server_name: str, tool_def: Any) -> "MCPToolSpecAdapter":
        return cls(
            server_name=server_name,
            tool_name=str(getattr(tool_def, "name", "") or ""),
            description=str(getattr(tool_def, "description", "") or ""),
            input_schema=getattr(tool_def, "input_schema", None) or {},
            annotations=getattr(tool_def, "annotations", None) or {},
        )

    def build_spec(self, runtime_name: str) -> ToolSpec:
        required = self._required_args()
        arg_roles = self._infer_arg_roles(required)
        capability = self._infer_capability(arg_roles)
        if capability in {"artifact.parse", "memory.read"}:
            exposure = "deferred"
        elif capability == "web.connector":
            # Redundant with native web_search/web_fetch — keep hidden.
            exposure = "hidden"
        elif self._is_read_only_annotated():
            # A server that declares the tool read-only and has no native
            # equivalent is safe to surface via tool_search (deferred), so the
            # agent can actually discover and use it.
            exposure = "deferred"
        else:
            exposure = "hidden"

        return ToolSpec(
            name=runtime_name,
            capability=capability,
            toolset="mcp",
            exposure=exposure,
            required_args=required,
            arg_roles=arg_roles,
            repair_policy=self._repair_policy(arg_roles),
            accepted_resource_types=self._accepted_resource_types(capability),
            rejected_resource_types=self._rejected_resource_types(capability),
            empty_args_policy=self._empty_args_policy(arg_roles, capability),
            blocked_guidance=self._blocked_guidance(capability),
        )

    def _is_read_only_annotated(self) -> bool:
        ann = self.annotations or {}
        return bool(ann.get("readOnlyHint")) and not (
            bool(ann.get("destructiveHint")) or bool(ann.get("openWorldHint"))
        )

    def _required_args(self) -> tuple[str, ...]:
        schema = self.input_schema or {}
        required = schema.get("required", [])
        return tuple(str(field) for field in required if isinstance(field, str))

    def _infer_arg_roles(self, required: tuple[str, ...]) -> dict[str, str]:
        roles: dict[str, str] = {}
        for arg in required:
            lower = arg.lower()
            if lower in _PATH_ARG_NAMES or lower.endswith("_path"):
                roles[arg] = "workspace_file"
            elif lower in _URL_ARG_NAMES:
                roles[arg] = "latest_url"
            elif lower in _QUERY_ARG_NAMES:
                roles[arg] = "search_query"
            elif lower in _ARTIFACT_ARG_NAMES:
                roles[arg] = "latest_artifact"
            elif lower in _CONTENT_ARG_NAMES:
                roles[arg] = "generated_content"
            elif lower in _DOCUMENT_SOURCE_ARG_NAMES and self._looks_like_document_parser():
                roles[arg] = "explicit_document_source"
            elif lower in {"memory_id", "memory_key", "memory"} and self._looks_like_memory_reader():
                roles[arg] = "latest_memory"
        return roles

    def _infer_capability(self, arg_roles: dict[str, str]) -> str:
        if self._looks_like_document_parser() and "explicit_document_source" in set(arg_roles.values()):
            return "artifact.parse"
        if self._looks_like_memory_reader():
            return "memory.read"
        if self._looks_like_web_search_or_fetch():
            return "web.connector"
        return "mcp"

    def _repair_policy(self, arg_roles: dict[str, str]) -> dict[str, str]:
        policies: dict[str, str] = {}
        for arg, role in arg_roles.items():
            if role == "generated_content":
                policies[arg] = "needs_model_generation"
            elif role == "explicit_document_source":
                policies[arg] = "routing_correction"
            elif role in {"workspace_file", "latest_url", "search_query", "latest_artifact", "latest_memory"}:
                policies[arg] = "resource_resolver"
        return policies

    def _accepted_resource_types(self, capability: str) -> tuple[str, ...]:
        if capability == "artifact.parse":
            return ("uploaded_document", "artifact_ref", "web_url")
        if capability == "memory.read":
            return ("memory_ref",)
        return ()

    def _rejected_resource_types(self, capability: str) -> tuple[str, ...]:
        if capability == "artifact.parse":
            return ("workspace_file",)
        return ()

    def _empty_args_policy(self, arg_roles: dict[str, str], capability: str) -> str:
        if capability == "artifact.parse":
            return "block"
        return "repair_or_block" if arg_roles else "block"

    def _blocked_guidance(self, capability: str) -> str:
        if capability == "artifact.parse":
            return (
                "This parser is for explicit external document sources. For workspace source files, "
                "use read_file or grep_files and continue without this parser."
            )
        return ""

    def _looks_like_document_parser(self) -> bool:
        tokens = self._tokens()
        return bool(tokens & {"docparse", "document", "doc", "pdf", "docx", "parse", "parser", "extract"})

    def _looks_like_memory_reader(self) -> bool:
        tokens = self._tokens()
        has_memory = bool(tokens & {"memory", "memories", "remember", "recall"})
        has_read = bool(tokens & {"get", "read", "recall", "retrieve", "search", "lookup"})
        return has_memory and has_read

    def _looks_like_web_search_or_fetch(self) -> bool:
        tokens = self._tokens()
        has_web = bool(tokens & {"web", "internet", "url", "browser", "search", "fetch"})
        has_operation = bool(tokens & {"search", "fetch", "crawl", "page", "lookup"})
        return has_web and has_operation

    def _tokens(self) -> set[str]:
        schema_text = ""
        schema = self.input_schema or {}
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            schema_text = " ".join(
                f"{name} {meta.get('description', '') if isinstance(meta, dict) else ''}"
                for name, meta in properties.items()
            )
        raw = f"{self.server_name} {self.tool_name} {self.description} {schema_text}".lower()
        return set(_TOKEN_RE.findall(raw.replace("_", " ").replace("-", " ")))
