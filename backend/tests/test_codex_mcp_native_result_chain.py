from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import wave
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.config import LLMSettings, TokenBudget
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType, estimate_llm_message_tokens
from backend.llm.openai_adapter import _responses_function_call_output_item, _openai_chat_messages
from backend.mcp.client import MCPCallResult, MCPClient, MCPToolDef
from backend.llm.base import ToolCallEvent
from backend.llm.provider_contracts import ModelDefinition
from backend.tools.tool_search import DeferredToolCatalog
from backend.mcp.config_file import write_mcp_config
from backend.mcp.manager import MCPServerConfig, MCPServerManager
from backend.mcp.registry import MCPToolProxy, MCPToolRegistry
from backend.mcp.result_projection import budget_mcp_output, estimate_audio_tokens, mcp_output_visible_bytes
from backend.tests.test_code_execution import ScriptModel, run_model
from backend.tests.test_harness_mcp_services_regressions import MCPPeer, manager_fixture
from backend.tools.base import ToolResult
from backend.tools.registry import ToolRegistry


@pytest.mark.parametrize("is_error", [False, True])
def test_real_stdio_result_reaches_code_vm_without_private_meta_or_model_truncation(tmp_path, is_error):
    server = tmp_path / "native_mcp.py"
    server.write_text('''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "native-fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "native", "title": "Native row inspector", "description": "Native result", "inputSchema": {"type": "object", "properties": {"fail": {"type": "boolean"}}, "additionalProperties": False}, "outputSchema": {"type": "object", "properties": {"rows": {"type": "array"}}}, "annotations": {"readOnlyHint": True}, "_meta": {"anthropic/alwaysLoad": True}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "x" * 90000, "_meta": {"block-owner": "retained"}}, {"type": "image", "data": "AA==", "mimeType": "image/png", "_meta": {"codex/imageDetail": "original"}}, {"type": "custom-widget", "payload": {"value": "untouched"}}], "structuredContent": {"rows": [{"id": n} for n in range(5000)]}, "isError": request["params"]["arguments"]["fail"], "_meta": {"private-owner": "host-only"}}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
''', encoding="utf-8")

    async def scenario():
        client = MCPClient("native", command=sys.executable, args=["-u", str(server)], workspace_root=tmp_path)
        await client.connect()
        try:
            [definition] = await client.list_tools()
            assert definition.title == "Native row inspector"
            assert definition.output_schema == {"type": "object", "properties": {"rows": {"type": "array"}}}
            tool = MCPToolProxy("native", definition, client)
            code = 'const r = await tools.mcp__native__native({fail: ' + str(is_error).lower() + '}); text({length:r.content[0].text.length,last:r.structuredContent.rows.at(-1).id,error:r.isError,private:r._meta === undefined,block:r.content[0]._meta["block-owner"],detail:r.content[1]._meta["codex/imageDetail"],unknown:r.content[2].payload.value,declared:ALL_TOOLS.find(t => t.name === "mcp__native__native").output_schema.properties.structuredContent.properties.rows.type});'
            model = ScriptModel(code)
            state, builder, _, events, _ = await run_model(tmp_path, model, [tool])
            assert state.terminal_status == "completed", model.reports
            observed = json.loads(model.reports[-1]["output"][0])
            assert observed == {"length": 90000, "last": 4999, "error": is_error, "private": True, "block": "retained", "detail": "original", "unknown": "untouched", "declared": "array"}
            leaf = next(record for record in state.tool_calls if record.call_source.get("kind") == "code_mode")
            assert leaf.result_payload["mcp_result"]["structuredContent"]["rows"][-1] == {"id": 4999}
            assert "_meta" not in leaf.result_payload["mcp_result"]
            assert all("mcp_result" not in event.data for event in events)
            assert [message.name for message in builder._history if message.role == "tool"] == ["tool_exec"]
            schema = tool.model_schema().output_schema
            assert schema["properties"]["structuredContent"] == definition.output_schema
            assert schema["required"] == ["content"]
        finally:
            assert await client.close()
    asyncio.run(scenario())


def _builder(tmp_path, *, limit=80, original=False):
    settings = LLMSettings(api_key="", truncation_policy={"mode": "tokens", "limit": limit}, supports_image_detail_original=original)
    return ContextBuilder(llm=SimpleNamespace(_settings=settings), token_budget=TokenBudget(total=96000), workspace_root=tmp_path)


def test_history_uses_tighter_source_limit_and_cold_restore_preserves_bounded_native_body(tmp_path):
    raw = {"content": [{"type": "text", "text": "ignored-body"}], "structuredContent": {"rows": "头" * 8000}, "isError": True}
    builder = _builder(tmp_path)
    builder.append_user("Read the operation")
    builder.append_tool_result("native-id", "mcp__source__read", ToolResult("display preview", mcp_result=raw, is_error=True,
        runtime_metadata={"mcp_wall_time_seconds": 0.125, "mcp_output_token_limit": 50}))
    message = builder._history[-1]
    assert isinstance(message.mcp_output, str)
    assert message.mcp_output.startswith("Wall time: 0.1250 seconds\nOutput:")
    assert "tokens truncated" in message.mcp_output
    assert "ignored-body" not in message.mcp_output
    assert len(message.mcp_output.encode("utf-8")) < 300
    assert len(raw["structuredContent"]["rows"]) == 8000
    assert _responses_function_call_output_item(message)["output"] == message.mcp_output
    assert message.to_openai_message()["content"] == message.mcp_output
    assert estimate_llm_message_tokens(message) < 100
    snapshot = builder.export_snapshot()
    assert "mcp_result" not in snapshot["history"][-1]
    cold = _builder(tmp_path, limit=1)
    cold.load_snapshot(snapshot)
    assert cold._history[-1].mcp_output == message.mcp_output
    assert cold._history_tokens_total == builder._history_tokens_total
    # Clearing a result replaces its complete model body, so native data cannot
    # override a later compaction marker.
    cleared = replace(message, content="[Old tool result content cleared]", mcp_output=None)
    assert _responses_function_call_output_item(cleared)["output"] == cleared.content


def test_encrypted_and_image_native_items_survive_without_entering_plain_logs(tmp_path):
    raw = {"content": [
        {"type": "text", "text": "secret-ciphertext", "_meta": {"codex/encryptedContent": True}},
        {"type": "text", "text": "visible-output"},
        {"type": "image", "data": "AA==", "mimeType": "image/png", "_meta": {"codex/imageDetail": "original"}},
        {"type": "resource_link", "uri": "docs://actual", "name": "Actual"},
    ], "structuredContent": {"discarded": "encrypted body takes precedence"}, "isError": False}
    builder = _builder(tmp_path)
    builder.append_tool_result("native-image", "mcp__source__image", ToolResult("visible-output", mcp_result=raw))
    message = builder._history[-1]
    assert "secret-ciphertext" not in message.content
    assert "discarded" not in message.content
    wire = _responses_function_call_output_item(message)["output"]
    assert wire[1] == {"type": "encrypted_content", "encrypted_content": "secret-ciphertext"}
    assert next(item for item in wire if item["type"] == "input_image")["detail"] == "high"
    assert any("docs://actual" in item.get("text", "") for item in wire)
    assert message.images == []
    assert estimate_llm_message_tokens(message) == (mcp_output_visible_bytes(wire) + 3) // 4
    with pytest.raises(ValueError, match="Responses transport"):
        message.to_openai_message()
    cold = _builder(tmp_path)
    cold.load_snapshot(builder.export_snapshot())
    assert _responses_function_call_output_item(cold._history[-1]) == _responses_function_call_output_item(message)
    original = budget_mcp_output(raw, wall_time=0, policy={"mode": "tokens", "limit": 1000},
        output_token_limit=None, original_image_detail_supported=True)
    assert next(item for item in original if item["type"] == "input_image")["detail"] == "original"


def test_audio_cost_uses_decoded_duration_and_source_omission_marker():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0" * 32000)
    url = "data:audio/wav;base64," + base64.b64encode(buffer.getvalue()).decode()
    assert estimate_audio_tokens(url) == 20
    raw = {"content": [{"type": "audio", "data": url, "mimeType": "audio/wav"}], "isError": False}
    retained = budget_mcp_output(raw, wall_time=0, policy={"mode": "tokens", "limit": 40}, output_token_limit=None, original_image_detail_supported=False)
    assert retained[-1] == {"type": "input_audio", "audio_url": url}
    omitted = budget_mcp_output(raw, wall_time=0, policy={"mode": "tokens", "limit": 15}, output_token_limit=None, original_image_detail_supported=False)
    assert omitted[-1] == {"type": "input_text", "text": "[omitted 1 audio items ...]"}
    assert not any(item["type"] == "input_audio" for item in omitted)


def test_chat_media_projection_uses_admitted_content_and_not_structured_overridden_images():
    message = LLMMessage("tool", "display", name="image", tool_call_id="image-id", mcp_output=[
        {"type": "input_text", "text": "Actual pixels"},
        {"type": "input_image", "image_url": "data:image/png;base64,AA==", "detail": "high"},
    ])
    projected = _openai_chat_messages([message])
    assert projected[0]["content"] == "Actual pixels"
    assert projected[1]["content"][1]["image_url"]["url"] == "data:image/png;base64,AA=="


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_output_limit_rejects_invalid_source_nonzero_values_without_publishing(tmp_path, limit):
    path = tmp_path / "mcp.json"
    path.write_text('{"servers":{}}', encoding="utf-8")
    with pytest.raises(ValueError, match="positive integer"):
        write_mcp_config(json.dumps({"servers": {"fixture": {"transport": "stdio", "command": "python", "tools": {"native": {"output_token_limit": limit}}}}}), path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"servers": {}}


@pytest.mark.asyncio
async def test_per_tool_output_limit_reload_republishes_bound_catalog_without_reconnecting(tmp_path):
    peer = MCPPeer("native", [MCPToolDef("read", "Read native", {"type": "object", "properties": {}}, annotations={"readOnlyHint": True})])
    manager = manager_fixture(tmp_path)
    manager._create_client = lambda config, **kwargs: peer
    config = MCPServerConfig("server", command="python", tool_output_token_limits={"read": 100})
    await manager.start_server(config)
    registry = ToolRegistry()
    catalog = MCPToolRegistry(registry, mcp_manager=manager)
    catalog.register_server_tools("server", peer.tools, peer)
    old = registry.get_tool("mcp__server__read")
    manager.load_config = lambda: [replace(config, tool_output_token_limits={"read": 10})]
    await manager.reload_config()
    assert peer.closed == 0
    assert (await old.execute({})).is_error
    catalog.sync()
    current = registry.get_tool("mcp__server__read")
    result = await current.execute({})
    assert result.runtime_metadata["mcp_output_token_limit"] == 10
    await manager.stop_all()


def test_output_token_limit_json_save_and_manager_parse_share_the_actual_value(tmp_path):
    path = tmp_path / "mcp.json"
    raw = {"servers": {"fixture": {"transport": "stdio", "command": "python", "tools": {"native": {"approval_mode": "writes", "output_token_limit": 73}}}}}
    write_mcp_config(json.dumps(raw), path)
    [config] = MCPServerManager(config_path=path, workspace_root=None)._load_local_configs()
    assert config.tool_output_token_limits == {"native": 73}
    assert config.tool_approval_modes == {"native": "writes"}


def test_nullable_root_properties_reaches_query_execution_and_title_reaches_discovery(tmp_path):
    class Peer:
        connected = True
        def __init__(self):
            self.calls = []
        async def call_tool(self, name, args, *, request_owner):
            self.calls.append((name, args))
            return MCPCallResult(content=[{"type": "text", "text": "Actual no-argument result"}])

    async def scenario():
        peer = Peer()
        definition = MCPToolDef("operation", "An operation", {"type": "object", "properties": None},
            annotations={"readOnlyHint": True}, title="Stellar ephemeris")
        registry = ToolRegistry()
        catalog = MCPToolRegistry(registry)
        assert catalog.register_server_tools("empty", [definition], peer) == 1
        tool = registry.get_tool("mcp__empty__operation")
        assert tool.get_schema().parameters["properties"] == {}
        assert definition.input_schema["properties"] is None
        assert [entry.name for entry in DeferredToolCatalog(registry).search("ephemeris", 8)] == [tool.name]
        class DiscoveryModel(LLMAdapter):
            def __init__(self):
                self.steps = 0
                self._settings = LLMSettings(api_key="", tool_mode="direct", supports_search_tool=True)
            async def simple_chat(self, messages, *, max_tokens=None):
                raise AssertionError("No auxiliary request is expected")
            async def stream_chat(self, messages, tools=None, metadata=None):
                self.steps += 1
                if self.steps == 1:
                    call = ToolCallEvent(id="title-discovery", name="tool_search", arguments={"query": "ephemeris"})
                elif self.steps == 2:
                    message = next(message for message in reversed(messages) if message.role == "tool")
                    [namespace] = message.tool_search_output
                    assert namespace["name"] == tool.model_namespace
                    assert [entry["name"] for entry in namespace["tools"]] == [tool.model_tool_name]
                    assert tool.name in {item["function"]["name"] for item in tools}
                    call = ToolCallEvent(id="null-properties", name=tool.name, arguments={})
                else:
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="The discovered no-argument operation completed.", phase="final_answer")
                    yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")
                    return
                yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[call])
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
        model = DiscoveryModel()
        info = ModelDefinition(provider="custom", id="fixture", name="Search-capable discovery fixture", api="openai-responses", base_url="",
            supports_search_tool=True)
        state, _, _, events, _ = await run_model(tmp_path, model, [tool], model_info=info)
        assert state.terminal_status == "completed"
        assert peer.calls == [("operation", {})]
        assert any(event.type == "tool_result" and "Actual no-argument result" in event.data.get("summary", "") for event in events)
    asyncio.run(scenario())
