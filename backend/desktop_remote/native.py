"""Desktop-only operations dispatched on the host that owns the window."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from pathlib import Path
import shutil
import stat

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.api import _state
from backend.async_cleanup import _consume_task_result
from backend.attachments.store import MAX_ATTACHMENT_CONTENT_BYTES
from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.services.chat_api_service import ChatApiServiceError, reserve_attachment_upload_context, upload_document_payload
from backend.services.tool_registry_factory import get_attachment_store
from backend.services.workspace_api_service import workspace_search_payload
from backend.workspace.service import WorkspaceService
from backend.workspace.trust import TRUSTED_WORKSPACES_FILE, trusted_workspace_roots

from .terminal import NativeTerminals


class NativeRequest(BaseModel):
    window_instance_id: str
    window_owner_id: str
    window_binding_token: str
    channel: str
    args: list


def _service(path: str) -> tuple[WorkspaceService, Path]:
    target = Path(path).expanduser().resolve()
    roots = [root for root in trusted_workspace_roots() if target.is_relative_to(root)]
    if not roots:
        raise HTTPException(403, "remote path is outside the selected host's trusted workspaces")
    root = max(roots, key=lambda item: len(item.parts))
    return WorkspaceService(get_workspace_root=lambda: root), target


def _trust(path: str) -> str:
    target = Path(path).expanduser().resolve(strict=True)
    if not target.is_dir():
        raise HTTPException(409, "remote workspace path is not a directory")
    with file_mutation_locks([TRUSTED_WORKSPACES_FILE]):
        payload = json.loads(TRUSTED_WORKSPACES_FILE.read_text(encoding="utf-8")) if TRUSTED_WORKSPACES_FILE.exists() else []
        roots = payload if isinstance(payload, list) else payload["roots"]
        if str(target) not in roots:
            roots.append(str(target))
            TRUSTED_WORKSPACES_FILE.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(TRUSTED_WORKSPACES_FILE, json.dumps(payload, ensure_ascii=False, indent=2))
    return str(target)


def _directory_entries(path: str | None, directories_only: bool) -> dict:
    directory = path.strip() if path and path.strip() else str(Path.home())
    target = Path(directory)
    if not target.is_dir():
        raise HTTPException(404, f"Directory not found: {target}")
    with os.scandir(target) as contents:
        entries = []
        for entry in contents:
            try:
                mode = os.stat(entry.path, follow_symlinks=True).st_mode
            except OSError:
                # Source fs/readDirectory omits an entry whose target metadata
                # cannot be read, including a dangling directory link.
                continue
            directory_entry = stat.S_ISDIR(mode)
            entries.append({"name": entry.name, "path": str(target / entry.name), "type": "directory" if directory_entry else "file"})
    return {"directoryPath": directory, "parentPath": str(target.parent) if target.parent != target else None,
            "entries": [entry for entry in entries if not directories_only or entry["type"] == "directory"]}


class RemoteNative:
    def __init__(self, emit, bridge_endpoint: str, bridge_token: str):
        self.terminals = NativeTerminals(emit)
        self.bridge_token = bridge_token
        self.bridge = httpx.AsyncClient(base_url=bridge_endpoint, headers={"authorization": f"Bearer {bridge_token}"}, timeout=30, trust_env=False)
        self.router = APIRouter(prefix="/api/desktop-host")
        self.router.add_api_route("/native", self.request, methods=["POST"])

    async def _binding(self, request: NativeRequest) -> None:
        response = await self.bridge.post("/v1/window-binding", json={
            "window_instance_id": request.window_instance_id, "window_owner_id": request.window_owner_id,
            "window_binding_token": request.window_binding_token,
        })
        if response.status_code != 200 or response.json().get("ok") is not True:
            raise HTTPException(403, "remote desktop window binding is no longer active")

    async def request(self, request: NativeRequest, http_request: Request):
        channel, args = request.channel.removeprefix("minicode:"), request.args
        if channel == "window:release":
            token = http_request.headers.get("x-minicode-desktop-lifecycle-token", "")
            if not hmac.compare_digest(token.encode(), self.bridge_token.encode()):
                raise HTTPException(403, "remote desktop lifecycle authority is required to release a window")
            await self.terminals.release_window(request.window_instance_id)
            return {"released": True}
        await self._binding(request)
        if channel == "workspace:directoryEntries":
            return await asyncio.to_thread(_directory_entries, args[0], args[1])
        if channel == "workspace:trust":
            return await asyncio.to_thread(_trust, args[0])
        if channel == "attachments:importRemoteFile":
            return await self._import_attachment(request, args[0])
        if channel.startswith("pty:"):
            if channel == "pty:spawn":
                if not args[1]:
                    raise HTTPException(422, "remote terminal conversation owner is required")
                _service(args[0])
            return await self.terminals.request(channel.removeprefix("pty:"), args, request.window_instance_id)
        if channel == "env:detect":
            return {**{command: shutil.which(command) is not None for command in ("git", "python", "node", "docker", "ollama")},
                    "home": str(Path.home())}
        if channel == "openPreferredPath":
            _service(args[0])
            return {"openInEditor": True, "path": str(Path(args[0]).expanduser().resolve())}
        if channel in {"openPath", "revealPath"}:
            raise HTTPException(409, "use the selected host's MiniCode editor to open remote files")
        if channel.startswith("fs:"):
            return await asyncio.to_thread(self._filesystem, channel, args)
        raise HTTPException(404, f"unknown remote desktop operation: {channel}")

    async def _import_attachment(self, request: NativeRequest, source: dict) -> dict:
        session = _state.ws_manager.get_session(source["sessionId"])
        if session is None or (session.window_instance_id, session.window_owner_id) != (request.window_instance_id, request.window_owner_id):
            raise HTTPException(403, "remote attachment session belongs to another window")
        try:
            context = reserve_attachment_upload_context(session_id=source["sessionId"],
                conversation_id=source["conversationId"], workspace_root=source["workspaceRoot"],
                ws_manager=_state.ws_manager, attachment_store=get_attachment_store())
        except ChatApiServiceError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

        def persist_import() -> dict:
            try:
                target = Path(source["path"]).expanduser().resolve(strict=True)
                if not stat.S_ISREG(target.stat().st_mode):
                    raise HTTPException(409, "remote attachment path is not a regular file")
                with target.open("rb") as stream:
                    content = stream.read(MAX_ATTACHMENT_CONTENT_BYTES + 1)
                if len(content) > MAX_ATTACHMENT_CONTENT_BYTES:
                    raise HTTPException(413, "Upload exceeds the 50 MB limit.")
                payload = upload_document_payload(context=context, file_name=target.name, raw_content=content,
                    source_path=str(target))
                return {**payload, "source": {"path": str(target), "name": target.name,
                    "mediaType": payload["attachment"]["media_type"], "sizeBytes": len(content)}}
            except FileNotFoundError as exc:
                raise HTTPException(404, str(exc)) from exc
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from exc
            except OSError as exc:
                raise HTTPException(400, str(exc)) from exc
            finally:
                context.release()

        # The parsing thread owns its reservation until its actual final write,
        # even if the renderer closes or the native HTTP request is cancelled.
        task = asyncio.create_task(asyncio.to_thread(persist_import))
        task.add_done_callback(_consume_task_result)
        try:
            return await asyncio.shield(task)
        except ChatApiServiceError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    def _filesystem(self, channel: str, args: list):
        service, target = _service(args[0])
        relative = str(target.relative_to(service.workspace_root_path()))
        if channel == "fs:listTree":
            result = service.list_tree(relative).model_dump()
            root = service.workspace_root_path()
            result["requested_path"] = str(target)
            for entry in result["entries"]:
                entry["path"] = str(root / entry["path"])
            return result
        if channel == "fs:readFile":
            result = service.read_file(relative).model_dump()
            result["path"] = str(target)
            return result
        if channel == "fs:searchFiles":
            root, query, limit, kind = args
            return workspace_search_payload(root=Path(root).expanduser().resolve(), query=query, limit=limit,
                                            include_tests=True, kind=kind)
        if channel == "fs:deletePath":
            import send2trash

            service.ensure_not_workspace_root(target)
            service.ensure_write_allowed(target)
            recursive, confirmed = args[1:3]
            if target.is_dir() and recursive and not confirmed:
                count = sum(1 for _ in target.rglob("*"))
                if count > 50:
                    return {"needsConfirmation": True, "path": str(target), "entryCount": count}
            send2trash.send2trash(str(target))
            return {"deleted": True, "path": str(target)}
        raise HTTPException(404, f"unknown remote filesystem operation: {channel}")

    async def close(self) -> None:
        await self.terminals.close()
        await self.bridge.aclose()
