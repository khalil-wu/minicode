from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.mcp.client import MCPToolDef
from backend.mcp.registry import MCPToolProxy
from backend.tools.mcp_tools import (
    ListMcpPromptsTool,
    ListMcpResourceNotificationsTool,
    ListMcpResourcesTool,
    ListMcpResourceTemplatesTool,
    ReadMcpResourceTool,
)
from backend.tools.swarm_tools import SendMessageTool


class _CatalogClient:
    def __init__(self, entry: SimpleNamespace | None = None, *, fail: bool = False) -> None:
        self.entry = entry
        self.fail = fail

    async def _list(self) -> list[SimpleNamespace]:
        if self.fail:
            raise RuntimeError("catalog unavailable")
        return [self.entry] if self.entry is not None else []

    async def list_resources(self) -> list[SimpleNamespace]:
        return await self._list()

    async def list_resource_templates(self) -> list[SimpleNamespace]:
        return await self._list()

    async def list_prompts(self) -> list[SimpleNamespace]:
        return await self._list()


class _Manager:
    def __init__(self, clients: list[tuple[str, object]]) -> None:
        self.clients = clients

    def iter_connected_clients(self) -> list[tuple[str, object]]:
        return self.clients


@pytest.mark.parametrize(
    "tool_type, entry, expected",
    [
        (
            ListMcpResourcesTool,
            SimpleNamespace(uri="docs://item", name="Item", mime_type="text/plain"),
            "docs://item",
        ),
        (
            ListMcpResourceTemplatesTool,
            SimpleNamespace(
                uri_template="docs://{name}", name="Template", mime_type="text/plain", description=""
            ),
            "docs://{name}",
        ),
        (
            ListMcpPromptsTool,
            SimpleNamespace(name="review", description="", arguments=[]),
            "Prompt: review",
        ),
    ],
)
def test_mcp_catalog_listing_reports_server_failures(tool_type, entry, expected) -> None:
    failed = _CatalogClient(fail=True)
    only_failure = asyncio.run(tool_type(_Manager([("broken", failed)])).execute({}))
    assert only_failure.is_error
    assert "broken: RuntimeError: catalog unavailable" in only_failure.content

    partial = asyncio.run(
        tool_type(_Manager([("working", _CatalogClient(entry)), ("broken", failed)])).execute({})
    )
    assert not partial.is_error
    assert partial.status == "partial"
    assert expected in partial.content
    assert "Unavailable servers: broken: RuntimeError: catalog unavailable" in partial.content


def test_empty_mcp_resource_is_a_successful_read() -> None:
    class _EmptyResourceClient:
        async def read_resource(self, uri: str) -> str:
            assert uri == "docs://empty"
            return ""

    result = asyncio.run(
        ReadMcpResourceTool(_Manager([("docs", _EmptyResourceClient())])).execute(
            {"uri": "docs://empty", "server": "docs"}
        )
    )
    assert not result.is_error
    assert result.content == "Resource docs://empty is empty."


def test_mcp_notification_read_reports_a_failed_queue() -> None:
    class _NotificationClient:
        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail

        def list_resource_subscriptions(self) -> list[str]:
            return []

        def consume_resource_notifications(self) -> list[dict[str, str]]:
            if self.fail:
                raise RuntimeError("queue unavailable")
            return [{"method": "resources/updated", "uri": "docs://item"}]

    only_failure = asyncio.run(
        ListMcpResourceNotificationsTool(
            _Manager([("broken", _NotificationClient(fail=True))])
        ).execute({})
    )
    assert only_failure.is_error
    assert "broken: RuntimeError: queue unavailable" in only_failure.content

    partial = asyncio.run(
        ListMcpResourceNotificationsTool(
            _Manager([("docs", _NotificationClient()), ("broken", _NotificationClient(fail=True))])
        ).execute({})
    )
    assert partial.status == "partial"
    assert "docs://item" in partial.content
    assert "Unavailable servers: broken: RuntimeError: queue unavailable" in partial.content


def test_mailbox_send_keeps_read_only_delegation_without_parallel_reordering() -> None:
    tool = SendMessageTool()
    assert tool.is_read_only({"recipient": "parent", "message": "report"})
    assert not tool.is_idempotent({"recipient": "parent", "message": "report"})
    assert not tool.is_concurrency_safe({"recipient": "parent", "message": "report"})


def test_mcp_proxy_exposes_its_effective_read_only_contract() -> None:
    proxy = MCPToolProxy(
        "desktop",
        MCPToolDef(
            name="inspect",
            description="Inspect selected context",
            annotations={"readOnlyHint": True, "openWorldHint": True},
        ),
        None,
    )
    assert not proxy.is_read_only({})
    assert proxy.to_runtime_metadata()["read_only"] is False
