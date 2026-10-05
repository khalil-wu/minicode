from __future__ import annotations

import asyncio

import pytest

import backend.tools.browser_control_tool as browser
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.browser_support import _CDPSession, browser_deadline, browser_submission


@pytest.mark.asyncio
async def test_cdp_request_wait_obeys_its_operation_deadline():
    class Socket:
        async def send(self, _payload):
            pass

        async def recv(self):
            await asyncio.Event().wait()

    token = browser_deadline.set(asyncio.get_running_loop().time() + 0.01)
    try:
        with pytest.raises(TimeoutError):
            await _CDPSession(Socket()).call("Runtime.enable")
    finally:
        browser_deadline.reset(token)


@pytest.mark.asyncio
async def test_cdp_send_cancellation_keeps_an_action_that_may_already_be_in_the_socket_uncertain():
    sent = asyncio.Event()
    submission = {"submitted": False, "mutates": True}

    class Socket:
        async def send(self, _payload):
            sent.set()
            await asyncio.Event().wait()

    token = browser_submission.set(submission)
    try:
        task = asyncio.create_task(_CDPSession(Socket()).call("Input.insertText", {"text": "fixture"}))
        await sent.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert submission["submitted"]
    finally:
        browser_submission.reset(token)


@pytest.mark.asyncio
async def test_element_deadline_includes_cdp_setup(monkeypatch):
    async def target(_self, _endpoint, _target_id):
        return {"webSocketDebuggerUrl": "ws://127.0.0.1/fixture"}

    class Session:
        async def call(self, _method, _params=None):
            await asyncio.Event().wait()

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr(browser.BrowserControlTool, "_select_target", target)
    monkeypatch.setattr(browser, "_cdp_session", lambda _url: SessionContext())
    result = await asyncio.wait_for(browser.BrowserControlTool().execute(
        {"action": "wait_for_element", "selector": "#fixture", "timeout_ms": 10},
    ), 0.3)
    assert result.status == "timeout"
    assert result.is_error


@pytest.mark.parametrize("interrupt", ["timeout", "cancelled"])
@pytest.mark.asyncio
async def test_embedded_interrupt_retains_remote_owner_until_observed_completion(monkeypatch, interrupt):
    started = asyncio.Event()
    settled = asyncio.Event()
    calls = []
    pending = {
        "resource_kind": "browser", "resource_id": "fixture", "reason": interrupt,
        "requested": True, "acknowledged": True, "completed": False,
        "pending": 1, "retry_safe": False, "execution_outcome": "uncertain",
    }

    class Response:
        status_code = 200

        def __init__(self, receipt):
            self.receipt = receipt

        def json(self):
            return {"ok": True, "cleanup_receipt": self.receipt}

        def raise_for_status(self):
            pass

    class Client:
        def __init__(self, **kwargs):
            self.timeout = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, *, headers, json):
            calls.append((url, json, self.timeout))
            if url.endswith("/command"):
                started.set()
                await asyncio.Event().wait()
            if json["action"] == "cancel":
                return Response(dict(pending))
            await settled.wait()
            return Response({**pending, "completed": True, "pending": 0, "execution_outcome": "completed"})

    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", "http://127.0.0.1:43123")
    monkeypatch.setattr(browser.httpx, "AsyncClient", Client)
    monkeypatch.setattr(browser, "BROWSER_OPERATION_SECONDS", 0.02 if interrupt == "timeout" else 30)
    context = ToolExecutionContext(permission=PermissionContext(), conversation_id="owner", tool_call_id="browser-call")
    task = asyncio.create_task(browser.BrowserControlTool().execute({"action": "evaluate", "expression": "fixture"}, context))
    await started.wait()
    try:
        if interrupt == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as raised:
                await task
            receipt = raised.value.cleanup_receipt
        else:
            result = await asyncio.wait_for(task, 0.3)
            assert result.status == "timeout"
            receipt = result.cleanup_receipt
        assert receipt["pending"] == 1 and not receipt["completed"]
        assert context.pending_cleanup_tasks
        submitted_id = calls[0][1]["operation_id"]
        assert all(call[1]["operation_id"] == submitted_id for call in calls)
        assert calls[0][2] is not None
        settled.set()
        await asyncio.gather(*list(context.pending_cleanup_tasks))
        assert receipt["completed"] and receipt["pending"] == 0
        assert context.cleanup_receipts["browser-call"]["completed"]
        assert not context.pending_cleanup_tasks
    finally:
        settled.set()
        task.cancel()
        await asyncio.gather(task, *list(context.pending_cleanup_tasks), return_exceptions=True)
