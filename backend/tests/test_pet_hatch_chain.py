from __future__ import annotations

import asyncio
import base64
import json
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw

from backend.config import LLMSettings
from backend.llm.openai_adapter import OpenAIAdapter
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox import SandboxPolicy
from backend.services import pet_hatch_service
from backend.skills.manager import SkillManager
from backend.tools import image_generation_tool
from backend.tools.hatch_pet_tool import HatchPetTool


def image_bytes(color: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), color).save(output, format="PNG")
    return output.getvalue()


def test_hatch_reads_conditional_review_through_the_captured_executor(monkeypatch):
    from pathlib import PurePosixPath
    from backend.tools import hatch_pet_tool
    captured = object()
    reads = []
    class Service:
        def __init__(self, context): assert context is captured
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        def resolve(self, value): return PurePosixPath("/captured") / value
        async def read_bytes(self, path, **_kwargs):
            reads.append(str(path))
            if path.name == "direction-blind-validation.json": return b'{"ok":false}'
            if path.name == "blind-review-resolution.json": raise PermissionError("Captured executor denied this QA file")
            return b'{}'
    monkeypatch.setattr(hatch_pet_tool, "uses_executor_io", lambda context: context is captured)
    monkeypatch.setattr(hatch_pet_tool, "ExecutorWorkspaceService", Service)
    monkeypatch.setattr(hatch_pet_tool, "local_image_path", lambda *_args: pytest.fail("Do not rebind executor reads to the controller"))
    with pytest.raises(PermissionError, match="Captured executor denied"):
        asyncio.run(hatch_pet_tool.read_hatch_files("run", captured))
    assert reads[-1] == "/captured/run/qa/blind-review-resolution.json"


def test_hatch_preparation_matches_the_new_conversation_provider_rule(monkeypatch):
    from types import SimpleNamespace
    from fastapi import Response
    from backend.api import routes_pets
    calls = []
    session = SimpleNamespace(provider="custom", active_conversation_id="", _model_override_active=True)
    monkeypatch.setattr(routes_pets._state, "ws_manager", SimpleNamespace(get_session=lambda value: session if value == "actual-session" else None))
    monkeypatch.setattr(routes_pets._state, "bootstrap", SimpleNamespace(skill_manager=object()))
    async def prepare(_manager, **kwargs): calls.append(kwargs); return {}
    monkeypatch.setattr(routes_pets, "prepare_hatch_pet", prepare)
    asyncio.run(routes_pets.prepare_pet_api(Response(), "actual-session"))
    session.active_conversation_id = "existing-conversation"
    asyncio.run(routes_pets.prepare_pet_api(Response(), "actual-session"))
    assert calls == [{"provider": "custom"}, {"provider": None}]


def test_image_references_are_attached_to_one_real_edit_request():
    calls = []
    blue, red = image_bytes("blue"), image_bytes("red")

    async def respond(request):
        calls.append(request)
        body = await request.aread()
        assert request.url.path == "/v1/images/edits"
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        assert body.count(b'name="image[]"') == 2
        assert blue in body and red in body
        assert request.headers["x-tenant"] == "demo"
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(blue).decode()}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            adapter = OpenAIAdapter(LLMSettings(api_key="test-key", model="image-model", base_url="https://images.example/v1", default_headers=(("X-Tenant", "demo"),)), http_client=client)
            result = await adapter._generate_images_response("Keep the same pet", input_images=[("base.png", blue, "image/png"), ("guide.png", red, "image/png")])
            assert result.data[0].b64_json == base64.b64encode(blue).decode()
            await adapter.aclose()
    asyncio.run(run())
    assert len(calls) == 1


def test_rejected_reference_edit_never_switches_to_prompt_only_generation():
    calls = []
    async def respond(request):
        calls.append(request.url.path)
        return httpx.Response(400, json={"error": {"message": "Image edits are unsupported"}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            adapter = OpenAIAdapter(LLMSettings(api_key="test-key", model="image-model", base_url="https://images.example/v1"), http_client=client)
            with pytest.raises(httpx.HTTPStatusError):
                await adapter._generate_images_response("Pet", input_images=[("base.png", image_bytes("blue"), "image/png")])
            await adapter.aclose()
    asyncio.run(run())
    assert calls == ["/v1/images/edits"]


def test_image_tool_reads_references_and_saves_new_bytes_without_replacing_user_files(tmp_path, monkeypatch):
    source = tmp_path / "base.png"
    source.write_bytes(image_bytes("red"))
    generated = image_bytes("blue")
    calls = []
    class Adapter:
        def __init__(self, _settings): pass
        async def generate_images(self, prompt, **kwargs):
            calls.append(kwargs)
            return [(base64.b64encode(generated).decode(), "image/png")]
        async def aclose(self): pass
    monkeypatch.setattr(image_generation_tool, "OpenAIAdapter", Adapter)
    tool = image_generation_tool.GenerateImageTool()
    monkeypatch.setattr(tool, "_settings_for_context", lambda _context, **_overrides: {"enabled": True, "api_key": "test", "base_url": "https://images.example/v1", "model": "image-model", "provider": "custom"})
    context = ToolExecutionContext(permission=PermissionContext(workspace_root=tmp_path), workspace_root=tmp_path,
        sandbox_policy=SandboxPolicy(workspace_root=tmp_path, writable_roots=(tmp_path,)))
    output = tmp_path / "generated.png"
    result = asyncio.run(tool.execute({"prompt": "Pet", "input_images": [str(source)], "output_path": str(output)}, context))
    assert not result.is_error
    assert calls[0]["input_images"] == [("base.png", source.read_bytes(), "image/png")]
    assert output.read_bytes() == generated
    failed = asyncio.run(tool.execute({"prompt": "Pet", "output_path": str(source)}, context))
    assert failed.is_error and "already exists" in failed.content
    assert len(calls) == 1 and source.read_bytes() == image_bytes("red")


def test_create_prepares_and_enables_the_exact_local_bundle_only_on_request(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(pet_hatch_service, "get_image_generation_settings", lambda *_args, **_kwargs: {"enabled": True})
    target = tmp_path / "config" / "skills" / ".bundled" / "hatch-pet" / "SKILL.md"
    assert not target.exists()
    result = asyncio.run(pet_hatch_service.prepare_hatch_pet(SkillManager()))
    assert Path(result["skill"]["path"]) == target
    assert result["skill"]["name"] == "hatch-pet"
    target.write_text(target.read_text(encoding="utf-8") + "\nUser retained note.\n", encoding="utf-8")
    asyncio.run(pet_hatch_service.prepare_hatch_pet(SkillManager()))
    assert target.read_text(encoding="utf-8").endswith("User retained note.\n")
    monkeypatch.setattr(pet_hatch_service, "get_image_generation_settings", lambda *_args, **_kwargs: {"enabled": False, "reason": "Image profile is missing"})
    with pytest.raises(ValueError, match="Image profile is missing"):
        asyncio.run(pet_hatch_service.prepare_hatch_pet(SkillManager()))


def test_publish_validates_v2_and_emits_selection_event_for_the_actual_pet(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "config"))
    run = tmp_path / "run"
    (run / "final").mkdir(parents=True)
    (run / "qa").mkdir()
    atlas = Image.new("RGBA", (1536, 2288))
    draw = ImageDraw.Draw(atlas)
    for row, frames in enumerate([7, 8, 8, 4, 5, 8, 6, 6, 6, 8, 8]):
        for column in range(frames):
            x, y = column * 192, row * 208
            draw.ellipse((x + 60, y + 75, x + 130, y + 145), fill=(30, 70, 200, 255))
    atlas.save(run / "final" / "spritesheet-extended.webp", lossless=True)
    request = {"pet_id": "test-pet", "display_name": "Test Pet", "description": "A test fixture", "chroma_key": {"hex": "#00FF00"}}
    (run / "pet_request.json").write_text(json.dumps(request))
    labels = ["000", "022.5", "045", "067.5", "090", "112.5", "135", "157.5", "180", "202.5", "225", "247.5", "270", "292.5", "315", "337.5"]
    for name, data in {"review": {"errors": []}, "chroma-despill-extended": {"ok": True}, "direction-blind-validation": {"ok": True},
            "direction-semantics": {"directions": [{"label": label, "verdict": "pass", "reason": "Fixture only"} for label in labels]}}.items():
        (run / "qa" / f"{name}.json").write_text(json.dumps(data))
    events = []
    async def emit(kind, data): events.append((kind, data))
    context = ToolExecutionContext(permission=PermissionContext(workspace_root=tmp_path), workspace_root=tmp_path,
        sandbox_policy=SandboxPolicy(workspace_root=tmp_path), emit_event=emit, conversation_id="pet-conversation")
    result = asyncio.run(HatchPetTool().execute({"action": "publish", "run_directory": str(run)}, context))
    assert not result.is_error
    published = tmp_path / "config" / "pets" / "test-pet"
    assert json.loads((published / "pet.json").read_text())["spriteVersionNumber"] == 2
    assert (published / "spritesheet.webp").read_bytes() == (run / "final" / "spritesheet-extended.webp").read_bytes()
    assert events == [("pet.published", {"pet_id": "custom:test-pet", "conversation_id": "pet-conversation"})]
    with pytest.raises(FileExistsError):
        asyncio.run(HatchPetTool().execute({"action": "publish", "run_directory": str(run)}, context))
    assert len(events) == 1
