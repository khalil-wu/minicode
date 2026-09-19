from __future__ import annotations

import base64
import io
import json
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from backend.api.routes_chat import _native_body_response
from backend.artifact.store import ArtifactStore
from backend.mcp.client import MCPCallResult, MCPToolDef
from backend.mcp.registry import MCPToolProxy
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.artifact_service import read_artifact_content
from backend.services.chat_api_service import ChatApiServiceError, generated_artifact_native_payload
from backend.tests.test_code_execution import ScriptModel, run_model
from backend.tools.agent_artifact_tools import ReadArtifactTool
from backend.tools.base import BaseTool, ToolResult, ToolSchema


def wav_data(sample=0):
    out = io.BytesIO()
    with wave.open(out, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(bytes([sample, 0]) * 800)
    return base64.b64encode(out.getvalue()).decode("ascii")


class VoiceTool(BaseTool):
    name = "voice"
    read_only = True
    def get_schema(self):
        return ToolSchema(self.name, "Produce audio fixtures", {"type": "object", "properties": {}})
    async def execute(self, args, context=None):
        return ToolResult("Two audio outputs", audios=[
            {"media_type": "audio/wav", "data": wav_data(0)},
            {"media_type": "audio/wav", "data": wav_data(1)},
        ])


@pytest.mark.asyncio
@pytest.mark.parametrize("expression", [
    'audio(r.audios[1]);',
    'audio("data:audio/wav;base64," + r.audios[1].data);',
    'audio({type:"audio", mimeType:"audio/wav", data:r.audios[1].data});',
    'audio({audio_url:"data:audio/wav;base64," + r.audios[1].data});',
])
async def test_selected_audio_is_persisted_once_and_references_survive_cold_read(tmp_path, expression):
    model = ScriptModel('const r = await tools.voice({}); ' + expression)
    state, builder, journal, events, _ = await run_model(tmp_path, model, [VoiceTool()])
    assert state.terminal_status == "completed", model.reports
    assert model.reports[-1]["audio_count"] == 1
    previews = [event for event in events if event.type == "artifact.preview"]
    assert len(previews) == 1  # unselected nested output never becomes an artifact
    artifact_id = previews[0].data["artifact_id"]
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    assert store.get(artifact_id, conversation_id="code-conv", workspace_root=tmp_path) == wav_data(1)
    assert store.get(artifact_id, conversation_id="another", workspace_root=tmp_path) is None
    history = json.dumps(builder.export_snapshot(), ensure_ascii=False)
    assert artifact_id in history and "not transcribed" in history
    assert wav_data(1) not in history
    assert any(event.payload.get("lifecycle") == "artifact_preview" and event.payload["artifact_id"] == artifact_id for event in journal.read_events())
    recovered = journal.reconstruct_history()
    assert artifact_id in json.dumps(recovered)
    preview = read_artifact_content(store, SimpleNamespace(find_payload=lambda *a, **kw: None), artifact_id,
        conversation_id="code-conv", workspace_root=str(tmp_path)).to_event()
    assert preview.data["content"] == "" and "url" not in preview.data
    reader = ReadArtifactTool(store)
    result = await reader.execute({"artifact_id": artifact_id}, ToolExecutionContext(
        permission=PermissionContext(), conversation_id="code-conv", workspace_root=tmp_path))
    assert wav_data(1) not in result.content
    assert "not transcribed" in result.content
    store.shutdown()


@pytest.mark.asyncio
async def test_audio_yield_and_wait_emit_each_output_once(tmp_path):
    model = ScriptModel('const r = await tools.voice({}); audio(r.audios[0]); await yield_control(); '
        'await new Promise(resolve => setTimeout(resolve, 80)); audio(r.audios[1]);')
    state, _, _, events, _ = await run_model(tmp_path, model, [VoiceTool()])
    previews = [event.data for event in events if event.type == "artifact.preview"]
    assert state.terminal_status == "completed"
    assert len(previews) == 2 and len({item["artifact_id"] for item in previews}) == 2
    assert sum(report["audio_count"] for report in model.reports) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("expression", ['audio("https://example.invalid/voice.wav")', 'audio({mimeType:"image/png",data:"AAAA"})'])
async def test_audio_helper_rejects_non_audio_input(tmp_path, expression):
    model = ScriptModel(expression)
    _, _, _, events, _ = await run_model(tmp_path, model, [])
    assert model.reports[-1]["status"] == "failed"
    assert "audio() requires" in model.reports[-1]["error"]
    assert not any(event.type == "artifact.preview" for event in events)


@pytest.mark.asyncio
async def test_mcp_audio_is_available_to_code_and_direct_results(tmp_path):
    block = {"type": "audio", "mimeType": "audio/wav", "data": wav_data()}
    client = SimpleNamespace(connected=True, call_tool=AsyncMock(return_value=MCPCallResult(content=[block])))
    tool = MCPToolProxy("sound", MCPToolDef(name="voice", description="Audio output", input_schema={"type": "object"},
        annotations={"readOnlyHint": True}), client)
    normalized = await tool.execute({})
    assert normalized.audios == [{"media_type": "audio/wav", "data": wav_data()}]
    model = ScriptModel(f'await tools.tool_search({{query:"select:{tool.name}"}}); const r = await tools.{tool.name}({{}}); audio(r.audios[0]);')
    state, _, _, events, _ = await run_model(tmp_path / "nested", model, [tool])
    assert state.terminal_status == "completed", model.reports
    assert len([event for event in events if event.type == "artifact.preview"]) == 1
    from backend.llm.base import ToolCallEvent
    async def after_discovery(_report):
        return ToolCallEvent(id="audio-direct", name=tool.name, arguments={})
    direct = ScriptModel("", first_call=ToolCallEvent(id="discover", name="tool_search", arguments={"query": f"select:{tool.name}"}), after_first=after_discovery)
    state, builder, _, events, _ = await run_model(tmp_path / "direct", direct, [tool])
    assert state.terminal_status == "completed"
    assert len([event for event in events if event.type == "artifact.preview"]) == 1
    assert wav_data() not in json.dumps(builder.export_snapshot())


def test_audio_raw_response_preserves_bytes_ranges_and_conversation_owner(tmp_path):
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    data = wav_data()
    artifact_id = store.save(data, source="test", type="audio", media_type="audio/wav", conversation_id="owner", workspace_root=tmp_path)
    session = SimpleNamespace(artifact_store=store,
        conversation_repo=SimpleNamespace(get_conversation=lambda value: SimpleNamespace(id=value, archived=False)),
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda _: str(tmp_path)))
    manager = SimpleNamespace(get_session=lambda _: session)
    body, media_type, name = generated_artifact_native_payload(session_id="session", conversation_id="owner", artifact_id=artifact_id, ws_manager=manager)
    assert body == base64.b64decode(data) and name.endswith(".wav")
    request = Request({"type": "http", "headers": [(b"range", b"bytes=10-29")]})
    response = _native_body_response(request, body=body, media_type=media_type, file_name=name)
    assert response.status_code == 206 and response.body == body[10:30]
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["content-disposition"].startswith("inline")
    with pytest.raises(ChatApiServiceError) as denied:
        generated_artifact_native_payload(session_id="session", conversation_id="other", artifact_id=artifact_id, ws_manager=manager)
    assert denied.value.status_code == 404
