"""Runtime facts and publication for the shipped local Hatch Pet Skill."""
from __future__ import annotations

import json

from backend.async_cleanup import to_thread_cancel_safe
from backend.services.pet_hatch_service import hatch_runtime, publish_hatched_pet
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema, TOOL_SIDE_EFFECT_EXTERNAL
from backend.tools.contracts import ToolSpec
from backend.tools.image_generation_io import local_image_path
from backend.workspace.executor_service import ExecutorWorkspaceService, uses_executor_io


async def read_hatch_files(value, context):
    required = ("pet_request.json", "final/spritesheet-extended.webp", "qa/review.json",
        "qa/chroma-despill-extended.json", "qa/direction-semantics.json", "qa/direction-blind-validation.json")
    if uses_executor_io(context):
        async with ExecutorWorkspaceService(context) as service:
            directory = service.resolve(value)
            files = {relative: await service.read_bytes(directory.joinpath(relative), limit=50 * 1024 * 1024) for relative in required}
            if not json.loads(files["qa/direction-blind-validation.json"])["ok"]:
                files["qa/blind-review-resolution.json"] = await service.read_bytes(directory.joinpath("qa/blind-review-resolution.json"))
    else:
        directory = local_image_path(value, context)
        async def read(relative):
            path = local_image_path(str(directory / relative), context)
            return await to_thread_cancel_safe(path.read_bytes)
        files = {relative: await read(relative) for relative in required}
        if not json.loads(files["qa/direction-blind-validation.json"])["ok"]:
            files["qa/blind-review-resolution.json"] = await read("qa/blind-review-resolution.json")
    return files


class HatchPetTool(BaseTool):
    name = "hatch_pet"
    description = "Get the local Hatch Pet Python/Pillow runtime, or publish a validated v2 pet run into MiniCode and notify the desktop to refresh and select it. Use with the hatch-pet Skill."
    permission = PermissionLevel.AUTO
    should_defer = True
    requires_execution_environment = True
    search_hint = "hatch pet runtime python pillow publish sprite atlas"
    side_effect_kind = TOOL_SIDE_EFFECT_EXTERNAL
    idempotent = False
    result_kind = "file"
    activity_kind = "fileChange"
    workspace_path_fields = ("run_directory",)

    def get_spec(self):
        return ToolSpec(self.name, capability="pet.manage", exposure="deferred")

    def get_schema(self):
        return ToolSchema(self.name, self.description, {
            "type": "object", "properties": {
                "action": {"type": "string", "enum": ["runtime", "publish"]},
                "run_directory": {"type": "string", "description": "Absolute or workspace-relative completed run folder; required for publish."},
            }, "required": ["action"], "additionalProperties": False,
        })

    async def execute(self, args, context=None):
        if context is None or context.turn_environment is not None and context.turn_environment.is_remote:
            raise ValueError("Hatch Pet uses the local MiniCode execution environment.")
        if args["action"] == "runtime":
            return ToolResult(json.dumps(hatch_runtime(), ensure_ascii=False))
        files = await read_hatch_files(args["run_directory"], context)
        result = await to_thread_cancel_safe(publish_hatched_pet, files)
        if context.emit_event is not None:
            await context.emit_event("pet.published", {"pet_id": result["pet_id"], "conversation_id": context.conversation_id})
        return ToolResult(json.dumps(result, ensure_ascii=False), display_summary=f"Published {result['manifest']['displayName']}")
