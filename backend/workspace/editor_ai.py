from __future__ import annotations

import json
from dataclasses import asdict
from typing import Literal

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .service import WorkspaceService


class EditorCompletionRequest(BaseModel):
    path: str
    prefix: str = Field(max_length=16000)
    suffix: str = Field(max_length=8000)
    selected: str = Field(default="", max_length=32000)
    instruction: str = Field(default="", max_length=4000)
    mode: Literal["complete", "edit"] = "complete"
    model: str = ""
    provider: str = ""
    max_tokens: int = Field(default=256, ge=32, le=4096)


def editor_completion_response(
    service: WorkspaceService, request: EditorCompletionRequest
):
    from backend.config import load_config
    from backend.llm.base import LLMMessage, LLMTurnContext, SideQueryOptions
    from backend.llm.model_registry import create_session_llm

    root = service.workspace_root_path()
    path = service.resolve_workspace_path(request.path)
    service.ensure_editor_file_allowed(
        root / service.normalize_workspace_relative(request.path),
        operation="read",
        resolved_path=path,
    )
    config = load_config(cwd=root)
    try:
        adapter = create_session_llm(
            config, request.model or None, provider_override=request.provider or None
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    instruction = (
        "You complete code at the cursor. Return only the missing code, without Markdown fences, explanations, or repeating the prefix or suffix. Keep the surrounding style and indentation."
        if request.mode == "complete"
        else "You edit a selected code block. Return only the complete replacement block, without Markdown fences or explanations. Follow the user's instruction and preserve surrounding code."
    )
    content = json.dumps(
        {
            "file": str(path.relative_to(root)),
            "prefix": request.prefix,
            "selection": request.selected,
            "suffix": request.suffix,
            "instruction": request.instruction,
        },
        ensure_ascii=False,
    )

    async def generate():
        context = LLMTurnContext()
        try:
            text = await adapter.side_query(
                [
                    LLMMessage(role="system", content=instruction),
                    LLMMessage(role="user", content=content),
                ],
                options=SideQueryOptions(
                    operation="editor_inline_" + request.mode,
                    max_tokens=request.max_tokens,
                    disable_reasoning=True,
                    enable_prompt_cache=False,
                    attempt_timeout_seconds=30,
                    max_retries=0,
                ),
                turn_context=context,
            )
            if text.startswith("~~~") or text.startswith(chr(96) * 3):
                lines = text.splitlines(keepends=True)
                if len(lines) >= 2 and lines[-1].strip() == text[:3]:
                    text = "".join(lines[1:-1])
            yield json.dumps(
                {
                    "text": text,
                    "usage": asdict(context.usage),
                    "model": request.model or config.llm.model,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            yield json.dumps({"error": str(exc)}, ensure_ascii=False)
        finally:
            await adapter.aclose()

    # StreamingResponse cancels its generator (and the provider request) when
    # the editor aborts a stale completion. No agent turn or tools are started.
    return StreamingResponse(
        generate(), media_type="application/json", headers={"Cache-Control": "no-store"}
    )
