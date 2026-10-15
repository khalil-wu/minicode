"""Nine Source direct-model-only History/Notes service operations."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from backend.agent.history_notes import HistoryNotesBackend, HistoryNotesError
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema, TOOL_SIDE_EFFECT_NONE
from backend.tools.contracts import ToolSpec


@dataclass(frozen=True)
class HistoryNotesAction:
    namespace: str
    name: str
    namespace_description: str
    description: str
    parameters: dict[str, Any]
    supports_parallel_tool_calls: bool

    @property
    def runtime_name(self) -> str:
        return f"{self.namespace}__{self.name}"

    @property
    def endpoint(self) -> str:
        return f"alpha/{self.namespace}/v2/{self.name}"


@cache
def history_notes_actions() -> tuple[HistoryNotesAction, ...]:
    # Literal schema and prompt descriptions captured from Source tools.rs.
    records = json.loads(Path(__file__).with_name("history_notes_schemas.json").read_text(encoding="utf-8"))
    return tuple(HistoryNotesAction(**record) for record in records)


def history_notes_model_output(result: Any) -> tuple[Any, str | list[dict[str, Any]]]:
    """Source separates image attachments before text or hook serialization."""
    log_result = deepcopy(result)
    has_images = isinstance(log_result, dict) and "images" in log_result
    images = log_result.pop("images") if has_images else None
    text = json.dumps(log_result, ensure_ascii=False, separators=(",", ":"))
    encrypted = log_result.get("encrypted_output") if isinstance(log_result, dict) else None
    output: str | list[dict[str, Any]] = (
        [{"type": "encrypted_content", "encrypted_content": encrypted}]
        if isinstance(encrypted, str) else text
    )
    if has_images:
        invalid_image = "History backend returned invalid image content."
        if not isinstance(images, list):
            raise HistoryNotesError(invalid_image)
        content = [{"type": "input_text", "text": output}] if isinstance(output, str) else output
        for image in images:
            if (
                not isinstance(image, dict)
                or not isinstance(image.get("data"), str)
                or not isinstance(image.get("mime_type"), str)
                or image.get("detail") not in (None, "auto", "low", "high", "original")
            ):
                raise HistoryNotesError(invalid_image)
            attachment = {
                "type": "input_image",
                "image_url": f"data:{image['mime_type']};base64,{image['data']}",
            }
            if image.get("detail") is not None:
                attachment["detail"] = image["detail"]
            content.append(attachment)
        output = content
    return log_result, output


class HistoryNotesTool(BaseTool):
    private_model_only = True
    projection_visibility = "debug"
    permission = PermissionLevel.AUTO
    should_defer = False
    deferred_catalog_scopes = ()
    max_result_chars = None
    idempotent = False
    side_effect_kind = TOOL_SIDE_EFFECT_NONE
    # Source private virtual state does not mutate the agent's environment.
    # The independent parallel flag still serializes append/write calls.
    read_only = True

    def __init__(
        self,
        action: HistoryNotesAction,
        backend: HistoryNotesBackend,
        session_id: str,
        current_agent_name: str,
    ) -> None:
        self.action = action
        self.backend = backend
        self.session_id = session_id
        self.current_agent_name = current_agent_name
        self.name = action.runtime_name
        self.description = action.description
        self.model_namespace = action.namespace
        self.model_tool_name = action.name
        self.model_namespace_description = action.namespace_description

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name, capability="context.history_notes",
            exposure="direct_model_only",
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            self.name, self.description, deepcopy(self.action.parameters), strict=False,
            namespace=self.model_namespace,
            namespace_description=self.model_namespace_description,
            namespace_tool_name=self.model_tool_name,
        )

    def get_workspace_paths(self, args: dict[str, Any] | None = None) -> list[str]:
        # Source note paths are virtual and never filesystem permissions.
        return []

    def model_schema(self) -> ToolSchema:
        return self.get_schema()

    def get_execution_schema(self) -> ToolSchema:
        # Source handle_call parses JSON and checks the object boundary only.
        # Argument semantics belong to the remote service, not a second local
        # schema validator that can reject calls the service would accept.
        return ToolSchema(self.name, self.description, {"type": "object"})

    def is_concurrency_safe(self, args: dict[str, Any] | None = None) -> bool:
        return self.action.supports_parallel_tool_calls

    async def execute(
        self, args: dict[str, Any], context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        # The shared exposure boundary rejects these tools from Code Mode;
        # the tool retains Source's explicit model-facing error as well.
        if context is not None and context.source_for_call(context.tool_call_id).kind == "code_mode":
            message = "History tools are unavailable in code mode."
            return ToolResult(message, is_error=True, history_notes_output=message)
        issuing = context.model_execution if context is not None else None
        model_info = issuing.model_info if issuing is not None else self.backend.model_info
        settings = issuing.config.llm if issuing is not None else None
        policy = model_info.truncation_policy if model_info is not None else settings.truncation_policy
        if policy is None:
            # Source models-manager/model_info.rs supplies Bytes(10_000)
            # for a model without a catalog-owned output policy.
            policy = {"mode": "bytes", "limit": 10_000}
        try:
            result = await self.backend.call(
                self.action.endpoint, self.session_id, self.current_agent_name,
                args, dict(policy),
            )
            log_result, output = history_notes_model_output(result)
        except HistoryNotesError as error:
            return ToolResult(
                str(error), is_error=True, history_notes_output=str(error),
            )
        return ToolResult(
            json.dumps(log_result, ensure_ascii=False, separators=(",", ":")),
            history_notes_output=output,
            runtime_metadata={"hook_output": log_result},
        )
