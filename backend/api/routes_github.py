"""User-initiated GitHub connection; tokens remain in the CLI credential store."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import aclosing

import anyio

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from starlette.types import Scope, Receive, Send
from backend.services.github_service import (
    GitHubCommandError, github_cli_command, github_connection_status, github_login_events,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class _GithubLoginResponse(StreamingResponse):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


@router.get("/api/github/status")
async def github_status():
    try:
        return await github_connection_status()
    except (GitHubCommandError, OSError, asyncio.TimeoutError) as exc:
        logger.warning("GitHub connection status failed: %s", exc)
        raise HTTPException(status_code=502, detail="暂时无法读取 GitHub 连接状态，请重试。") from exc


@router.post("/api/github/login")
async def github_login():
    command = github_cli_command()
    if command is None:
        raise HTTPException(status_code=503, detail="GitHub 连接组件尚未就绪，请重新打开 MiniCode。")

    async def events():
        try:
            async with aclosing(github_login_events(command)) as authorization:
                async for event in authorization:
                    yield json.dumps(event, ensure_ascii=False) + "\n"
        except (GitHubCommandError, OSError, asyncio.TimeoutError) as exc:
            logger.warning("GitHub authorization failed: %s", exc)
            yield json.dumps({"phase": "error", "message": "GitHub 连接未完成，请重试。"}, ensure_ascii=False) + "\n"

    return _GithubLoginResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})
