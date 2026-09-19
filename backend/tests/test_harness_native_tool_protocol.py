from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from lark import Lark, UnexpectedInput

from backend.agent.checkpoint import load_latest_checkpoint, save_checkpoint
from backend.agent.context import ContextBuilder
from backend.agent.max_output_recovery import _continuation_provider_items
from backend.agent.state import AgentState
from backend.config import AppConfig, LLMSettings, load_llm_settings
from backend.llm.base import LLMMessage, StreamEventType
from backend.llm.model_runtime import ModelRuntime
from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.openai_trace import _safe_tool_schema_hashes
from backend.services.llm_adapter_factory import build_provider_adapter, create_session_llm
from backend.tests.test_committed_tool_scheduling import run_case
from backend.tools.apply_patch import APPLY_PATCH_GRAMMAR, ApplyPatchTool
from backend.tools.apply_patch_parser import parse_patch


PATCH = '*** Begin Patch\n*** Add File: result.txt\n+中文 😀 "quoted" C:\\repo\\file\n+\n*** Add File: empty.txt\n*** End Patch'


def completed(output, *, text=""):
    return {"type": "response.completed", "response": {
        "id": "response-1", "status": "completed", "output": output, "output_text": text,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }}


def custom_item(patch=PATCH, *, name="apply_patch"):
    return {"type": "custom_tool_call", "id": "custom-1", "call_id": "call-1", "name": name, "input": patch}


def patch_events(*, terminal_only=False, duplicate_done=False):
    item = custom_item()
    if terminal_only:
        return [completed([item])]
    events = [{"type": "response.output_item.added", "output_index": 0, "item": {**item, "input": ""}}]
    for offset in range(0, len(PATCH), 7):
        events.append({"type": "response.custom_tool_call_input.delta", "item_id": "custom-1", "delta": PATCH[offset:offset + 7]})
    done = {"type": "response.custom_tool_call_input.done", "item_id": "custom-1", "input": PATCH}
    events.extend([done, done] if duplicate_done else [done])
    events.extend([
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        completed([item]),
    ])
    return events


class ResponsesEndpoint:
    def __init__(self, batches):
        self.batches = batches
        self.requests = []

    async def __call__(self, request):
        self.requests.append(json.loads(request.content))
        events = self.batches[len(self.requests) - 1]
        payload = "\n\n".join("data: " + json.dumps(event, ensure_ascii=False) for event in events) + "\n\n"
        return httpx.Response(200, content=payload.encode(), headers={"content-type": "text/event-stream"})


def settings(**changes):
    return LLMSettings(api_key="fixture", provider="openai", model="fixture-model",
                       base_url="https://fixture.invalid/v1", wire_api="responses", **changes)


@pytest.mark.parametrize("terminal_only, duplicate_done, approve", [(False, False, True), (False, True, True), (True, False, True), (False, False, False)])
def test_native_patch_executes_once_through_query_engine_and_replays_after_checkpoint(
    tmp_path, monkeypatch, terminal_only, duplicate_done, approve,
):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))

    class CountPatch(ApplyPatchTool):
        calls = 0
        async def execute(self, args, context=None):
            self.calls += 1
            return await super().execute(args, context)

    async def scenario():
        endpoint = ResponsesEndpoint([patch_events(terminal_only=terminal_only, duplicate_done=duplicate_done), [completed([], text="Finished.")]])
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            adapter = OpenAIAdapter(settings(supports_custom_tools=True, model_instructions="Fixture model guidance."), http_client=client)
            tool = CountPatch()
            async def approval(*args, **kwargs):
                return {"action": "approve" if approve else "reject"}
            state, builder, _ = await run_case(tmp_path, adapter, [tool], approval=approval)
            assert state.terminal_status == "completed", state.stopped_reason
            assert tool.calls == int(approve)
            if approve:
                assert (tmp_path / "result.txt").read_text(encoding="utf-8") == '中文 😀 "quoted" C:\\repo\\file\n\n'
                assert (tmp_path / "empty.txt").read_bytes() == b""
            else:
                assert not (tmp_path / "result.txt").exists()
            assert len(endpoint.requests) == 2
            first, second = endpoint.requests
            assert first["tools"][0]["type"] == "custom"
            assert "Do not wrap it in JSON" in first["tools"][0]["description"]
            assert "Fixture model guidance." in first["instructions"]
            native = [item for item in second["input"] if item.get("type") == "custom_tool_call"]
            outputs = [item for item in second["input"] if item.get("type") == "custom_tool_call_output"]
            assert len(native) == len(outputs) == 1
            assert native[0]["input"] == PATCH
            assert native[0]["call_id"] == outputs[0]["call_id"] == "call-1"
            assert "status" not in outputs[0]
            assert all(item.get("type") != "function_call_output" for item in second["input"])

            snapshot = builder.export_snapshot()
            save_checkpoint(session_id="native-resume", conversation_id="conv-native", user_message="continue",
                iterations=state.iterations, reply=state.reply, messages=snapshot["history"], context_snapshot=snapshot,
                tool_calls=state.tool_calls, active_skills=[], disabled_tools=set(), stopped_reason="timeout",
                last_mutation_index=0, base_dir=tmp_path)
            restored = load_latest_checkpoint("native-resume", tmp_path, conversation_id="conv-native")
            assert restored is not None
            builder.load_snapshot(restored.context_snapshot)
            replay = adapter._build_responses_input(builder._history)
            assert [item for item in replay if item.get("type") == "custom_tool_call"] == native
            # A destination without custom tools replays the same canonical arguments as JSON.
            adapter._settings = replace(adapter._settings, supports_custom_tools=False)
            fallback = adapter._build_responses_input(builder._history)
            calls = [item for item in fallback if item.get("type") == "function_call"]
            assert len(calls) == 1
            assert json.loads(calls[0]["arguments"]) == {"patch": PATCH}
            assert len([item for item in fallback if item.get("type") == "function_call_output"]) == 1
            assert not any(item.get("type", "").startswith("custom_") for item in fallback)
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["unknown_name", "invalid_input", "conflicting_done", "truncated", "missing_terminal"])
def test_native_protocol_failure_does_not_publish_executable_call(tmp_path, kind):
    item = custom_item()
    events = [{"type": "response.output_item.added", "item": {**item, "input": ""}}]
    if kind == "unknown_name":
        events = [completed([custom_item(name="unoffered")])]
    elif kind == "invalid_input":
        events = [completed([{**item, "input": {"patch": PATCH}}])]
    elif kind == "conflicting_done":
        events += [
            {"type": "response.custom_tool_call_input.done", "item_id": "custom-1", "input": PATCH},
            {"type": "response.output_item.done", "item": {**item, "input": PATCH + "changed"}},
        ]
    elif kind == "truncated":
        events += [{"type": "response.custom_tool_call_input.delta", "item_id": "custom-1", "delta": PATCH[:40]},
                   {"type": "response.incomplete", "response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": [custom_item(PATCH[:40])]}}]
    async def scenario():
        endpoint = ResponsesEndpoint([events])
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            adapter = OpenAIAdapter(settings(supports_custom_tools=True), http_client=client)
            emitted = [event async for event in adapter.stream_chat([LLMMessage(role="user", content="edit")], [ApplyPatchTool().model_schema().to_openai_tool()])]
            if kind == "truncated":
                terminal = next(event for event in emitted if event.type == StreamEventType.DONE)
                assert terminal.finish_reason == "max_output_tokens"
                assert _continuation_provider_items(terminal.provider_items) == []
                assert not any(event.tool_calls_committed for event in emitted)
            else:
                assert any(event.type == StreamEventType.ERROR for event in emitted)
                assert not any(event.tool_calls_committed for event in emitted)
    asyncio.run(scenario())


@pytest.mark.parametrize("patch", [
    PATCH,
    "*** Begin Patch\n*** Delete File: old.txt\n*** End Patch\n",
    "*** Begin Patch\n*** Update File: a.txt\n*** Move to: b.txt\n*** End Patch",
    "*** Begin Patch\n*** Update File: a.txt\n@@\n old\n-previous\n+next\n*** End of File\n*** End Patch",
])
def test_patch_grammar_and_executor_parser_accept_the_same_supported_envelopes(patch):
    grammar = Lark(APPLY_PATCH_GRAMMAR, parser="lalr")
    grammar.parse(patch)
    assert parse_patch(patch)
    with pytest.raises(UnexpectedInput):
        grammar.parse(json.dumps({"patch": patch}))
    with pytest.raises(UnexpectedInput):
        grammar.parse(patch[:patch.rfind("*** End Patch")])


def test_native_schema_is_opt_in_and_diagnostics_track_grammar():
    schema = ApplyPatchTool().model_schema().to_openai_tool()
    function = OpenAIAdapter._convert_tools_to_responses_format([schema])
    native = OpenAIAdapter._convert_tools_to_responses_format([schema], supports_custom_tools=True)
    assert function[0]["type"] == "function"
    assert "parameters" in function[0] and "parameters" not in native[0]
    assert "_minicode_freeform" not in json.dumps(function + native + OpenAIAdapter._normalize_chat_tools([schema]))
    changed = [{**native[0], "format": {**native[0]["format"], "definition": "start: /.+/"}}]
    assert _safe_tool_schema_hashes(native) != _safe_tool_schema_hashes(changed)


def test_truncated_native_patch_recovers_without_orphan_calls_or_partial_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    partial = custom_item(PATCH[:40])
    truncated = {"type": "response.incomplete", "response": {
        "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
        "usage": {"input_tokens": 10, "output_tokens": 10}, "output": [partial],
    }}
    async def scenario():
        endpoint = ResponsesEndpoint([[truncated], patch_events(), [completed([], text="Finished.")]])
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            adapter = OpenAIAdapter(settings(supports_custom_tools=True), http_client=client)
            state, _, _ = await run_case(tmp_path, adapter, [ApplyPatchTool()])
            assert state.terminal_status == "completed", state.stopped_reason
            assert len(endpoint.requests) == 3
            assert len(state.tool_calls) == 1
            assert state.tool_calls[0].status == "success"
            assert not any(item.get("type") in {"custom_tool_call", "function_call"} for item in endpoint.requests[1]["input"])
            assert (tmp_path / "result.txt").read_text(encoding="utf-8").startswith("中文 😀")
    asyncio.run(scenario())


def test_selected_model_metadata_drives_adapter_prompt_and_refresh_without_crosstalk(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    snapshot = {"llm": {"provider": "openai", "openai": {
        "api_key": "fixture", "base_url": "https://fixture.invalid/v1", "model": "alpha", "wire_api": "responses",
        "available_models": ["alpha", "beta"], "model_metadata": {
            "alpha": {"supports_custom_tools": True, "responses_websocket": True, "model_instructions": "Alpha model guidance."},
            "beta": {"supports_custom_tools": False, "model_instructions": "Beta model guidance."},
        },
    }}}
    loaded = load_llm_settings(snapshot)
    assert loaded.supports_custom_tools and loaded.model_instructions == "Alpha model guidance."
    assert loaded.responses_websocket
    runtime = ModelRuntime(settings_snapshot=snapshot, provider_configs={}, models_path=tmp_path / "models.json")
    alpha = build_provider_adapter("openai", model_override="alpha", model_runtime=runtime)
    beta = build_provider_adapter("openai", model_override="beta", model_runtime=runtime)
    builder = ContextBuilder(llm=alpha, workspace_root=tmp_path)
    state = AgentState(user_message="work", workspace_root=tmp_path)
    first = builder.base_system_prompt(state)
    assert "Alpha model guidance." in first
    assert "Beta model guidance." not in first
    assert alpha._settings.supports_custom_tools
    builder.bind_llm(beta)
    second = builder.base_system_prompt(state)
    assert "Beta model guidance." in second and "Alpha model guidance." not in second
    assert not beta._settings.supports_custom_tools
    assert not beta._settings.responses_websocket
    assert first != second
    assert first == ContextBuilder(llm=alpha, workspace_root=tmp_path).base_system_prompt(state)
    changed = create_session_llm(AppConfig(llm=loaded), model_override="unknown")
    assert not changed._settings.supports_custom_tools and not changed.model_instructions()
    assert not changed._settings.responses_websocket

    runtime._model_configs["openai"] = {"model_overrides": {"alpha": {"supports_custom_tools": False, "model_instructions": ""}}}
    revised = build_provider_adapter("openai", model_override="alpha", model_runtime=runtime)
    assert not revised._settings.supports_custom_tools and not revised.model_instructions()


@pytest.mark.parametrize("source", ["models_json", "extension"])
def test_native_model_behavior_survives_config_and_extension_adapter_construction(tmp_path, monkeypatch, source):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    profile = {"api_key": "fixture", "models": [{
        "id": "native-model", "api": "openai-responses", "base_url": "https://fixture.invalid/v1",
        "supports_custom_tools": True, "responses_websocket": True, "model_instructions": "Bound to native-model.",
        "context_window": 128000, "max_tokens": 8000,
    }]}
    models_path = tmp_path / "models.json"
    if source == "models_json":
        models_path.write_text(json.dumps({"providers": {"native": profile}}), encoding="utf-8")
        runtime = ModelRuntime(models_path=models_path)
    else:
        runtime = ModelRuntime(provider_configs={})
        runtime.register_provider("native", profile)
    adapter = build_provider_adapter("native", model_override="native-model", model_runtime=runtime)
    assert adapter._settings.supports_custom_tools
    assert adapter._settings.responses_websocket
    assert adapter.model_instructions() == "Bound to native-model."
    projected = runtime.get_model("native", "native-model").to_extension_dict()
    assert projected["supports_custom_tools"] is True
    assert projected["responses_websocket"] is True
    assert projected["model_instructions"] == "Bound to native-model."
