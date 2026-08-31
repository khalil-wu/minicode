"""Tests for the tool lifecycle event contract (tool_call / tool_result)."""

from __future__ import annotations

import time

from backend.agent.message import AgentEvent
from backend.agent.tool_issues import classify_tool_issue
from backend.agent.tool_execution import (
    display_summary_for_result,
    result_kind_for_tool,
)
from backend.agent.tool_projection import projection_for_tool
from backend.llm.base import ToolCallEvent
from backend.tools.base import PermissionLevel, ToolResult
from backend.tools.registry import ToolRegistry
from backend.mcp.registry import MCPToolProxy
from backend.mcp.client import MCPCallResult, MCPToolDef


class TestToolCallEvent:
    def test_minimal_tool_call_has_required_fields(self):
        ev = AgentEvent.tool_call(id="tc_1", name="read_file", args={"path": "a.py"})
        msg = ev.to_ws_message()
        assert msg["type"] == "tool_call"
        assert msg["id"] == "tc_1"
        assert msg["name"] == "read_file"
        assert msg["args"] == {"path": "a.py"}
        assert msg["status"] == "running"

    def test_tool_call_with_metadata(self):
        now = int(time.time() * 1000)
        ev = AgentEvent.tool_call(
            id="tc_2",
            name="web_fetch",
            args={"url": "https://example.com"},
            started_at=now,
            display_hint="Fetching page",
        )
        msg = ev.to_ws_message()
        assert msg["started_at"] == now
        assert msg["display_hint"] == "Fetching page"

    def test_tool_call_omits_empty_optional_fields(self):
        ev = AgentEvent.tool_call(id="tc_3", name="grep_files", args={"pattern": "TODO"})
        msg = ev.to_ws_message()
        assert "started_at" not in msg
        assert "display_hint" not in msg
        assert "input_summary" not in msg


class TestToolResultEvent:
    def test_minimal_tool_result_has_required_fields(self):
        ev = AgentEvent.tool_result(id="tc_1", summary="Found 3 matches")
        msg = ev.to_ws_message()
        assert msg["type"] == "tool_result"
        assert msg["id"] == "tc_1"
        assert msg["summary"] == "Found 3 matches"
        assert msg["is_error"] is False
        assert msg["status"] == "success"

    def test_error_result_sets_failed_status(self):
        ev = AgentEvent.tool_result(id="tc_2", summary="File not found", is_error=True)
        msg = ev.to_ws_message()
        assert msg["is_error"] is True
        assert msg["status"] == "failed"

    def test_explicit_status_overrides_default(self):
        ev = AgentEvent.tool_result(id="tc_3", summary="Skipped", status="blocked")
        msg = ev.to_ws_message()
        assert msg["status"] == "blocked"
        assert msg["is_error"] is False

    def test_full_metadata_passthrough(self):
        ev = AgentEvent.tool_result(
            id="tc_4",
            summary="Fetched page",
            duration_ms=1234,
            display_summary="Fetched page: example.com",
            result_kind="web",
            limitation="partial extraction",
            provider="hosted provider",
            source_url="https://example.com",
            evidence_type="fetched",
        )
        msg = ev.to_ws_message()
        assert msg["duration_ms"] == 1234
        assert msg["display_summary"] == "Fetched page: example.com"
        assert msg["result_kind"] == "web"
        assert msg["limitation"] == "partial extraction"
        assert msg["provider"] == "hosted provider"
        assert msg["source_url"] == "https://example.com"
        assert msg["evidence_type"] == "fetched"

    def test_omits_empty_optional_fields(self):
        ev = AgentEvent.tool_result(id="tc_5", summary="ok")
        msg = ev.to_ws_message()
        assert "duration_ms" not in msg
        assert "display_summary" not in msg
        assert "result_kind" not in msg
        assert "limitation" not in msg
        assert "source_url" not in msg

    def test_structured_error_info_passthrough(self):
        ev = AgentEvent.tool_result(
            id="tc_6",
            summary="missing required url",
            is_error=True,
            error_info={
                "code": "tool.schema.missing_required",
                "category": "validation",
                "user_message": "工具调用缺少必要参数。",
                "model_observation": "Repair the args before retrying.",
                "developer_detail": "missing required url",
                "recoverable": True,
            },
        )

        msg = ev.to_ws_message()

        assert msg["error_info"]["code"] == "tool.schema.missing_required"
        assert msg["error_info"]["category"] == "validation"
        assert msg["error_info"]["developer_detail"] == "missing required url"


class _ProjectionTool:
    name = "custom_projection_tool"

    def to_projection_metadata(self):
        return {
            "result_kind": "web",
            "activity_kind": "webSearch",
            "display_label": "Fetch documentation",
        }


class TestToolOwnedProjection:
    def test_uses_metadata_declared_by_registered_tool(self):
        registry = ToolRegistry()
        registry.register(_ProjectionTool())

        projection = projection_for_tool("custom_projection_tool", registry)

        assert projection.result_kind == "web"
        assert projection.activity_kind == "webSearch"
        assert projection.display_hint == "Fetch documentation"
        assert result_kind_for_tool("custom_projection_tool", registry) == "web"

    def test_unknown_tool_remains_generic_without_name_inference(self):
        projection = projection_for_tool("web_search_like_custom_name", ToolRegistry())

        assert projection.result_kind == "generic"
        assert projection.activity_kind == "genericTool"
        assert projection.display_hint == "web_search_like_custom_name"


class TestToolErrorTaxonomy:
    def _tc(self, name: str, args: dict) -> ToolCallEvent:
        return ToolCallEvent(id="tc_error", name=name, arguments=args)

    def test_classifies_missing_required_args(self):
        info = classify_tool_issue(
            self._tc("web_fetch", {}),
            ToolResult(
                content="Tool 'web_fetch' is missing required argument(s): url",
                is_error=True,
                error_kind="validation_error",
                user_summary="工具调用缺少必要参数。",
                projection="error",
            ),
            "blocked",
        )

        assert info is not None
        assert info.error_kind == "validation_error"
        assert info.projection == "error"
        assert info.recoverable is True

    def test_classifies_generated_content_repair_as_nonfatal(self):
        info = classify_tool_issue(
            self._tc("write_file", {"file_path": "README.md"}),
            ToolResult(
                content="Tool 'write_file' requires generated content for: content. Generate the required content first, then call write_file with all required fields.",
                is_error=False,
                error_kind="missing_generated_content",
                user_summary="需要先生成完整内容。",
                projection="status",
            ),
            "blocked",
        )

        assert info is not None
        assert info.error_kind == "missing_generated_content"
        assert info.projection == "status"
        assert info.recoverable is True

    def test_classifies_disabled_tools_and_stale_evidence(self):
        disabled = classify_tool_issue(
            self._tc("web_search", {"query": "weather"}),
            ToolResult(
                content="Tool 'web_search' is disabled for this turn. Continue without calling it.",
                is_error=False,
                error_kind="tool_disabled",
                user_summary="该工具本轮不可用。",
                projection="status",
            ),
            "blocked",
        )
        stale = classify_tool_issue(
            self._tc("web_fetch", {"url": "https://example.com"}),
            ToolResult(
                content="Current evidence is stale; refresh the source.",
                is_error=False,
                error_kind="stale_evidence",
                user_summary="当前证据可能已经过期。",
                projection="warning",
            ),
            "blocked",
        )

        assert disabled is not None
        assert disabled.error_kind == "tool_disabled"
        assert disabled.projection == "status"
        assert stale is not None
        assert stale.error_kind == "stale_evidence"
        assert stale.projection == "warning"

    def test_classifies_proxy_auth_as_network_error(self):
        info = classify_tool_issue(
            self._tc("web_search", {"query": "weather"}),
            ToolResult(content="Search failed: 407 Proxy Authentication Required", is_error=True),
            "failed",
        )

        assert info is not None
        assert info.error_kind == "execution_error"
        assert info.user_summary == "工具执行失败。"

    def test_does_not_infer_permission_denial_from_error_copy(self):
        info = classify_tool_issue(
            self._tc("read_file", {"file_path": "settings.json"}),
            ToolResult(
                content="路径 'C:\\Desktop\\MiniCode\\settings.json' 不在允许范围内。允许的路径: ['./src']。禁止的路径模式: ['settings.json']。",
                is_error=True,
            ),
            "blocked",
        )

        assert info is not None
        assert info.error_kind == "execution_error"
        assert info.user_summary == "工具执行失败。"
        assert info.projection == "error"

    def test_preserves_structured_permission_denial(self):
        info = classify_tool_issue(
            self._tc("read_file", {"file_path": "settings.json"}),
            ToolResult(
                content="Policy rejected the read.",
                is_error=True,
                error_kind="permission_required",
                user_summary="该工具调用被权限策略阻止。",
                developer_detail="matched deny rule",
                projection="approval",
                model_observation="Use a path inside the allowed workspace.",
            ),
            "blocked",
        )

        assert info is not None
        assert info.error_kind == "permission_required"
        assert info.developer_detail == "matched deny rule"
        assert info.projection == "approval"


class TestDisplaySummaryGeneration:
    def _tc(self, name: str, args: dict) -> ToolCallEvent:
        return ToolCallEvent(id="tc_test", name=name, arguments=args)

    def test_registered_tool_label_is_used_without_argument_inference(self):
        registry = ToolRegistry()
        registry.register(_ProjectionTool())
        tc = self._tc("custom_projection_tool", {"url": "https://example.com/page"})

        summary = display_summary_for_result(
            tc,
            ToolResult(content="Fetched ok"),
            status="success",
            tool_registry=registry,
        )

        assert summary == "Completed: Fetch documentation"

    def test_terminal_status_uses_generic_registered_label(self):
        tc = self._tc("unknown_tool", {"secret": "not projected"})

        assert display_summary_for_result(tc, ToolResult(content="Error"), status="failed") == "Failed: unknown_tool"
        assert display_summary_for_result(tc, ToolResult(content="Denied"), status="blocked") == "Blocked: unknown_tool"
        assert display_summary_for_result(tc, ToolResult(content="Stopped"), status="cancelled") == "Cancelled: unknown_tool"
        assert display_summary_for_result(tc, ToolResult(content="Late"), status="timeout") == "Timed out: unknown_tool"

    def test_result_display_summary_takes_precedence(self):
        tc = self._tc("web_search", {"query": "test"})
        result = ToolResult(content="ok", display_summary="Custom summary from tool")
        summary = display_summary_for_result(tc, result, status="success")
        assert summary == "Custom summary from tool"

class TestMCPProxyContract:
    def test_mcp_proxy_projects_minicode_style_activity_metadata(self):
        class _Client:
            connected = True

        registry = ToolRegistry()
        proxy = MCPToolProxy(
            "github",
            MCPToolDef(
                name="search_users",
                description="Search GitHub users",
                input_schema={"type": "object"},
            ),
            _Client(),  # type: ignore[arg-type]
        )
        registry.register(proxy)

        projection = projection_for_tool(proxy.name, registry)

        assert proxy.name == "mcp__github__search_users"
        assert projection.activity_kind == "mcpToolCall"
        assert projection.display_hint == "github.search_users"

    def test_mcp_error_is_not_reclassified_as_success(self):
        class _Client:
            connected = True

            async def call_tool(self, name, args, **_kwargs):
                return MCPCallResult(
                    content=[{"type": "text", "text": "Tool call timed out"}],
                    is_error=True,
                )

        proxy = MCPToolProxy(
            "memory-rag",
            MCPToolDef(name="remember", description="remember", input_schema={"type": "object"}),
            _Client(),  # type: ignore[arg-type]
        )

        import asyncio

        result = asyncio.run(proxy.execute({"content": "记住这个"}))

        assert result.is_error
        assert "Tool call timed out" in result.content

    def test_open_world_mcp_tool_requires_confirmation_even_when_read_only(self):
        class _Client:
            connected = True

            async def call_tool(self, name, args):
                return MCPCallResult(content=[{"type": "text", "text": "ok"}])

        proxy = MCPToolProxy(
            "figma-desktop",
            MCPToolDef(
                name="get_design_context",
                description="Read selected design context",
                input_schema={"type": "object"},
                annotations={"readOnlyHint": True, "openWorldHint": True},
            ),
            _Client(),  # type: ignore[arg-type]
        )

        assert proxy.is_read_only({}) is False
        assert proxy.permission == PermissionLevel.CONFIRM

    def test_server_name_and_annotations_cannot_make_open_world_tool_read_only(self):
        class _ExtensionClient:
            connected = True
            _command = "node"
            _args = ["untrusted-websearch.js"]

            async def call_tool(self, name, args):
                return MCPCallResult(content=[{"type": "text", "text": "ok"}])

        proxy = MCPToolProxy(
            "websearch",
            MCPToolDef(
                name="fetch_page",
                description="Claims to fetch a public page",
                input_schema={"type": "object"},
                annotations={"readOnlyHint": True, "openWorldHint": True},
            ),
            _ExtensionClient(),  # type: ignore[arg-type]
        )

        assert proxy.is_read_only({"url": "https://example.com"}) is False
        assert proxy.permission == PermissionLevel.CONFIRM

    def test_destructive_mcp_tool_requires_confirmation_even_when_read_only(self):
        class _Client:
            connected = True

            async def call_tool(self, name, args):
                return MCPCallResult(content=[{"type": "text", "text": "ok"}])

        proxy = MCPToolProxy(
            "github",
            MCPToolDef(
                name="delete_issue",
                description="Delete an issue",
                input_schema={"type": "object"},
                annotations={"readOnlyHint": True, "destructiveHint": True},
            ),
            _Client(),  # type: ignore[arg-type]
        )

        assert proxy.is_read_only({}) is False
        assert proxy.permission == PermissionLevel.CONFIRM

    def test_closed_world_read_only_mcp_tool_can_auto_run(self):
        class _Client:
            connected = True

            async def call_tool(self, name, args):
                return MCPCallResult(content=[{"type": "text", "text": "ok"}])

        proxy = MCPToolProxy(
            "local-docs",
            MCPToolDef(
                name="search_docs",
                description="Search indexed local docs",
                input_schema={"type": "object"},
                annotations={"readOnlyHint": True, "openWorldHint": False},
            ),
            _Client(),  # type: ignore[arg-type]
        )

        assert proxy.is_read_only({}) is True
        assert proxy.permission == PermissionLevel.AUTO
