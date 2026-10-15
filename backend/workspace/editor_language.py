from __future__ import annotations

import asyncio
import hmac
import os
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field
import httpx

from .service import WorkspaceService


class EditorDocument(BaseModel):
    document_id: str = Field(min_length=1)
    model_version: int = Field(ge=1)
    path: str
    content: str
    language: Literal["python", "yaml", "c", "cpp"] | None = None


class EditorLanguageRequest(BaseModel):
    window_owner_id: str = Field(min_length=1)
    window_instance_id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    snapshot_revision: int = Field(ge=1)
    method: Literal[
        "completion",
        "hover",
        "definition",
        "references",
        "rename",
        "signatureHelp",
        "documentSymbol",
        "formatting",
        "diagnostics",
    ]
    documents: list[EditorDocument] = Field(min_length=1, max_length=1000)
    line: int = Field(default=0, ge=0)
    character: int = Field(default=0, ge=0)
    new_name: str = ""
    tab_size: int = Field(default=4, ge=1, le=8)
    insert_spaces: bool = True


class EditorLanguageLifecycleRequest(BaseModel):
    action: Literal["close", "release"]
    window_owner_id: str = Field(min_length=1)
    window_instance_id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    document_ids: list[str] = Field(default_factory=list)


class EditorLanguageWindowReleaseRequest(BaseModel):
    action: Literal["release_window"]
    window_owner_id: str = Field(min_length=1)
    window_instance_id: str = Field(min_length=1)


EditorLanguageLifecycleOperation = Annotated[
    EditorLanguageLifecycleRequest | EditorLanguageWindowReleaseRequest, Field(discriminator="action"),
]


async def authenticate_editor_window(request: EditorLanguageRequest | EditorLanguageLifecycleOperation, *, binding_token: str, desktop_lifecycle_token: str = ""):
    from backend.lsp.editor import editor_language_servers

    if isinstance(request, EditorLanguageWindowReleaseRequest) and desktop_lifecycle_token:
        expected = os.environ.get("MINICODE_EMBEDDED_BROWSER_TOKEN", "")
        if expected and hmac.compare_digest(expected, desktop_lifecycle_token):
            return
        raise HTTPException(status_code=403, detail="Invalid native editor lifecycle owner")
    if not binding_token:
        raise HTTPException(status_code=403, detail="Editor window binding is required")
    endpoint = os.environ.get("MINICODE_EMBEDDED_BROWSER_ENDPOINT", "")
    if endpoint:
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
                response = await client.post(endpoint.rstrip("/") + "/v1/window-binding",
                    headers={"Authorization": "Bearer " + os.environ.get("MINICODE_EMBEDDED_BROWSER_TOKEN", "")},
                    json={"window_instance_id": request.window_instance_id, "window_owner_id": request.window_owner_id,
                          "window_binding_token": binding_token})
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"Native editor window verification failed: {exc}") from exc
        if response.status_code != 200 or response.json().get("ok") is not True:
            raise HTTPException(status_code=403, detail="Invalid editor window binding")
    elif not editor_language_servers.bind_browser_window(request.window_instance_id, request.window_owner_id, binding_token):
        raise HTTPException(status_code=403, detail="Invalid editor window binding")


async def editor_language_lifecycle(request: EditorLanguageLifecycleOperation, *, binding_token: str = "", desktop_lifecycle_token: str = ""):
    from backend.lsp.editor import editor_language_servers

    await authenticate_editor_window(request, binding_token=binding_token, desktop_lifecycle_token=desktop_lifecycle_token)
    try:
        if request.action == "release_window":
            await editor_language_servers.release_window(request.window_instance_id)
        elif request.action == "close":
            await editor_language_servers.close_documents(
                window_instance_id=request.window_instance_id, client_id=request.client_id, document_ids=request.document_ids,
            )
        else:
            await editor_language_servers.release_client(window_instance_id=request.window_instance_id, client_id=request.client_id)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"action": request.action, "window_instance_id": request.window_instance_id}


async def editor_language_request(
    service: WorkspaceService, request: EditorLanguageRequest, *, binding_token: str
):
    from backend.lsp.editor import EditorLanguageStateError, editor_language_servers

    await authenticate_editor_window(request, binding_token=binding_token)
    root = service.workspace_root_path()
    documents = []
    for document in request.documents:
        path = service.resolve_workspace_path(document.path)
        service.ensure_editor_file_allowed(
            root / service.normalize_workspace_relative(document.path),
            operation="read",
            resolved_path=path,
        )
        language = document.language or {
            ".py": "python", ".pyi": "python", ".yaml": "yaml", ".yml": "yaml",
            ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
            ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
        }.get(path.suffix.lower())
        if language is None:
            raise HTTPException(
                status_code=422, detail="语言服务只接受 Python、YAML、C 或 C++ 文档。"
            )
        documents.append({"path": str(path), "content": document.content, "language": language,
                          "document_id": document.document_id, "model_version": document.model_version})
    path = service.resolve_workspace_path(request.documents[0].path)
    language = documents[0]["language"]
    documents = [
        doc
        for doc in documents
        if doc["language"] == language or (language in {"c", "cpp"} and doc["language"] in {"c", "cpp"})
    ]
    params = {
        "textDocument": {"uri": path.as_uri()},
        "position": {"line": request.line, "character": request.character},
    }
    if request.method == "references":
        params["context"] = {"includeDeclaration": True}
    elif request.method == "rename":
        params["newName"] = request.new_name
    elif request.method == "formatting":
        params["options"] = {
            "tabSize": request.tab_size,
            "insertSpaces": request.insert_spaces,
        }
    try:
        return await editor_language_servers.request(
            root, language, request.method, documents, params,
            window_instance_id=request.window_instance_id, client_id=request.client_id, snapshot_revision=request.snapshot_revision,
        )
    except EditorLanguageStateError as exc:
        raise HTTPException(status_code=409, detail={"code": "editor.document_stale", "message": str(exc)}) from exc
    except (RuntimeError, TimeoutError) as exc:
        raise HTTPException(
            status_code=503, detail=str(exc) or "语言服务未及时返回诊断，请重试。"
        ) from exc
