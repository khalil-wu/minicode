"""Optional OpenAI-compatible Images API tool for text/agent models."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit, urlunsplit
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
    """Generate an image through an OpenAI-compatible Images API."""

    name = "generate_image"
    description = (
        "Generate or edit an image with an OpenAI-compatible Images API. Use the "
        "imagegen skill for image tasks. If the user provides an external API, use "
        "its base URL, model and credentials from the conversation; otherwise use "
        "an available image channel. Direct calls display the result; in tool_exec, "
        "use generatedImage(result.images[0]) to display the selected image."
    )
    permission = PermissionLevel.AUTO
    read_only = False
    open_world = True
    always_load = True
    mutates_workspace = True
    requires_execution_environment = True
    workspace_path_fields = ("input_images", "output_path")
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
        snapshot = context.model_execution
        if snapshot is not None:
            provider = snapshot.provider.strip().lower()
            return provider if provider in {"openai", "anthropic", "custom"} else None
        candidates = []
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
        *,
        model_override: str | None = None,
        api_key_override: str | None = None,
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
        snapshot = context.model_execution if context is not None else None
        if snapshot is None:
            return get_image_generation_settings(provider, model_override=model_override, api_key_override=api_key_override)
        stack = snapshot.config.config_layer_stack
        settings_data = (
            stack.effective_config() if stack is not None
            else {"llm": {"provider": provider, provider: {
                **asdict(snapshot.config.llm),
                "default_headers": dict(snapshot.config.llm.default_headers),
            }}}
        )
        return get_image_generation_settings(provider, settings_data=settings_data,
            model_override=model_override, api_key_override=api_key_override)

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="image.generate",
            toolset="core",
            exposure="core",
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
                        "description": "Optional output dimensions supported by the selected image API.",
                    },
                    "quality": {
                        "type": "string",
                        "description": "Optional quality supported by the selected image API.",
                    },
                    "api_base_url": {
                        "type": "string",
                        "description": "User-provided OpenAI-compatible API base URL, such as https://example.com/v1. Omit to use an available image channel.",
                    },
                    "api_key": {
                        "type": "string",
                        "description": "Credential supplied by the user for this API, if required.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Image model supported by the API. For an explicit API, defaults to gpt-image-2.",
                    },
                    "transparent_background": {
                        "type": "boolean",
                        "description": "Request a transparent background when the selected API supports it.",
                    },
                    "input_images": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "maxItems": 5,
                        "description": "Reference PNG, JPEG or WebP paths in the accepted execution environment. All images are attached to one edit request in this order.",
                    },
                    "num_last_images_to_include": {
                        "type": "integer", "minimum": 1, "maximum": 5,
                        "description": "Use this many most recent conversation images as edit references, instead of input_images.",
                    },
                    "output_path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Optional new file path in the accepted execution environment. Existing files are never replaced.",
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
        tool_call_id = str(context.tool_call_id or "").strip()
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
        explicit_base_url = str(args.get("api_base_url") or "").strip()
        if explicit_base_url:
            parsed_url = urlsplit(explicit_base_url)
            for endpoint in ("/images/generations", "/images/edits"):
                if parsed_url.path.rstrip("/").endswith(endpoint):
                    base_path = parsed_url.path.rstrip("/").removesuffix(endpoint) or "/"
                    explicit_base_url = urlunsplit(parsed_url._replace(path=base_path))
                    break
            active_settings = context.model_execution.config.llm if context is not None and context.model_execution is not None else None
            settings = {
                "enabled": True,
                "provider": "custom",
                "base_url": explicit_base_url,
                "api_key": str(args.get("api_key") or ""),
                "model": str(args.get("model") or "gpt-image-2"),
                "proxy_mode": active_settings.proxy_mode if active_settings is not None else "inherit",
                "default_headers": (),
                "auth_header": bool(args.get("api_key")),
                "size": "auto",
                "quality": "auto",
            }
        else:
            settings = self._settings_for_context(context,
                model_override=str(args["model"]) if args.get("model") else None,
                api_key_override=str(args["api_key"]) if args.get("api_key") else None)
        metadata = context.metadata if context is not None else {}
        turn_context = (
            context.run_context.llm_turn_context
            if context is not None and context.run_context is not None else None
        )
        tool_call_id = str(
            getattr(context, "tool_call_id", "") or ""
        ).strip()
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
                user_summary="当前没有可用的图像生成接口。",
                developer_detail=reason,
                recoverable=False,
                projection="error",
                model_observation=(
                    "No usable image API was available for this request. Use a suitable "
                    "user-provided API or another available workflow; do not claim an image was generated."
                ),
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
            from backend.tools.image_generation_io import prepare_image_output, read_recent_images, read_reference_images, save_image_output

            reference_paths = args.get("input_images", [])
            recent_count = args.get("num_last_images_to_include")
            output_path = args.get("output_path")
            save_error = ""
            if reference_paths and recent_count is not None:
                raise ValueError("Use either input_images or num_last_images_to_include, not both.")
            if (reference_paths or recent_count is not None or output_path) and context is None:
                raise ValueError("Image file inputs and outputs require an accepted execution context.")
            if output_path:
                await prepare_image_output(output_path, context)
            input_images = (await read_reference_images(reference_paths, context) if reference_paths
                else await read_recent_images(context, recent_count) if recent_count is not None else None)
            await self._emit_progress(context, progress_id=progress_id, status="running",
                message="正在生成图像", detail="正在发送图像请求")
            images = await adapter.generate_images(
                prompt,
                size=str(args.get("size") or settings.get("size") or "1024x1024"),
                quality=str(args.get("quality") or settings.get("quality") or ""),
                input_images=input_images,
                background="transparent" if args.get("transparent_background") is True else None,
                metadata=metadata,
                turn_context=turn_context,
            )
            if output_path:
                try:
                    await save_image_output(output_path, images[0][0], context)
                except Exception as save_exc:
                    # The generation succeeded even when the requested file
                    # write collided; keep the result and the user's file.
                    save_error = (redact_secrets(_clean_error_message(save_exc) or str(save_exc)).strip()[:400]
                        or "the image file could not be saved")
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
            detail=(f"已生成 {len(images)} 张图片" if not save_error
                else f"已生成 {len(images)} 张图片，但保存到目标路径失败：{save_error}"),
            count=len(images),
        )
        return ToolResult(
            content=(
                f"Generated {len(images)} validated image(s). "
                + ("Use generatedImage(result.images[0]) to display the selected image."
                   if context is not None and context.result_sink is not None
                   else "The host attached the image artifact to this assistant turn.")
                + (f" Saved image to {output_path}." if output_path and not save_error
                   else f" The image file could not be saved to {output_path} ({save_error}); the original file was left unchanged and the generated image is still attached." if save_error
                   else "")
            ),
            images=[
                {"data": image_data, "media_type": media_type}
                for image_data, media_type in images
            ],
            result_kind=self.result_kind,
            display_summary=f"Generated {len(images)} image(s)",
            provider=str(settings.get("provider") or "image"),
            status="partial" if save_error else "success",
            recoverable=True,
            user_summary="图像已生成，但无法保存到指定路径；原文件保持不变。" if save_error else None,
            developer_detail=save_error or None,
        )
