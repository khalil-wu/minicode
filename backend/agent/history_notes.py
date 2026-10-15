"""Source History/Notes extension, backed only by the actual Codex service.

The model-facing operations are private rollout state. This module owns the
service protocol and activation; public projection remains a separate boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from backend.llm.proxy_policy import provider_httpx_proxy_kwargs

if TYPE_CHECKING:
    from backend.agent.context_window import ContextWindowConfig
    from backend.llm.model_runtime import ModelRuntime
    from backend.llm.provider_contracts import ModelDefinition
    from backend.mcp.manager import MCPServerManager
    from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

HISTORY_NOTES_BACKEND_TIMEOUT = 35.0
MAX_THREAD_HINT_BYTES = 4_000
ENCRYPTED_TOOL_ARGUMENTS_HEADER = "x-openai-encrypted-tool-arguments"
TOOL_OUTPUT_TRUNCATION_POLICY_HEADER = "x-openai-tool-output-truncation-policy"
OPERATION_ERROR_PREFIX = "Unable to perform operation:"
ENCRYPTED_ARGUMENT_ENDPOINTS = frozenset({
    "alpha/history/v2/search_contents",
    "alpha/notes/v2/search_contents",
    "alpha/notes/v2/append_to_file",
    "alpha/notes/v2/write_file",
})


class HistoryNotesError(RuntimeError):
    """Source RespondToModel error with a bounded, non-secret message."""


@dataclass(frozen=True)
class HistoryNotesBackend:
    model_runtime: ModelRuntime
    model_info: ModelDefinition

    async def call(
        self,
        path: str,
        session_id: str,
        current_agent_name: str,
        arguments: Any,
        truncation_policy: dict[str, Any],
    ) -> Any:
        if not isinstance(arguments, dict):
            raise HistoryNotesError("History tool arguments must be a JSON object")
        body = deepcopy(arguments)
        body["context"] = {
            "session_id": session_id,
            "current_agent_name": current_agent_name,
        }
        try:
            request = await self.model_runtime.resolve_history_notes_backend_request(self.model_info)
        except Exception as error:
            # Source maps provider/auth resolution errors at this service
            # boundary; callback exceptions must not disclose auth material.
            raise HistoryNotesError(
                f"{OPERATION_ERROR_PREFIX} Could not resolve backend authentication."
            ) from error
        url = f"{request.base_url.rstrip('/')}/{path.lstrip('/')}"
        headers = dict(request.headers)
        # The OpenAI adapter attaches this exact resolved request credential;
        # reuse it for service HTTP without reading an ambient API key.
        if request.api_key and not any(name.lower() == "authorization" for name in headers):
            headers["Authorization"] = f"Bearer {request.api_key}"
        headers[TOOL_OUTPUT_TRUNCATION_POLICY_HEADER] = json.dumps(
            truncation_policy, separators=(",", ":"), ensure_ascii=False,
        )
        if path in ENCRYPTED_ARGUMENT_ENDPOINTS:
            headers[ENCRYPTED_TOOL_ARGUMENTS_HEADER] = "true"
        try:
            async with httpx.AsyncClient(
                timeout=HISTORY_NOTES_BACKEND_TIMEOUT,
                follow_redirects=True,
                max_redirects=10,
                **provider_httpx_proxy_kwargs(url, proxy_mode=request.proxy_mode),
            ) as client:
                async with asyncio.timeout(HISTORY_NOTES_BACKEND_TIMEOUT):
                    response = await client.post(url, headers=headers, json=body)
                    response.raise_for_status()
        except (httpx.HTTPError, TimeoutError) as error:
            raise HistoryNotesError(
                f"{OPERATION_ERROR_PREFIX} The backend request failed."
            ) from error
        try:
            return response.json()
        except ValueError as error:
            raise HistoryNotesError(
                f"{OPERATION_ERROR_PREFIX} The backend returned invalid JSON."
            ) from error


def register_history_notes_tools(
    registry: ToolRegistry,
    config: ContextWindowConfig | None,
    model_runtime: ModelRuntime | None,
    model_info: ModelDefinition | None,
    *,
    session_id: str,
    agent_name: str,
) -> HistoryNotesBackend | None:
    """Publish only the tools authorized by the current Source config stack."""
    from backend.tools.history_notes import HistoryNotesTool, history_notes_actions

    enabled = (
        config is not None
        and config.use_history_notes_extension
        and model_runtime is not None
        and model_info is not None
        and model_runtime.history_notes_backend_enabled(model_info)
    )
    if not enabled:
        for action in history_notes_actions():
            registry.unregister(action.runtime_name)
        return None
    backend = HistoryNotesBackend(model_runtime, model_info)
    for action in history_notes_actions():
        existing = registry.get_tool(action.runtime_name)
        if (
            isinstance(existing, HistoryNotesTool)
            and existing.backend == backend
            and existing.session_id == session_id
            and existing.current_agent_name == agent_name
        ):
            continue
        registry.register(
            HistoryNotesTool(action, backend, session_id, agent_name),
            replace=existing is not None,
            owner="codex-history-notes",
        )
    return backend


async def contribute_history_notes_thread_context(
    backend: HistoryNotesBackend | None,
    *,
    session_id: str,
    agent_name: str,
) -> str | None:
    """Contribute once per context window; a failed hint has no replacement."""
    if backend is None:
        return None
    try:
        result = await backend.call(
            "alpha/notes/v2/thread_hint", session_id, agent_name, {},
            {"mode": "bytes", "limit": MAX_THREAD_HINT_BYTES},
        )
    except HistoryNotesError:
        logger.debug("History/Notes context-window hint failed")
        return None
    text = result.get("text") if isinstance(result, dict) else None
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_THREAD_HINT_BYTES:
        logger.debug("History/Notes context-window hint failed")
        return None
    return text or None


async def contribute_legacy_notes_thread_context(
    manager: MCPServerManager | None,
    *,
    thread_id: str,
    owner_session_id: str,
    conversation_id: str,
) -> str | None:
    """Source's existing notes/thread_hint bridge when native Notes is off.

    The caller owns the native-config branch. An absent native auth identity
    or a failed native request must never select this bridge as a replacement.
    """
    if manager is None:
        return None
    await manager.wait_for_server_startup("notes")
    if not any(tool.name == "thread_hint" for tool in manager.get_all_tools().get("notes", [])):
        return None
    client = manager.get_client("notes")
    if client is None:
        return None
    result = await client.call_tool(
        "thread_hint", None,
        request_owner={
            "session_id": owner_session_id,
            "conversation_id": conversation_id,
            "mcp_manager": manager,
        },
        request_meta={"threadId": thread_id},
    )
    if result.is_error and result.wire_result is None:
        # MCPClient already normalizes a transport/RPC exception into this result;
        # a server-returned isError still has wire_result and remains Source
        # content, since the Source contributor does not filter that field.
        return None
    text = "\n".join(value for block in result.content
        if isinstance(block, dict) and isinstance(value := block.get("text"), str) and value != "")
    return text or None
