from __future__ import annotations

import json
from typing import Any

from backend.agent.prompting import (
    get_prompt_pack,
    load_prompt_pack_into_context,
    prompt_pack_catalog,
)
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec


class PromptPackLoadTool(BaseTool):
    name = "prompt_pack_load"
    should_defer = True
    search_hint = "prompt rule pack frontend trace analysis document browser preview git automation"
    read_only = True
    permission = PermissionLevel.AUTO
    description = (
        "Load a task-specific prompt rule pack for the next model request. "
        "Use only when the current request or conversation context makes a listed pack materially relevant. "
        "Catalog: frontend_visual, trace_analysis, documents_data, browser_computer, git_thread_automation."
    )

    def model_description(self) -> str:
        return "Load one task-specific prompt rule pack by exact name for the next model request."

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "pack": {
                        "type": "string",
                        "enum": [item["name"] for item in prompt_pack_catalog()],
                    },
                },
                "required": ["pack"],
            },
        )

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="prompt.pack.load",
            toolset="core",
            exposure="core",
            required_args=("pack",),
            empty_args_policy="block",
        )

    def is_concurrency_safe(self, args: dict[str, Any] | None = None) -> bool:
        return False

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "pack": {
                        "type": "string",
                        "enum": [item["name"] for item in prompt_pack_catalog()],
                        "description": "Exact prompt pack name to load.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief reason grounded in the current request or conversation context.",
                    },
                },
                "required": ["pack"],
            },
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        pack_name = str(args.get("pack") or "").strip()
        reason = str(args.get("reason") or "").strip()
        pack = get_prompt_pack(pack_name)
        if pack is None:
            available = ", ".join(item["name"] for item in prompt_pack_catalog())
            return self._error_result(f"Unknown prompt pack '{pack_name}'. Available: {available}")

        metadata = getattr(context, "metadata", None)
        prompt_context = metadata.get("prompt_context") if isinstance(metadata, dict) else None
        if not isinstance(prompt_context, dict):
            return self._error_result("Prompt context is unavailable; cannot load a prompt pack for the next request")

        loaded = load_prompt_pack_into_context(
            prompt_context,
            pack.name,
            reason=reason,
            source="model_selected",
        )
        if loaded is None:
            return self._error_result(f"Unable to load prompt pack '{pack.name}'")

        payload = {
            "loaded_prompt_pack": loaded.name,
            "title": loaded.title,
            "next_request": "The full prompt pack will appear after the stable prompt boundary.",
        }
        if reason:
            payload["reason"] = reason
        return ToolResult(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            display_summary=f"Loaded prompt pack {loaded.name}",
            result_kind="generic",
        )
