"""Optional OpenAI-compatible Images API tool for text/agent models."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.config import LLMSettings, get_image_generation_settings
from backend.llm.errors import classify_llm_error
from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.openai_errors import _clean_error_message
from backend.permissions.context import ToolExecutionContext
from backend.secret_redaction import redact_secrets
from backend.tools.base import (
    TOOL_SIDE_EFFECT_EXTERNAL,
    BaseTool,
    PermissionLevel,
    ToolResult,
    ToolSchema,
)
from backend.tools.contracts import ToolSpec


class GenerateImageTool(BaseTool):
    """Generate one validated image through the active profile's image channel."""

    name = "generate_image"
    description = (
        "Generate an image from a textual prompt using the image channel explicitly "
        "configured for the active provider profile. Use only when the user asks to "
        "create or render an image."
    )
    permission = PermissionLevel.AUTO
    read_only = False
    open_world = True
    always_load = True
    mutates_external_state = True
    side_effect_kind = TOOL_SIDE_EFFECT_EXTERNAL
    idempotent = False
    timeout_seconds = 180.0
    result_kind = "image_generation"
    activity_kind = "imageGeneration"
    display_label = "Generate image"

    @staticmethod
    def _provider_for_context(context: ToolExecutionContext | None) -> str | None:
        if context is None:
            return None
        metadata = context.metadata if isinstance(context.metadata, dict) else {}
        parent_runtime = metadata.get("_subagent_parent_runtime")
        candidates = []
        if isinstance(parent_runtime, dict):
            candidates.append(parent_runtime.get("provider"))
        candidates.append(metadata.get("provider"))
        llm_settings = getattr(getattr(context, "llm", None), "_settings", None)
        candidates.append(getattr(llm_settings, "provider", None))
        for candidate in candidates:
            provider = str(candidate or "").strip().lower()
            if provider in {"openai", "anthropic", "custom"}:
                return provider
        return None

    def _settings_for_context(
        self,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        provider = self._provider_for_context(context)
        if context is not None and provider is None:
            return {
                "enabled": False,
                "provider": "",
                "reason": (
                    "The active runtime provider has no explicit MiniCode image "
                    "channel; refusing to borrow another provider's credentials."
                ),
            }
        return get_image_generation_settings(provider)

    def get_spec(self) -> ToolSpec:
        settings = self._settings_for_context()
        return ToolSpec(
            name=self.name,
            capability="image.generate",
            toolset="core",
            exposure="core" if settings.get("enabled") else "hidden",
            always_load=True,
            required_args=("prompt",),
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 32000,
                        "description": "A complete visual description of the image to generate.",
                    },
                    "size": {
                        "type": "string",
                        "enum": ["auto", "1024x1024", "1536x1024", "1024x1536"],
                        "description": "Optional output dimensions; omit to use the profile default.",
                    },
                    "quality": {
                        "type": "string",
                        "enum": ["auto", "low", "medium", "high", "standard", "hd"],
                        "description": "Optional provider-supported quality; omit to use the profile default.",
                    },
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        )

    async def _emit_progress(
        self,
        context: ToolExecutionContext | None,
        *,
        progress_id: str,
        status: str,
        message: str,
        detail: str = "",
        count: int | None = None,
    ) -> None:
        emitter = getattr(context, "emit_event", None) if context is not None else None
        if not callable(emitter):
            return
        metadata = context.metadata if isinstance(context.metadata, dict) else {}
        tool_call_id = str(metadata.get("_current_tool_call_id") or "").strip()
        payload: dict[str, Any] = {
            "id": progress_id,
            "stage": "image_generation",
            "phase": "image_generation",
            "status": status,
            "message": message,
            "label": "图像生成",
            "summary": message,
            "visibility": "timeline",
            "tool_name": self.name,
            "ephemeral": status == "running",
        }
        if detail:
            payload["detail"] = detail
        if count is not None:
            payload["count"] = max(0, int(count))
        if tool_call_id:
            payload["tool_call_id"] = tool_call_id
            payload["step_id"] = tool_call_id
        await emitter("agent.progress", payload)

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        prompt = str(args.get("prompt") or "").strip()
        settings = self._settings_for_context(context)
        metadata = context.metadata if context is not None and isinstance(context.metadata, dict) else {}
        tool_call_id = str(metadata.get("_current_tool_call_id") or "").strip()
        progress_id = f"image-generation:{tool_call_id or uuid4().hex[:16]}"

        if not settings.get("enabled"):
            reason = redact_secrets(str(settings.get("reason") or "Image generation is not configured."))[:400]
            await self._emit_progress(
                context,
                progress_id=progress_id,
                status="failed",
                message="图像生成失败",
                detail=reason,
            )
            return ToolResult(
                content=f"Image generation is unavailable: {reason}",
                is_error=True,
                result_kind=self.result_kind,
                provider=str(settings.get("provider") or "image"),
                provider_error_type="configuration",
                error_kind="configuration_error",
                user_summary="图像生成通道尚未正确配置。",
                developer_detail=reason,
                recoverable=False,
                projection="error",
                model_observation=(
                    "The configured image channel is unavailable. Explain the configuration "
                    "problem without claiming that an image was generated."
                ),
            )

        await self._emit_progress(
            context,
            progress_id=progress_id,
            status="running",
            message="正在生成图像",
            detail="图像请求已提交",
        )
        adapter = OpenAIAdapter(
            LLMSettings(
                api_key=str(settings["api_key"]),
                provider=str(settings.get("provider") or "custom"),
                base_url=str(settings["base_url"]),
                model=str(settings["model"]),
                wire_api="chat",
                proxy_mode=str(settings.get("proxy_mode") or "inherit"),
                default_headers=tuple(settings.get("default_headers") or ()),
                auth_header=bool(settings.get("auth_header", False)),
                image_size=str(settings.get("size") or "1024x1024"),
                image_quality=str(settings.get("quality") or ""),
            )
        )
        try:
            images = await adapter.generate_images(
                prompt,
                size=str(args.get("size") or settings.get("size") or "1024x1024"),
                quality=str(args.get("quality") or settings.get("quality") or ""),
                metadata=metadata,
            )
        except Exception as exc:
            classification = classify_llm_error(exc)
            detail = redact_secrets(_clean_error_message(exc) or str(exc)).strip()[:400]
            detail = detail or "Provider image request failed."
            await self._emit_progress(
                context,
                progress_id=progress_id,
                status="failed",
                message="图像生成失败",
                detail=detail,
            )
            return ToolResult(
                content=f"Image generation failed: {detail}",
                is_error=True,
                result_kind=self.result_kind,
                provider=str(settings.get("provider") or "image"),
                provider_error_type=classification.provider_error_type,
                error_kind=classification.error_type,
                user_summary="图像生成失败。",
                developer_detail=detail,
                recoverable=classification.retryable,
                projection="error",
                model_observation=(
                    "Image generation failed. Do not claim success. Tell the user whether "
                    "retrying is appropriate based on the error."
                ),
            )
        finally:
            await adapter.aclose()

        await self._emit_progress(
            context,
            progress_id=progress_id,
            status="completed",
            message="图像生成完成",
            detail=f"已生成 {len(images)} 张图片",
            count=len(images),
        )
        return ToolResult(
            content=(
                f"Generated {len(images)} validated image(s). The host attached the image "
                "artifact to this assistant turn."
            ),
            images=[
                {"data": image_data, "media_type": media_type}
                for image_data, media_type in images
            ],
            result_kind=self.result_kind,
            display_summary=f"Generated {len(images)} image(s)",
            provider=str(settings.get("provider") or "image"),
            status="success",
            recoverable=True,
        )
