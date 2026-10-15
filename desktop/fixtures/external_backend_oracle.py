from __future__ import annotations

import os
import traceback

import uvicorn
from fastapi import Request
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException

import backend.main as backend_main
import backend.llm.model_registry as model_registry
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent

models = []
oracle_report = None
native_http_exception_handler = backend_main.app.exception_handlers[HTTPException]


@backend_main.app.exception_handler(HTTPException)
async def record_native_lifecycle_failure(request: Request, error: HTTPException):
    if request.url.path == "/api/workspace/language/lifecycle":
        print(f"[oracle-language-lifecycle-error] HTTP {error.status_code}: {error.detail}", flush=True)
        traceback.print_exception(error)
    return await native_http_exception_handler(request, error)


class NativeBrowserModel(LLMAdapter):
    def __init__(self, settings):
        self._settings = settings
        self._provider_id = "openai"
        self.calls = 0
        self.request_owners = []
        self.saw_browser_result = False
        self.host_secret_leaked = False
        models.append(self)

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.calls += 1
        self.request_owners.append({"session_id": (metadata or {}).get("session_id", ""),
                                   "query_source": (metadata or {}).get("query_source", "")})
        self.saw_browser_result |= any(message.role == "tool" and "external-tab" in message.content for message in messages)
        self.host_secret_leaked |= os.environ["MINICODE_EMBEDDED_BROWSER_TOKEN"] in repr((messages, metadata))
        if self.calls == 1:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(
                id="external-native-discovery", name="tool_search", arguments={"query": "browser_control"},
            )])
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
        elif self.calls == 2:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(
                id="external-native-browser", name="browser_control", arguments={"action": "list_targets"},
            )])
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="The owned native browser was observed.", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop", end_turn=True)

    async def simple_chat(self, messages, *, max_tokens=None):
        return "Owned oracle model."


def native_model_factory(config, **kwargs):
    return NativeBrowserModel(config.llm)


backend_main._create_session_llm = native_model_factory
model_registry.create_session_llm = native_model_factory


@backend_main.app.get("/oracle")
async def oracle_page():
    return FileResponse(os.environ["MINICODE_ORACLE_FRONTEND_FILE"], media_type="text/html")


@backend_main.app.get("/oracle/language-state")
async def language_state():
    from backend.lsp.editor import editor_language_servers

    return {"clients": [{"window_instance_id": key[0], "client_id": key[1], "pid": client._process.pid}
                        for key, client in editor_language_servers.clients.items()]}


@backend_main.app.post("/oracle/report")
async def store_report(request: Request):
    global oracle_report
    oracle_report = await request.json()
    return {"stored": True}


@backend_main.app.get("/oracle/report")
async def get_report():
    return {"report": oracle_report, "models": [{"calls": model.calls, "request_owners": model.request_owners, "saw_browser_result": model.saw_browser_result,
             "host_secret_leaked": model.host_secret_leaked} for model in models]}


if __name__ == "__main__":
    uvicorn.run(backend_main.app, host="127.0.0.1", port=int(os.environ["MINICODE_ORACLE_BACKEND_PORT"]), log_level="warning")
