from __future__ import annotations

import asyncio
import base64
import io
import json
import wave
from types import SimpleNamespace

import httpx
import pytest

from backend.agent.code_execution_output import code_mode_model_output, wav_duration_seconds
from backend.agent.code_execution_vm import CodeVM
from backend.agent.context import ContextBuilder
from backend.config import LLMSettings, TokenBudget
from backend.llm.base import LLMMessage, estimate_llm_message_tokens
from backend.llm.openai_adapter import OpenAIAdapter, _openai_chat_messages, _responses_function_call_output_item
from backend.llm.provider_contracts import ModelDefinition
from backend.mcp.result_projection import mcp_output_visible_bytes
from backend.tests.test_code_execution import ScriptModel, run_model
from backend.tests.test_harness_native_tool_protocol import ResponsesEndpoint, completed
from backend.tools.code_execution import ToolExecTool, ToolWaitTool


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jVZkAAAAASUVORK5CYII="


def wav_url(frames: int) -> str:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0\0" * frames)
    return "data:audio/wav;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.mark.parametrize("expression,detail", [
    ('image("data:image/png;base64,' + PNG + '")', "high"),
    ('image({image_url:"data:image/png;base64,' + PNG + '",detail:"LOW"})', "low"),
    ('image({type:"image",mimeType:"image/png",data:"' + PNG + '",_meta:{"codex/imageDetail":"original"}})', "original"),
    ('image({type:"image",mime_type:"image/png",data:"' + PNG + '",_meta:{"codex/imageDetail":"original"}},"AUTO")', "auto"),
    ('image({type:"image",mimeType:"image/png",data:"' + PNG + '",_meta:{"codex/imageDetail":"invalid"}})', "high"),
    ('image({image_url:"data:image/png;base64,' + PNG + '",detail:"high"},null)', "high"),
])
def test_v8_image_helper_matches_source_detail_rules(expression, detail):
    async def scenario():
        vm = CodeVM()
        try:
            result = await vm.step("start", {"code": expression + ";", "tools": [], "storage": {}})
            assert result["done"] and result["error"] == ""
            assert result["output"] == [{"kind": "image", "image_url": "data:image/png;base64," + PNG, "detail": detail}]
        finally:
            vm.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("expression,message", [
    ('image("https://example.invalid/image.png")', "remote image URLs are not supported"),
    ('image({type:"audio",data:"AA=="})', 'image only accepts MCP image blocks'),
    ('image({image_url:"data:image/png;base64,AA==",detail:"bogus"})', "image detail must be one of"),
    ('image("data:image/png;base64,AA==",3)', "image detail must be a string"),
    ('audio("https://example.invalid/audio.wav")', "invalid audio output"),
    ('generatedImage({image_url:"data:image/png;base64,AA==",output_hint:null})', "output_hint must be a string"),
])
def test_helper_input_errors_are_source_errors(expression, message):
    async def scenario():
        vm = CodeVM()
        try:
            result = await vm.step("start", {"code": expression + ";", "tools": [], "storage": {}})
            assert result["done"] and message in result["error"]
            assert result["output"] == []
        finally:
            vm.close()
    asyncio.run(scenario())


def test_wav_probe_uses_present_data_and_streaming_header():
    url = wav_url(199)
    assert wav_duration_seconds(url) == 199 / 8000
    raw = bytearray(base64.b64decode(url.split(",", 1)[1]))
    raw[40:44] = (0xffffffff).to_bytes(4, "little")
    assert wav_duration_seconds("data:audio/wav;base64," + base64.b64encode(raw).decode()) == 199 / 8000
    assert wav_duration_seconds("data:audio/mp3;base64,AAAA") is None


@pytest.mark.parametrize("frames,expected", [(199, False), (200, True)])
def test_tiny_wav_changes_to_source_text_before_artifact_and_native_admission(tmp_path, frames, expected):
    async def scenario():
        url = wav_url(frames)
        state, builder, _, events, _ = await run_model(tmp_path, ScriptModel('audio(' + json.dumps(url) + ');text("tail");'))
        assert state.terminal_status == "completed"
        tool = next(message for message in builder._history if message.name == "tool_exec")
        body = _responses_function_call_output_item(tool)["output"]
        if expected:
            assert body[1] == {"type": "input_audio", "audio_url": url}
        else:
            assert body[1] == {"type": "input_text", "text": "Audio output omitted because the clip is shorter than 25 ms; use a longer clip."}
        assert body[-1] == {"type": "input_text", "text": "tail"}
        assert len([event for event in events if event.type == "artifact.preview"]) == int(expected)
    asyncio.run(scenario())


def test_token_budget_counts_native_audio_and_preserves_order_and_images():
    url = wav_url(8000)
    items = [{"type": "input_text", "text": "first"},
        {"type": "input_audio", "audio_url": url},
        {"type": "input_image", "image_url": "data:image/png;base64," + PNG, "detail": "original"},
        {"type": "input_text", "text": "last"}]
    output = code_mode_model_output(items, status="Script completed", wall_time=.25, max_tokens=5, original_supported=False)
    assert output[0]["text"] == "Script completed\nWall time 0.3 seconds\nOutput:\n"
    assert [item["type"] for item in output] == ["input_text", "input_text", "input_image", "input_text", "input_text"]
    assert output[2]["detail"] == "high"
    assert output[-1] == {"type": "input_text", "text": "[omitted 1 audio items ...]"}
    assert items[2]["detail"] == "original"
    original = code_mode_model_output(items, status="Script completed", wall_time=0, max_tokens=50, original_supported=True)
    assert original[2] == {"type": "input_audio", "audio_url": url}
    assert original[3]["detail"] == "original"


def test_source_exec_and_wait_use_their_actual_token_budget_fields():
    execute = ToolExecTool().get_schema().parameters["properties"]
    wait = ToolWaitTool().get_schema().parameters["properties"]
    assert "max_output_tokens" in execute and "max_chars" not in execute
    assert "max_tokens" in wait and "max_chars" not in wait
    assert execute["max_output_tokens"]["minimum"] == wait["max_tokens"]["minimum"] == 0


def test_selected_raw_body_is_private_and_cold_replay_keeps_only_admitted_native_body(tmp_path):
    async def scenario():
        model = ScriptModel('text("BEGIN" + "汉".repeat(20000) + "END");', max_output_tokens=30)
        state, builder, journal, events, _ = await run_model(tmp_path, model)
        assert state.terminal_status == "completed"
        message = next(item for item in builder._history if item.name == "tool_exec")
        wire = _responses_function_call_output_item(message)["output"]
        assert "original token count" in wire[1]["text"]
        assert wire[1]["text"].endswith("END") and "BEGIN" in wire[1]["text"]
        assert "cell_id" not in "\n".join(item["text"] for item in wire)
        record = next(item for item in state.tool_calls if item.tool_name == "tool_exec")
        assert len(record.result_payload["code_mode_raw_output"][0]["text"]) == 20008
        assert record.result_payload["code_mode_output"] == wire
        assert all("code_mode_raw_output" not in event.data and "code_mode_output" not in event.data for event in events)
        assert estimate_llm_message_tokens(message) == (mcp_output_visible_bytes(wire) + 3) // 4
        replayed = journal.reconstruct_history()
        receipt = next(item for item in replayed if item.get("name") == "tool_exec" and item["role"] == "tool")
        assert receipt["code_mode_output"] == wire
        assert "code_mode_raw_output" not in receipt
        assert "code_mode_raw_output" not in json.dumps(replayed)
        cold = ContextBuilder(llm=SimpleNamespace(_settings=LLMSettings(api_key="")), token_budget=TokenBudget(total=96000), workspace_root=tmp_path)
        cold.load_snapshot(builder.export_snapshot())
        replay = next(item for item in cold._history if item.name == "tool_exec")
        assert _responses_function_call_output_item(replay)["output"] == wire
        assert estimate_llm_message_tokens(replay) == estimate_llm_message_tokens(message)
        snapshot = builder.export_snapshot()
        snapshot["history"] = replayed
        restored = ContextBuilder(llm=SimpleNamespace(_settings=LLMSettings(api_key="")), token_budget=TokenBudget(total=96000), workspace_root=tmp_path)
        restored.load_snapshot(snapshot)
        journal_message = next(item for item in restored._history if item.name == "tool_exec")
        assert _responses_function_call_output_item(journal_message)["output"] == wire
    asyncio.run(scenario())


@pytest.mark.parametrize("original", [False, True])
def test_actual_responses_request_preserves_helper_order_detail_audio_and_no_json_receipt(tmp_path, original):
    async def scenario():
        url = wav_url(800)
        source = 'text("before");image({type:"image",mimeType:"image/png",data:' + json.dumps(PNG) + ',_meta:{"codex/imageDetail":"original"}});audio(' + json.dumps(url) + ');text("after");'
        endpoint = ResponsesEndpoint([
            [completed([{"type": "custom_tool_call", "id": "source-exec", "call_id": "script-parent", "name": "tool_exec", "input": source, "status": "completed"}])],
            [completed([{"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "Complete."}]}])],
        ])
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            adapter = OpenAIAdapter(LLMSettings(api_key="fixture", model="fixture", base_url="https://fixture.invalid/v1", wire_api="responses", supports_custom_tools=True,
                supports_image_detail_original=original, max_tokens=1024), http_client=client)
            info = ModelDefinition(provider="custom", id="fixture", name="Native media fixture", api="openai-responses", base_url="https://fixture.invalid/v1",
                supports_custom_tools=True, supports_image_detail_original=original)
            state, builder, _, _, _ = await run_model(tmp_path, adapter, model_info=info)
            assert state.terminal_status == "completed"
            output = next(item["output"] for item in endpoint.requests[1]["input"] if item.get("type") == "custom_tool_call_output")
            assert [item["type"] for item in output] == ["input_text", "input_text", "input_image", "input_audio", "input_text"]
            assert output[0]["text"].startswith("Script completed\nWall time ")
            assert output[1]["text"] == "before" and output[-1]["text"] == "after"
            assert output[2]["detail"] == ("original" if original else "high")
            assert output[3] == {"type": "input_audio", "audio_url": url}
            assert "cell_id" not in json.dumps(output)
            message = next(item for item in builder._history if item.name == "tool_exec")
            assert message.code_mode_output == output
    asyncio.run(scenario())


def test_chat_bridge_keeps_real_detail_and_audio_input():
    url = wav_url(800)
    message = LLMMessage("tool", "display receipt", tool_call_id="cell-parent", name="tool_exec", code_mode_output=[
        {"type": "input_text", "text": "Script completed"},
        {"type": "input_image", "image_url": "data:image/png;base64," + PNG, "detail": "low"},
        {"type": "input_audio", "audio_url": url},
    ])
    projected = _openai_chat_messages([message])
    assert projected[0]["content"] == "Script completed"
    assert projected[1]["content"][0]["image_url"]["detail"] == "low"
    assert projected[1]["content"][1] == {"type": "input_audio", "input_audio": {"data": url.split(",", 1)[1], "format": "wav"}}
