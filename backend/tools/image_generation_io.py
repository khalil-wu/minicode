"""Reference and generated image files in the tool's accepted filesystem."""
from __future__ import annotations

import asyncio
import base64
from collections import Counter
from io import BytesIO
from pathlib import Path

from PIL import Image

from backend.atomic_io import atomic_write_bytes
from backend.mcp.result_projection import mcp_output_images
from backend.permissions.context import ToolExecutionContext
from backend.sandbox.policy import sandbox_policy_for_permission_context
from backend.tools.path_resolution import _is_bypass_mode, _resolve_path
from backend.workspace.executor_service import ExecutorWorkspaceService, uses_executor_io


def local_image_path(value: str, context: ToolExecutionContext, *, write: bool = False) -> Path:
    path = _resolve_path(value, context, allow_workspace_escape=_is_bypass_mode(context), allow_declared_read_root=not write)
    policy = context.sandbox_policy or sandbox_policy_for_permission_context(context.workspace_root, context.permission)
    access = policy.resolve(cwd=context.workspace_root).resolve_access(path)
    if not (access.can_write if write else access.can_read):
        raise PermissionError(f"Image {'write' if write else 'read'} permission denied: {path}")
    return path


async def read_reference_images(paths: list[str], context: ToolExecutionContext) -> list[tuple[str, bytes, str]]:
    images = []
    for value in paths:
        if uses_executor_io(context):
            async with ExecutorWorkspaceService(context) as service:
                path = service.resolve(value)
                raw = await service.read_bytes(path, limit=50 * 1024 * 1024)
                name = path.name
        else:
            path = local_image_path(value, context)
            raw = await asyncio.to_thread(path.read_bytes)
            name = path.name
        with Image.open(BytesIO(raw)) as image:
            media_type = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image.format]
            image.verify()
        images.append((name, raw, media_type))
    return images


async def read_recent_images(context: ToolExecutionContext, count: int) -> list[tuple[str, bytes, str]]:
    """Resolve recent conversation images under their existing attachment owner."""
    if context.run_context is None or context.run_context.context_builder is None:
        raise ValueError("Conversation images are unavailable in this execution context")
    builder = context.run_context.context_builder
    history = tuple(builder._history)

    def collect() -> list[tuple[str, bytes, str]]:
        found: list[tuple[str, bytes, str]] = []
        generated_aliases: Counter[str] = Counter()
        for message in reversed(history):
            images_by_id = {item["artifact_id"]: item for item in message.images if item.get("artifact_id")}
            candidates = []
            for ref in message.attachment_refs:
                if ref.get("kind") != "image":
                    continue
                candidates.append({"reference": ref, "image": images_by_id.pop(ref["artifact_id"], None)})
            candidates.extend(item for item in message.images
                if not item.get("artifact_id") or item["artifact_id"] in images_by_id)
            for output in (message.mcp_output, message.code_mode_output, message.history_notes_output, message.native_tool_output):
                if output is not None:
                    candidates.extend(mcp_output_images(output))
            for item in message.provider_items:
                if item.get("type") == "image_generation_call" and item.get("result"):
                    candidates.append({"data": item["result"], "media_type": "image/png", "generated_result": True})
            for candidate in reversed(candidates):
                ref = candidate.get("reference")
                image = candidate.get("image") if ref else candidate
                if ref and (image is None or not image.get("data")):
                    payload = builder._attachment_store.get_payload(
                        ref["artifact_id"], conversation_id=context.conversation_id,
                        workspace_root=str(context.workspace_root or ""),
                    )
                    if payload is None or not payload.get("native_data"):
                        raise ValueError(f"Conversation image is unavailable: {ref['artifact_id']}")
                    image = {"data": payload["native_data"], "media_type": ref.get("media_type") or "image/png"}
                if not image.get("data"):
                    continue
                encoded = image["data"]
                if ref and ref.get("input_source") == "assistant_generated":
                    generated_aliases[encoded] += 1
                elif (message.name == "generate_image" or image.get("generated_result")) and generated_aliases[encoded]:
                    generated_aliases[encoded] -= 1
                    continue
                raw = base64.b64decode(encoded, validate=True)
                with Image.open(BytesIO(raw)) as decoded:
                    media_type = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[decoded.format]
                    decoded.verify()
                found.append((f"image-{len(found) + 1}.{media_type.split('/')[1]}", raw, media_type))
                if len(found) == count:
                    return list(reversed(found))
        raise ValueError(f"Requested the last {count} conversation images, but only {len(found)} were available")

    return await asyncio.to_thread(collect)


async def prepare_image_output(value: str, context: ToolExecutionContext) -> None:
    if uses_executor_io(context):
        async with ExecutorWorkspaceService(context) as service:
            path = service.resolve(value)
            if not service.scope.resolve_access(path.uri).can_write:
                raise PermissionError(f"Image write permission denied: {value}")
            exists = await service.metadata(path) is not None
    else:
        exists = local_image_path(value, context, write=True).exists()
    if exists:
        raise FileExistsError(f"Image output already exists; choose a new path: {value}")


async def save_image_output(value: str, image_data: str, context: ToolExecutionContext) -> None:
    raw = base64.b64decode(image_data, validate=True)
    if uses_executor_io(context):
        async with ExecutorWorkspaceService(context) as service:
            path = service.resolve(value)
            if not service.scope.resolve_access(path.uri).can_write:
                raise PermissionError(f"Image write permission denied: {value}")
            async with service.mutate([path]):
                if await service.metadata(path) is not None:
                    raise FileExistsError(f"Image output already exists; choose a new path: {value}")
                await service.write_bytes(path, raw)
    else:
        path = local_image_path(value, context, write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(atomic_write_bytes, path, raw, overwrite=False)
