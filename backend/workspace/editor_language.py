from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .service import WorkspaceService


class EditorDocument(BaseModel):
    path: str
    content: str
    language: Literal["python", "yaml", "c", "cpp"] | None = None


class EditorLanguageRequest(BaseModel):
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


async def editor_language_request(
    service: WorkspaceService, request: EditorLanguageRequest
):
    from backend.lsp.editor import editor_language_servers

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
        documents.append({"path": str(path), "content": document.content, "language": language})
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
            root, language, request.method, documents, params
        )
    except (RuntimeError, TimeoutError) as exc:
        raise HTTPException(
            status_code=503, detail=str(exc) or "语言服务未及时返回诊断，请重试。"
        ) from exc
