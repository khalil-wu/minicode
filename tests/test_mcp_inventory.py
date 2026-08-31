from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import types
from mcp.shared.exceptions import McpError

from backend.mcp.client import (
    MCPAuthenticationError,
    MCPPromptArgDef,
    MCPPromptDef,
    MCPResourceDef,
    MCPResourceTemplateDef,
    MCPServerCapabilities,
)
from backend.mcp.oauth import MCPAuthenticationRequired
from backend.services import mcp_service
from backend.services.mcp_service import MCPInventoryServiceError, list_mcp_inventory
from backend.ws.command_dispatcher import COMMAND_BACKLOG_BYPASS_TYPES
from backend.ws.handlers import mcp as mcp_handlers


class _Manager:
    def __init__(self, client=None, lifecycle=None) -> None:
        self.client = client
        self.lifecycle = lifecycle
        self.client_lookups: list[str] = []

    def get_client(self, name: str):
        self.client_lookups.append(name)
        return self.client

    def get_server_lifecycle(self, name: str):
        return self.lifecycle


class _ConcurrentInventoryClient:
    def __init__(self) -> None:
        self.server_capabilities = MCPServerCapabilities(
            resources=True,
            resources_subscribe=True,
            resources_list_changed=True,
            prompts=True,
        )
        self.calls: list[str] = []
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()

    async def _result(self, method: str, value):
        self.calls.append(method)
        if len(self.calls) == 3:
            self.all_started.set()
        await self.release.wait()
        return value

    async def list_resources(self):
        return await self._result(
            "resources/list",
            [MCPResourceDef(uri="file:///guide.md", name="Guide", description="Project guide", mime_type="text/markdown")],
        )

    async def list_resource_templates(self):
        return await self._result(
            "resources/templates/list",
            [MCPResourceTemplateDef(uri_template="repo://{path}", name="Repository file", description="Read a repository file")],
        )

    async def list_prompts(self):
        return await self._result(
            "prompts/list",
            [MCPPromptDef(
                name="review",
                description="Review a change",
                arguments=[MCPPromptArgDef(name="path", description="File path", required=True)],
            )],
        )


def test_inventory_lists_standard_mcp_content_concurrently_on_explicit_call() -> None:
    async def scenario():
        client = _ConcurrentInventoryClient()
        manager = _Manager(client)
        task = asyncio.create_task(list_mcp_inventory(manager, " docs "))

        await asyncio.wait_for(client.all_started.wait(), timeout=1)
        assert not task.done()
        assert set(client.calls) == {
            "resources/list",
            "resources/templates/list",
            "prompts/list",
        }
        client.release.set()
        return manager, await task

    manager, inventory = asyncio.run(scenario())

    assert manager.client_lookups == ["docs"]
    assert inventory == {
        "server_name": "docs",
        "capabilities": {
            "resources": True,
            "resources_subscribe": True,
            "resources_list_changed": True,
            "prompts": True,
        },
        "resources": [{
            "uri": "file:///guide.md",
            "name": "Guide",
            "description": "Project guide",
            "mime_type": "text/markdown",
        }],
        "resource_templates": [{
            "uri_template": "repo://{path}",
            "name": "Repository file",
            "description": "Read a repository file",
            "mime_type": "text/plain",
        }],
        "prompts": [{
            "name": "review",
            "description": "Review a change",
            "arguments": [{"name": "path", "description": "File path", "required": True}],
        }],
        "empty": False,
    }


def test_inventory_round_trips_through_a_real_official_sdk_stdio_server() -> None:
    pytest.importorskip("mcp.server.fastmcp")
    fixture_server = Path(__file__).parent / "fixtures" / "mcp_inventory_server.py"

    async def scenario():
        from backend.mcp.client import MCPClient

        client = MCPClient(
            "inventory-stdio-integration",
            command=sys.executable,
            args=[str(fixture_server)],
            startup_timeout=8.0,
            request_timeout=8.0,
            tool_timeout=8.0,
        )
        try:
            await client.connect()
            return await list_mcp_inventory(_Manager(client), "inventory-stdio-integration")
        finally:
            await client.close()

    inventory = asyncio.run(scenario())

    assert inventory["capabilities"]["resources"] is True
    assert inventory["capabilities"]["prompts"] is True
    assert inventory["resources"] == [{
        "uri": "fixture://guide",
        "name": "Guide",
        "description": "A fixed resource exposed by the fixture server.",
        "mime_type": "text/markdown",
    }]
    assert inventory["resource_templates"] == [{
        "uri_template": "fixture://repo/{path}",
        "name": "Repository file",
        "description": "A resource template exposed by the fixture server.",
        "mime_type": "text/plain",
    }]
    assert inventory["prompts"] == [{
        "name": "review",
        "description": "Review a repository path with an optional tone.",
        "arguments": [
            {"name": "path", "description": "", "required": True},
            {"name": "tone", "description": "", "required": False},
        ],
    }]
    assert inventory["empty"] is False


def test_inventory_skips_unadvertised_methods_and_reports_empty() -> None:
    class Client:
        server_capabilities = MCPServerCapabilities()

        async def list_resources(self):  # pragma: no cover - must never run
            raise AssertionError("resources/list must respect negotiated capabilities")

        async def list_resource_templates(self):  # pragma: no cover - must never run
            raise AssertionError("resources/templates/list must respect negotiated capabilities")

        async def list_prompts(self):  # pragma: no cover - must never run
            raise AssertionError("prompts/list must respect negotiated capabilities")

    inventory = asyncio.run(list_mcp_inventory(_Manager(Client()), "empty"))

    assert inventory["resources"] == []
    assert inventory["resource_templates"] == []
    assert inventory["prompts"] == []
    assert inventory["empty"] is True


@pytest.mark.parametrize(
    ("lifecycle", "expected_code", "recoverable"),
    [
        (None, "server_not_found", False),
        ({"phase": "stopped"}, "not_connected", True),
        ({"phase": "auth_required"}, "authentication_required", False),
        ({"phase": "expired"}, "authentication_expired", False),
    ],
)
def test_inventory_projects_server_lifecycle_failures(lifecycle, expected_code: str, recoverable: bool) -> None:
    with pytest.raises(MCPInventoryServiceError) as raised:
        asyncio.run(list_mcp_inventory(_Manager(None, lifecycle), "missing"))

    assert raised.value.code == expected_code
    assert raised.value.recoverable is recoverable


def test_inventory_requires_completed_capability_negotiation() -> None:
    client = SimpleNamespace(server_capabilities=None)

    with pytest.raises(MCPInventoryServiceError) as raised:
        asyncio.run(list_mcp_inventory(_Manager(client), "pending"))

    assert raised.value.code == "capabilities_unavailable"
    assert raised.value.recoverable is True


def _wrapped_transport_error() -> RuntimeError:
    try:
        raise ConnectionError("socket closed")
    except ConnectionError as cause:
        try:
            raise RuntimeError("request wrapper") from cause
        except RuntimeError as wrapped:
            return wrapped


@pytest.mark.parametrize(
    ("failure", "expected_code", "recoverable", "mcp_code"),
    [
        (MCPAuthenticationRequired("https://auth.example/login"), "authentication_required", False, None),
        (MCPAuthenticationError(expired=True), "authentication_expired", False, None),
        (McpError(types.ErrorData(code=403, message="forbidden")), "authentication_required", False, 403),
        (McpError(types.ErrorData(code=408, message="late")), "timeout", True, 408),
        (McpError(types.ErrorData(code=-32602, message="bad params")), "protocol_error", True, -32602),
        (_wrapped_transport_error(), "transport_error", True, None),
    ],
)
def test_inventory_projects_auth_protocol_timeout_and_transport_failures(
    failure: BaseException,
    expected_code: str,
    recoverable: bool,
    mcp_code: int | None,
) -> None:
    class Client:
        server_capabilities = MCPServerCapabilities(prompts=True)

        async def list_prompts(self):
            raise failure

    with pytest.raises(MCPInventoryServiceError) as raised:
        asyncio.run(list_mcp_inventory(_Manager(Client()), "remote"))

    assert raised.value.code == expected_code
    assert raised.value.recoverable is recoverable
    if mcp_code is not None:
        assert raised.value.details["mcp_code"] == mcp_code


def test_inventory_applies_the_existing_mcp_request_timeout(monkeypatch) -> None:
    class Client:
        server_capabilities = MCPServerCapabilities(prompts=True)

        async def list_prompts(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(mcp_service, "MCP_REQUEST_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(MCPInventoryServiceError) as raised:
        asyncio.run(list_mcp_inventory(_Manager(Client()), "slow"))

    assert raised.value.code == "timeout"
    assert raised.value.recoverable is True


def test_inventory_propagates_external_task_cancellation() -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        class Client:
            server_capabilities = MCPServerCapabilities(prompts=True)

            async def list_prompts(self):
                started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(list_mcp_inventory(_Manager(Client()), "cancel-me"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


class _Session:
    def __init__(self) -> None:
        self.events = []

    async def send_event(self, event) -> None:
        self.events.append(event)


def _command_event(session: _Session, command: str):
    return next(event for event in session.events if event.data.get("command") == command)


def test_inventory_handler_returns_inventory_in_the_correlated_command_result(monkeypatch) -> None:
    inventory = {"server_name": "docs", "resources": [], "resource_templates": [], "prompts": [], "empty": True}

    async def list_inventory(manager, name: str):
        assert manager is manager_token
        assert name == "docs"
        return inventory

    manager_token = object()
    monkeypatch.setattr("backend.api.routes_health.get_mcp_manager", lambda: manager_token)
    monkeypatch.setattr(mcp_handlers, "list_mcp_inventory", list_inventory)
    session = _Session()

    assert asyncio.run(mcp_handlers.handle_mcp_inventory_list(
        session,
        {"name": "docs", "operation_id": "inventory-1"},
    )) is True

    event = _command_event(session, "mcp.inventory.list")
    assert event.data["level"] == "info"
    assert event.data["data"] == {"operation_id": "inventory-1", "inventory": inventory}
    assert session._mcp_inventory_tasks == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"operation_id": "inventory-1"},
        {"name": "docs"},
        {"name": "docs", "client_command_id": "transport-id"},
    ],
)
def test_inventory_handler_requires_explicit_name_and_operation_id(payload) -> None:
    session = _Session()

    asyncio.run(mcp_handlers.handle_mcp_inventory_list(session, payload))

    event = _command_event(session, "mcp.inventory.list")
    assert event.data["level"] == "error"
    assert event.data["data"] == {"error_code": "invalid_request", "recoverable": False}


def test_inventory_handler_rejects_duplicate_operation_ids(monkeypatch) -> None:
    async def should_not_run(*_args):  # pragma: no cover - duplicate must be fenced first
        raise AssertionError("duplicate inventory operation reached the MCP manager")

    session = _Session()
    session._mcp_inventory_tasks = {"same": object()}
    monkeypatch.setattr(mcp_handlers, "list_mcp_inventory", should_not_run)

    asyncio.run(mcp_handlers.handle_mcp_inventory_list(
        session,
        {"name": "docs", "operation_id": "same"},
    ))

    event = _command_event(session, "mcp.inventory.list")
    assert event.data["data"]["error_code"] == "operation_conflict"
    assert event.data["data"]["operation_id"] == "same"


def test_inventory_handler_preserves_typed_service_error_details(monkeypatch) -> None:
    async def fail(*_args):
        raise MCPInventoryServiceError(
            "MCP protocol error: bad request",
            code="protocol_error",
            recoverable=True,
            details={"mcp_code": -32602},
        )

    monkeypatch.setattr("backend.api.routes_health.get_mcp_manager", lambda: object())
    monkeypatch.setattr(mcp_handlers, "list_mcp_inventory", fail)
    session = _Session()

    asyncio.run(mcp_handlers.handle_mcp_inventory_list(
        session,
        {"name": "docs", "operation_id": "inventory-2"},
    ))

    event = _command_event(session, "mcp.inventory.list")
    assert event.data["level"] == "error"
    assert event.data["message"] == "MCP protocol error: bad request"
    assert event.data["data"] == {
        "operation_id": "inventory-2",
        "name": "docs",
        "error_code": "protocol_error",
        "recoverable": True,
        "mcp_code": -32602,
    }


def test_inventory_cancel_stops_the_live_request_and_cleans_session_state(monkeypatch) -> None:
    async def scenario() -> _Session:
        started = asyncio.Event()

        async def wait_for_cancel(*_args):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr("backend.api.routes_health.get_mcp_manager", lambda: object())
        monkeypatch.setattr(mcp_handlers, "list_mcp_inventory", wait_for_cancel)
        session = _Session()
        list_task = asyncio.create_task(mcp_handlers.handle_mcp_inventory_list(
            session,
            {"name": "docs", "operation_id": "inventory-3"},
        ))
        await started.wait()
        assert "inventory-3" in session._mcp_inventory_tasks

        await mcp_handlers.handle_mcp_inventory_cancel(
            session,
            {"name": "docs", "operation_id": "inventory-3"},
        )
        await list_task
        return session

    session = asyncio.run(scenario())

    list_event = _command_event(session, "mcp.inventory.list")
    cancel_event = _command_event(session, "mcp.inventory.cancel")
    assert list_event.data["data"]["error_code"] == "cancelled"
    assert cancel_event.data["data"] == {
        "operation_id": "inventory-3",
        "name": "docs",
        "cancelled": True,
    }
    assert session._mcp_inventory_tasks == {}
    assert session._mcp_inventory_cancelled == set()


def test_inventory_cancel_reports_when_operation_is_already_absent() -> None:
    session = _Session()

    asyncio.run(mcp_handlers.handle_mcp_inventory_cancel(
        session,
        {"name": "docs", "operation_id": "gone"},
    ))

    event = _command_event(session, "mcp.inventory.cancel")
    assert event.data["data"]["cancelled"] is False


def test_inventory_handler_does_not_convert_connection_shutdown_into_user_cancellation(monkeypatch) -> None:
    async def scenario() -> _Session:
        started = asyncio.Event()

        async def block(*_args):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr("backend.api.routes_health.get_mcp_manager", lambda: object())
        monkeypatch.setattr(mcp_handlers, "list_mcp_inventory", block)
        session = _Session()
        handler_task = asyncio.create_task(mcp_handlers.handle_mcp_inventory_list(
            session,
            {"name": "docs", "operation_id": "inventory-4"},
        ))
        await started.wait()
        handler_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handler_task
        return session

    session = asyncio.run(scenario())

    assert session.events == []
    assert session._mcp_inventory_tasks == {}
    assert session._mcp_inventory_cancelled == set()


def test_inventory_cancel_can_bypass_the_websocket_command_backlog() -> None:
    assert "mcp.inventory.cancel" in COMMAND_BACKLOG_BYPASS_TYPES
