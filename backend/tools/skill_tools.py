"""Source executor Skill namespace; locators retain their captured authority."""
from __future__ import annotations

import json
import math
from typing import Any

from backend.environments import ExecutorError
from backend.permissions.context import ToolExecutionContext
from backend.skills.environment_catalog import ExecutorSkillCatalog
from backend.tools.base import BaseTool, ToolResult, ToolSchema, TOOL_SIDE_EFFECT_NONE


def _response_budget(context: ToolExecutionContext | None) -> int:
    if context is not None and context.source_for_call(context.tool_call_id).kind == "code_mode":
        return 512 * 1024
    issuing = context.model_execution if context is not None else None
    info = issuing.model_info if issuing is not None else None
    policy = (info.truncation_policy if info is not None else None) or {"mode": "bytes", "limit": 10000}
    return min(512 * 1024, math.ceil(policy["limit"] * 1.2) * (4 if policy["mode"] == "tokens" else 1))


class _SkillTool(BaseTool):
    read_only = True
    side_effect_kind = TOOL_SIDE_EFFECT_NONE
    max_result_chars = None
    activity_kind = "genericTool"

    def __init__(self, catalog: ExecutorSkillCatalog):
        self.catalog = catalog

    def get_schema(self) -> ToolSchema:
        return ToolSchema(name=self.name, description=self.description,
            parameters=self.parameters, namespace="skills", namespace_tool_name=self.action,
            namespace_description="Skills", output_schema=self.output_schema)

    @staticmethod
    def output(value: dict) -> ToolResult:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return ToolResult(body, native_tool_output=body, runtime_metadata={"code_mode_result": value})

    @staticmethod
    def error(error: Exception) -> ToolResult:
        text = str(error)
        return ToolResult(text, is_error=True, native_tool_output=text)


class ListSkillsTool(_SkillTool):
    name = "skills__list"
    action = "list"
    display_label = "List skills"
    description = "List skills owned by the requested authority. Returns each skill's authority, package, and main_resource. Pass the package to skills.read, and pass next_cursor back as cursor to continue."
    parameters = {"type": "object", "additionalProperties": False,
        "properties": {"authority": {"type": "object", "properties": {"kind": {"const": "executor", "type": "string"}},
            "required": ["kind"], "additionalProperties": False}, "cursor": {"type": ["string", "null"]}},
        "required": ["authority"]}
    output_schema = {"type": "object", "properties": {"skills": {"type": "array", "items": {"type": "object"}},
        "warnings": {"type": "array", "items": {"type": "string"}}, "next_cursor": {"type": ["string", "null"]}},
        "required": ["skills", "warnings", "next_cursor"], "additionalProperties": False}

    async def execute(self, args: dict[str, Any], *, context: ToolExecutionContext | None = None) -> ToolResult:
        try:
            if args["authority"] != {"kind": "executor"} or set(args) - {"authority", "cursor"}:
                raise ValueError("skills.list requires executor authority")
            return self.output(self.catalog.list_page(cursor=args.get("cursor"), response_byte_budget=_response_budget(context)))
        except (KeyError, ValueError) as error:
            return self.error(error)


class ReadSkillTool(_SkillTool):
    name = "skills__read"
    action = "read"
    display_label = "Read skill"
    description = "Read one page from a skill. Pass its provided package directly; root aliases are resolved automatically. Omit resource to read SKILL.md; to read another file, use the same package and pass the file's complete skill:// identifier as resource. For executor-backed skills, skill_root is the skill's absolute directory in the executor filesystem and can be used to locate bundled scripts. If the package is not provided, use skills.list to find it. Pass next_cursor back as cursor to continue the same snapshot while it is cached; omit cursor to read again."
    parameters = {"type": "object", "properties": {"package": {"type": "string"},
        "resource": {"type": ["string", "null"]}, "cursor": {"type": ["string", "null"]}}, "required": ["package"]}
    output_schema = {"type": "object", "properties": {"resource": {"type": "string"},
        "contents": {"type": "string"}, "skill_root": {"type": "string"}, "next_cursor": {"type": ["string", "null"]}},
        "required": ["resource", "contents", "next_cursor"], "additionalProperties": False}

    async def execute(self, args: dict[str, Any], *, context: ToolExecutionContext | None = None) -> ToolResult:
        try:
            for name in ("package", "resource"):
                value = args.get(name)
                if name == "resource" and value is None:
                    continue
                if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 2048 or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
                    raise ValueError(f"{name} must be non-empty, contain no control characters, and be at most 2048 bytes")
            return self.output(await self.catalog.read_page(args["package"], args.get("resource"), cursor=args.get("cursor"), response_byte_budget=_response_budget(context)))
        except (ExecutorError, OSError, ValueError) as error:
            return self.error(error)


def bind_skill_tools(registry, skill_manager) -> None:
    for name in ("skills__list", "skills__read"):
        if registry.get_tool(name) is not None:
            registry.unregister(name)
    if skill_manager.executor_catalog is not None:
        registry.register(ListSkillsTool(skill_manager.executor_catalog))
        registry.register(ReadSkillTool(skill_manager.executor_catalog))
