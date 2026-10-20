"""Reference and generated image files in the tool's accepted filesystem."""
from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path

from PIL import Image

from backend.atomic_io import atomic_write_bytes
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
