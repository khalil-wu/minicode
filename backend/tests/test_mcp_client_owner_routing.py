from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from mcp import types

from backend.agent.message import UserCommand
from backend.bootstrap.app import AppBootstrap
from backend.cancellation_signal import CancellationSignal
from backend.mcp.client import MCPClient
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.handlers.mcp import _ProviderOAuthCallbacks
from backend.ws.turn_wait_state import TurnWaitState


def test_turn_wait_state_registers_each_waiter_in_one_lane() -> None:
    async def run() -> None:
        state = TurnWaitState()
        futures = {
            kind: asyncio.get_running_loop().create_future()
            for kind in ("approval", "user_input", "elicitation", "provider_oauth")
        }

        for kind, future in futures.items():
            state.register_waiter(kind, future, kind=kind)

        assert state.pending_approvals == {"approval": futures["approval"]}
        assert state.pending_user_input == {"user_input": futures["user_input"]}
        assert state.pending_elicitations == {"elicitation": futures["elicitation"]}
        assert state.provider_oauth_pending == {"provider_oauth": futures["provider_oauth"]}
        assert state.waiter_ids() == set(futures)

        state.clear_pending_waiters()
        assert state.waiter_ids() == set()
        assert all(future.cancelled() for future in futures.values())

    asyncio.run(run())


def test_approval_handler_registers_control_elicitation_in_elicitation_lane() -> None:
    async def run() -> None:
        session = _OAuthControlSession()
        session.turn_wait_state.pending_approval_payloads["ask-1"] = {
            "type": "control_request",
            "request_id": "ask-1",
            "conversation_id": "conv_oauth_a",
            "request": {
                "subtype": "elicitation",
                "tool_use_id": "ask-1",
                "question": "Choose a runtime",
            },
        }
        task = asyncio.create_task(session.approval_handler("ask-1"))
        for _ in range(20):
            if "ask-1" in session.turn_wait_state.pending_elicitations:
                break
            await asyncio.sleep(0)

        assert "ask-1" in session.turn_wait_state.pending_elicitations
        assert "ask-1" not in session.turn_wait_state.pending_approvals
        assert session._resolve_pending_approval(
            "ask-1",
            {"answer": "use-node"},
        )
        assert await task == {"answer": "use-node"}
        assert session.turn_wait_state.pending_elicitations == {}

    asyncio.run(run())


class _BootstrapElicitationSession:
    def __init__(
        self,
        *,
        response: dict | None = None,
        delivered: bool = True,
    ) -> None:
        self.turn_wait_state = TurnWaitState()
        self.response = response
        self.delivered = delivered
        self.sent: list[dict] = []
        self.terminal: list[dict] = []

    async def send_payload(self, payload: dict, *, log_context: str) -> bool:
        assert log_context == "mcp:elicitation"
        self.sent.append(payload)
        request_id = str(payload.get("request_id") or "")
        if self.delivered and self.response is not None:
            self.turn_wait_state.pending_elicitations[request_id].set_result(
                dict(self.response)
            )
        return self.delivered

    async def emit_approval_cancelled_once(
        self,
        request_ids: list[str],
        *,
        reason: str,
        conversation_id: str,
    ) -> None:
        self.terminal.append({
            "request_ids": list(request_ids),
            "reason": reason,
            "conversation_id": conversation_id,
        })


class _OAuthControlSession(SessionApprovalRuntimeMixin):
    def __init__(self) -> None:
        self.active_conversation_id = "conv_oauth_a"
        self.turn_wait_state = TurnWaitState()
        self.approval_diff_cache: dict[str, dict] = {}
        self.run_manager = SimpleNamespace(run_tasks={})
        self.delivered = True
        self.sent: list[dict] = []
        self.terminal: list[dict] = []
        self.command_dispatcher = object.__new__(SessionCommandDispatcher)
        self.command_dispatcher._session = self

    async def send_payload(self, payload: dict, *, log_context: str) -> bool:
        assert log_context in {
            "llm.provider.oauth.prompt",
            "llm.provider.oauth.auth",
            "llm.provider.oauth.device_code",
            "llm.provider.oauth.info",
            "llm.provider.oauth.progress",
            "reemit:approval",
        }
        self.sent.append(dict(payload))
        return self.delivered

    async def emit_approval_cancelled_once(
        self,
        request_ids: list[str],
        *,
        reason: str,
        conversation_id: str,
    ) -> None:
        self.terminal.append({
            "request_ids": list(request_ids),
            "reason": reason,
            "conversation_id": conversation_id,
        })


def test_mcp_server_callback_requires_one_unambiguous_owner() -> None:
    client = MCPClient(server_name="example")
    owner = {"session_id": "session-a", "conversation_id": "conversation-a", "task_id": "turn-a"}
    client._active_tool_request_owners = {1: owner, 2: dict(owner)}

    assert client._active_callback_owner() == owner

    client._active_tool_request_owners[3] = {
        "session_id": "session-b",
        "conversation_id": "conversation-b",
        "task_id": "turn-b",
    }
    assert client._active_callback_owner() is None


def test_official_sdk_elicitation_callback_adapts_product_response(monkeypatch) -> None:
    captured: dict = {}

    async def handler(payload: dict) -> dict:
        captured.update(payload)
        return {"action": "submit", "response": {"answer": "yes"}}

    async def run() -> None:
        client = MCPClient(server_name="example", elicitation_handler=handler)
        client._active_tool_request_owners = {
            1: {"session_id": "session-a", "conversation_id": "conversation-a"}
        }
        params = types.ElicitRequestFormParams(
            message="Continue?",
            requestedSchema={"type": "object"},
        )

        result = await client._sdk_elicitation_callback(SimpleNamespace(request_id=8), params)

        assert isinstance(result, types.ElicitResult)
        assert result.action == "accept"
        assert result.content == {"answer": "yes"}
        assert captured["prompt"] == "Continue?"
        assert captured["schema"] == {"type": "object"}

    monkeypatch.setattr("backend.mcp.client.feature_enabled", lambda name: name == "mcp_elicitation")
    asyncio.run(run())


def test_mcp_owner_fence_cancels_the_server_callback() -> None:
    async def run() -> None:
        bootstrap = object.__new__(AppBootstrap)
        cancelled = asyncio.Event()
        owner_cancel = asyncio.Event()
        owner_cancel.set()

        async def operation() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with pytest.raises(PermissionError, match="owning turn"):
            await bootstrap._await_mcp_owner_operation(
                operation(),
                {"cancel_event": owner_cancel},
                label="elicitation callback",
                maximum_seconds=60,
            )
        assert cancelled.is_set()

    asyncio.run(run())


def test_bootstrap_mcp_elicitation_uses_registered_owned_protocol_and_cleans_up() -> None:
    async def run() -> None:
        session = _BootstrapElicitationSession(
            response={"answer": "use-node"},
        )
        bootstrap = object.__new__(AppBootstrap)
        bootstrap._resolve_mcp_request_session = lambda _params: (
            session,
            {"conversation_id": "conversation-a"},
        )

        result = await bootstrap._handle_mcp_elicitation({
            "prompt": "Which runtime should be used?",
            "schema": {"type": "string", "enum": ["use-node", "use-bun"]},
        })

        assert result == {"action": "submit", "response": {"answer": "use-node"}}
        assert len(session.sent) == 1
        payload = session.sent[0]
        assert payload["conversation_id"] == "conversation-a"
        assert payload["type"] == "control_request"
        assert payload["request"] == {
            "subtype": "elicitation",
            "tool_use_id": payload["request_id"],
            "prompt": "Which runtime should be used?",
            "question": "Which runtime should be used?",
            "schema": {"type": "string", "enum": ["use-node", "use-bun"]},
        }
        assert session.terminal == [{
            "request_ids": [payload["request_id"]],
            "reason": "mcp_elicitation_resolved",
            "conversation_id": "conversation-a",
        }]
        assert session.turn_wait_state.pending_approvals == {}
        assert session.turn_wait_state.pending_elicitations == {}
        assert session.turn_wait_state.pending_approval_payloads == {}

    asyncio.run(run())


def test_bootstrap_mcp_elicitation_fails_immediately_when_prompt_cannot_be_delivered() -> None:
    async def run() -> None:
        session = _BootstrapElicitationSession(delivered=False)
        bootstrap = object.__new__(AppBootstrap)
        bootstrap._resolve_mcp_request_session = lambda _params: (
            session,
            {"conversation_id": "conversation-a"},
        )

        result = await bootstrap._handle_mcp_elicitation({"prompt": "Continue?"})

        assert result == {
            "action": "cancel",
            "error": "MCP elicitation could not be delivered",
        }
        request_id = session.sent[0]["request_id"]
        assert session.terminal == [{
            "request_ids": [request_id],
            "reason": "mcp_elicitation_delivery_failed",
            "conversation_id": "conversation-a",
        }]
        assert session.turn_wait_state.pending_approvals == {}
        assert session.turn_wait_state.pending_elicitations == {}
        assert session.turn_wait_state.pending_approval_payloads == {}

    asyncio.run(run())


def test_bootstrap_mcp_elicitation_rejection_and_timeout_emit_terminal_events() -> None:
    async def run_rejection() -> None:
        session = _BootstrapElicitationSession(
            response={"action": "reject"},
        )
        bootstrap = object.__new__(AppBootstrap)
        bootstrap._resolve_mcp_request_session = lambda _params: (
            session,
            {"conversation_id": "conversation-a"},
        )

        result = await bootstrap._handle_mcp_elicitation({"prompt": "Continue?"})

        assert result == {"action": "cancel", "error": "User cancelled the elicitation"}
        assert session.terminal[-1]["reason"] == "mcp_elicitation_rejected"
        assert session.turn_wait_state.pending_approvals == {}
        assert session.turn_wait_state.pending_elicitations == {}
        assert session.turn_wait_state.pending_approval_payloads == {}

    async def run_timeout() -> None:
        session = _BootstrapElicitationSession()
        bootstrap = object.__new__(AppBootstrap)
        bootstrap._resolve_mcp_request_session = lambda _params: (
            session,
            {"conversation_id": "conversation-a"},
        )

        async def timeout(*_args, **_kwargs):
            raise TimeoutError

        bootstrap._await_mcp_owner_operation = timeout
        result = await bootstrap._handle_mcp_elicitation({"prompt": "Continue?"})

        assert result == {"action": "cancel", "error": "User response timed out"}
        assert session.terminal[-1]["reason"] == "mcp_elicitation_timeout"
        assert session.turn_wait_state.pending_approvals == {}
        assert session.turn_wait_state.pending_elicitations == {}
        assert session.turn_wait_state.pending_approval_payloads == {}

    asyncio.run(run_rejection())
    asyncio.run(run_timeout())


def test_provider_oauth_prompt_restores_and_rejects_wrong_owner_response() -> None:
    async def run() -> None:
        session = _OAuthControlSession()
        callbacks = _ProviderOAuthCallbacks(session, "conv_oauth_a", "github-copilot")
        prompt_task = asyncio.create_task(callbacks.prompt({
            "message": "Enter the device verification code",
        }))

        for _ in range(20):
            if session.turn_wait_state.provider_oauth_pending:
                break
            await asyncio.sleep(0)
        assert len(session.turn_wait_state.provider_oauth_pending) == 1
        request_id = next(iter(session.turn_wait_state.provider_oauth_pending))
        pending_future = session.turn_wait_state.provider_oauth_pending[request_id]
        assert request_id not in session.turn_wait_state.pending_approvals
        pending_payload = session.turn_wait_state.pending_approval_payloads[request_id]
        assert pending_payload == {
            "type": "control_request",
            "request_id": request_id,
            "conversation_id": "conv_oauth_a",
            "timeout_seconds": 300.0,
            "expires_at": pending_payload["expires_at"],
            "request": {
                "subtype": "provider_auth_prompt",
                "prompt": "Enter the device verification code",
                "provider": "github-copilot",
                "prompt_type": "text",
                "allow_empty": False,
                "allow_custom": True,
            },
        }
        assert pending_payload["expires_at"] > 0

        await session.reemit_pending_state("conv_oauth_a")
        prompt_events = [
            payload for payload in session.sent
            if payload.get("type") == "control_request" and payload.get("request_id") == request_id
        ]
        assert len(prompt_events) == 2
        assert all(payload["conversation_id"] == "conv_oauth_a" for payload in prompt_events)

        await session.command_dispatcher._handle_control_response(UserCommand(
            type="control_response",
            data={
                "request_id": request_id,
                "conversation_id": "conversation-b",
                "response": {"subtype": "success", "response": {"answer": "wrong-owner"}},
            },
        ))
        await asyncio.sleep(0)
        assert not prompt_task.done()
        assert not pending_future.done()

        await session.command_dispatcher._handle_control_response(UserCommand(
            type="control_response",
            data={
                "request_id": request_id,
                "conversation_id": "conv_oauth_a",
                "response": {"subtype": "success", "response": {"answer": "ABCD-1234"}},
            },
        ))
        assert await prompt_task == "ABCD-1234"
        assert session.turn_wait_state.provider_oauth_pending == {}
        assert session.turn_wait_state.pending_approvals == {}
        assert session.turn_wait_state.pending_approval_payloads == {}
        assert session.terminal[-1]["request_ids"] == [request_id]
        assert session.terminal[-1]["reason"] == "provider_auth_resolved"
        assert session.terminal[-1]["conversation_id"] == "conv_oauth_a"

    asyncio.run(run())


def test_provider_oauth_cancel_requires_exact_owner_and_clears_every_pending_map() -> None:
    async def run() -> None:
        session = _OAuthControlSession()
        callbacks = _ProviderOAuthCallbacks(session, "conv_oauth_a", "github-copilot")
        prompt_task = asyncio.create_task(callbacks.prompt({
            "message": "Enter the verification code",
        }))

        for _ in range(20):
            if session.turn_wait_state.provider_oauth_pending:
                break
            await asyncio.sleep(0)
        request_id = next(iter(session.turn_wait_state.provider_oauth_pending))

        await session.command_dispatcher._handle_control_cancel(UserCommand(
            type="control_cancel_request",
            data={"request_id": request_id, "conversation_id": "conversation-b"},
        ))
        await asyncio.sleep(0)
        assert not prompt_task.done()

        await session.command_dispatcher._handle_control_cancel(UserCommand(
            type="control_cancel_request",
            data={"request_id": request_id, "conversation_id": "conv_oauth_a"},
        ))
        with pytest.raises(PermissionError, match="cancelled by the user"):
            await prompt_task
        assert session.turn_wait_state.provider_oauth_pending == {}
        assert session.turn_wait_state.pending_approvals == {}
        assert session.turn_wait_state.pending_approval_payloads == {}
        assert session.terminal[-1]["reason"] == "provider_auth_rejected"
        assert session.terminal[-1]["conversation_id"] == "conv_oauth_a"

    asyncio.run(run())


def test_provider_oauth_timeout_clears_all_pending_state(monkeypatch) -> None:
    monkeypatch.setattr(_ProviderOAuthCallbacks, "_PROMPT_TIMEOUT_SECONDS", 0.0)

    async def run() -> None:
        session = _OAuthControlSession()
        callbacks = _ProviderOAuthCallbacks(session, "conv_oauth_a", "github-copilot")

        with pytest.raises(asyncio.TimeoutError):
            await callbacks.prompt({"message": "Enter code"})

        assert session.turn_wait_state.provider_oauth_pending == {}
        assert session.turn_wait_state.pending_approvals == {}
        assert session.turn_wait_state.pending_approval_payloads == {}
        assert session.terminal[-1]["reason"] == "provider_auth_timeout"
        assert session.terminal[-1]["conversation_id"] == "conv_oauth_a"

    asyncio.run(run())


def test_provider_oauth_notifications_project_exact_owner_data_and_redact_display_secrets() -> None:
    async def run() -> None:
        session = _OAuthControlSession()
        callbacks = _ProviderOAuthCallbacks(session, "conv_oauth_a", "github-copilot")
        exposed_key = "sk-" + "A" * 32

        callbacks.notify({
            "type": "auth_url",
            "url": "https://login.example.test/authorize?state=expected",
            "instructions": f"Open the page. token={exposed_key}",
            "provider": "attacker-controlled",
            "access_token": exposed_key,
        })
        callbacks.notify({
            "type": "device_code",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://login.example.test/device",
            "interval_seconds": 5,
            "expires_in_seconds": 900,
            "refresh_token": exposed_key,
        })
        callbacks.notify({
            "type": "info",
            "message": f"Continue without exposing {exposed_key}",
            "links": [{
                "url": "https://help.example.test/oauth",
                "label": f"Help {exposed_key}",
            }],
        })
        callbacks.notify({"type": "progress", "message": "Waiting for authorization"})

        await callbacks.drain()
        projected = {event["type"]: event for event in session.sent}
        assert projected["llm.provider.oauth.auth"] == {
            "type": "llm.provider.oauth.auth",
            "conversation_id": "conv_oauth_a",
            "provider": "github-copilot",
            "url": "https://login.example.test/authorize?state=expected",
            "instructions": "Open the page. token=[REDACTED_SECRET]",
        }
        assert projected["llm.provider.oauth.device_code"] == {
            "type": "llm.provider.oauth.device_code",
            "conversation_id": "conv_oauth_a",
            "provider": "github-copilot",
            "userCode": "ABCD-EFGH",
            "verificationUri": "https://login.example.test/device",
            "intervalSeconds": 5,
            "expiresInSeconds": 900,
        }
        assert projected["llm.provider.oauth.info"]["links"] == [{
            "url": "https://help.example.test/oauth",
            "label": "Help [REDACTED_SECRET]",
        }]
        assert projected["llm.provider.oauth.progress"]["message"] == "Waiting for authorization"
        assert exposed_key not in repr(session.sent)
        await callbacks.close()

    asyncio.run(run())


def test_provider_oauth_notifications_reject_unsafe_urls_unknown_types_and_lost_owner() -> None:
    async def run() -> None:
        session = _OAuthControlSession()
        callbacks = _ProviderOAuthCallbacks(session, "conv_oauth_a", "github-copilot")

        for url in (
            "javascript:alert(1)",
            "https://user:password@example.test/authorize",
            "https://example.test/has whitespace",
            "https://example.test:invalid/authorize",
        ):
            with pytest.raises(ValueError):
                callbacks.notify({"type": "auth_url", "url": url})
        with pytest.raises(ValueError, match="Unsupported OAuth notification type"):
            callbacks.notify({"type": "token", "message": "secret"})
        with pytest.raises(ValueError, match="absolute HTTP"):
            callbacks.notify({
                "type": "info",
                "message": "Read more",
                "links": [{"url": "file:///tmp/token"}],
            })

        callbacks.notify({"type": "progress", "message": "Waiting"})
        session.active_conversation_id = "conv_oauth_b"
        with pytest.raises(RuntimeError, match="no longer active"):
            await callbacks.drain()
        assert session.sent == []
        await callbacks.close()

    asyncio.run(run())


def test_provider_oauth_prompt_preserves_secret_empty_and_select_contracts() -> None:
    async def wait_for_request(session: _OAuthControlSession) -> tuple[str, dict]:
        for _ in range(40):
            if session.turn_wait_state.provider_oauth_pending:
                request_id = next(iter(session.turn_wait_state.provider_oauth_pending))
                return request_id, session.turn_wait_state.pending_approval_payloads[request_id]
            await asyncio.sleep(0)
        raise AssertionError("OAuth prompt was not registered")

    async def respond(session: _OAuthControlSession, request_id: str, answer: str) -> None:
        await session.command_dispatcher._handle_control_response(UserCommand(
            type="control_response",
            data={
                "request_id": request_id,
                "conversation_id": "conv_oauth_a",
                "response": {"subtype": "success", "response": {"answer": answer}},
            },
        ))

    async def run() -> None:
        session = _OAuthControlSession()
        callbacks = _ProviderOAuthCallbacks(session, "conv_oauth_a", "github-copilot")

        secret_task = asyncio.create_task(callbacks.prompt({
            "type": "secret",
            "message": "Enter the provider secret",
            "placeholder": "paste exactly",
            "allow_empty": True,
        }))
        request_id, payload = await wait_for_request(session)
        assert payload["request"] == {
            "subtype": "provider_auth_prompt",
            "prompt": "Enter the provider secret",
            "provider": "github-copilot",
            "prompt_type": "secret",
            "allow_empty": True,
            "allow_custom": True,
            "placeholder": "paste exactly",
        }
        await respond(session, request_id, "")
        assert await secret_task == ""

        select_task = asyncio.create_task(callbacks.prompt({
            "type": "select",
            "message": "Select the login method",
            "options": [
                {"id": "browser", "label": "Browser", "description": "Open a callback page"},
                {"id": "device_code", "label": "Device code"},
            ],
        }))
        request_id, payload = await wait_for_request(session)
        assert payload["request"] == {
            "subtype": "provider_auth_prompt",
            "prompt": "Select the login method",
            "provider": "github-copilot",
            "prompt_type": "select",
            "allow_empty": False,
            "allow_custom": False,
            "options": [
                {"id": "browser", "label": "Browser", "description": "Open a callback page"},
                {"id": "device_code", "label": "Device code"},
            ],
        }
        await respond(session, request_id, "device_code")
        assert await select_task == "device_code"
        await callbacks.close()

    asyncio.run(run())


def test_provider_oauth_prompt_abort_and_delivery_failures_clear_all_state() -> None:
    async def wait_for_pending(session: _OAuthControlSession) -> None:
        for _ in range(40):
            if session.turn_wait_state.provider_oauth_pending:
                return
            await asyncio.sleep(0)
        raise AssertionError("OAuth prompt was not registered")

    async def run() -> None:
        session = _OAuthControlSession()
        callbacks = _ProviderOAuthCallbacks(session, "conv_oauth_a", "github-copilot")
        prompt_abort_event = asyncio.Event()
        prompt_abort = CancellationSignal(prompt_abort_event)
        prompt_task = asyncio.create_task(callbacks.prompt({
            "type": "manual_code",
            "message": "Paste the redirect URL",
            "signal": prompt_abort,
        }))
        await wait_for_pending(session)
        prompt_abort_event.set()
        with pytest.raises(asyncio.CancelledError):
            await prompt_task
        assert session.turn_wait_state.provider_oauth_pending == {}
        assert session.turn_wait_state.pending_approvals == {}
        assert session.turn_wait_state.pending_approval_payloads == {}

        login_task = asyncio.create_task(callbacks.prompt({"message": "Enter code"}))
        await wait_for_pending(session)
        await callbacks.close()
        with pytest.raises(asyncio.CancelledError):
            await login_task
        assert session.turn_wait_state.provider_oauth_pending == {}

        failed_session = _OAuthControlSession()
        failed_session.delivered = False
        failed_callbacks = _ProviderOAuthCallbacks(
            failed_session,
            "conv_oauth_a",
            "github-copilot",
        )
        failed_callbacks.notify({"type": "progress", "message": "Waiting"})
        with pytest.raises(ConnectionError, match="could not be delivered"):
            await failed_callbacks.drain()
        await failed_callbacks.close()

    asyncio.run(run())


def test_control_response_requires_pending_conversation_turn_and_message_owner() -> None:
    async def run() -> None:
        session = _OAuthControlSession()
        future = asyncio.get_running_loop().create_future()
        session.turn_wait_state.pending_approvals["control-owned"] = future
        session.turn_wait_state.pending_approval_payloads["control-owned"] = {
            "type": "control_request",
            "request_id": "control-owned",
            "conversation_id": "conv_oauth_a",
            "turn_id": "turn-a",
            "message_id": "message-a",
            "request": {
                "subtype": "elicitation",
                "tool_use_id": "control-owned",
                "prompt": "Choose",
                "question": "Choose",
            },
        }

        for owner in (
            {},
            {"conversation_id": "conversation-b", "turn_id": "turn-a", "message_id": "message-a"},
            {"conversation_id": "conv_oauth_a", "turn_id": "turn-b", "message_id": "message-a"},
            {"conversation_id": "conv_oauth_a", "turn_id": "turn-a", "message_id": "message-b"},
        ):
            await session.command_dispatcher._handle_control_response(UserCommand(
                type="control_response",
                data={
                    "request_id": "control-owned",
                    **owner,
                    "response": {"subtype": "success", "response": {"answer": "ignored"}},
                },
            ))
            assert not future.done()

        await session.command_dispatcher._handle_control_response(UserCommand(
            type="control_response",
            data={
                "request_id": "control-owned",
                "conversation_id": "conv_oauth_a",
                "turn_id": "turn-a",
                "message_id": "message-a",
                "response": {"subtype": "success", "response": {"answer": "accepted"}},
            },
        ))
        assert await future == {
            "answer": "accepted",
            "conversation_id": "conv_oauth_a",
            "turn_id": "turn-a",
            "message_id": "message-a",
        }

    asyncio.run(run())
