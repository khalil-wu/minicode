from __future__ import annotations

import asyncio
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Thread
from types import SimpleNamespace

import pytest
from PIL import Image

from backend.agent.context import ContextBuilder
from backend.agent.run_context import RunContext
from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.config import LLMSettings, TokenBudget
from backend.llm.base import LLMMessage, ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox import SandboxPolicy
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.tests.test_code_execution import ScriptModel, run_model
from backend.tools.image_generation_io import read_recent_images
from backend.tools.image_generation_tool import GenerateImageTool
from backend.tools.read_file import ReadFileTool
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema


def png(color: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def image_api():
    requests = []
    response_data = base64.b64encode(png("purple")).decode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, dict(self.headers), body))
            failed = self.path.startswith("/fail/")
            payload = {"error": {"message": "image service unavailable"}} if failed else {"data": [{"b64_json": response_data}]}
            encoded = json.dumps(payload).encode()
            self.send_response(503 if failed else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, response_data
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def execution_context(tmp_path, builder=None):
    settings = LLMSettings(api_key="unrelated-key", provider="custom", proxy_mode="direct",
        default_headers=(("X-Other-Provider", "private"),))
    return ToolExecutionContext(
        permission=PermissionContext(mode="bypass", workspace_root=tmp_path),
        workspace_root=tmp_path, conversation_id="image-conversation",
        sandbox_policy=SandboxPolicy(workspace_root=tmp_path, writable_roots=(tmp_path,)),
        model_execution=SimpleNamespace(config=SimpleNamespace(llm=settings)),
        run_context=RunContext(context_builder=builder),
    )


def test_imagegen_skill_is_discoverable_without_provider_setup(tmp_path):
    skill = next(item for item in SkillManager(SkillLoader(project_root=tmp_path)).discover()
        if item.name == "imagegen" and item.source_level == "builtin")
    assert skill.allow_implicit_invocation and skill.source_path.is_file()
    assert (skill.source_path.parent / "scripts" / "image_gen.py").is_file()
    assert GenerateImageTool().get_spec().exposure == "core"


def test_external_api_receives_only_its_own_credentials_and_full_endpoint(image_api, tmp_path):
    host, calls, encoded = image_api
    result = asyncio.run(GenerateImageTool().execute({
        "prompt": "a purple square", "api_base_url": host + "/v1/images/generations?api-version=fixture",
        "api_key": "conversation-key", "model": "fixture-image", "size": "2048x1152",
        "quality": "high", "transparent_background": True,
    }, execution_context(tmp_path)))

    assert not result.is_error and result.images[0]["data"] == encoded
    assert len(calls) == 1
    path, headers, body = calls[0]
    assert path == "/v1/images/generations?api-version=fixture"
    assert headers["Authorization"] == "Bearer conversation-key"
    assert "X-Other-Provider" not in headers
    assert json.loads(body) == {"model": "fixture-image", "prompt": "a purple square", "n": 1,
        "size": "2048x1152", "quality": "high", "response_format": "b64_json", "background": "transparent"}


def test_external_edit_uses_its_reference_and_does_not_borrow_key(image_api, tmp_path):
    host, calls, encoded = image_api
    source = tmp_path / "input.png"
    source.write_bytes(png("red"))
    output = tmp_path / "output.png"
    result = asyncio.run(GenerateImageTool().execute({
        "prompt": "turn it purple", "api_base_url": host + "/images/edits",
        "model": "fixture-image", "input_images": [str(source)], "output_path": str(output),
    }, execution_context(tmp_path)))

    assert not result.is_error and output.read_bytes() == base64.b64decode(encoded)
    assert len(calls) == 1
    path, headers, body = calls[0]
    assert path == "/images/edits"
    assert "Authorization" not in headers and "X-Other-Provider" not in headers
    assert headers["Content-Type"].startswith("multipart/form-data;")
    assert source.read_bytes() in body and b'turn it purple' in body


def test_failure_is_not_reported_as_a_generated_image(image_api, tmp_path):
    host, calls, _ = image_api
    result = asyncio.run(GenerateImageTool().execute({
        "prompt": "a cat", "api_base_url": host + "/fail/v1", "model": "fixture-image",
    }, execution_context(tmp_path)))
    assert result.is_error and result.images == [] and result.projection == "error"
    assert len(calls) == 1


def test_explicit_model_and_key_override_the_available_channel_without_mutating_it(monkeypatch):
    settings = {"llm": {"provider": "custom", "custom": {"api_key": "stored-key",
        "base_url": "https://configured.invalid/v1", "model": "chat-model", "image_model": "stored-model",
        "wire_api": "chat", "proxy_mode": "direct", "headers": {"Authorization": "Bearer stored-key"}, "auth_header": True}}}
    captured = []
    class Adapter:
        def __init__(self, value): captured.append(value)
        async def generate_images(self, *_args, **_kwargs): return [(base64.b64encode(png("red")).decode(), "image/png")]
        async def aclose(self): pass
    from backend.tools import image_generation_tool
    tool = GenerateImageTool()
    monkeypatch.setattr(image_generation_tool, "OpenAIAdapter", Adapter)
    snapshot = SimpleNamespace(provider="custom", config=SimpleNamespace(config_layer_stack=SimpleNamespace(effective_config=lambda: settings)))
    context = ToolExecutionContext(permission=PermissionContext(), model_execution=snapshot)
    result = asyncio.run(tool.execute({"prompt": "cat", "model": "chosen-model", "api_key": "chosen-key"}, context))
    assert not result.is_error
    assert captured[0].api_key == "chosen-key" and captured[0].model == "chosen-model"
    assert captured[0].default_headers == ()
    assert settings["llm"]["custom"]["api_key"] == "stored-key" and settings["llm"]["custom"]["image_model"] == "stored-model"


def test_conversation_model_enables_an_existing_endpoint_without_image_model_setup(monkeypatch):
    settings = {"llm": {"provider": "custom", "custom": {"api_key": "configured-key",
        "base_url": "https://configured.invalid/v1", "model": "chat-model", "wire_api": "chat", "auth_header": True}}}
    captured = []
    class Adapter:
        def __init__(self, value): captured.append(value)
        async def generate_images(self, *_args, **_kwargs): return [(base64.b64encode(png("red")).decode(), "image/png")]
        async def aclose(self): pass
    from backend.tools import image_generation_tool
    from backend import config_helpers
    tool = GenerateImageTool()
    monkeypatch.setattr(image_generation_tool, "OpenAIAdapter", Adapter)
    monkeypatch.setattr(config_helpers, "_custom_provider_api_key", lambda _base_url: "configured-key")
    snapshot = SimpleNamespace(provider="custom", config=SimpleNamespace(config_layer_stack=SimpleNamespace(effective_config=lambda: settings)))
    context = ToolExecutionContext(permission=PermissionContext(), model_execution=snapshot)
    result = asyncio.run(tool.execute({"prompt": "cat", "model": "chosen-model"}, context))
    assert not result.is_error and captured[0].model == "chosen-model"


def test_recent_images_include_cold_attachments_and_native_tool_outputs_in_order(tmp_path):
    attachment_store = AttachmentStore(base_dir=tmp_path / "attachments")
    llm = SimpleNamespace(_settings=LLMSettings(api_key=""))
    builder = ContextBuilder(llm=llm, token_budget=TokenBudget(total=96000),
        conversation_id="image-conversation", workspace_root=tmp_path)
    builder._attachment_store = attachment_store
    image_data = {name: base64.b64encode(png(color)).decode() for name, color in
        (("one", "red"), ("two", "green"), ("three", "blue"), ("mcp", "yellow"),
         ("cell", "white"), ("generated", "purple"))}
    for name in ("one", "two", "three", "generated"):
        attachment_store.save(artifact_id=name, content="", metadata={"conversation_id": "image-conversation", "workspace_root": str(tmp_path)},
            native_data=image_data[name])
    refs = [{"artifact_id": name, "kind": "image", "media_type": "image/png"} for name in ("one", "two", "three")]
    generated_ref = {"artifact_id": "generated", "kind": "image", "media_type": "image/png", "input_source": "assistant_generated"}
    def native(name):
        return [{"type": "input_image", "image_url": "data:image/png;base64," + image_data[name], "detail": "high"}]
    builder._history = [
        LLMMessage("user", images=[{"artifact_id": name, "data": image_data[name], "media_type": "image/png"}
            for name in ("one", "three")], attachment_refs=refs),
        LLMMessage("tool", mcp_output=native("mcp")),
        LLMMessage("tool", code_mode_output=native("cell")),
        LLMMessage("tool", name="generate_image", images=[{"data": image_data["generated"], "media_type": "image/png"}]),
        LLMMessage("user", attachment_refs=[generated_ref]),
    ]
    snapshot = builder.export_snapshot()
    cold = ContextBuilder(llm=llm, token_budget=TokenBudget(total=96000),
        conversation_id="image-conversation", workspace_root=tmp_path)
    cold._attachment_store = attachment_store
    cold.load_snapshot(snapshot)
    result = asyncio.run(read_recent_images(execution_context(tmp_path, cold), 5))
    assert [raw for _, raw, _ in result] == [base64.b64decode(image_data[name])
        for name in ("two", "three", "mcp", "cell", "generated")]
    foreign = execution_context(tmp_path, cold)
    foreign.conversation_id = "another-conversation"
    with pytest.raises(ValueError, match="Conversation image is unavailable"):
        asyncio.run(read_recent_images(foreign, 1))
    attachment_store._path_for("one").unlink()
    cold._history = [LLMMessage("user", attachment_refs=[refs[0], refs[2]])]
    recent = asyncio.run(read_recent_images(execution_context(tmp_path, cold), 1))
    assert recent[0][1] == base64.b64decode(image_data["three"])
    with pytest.raises(ValueError, match="Conversation image is unavailable"):
        asyncio.run(read_recent_images(execution_context(tmp_path, cold), 2))


def test_direct_image_call_emits_one_real_image(image_api, tmp_path):
    host, calls, encoded = image_api
    call = ToolCallEvent(id="direct-image", name="generate_image", arguments={
        "prompt": "purple", "api_base_url": host + "/v1", "model": "fixture-image"})
    async def run():
        state, _, _, events, _ = await run_model(tmp_path, ScriptModel("", first_call=call), [GenerateImageTool()])
        assert state.terminal_status == "completed"
        images = [event for event in events if event.type == "image_chunk"]
        assert len(images) == 1 and images[0].data["image_data"] == encoded
        assert images[0].data["tool_call_id"] == "direct-image"
    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize("publish", [False, True])
def test_code_mode_publishes_only_the_selected_generated_image(image_api, tmp_path, publish):
    host, calls, encoded = image_api
    args = json.dumps({"prompt": "purple", "api_base_url": host + "/v1", "model": "fixture-image"})
    code = "const result = await tools.generate_image(" + args + "); "
    code += "generatedImage(result.images[0]);" if publish else 'text("not selected");'
    async def run():
        state, builder, _, events, _ = await run_model(tmp_path, ScriptModel(code), [GenerateImageTool()])
        assert state.terminal_status == "completed"
        assert not [event for event in events if event.type == "image_chunk"]
        previews = [event.data for event in events if event.type == "artifact.preview"]
        assert len(previews) == int(publish)
        if publish:
            assert previews[0]["source"] == "image_generation"
            progress = next(event.data for event in events if event.type == "agent.progress" and event.data["stage"] == "image_generation")
            assert previews[0]["tool_call_id"] == progress["tool_call_id"]
            assert previews[0]["operation"] == "generate_image"
            output = next(message.code_mode_output for message in builder._history if message.name == "tool_exec")
            assert next(item for item in output if item["type"] == "input_image")["image_url"] == "data:image/png;base64," + encoded
    asyncio.run(run())
    assert len(calls) == 1


def test_non_openai_workflow_projects_real_workspace_file_through_generated_image(tmp_path):
    image_path = tmp_path / "external.png"
    image_path.write_bytes(png("orange"))
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    code = 'const result = await tools.read_file({file_path:' + json.dumps(str(image_path)) + '}); generatedImage(result.images[0]);'
    async def run():
        try:
            state, _, _, events, _ = await run_model(tmp_path, ScriptModel(code), [ReadFileTool(store)])
            assert state.terminal_status == "completed"
            previews = [event.data for event in events if event.type == "artifact.preview"]
            assert len(previews) == 1 and previews[0]["source"] == "image_generation"
        finally:
            await store.aclose()
    asyncio.run(run())


def test_code_mode_identical_generated_images_keep_distinct_leaf_calls_and_artifacts(image_api, tmp_path):
    host, calls, encoded = image_api
    args = json.dumps({"prompt": "identical pixels", "api_base_url": host + "/v1", "model": "fixture-image"})
    code = f"const first = await tools.generate_image({args}); const second = await tools.generate_image({args}); generatedImage(first.images[0]); generatedImage(second.images[0]);"
    async def run():
        state, _, _, events, _ = await run_model(tmp_path, ScriptModel(code), [GenerateImageTool()])
        assert state.terminal_status == "completed"
        previews = [event.data for event in events if event.type == "artifact.preview"]
        terminal = [event.data for event in events if event.type == "agent.progress" and event.data["stage"] == "image_generation" and event.data["status"] == "completed"]
        assert len(previews) == len(terminal) == 2
        assert len({item["artifact_id"] for item in previews}) == 2
        assert [item["tool_call_id"] for item in previews] == [item["tool_call_id"] for item in terminal]
        assert all(item["operation"] == "generate_image" for item in previews)
    asyncio.run(run())
    assert len(calls) == 2


def test_generated_image_keeps_leaf_ownership_without_losing_display_intent(tmp_path):
    encoded = base64.b64encode(png("orange")).decode()
    class ExternalImage(BaseTool):
        name = "external_image"
        permission = PermissionLevel.AUTO
        read_only = True
        def get_schema(self):
            return ToolSchema(self.name, "Return an external image", {"type": "object", "properties": {}})
        async def execute(self, _args, context=None):
            artifact_id = context.artifact_store.save(encoded, source=self.name, type="image", media_type="image/png",
                conversation_id=context.conversation_id, workspace_root=context.workspace_root)
            return ToolResult("external image", artifact_id=artifact_id, artifact_kind="image",
                artifact_media_type="image/png", images=[{"data": encoded, "media_type": "image/png"}])
    async def run():
        state, _, _, events, _ = await run_model(tmp_path,
            ScriptModel('const result = await tools.external_image({}); generatedImage(result.images[0]);'), [ExternalImage()])
        assert state.terminal_status == "completed"
        previews = [event.data for event in events if event.type == "artifact.preview"]
        assert len(previews) == 1 and previews[0]["source"] == "image_generation"
        assert previews[0]["operation"] == "external_image"
    asyncio.run(run())
