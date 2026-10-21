from __future__ import annotations

import json
import re
import sqlite3
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from backend.config import AppConfig, LLMSettings
from backend.llm import openai_adapter
from scripts.audit_long_task_session import (
    configure_v2_board_audit, namespace_preflight, provider_identity, v2_board_evidence, v2_board_tool_schemas,
)
from backend.tools.toolsets import SOURCE_V2_COORDINATION_TOOLS, SOURCE_MESSAGE_BOARD_TOOLS


def full_proof(settings, tool_namespace="collaboration"):
    schemas = v2_board_tool_schemas(tool_namespace)
    tools = openai_adapter.OpenAIAdapter._convert_tools_to_responses_format(schemas)
    return {"passed": True, **provider_identity(settings), "namespace": tool_namespace,
        "tool_names": sorted(SOURCE_V2_COORDINATION_TOOLS | SOURCE_MESSAGE_BOARD_TOOLS),
        "requests": [{"tools": deepcopy(tools)}, {"tools": deepcopy(tools)}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
@pytest.mark.parametrize("tool_namespace", ["collaboration", "agents"])
async def test_namespace_preflight_uses_real_source_schema_result_and_exact_wire(tmp_path, monkeypatch, rejected, tool_namespace):
    settings = LLMSettings(api_key="fixture-provider-secret", model="fixture-model", base_url="https://audit.invalid/v1", wire_api="responses")
    requests = []
    async def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        tools = body["tools"]
        assert len(tools) == 1 and tools[0]["type"] == "namespace" and tools[0]["name"] == tool_namespace
        functions = {tool["name"]: tool for tool in tools[0]["tools"]}
        assert set(functions) == SOURCE_V2_COORDINATION_TOOLS | SOURCE_MESSAGE_BOARD_TOOLS
        assert len(tools[0]["tools"]) == 15
        assert functions["create_channel"]["parameters"]["required"] == ["channel_name"]
        assert functions["post"]["parameters"]["required"] == ["text"]
        assert functions["spawn_agent"]["parameters"]["required"] == ["message", "task_name"]
        assert "agent_type" not in functions["spawn_agent"]["parameters"]["properties"]
        assert f"{tool_namespace}.list_agents" in body["input"][0]["content"]
        if rejected:
            return httpx.Response(400, request=request, json={"error": {"type": "invalid_request_error",
                "message": "fixture-provider-secret rejected the namespace"}})
        if len(requests) == 1:
            item = {"type": "function_call", "id": "item-list", "call_id": "call-list", "name": "list_agents",
                "namespace": tool_namespace, "arguments": "{}", "status": "completed"}
            frames = [{"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": "", "status": "in_progress"}},
                {"type": "response.function_call_arguments.done", "item_id": item["id"], "arguments": "{}"},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {"id": "response", "status": "completed", "output": [item]}}]
        else:
            output = next(item for item in body["input"] if item.get("type") == "function_call_output")
            assert json.loads(output["output"])["agents"] == [{"agent_name": "/root", "agent_status": "running"}]
            nonce = re.search(r"followed by ([a-f0-9]{32})", body["input"][0]["content"]).group(1)
            text = "NAMESPACE_PROBE_OK " + nonce
            item = {"id": "answer", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}
            frames = [{"type": "response.output_text.delta", "item_id": "answer", "output_index": 0, "content_index": 0, "delta": text},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {"id": "response-2", "status": "completed", "output": [item]}}]
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
            content="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames).encode())
    real = openai_adapter.OpenAIAdapter
    class FixtureAdapter(real):
        def __init__(self, selected):
            super().__init__(selected, http_client=http)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(openai_adapter, "OpenAIAdapter", FixtureAdapter)
        receipt = await namespace_preflight(settings, tmp_path / "runtime", tool_namespace)
    assert receipt["passed"] is not rejected
    assert settings.api_key not in json.dumps(receipt)
    assert len(requests) == (1 if rejected else 2)
    assert receipt["namespace"] == tool_namespace
    assert receipt["tool_names"] == sorted(SOURCE_V2_COORDINATION_TOOLS | SOURCE_MESSAGE_BOARD_TOOLS)
    if not rejected:
        assert receipt["actual_tool_result"]["agents"][0]["agent_name"] == "/root"
        assert receipt["native_call"]["namespace"] == tool_namespace


@pytest.mark.parametrize("tool_namespace", ["collaboration", "agents"])
def test_only_exact_successful_provider_receipt_can_capture_production_child_v2_metadata(tmp_path, monkeypatch, tool_namespace):
    monkeypatch.setattr("backend.llm.model_runtime._DEFAULT_MODELS_CONFIG_FILE", tmp_path / "production-models.json")
    config = AppConfig(llm=LLMSettings(api_key="never-persist-this", model="gpt-6.1-sol", wire_api="responses", base_url="https://audit.invalid/v1", context_window=128_000))
    profile = {"llm": {"provider": "custom", "custom": {"model": config.llm.model, "base_url": config.llm.base_url,
        "available_models": [config.llm.model], "wire_api": "responses"}}, "agent": {"max_turn_seconds": 5400}}
    proof = full_proof(config.llm, tool_namespace)
    for changed in ({"passed": False}, {"model": "different"}, {"wire_api": "chat"}, {"endpoint_sha256": "different"}):
        with pytest.raises(AssertionError):
            configure_v2_board_audit(tmp_path, profile, config, {**proof, **changed}, tool_namespace)
        assert not (tmp_path / ".minicode").exists()
    snapshot = configure_v2_board_audit(tmp_path, profile, config, proof, tool_namespace)
    assert snapshot["multi_agent_version"] == "v2" and snapshot["board_backend"] == "local"
    assert snapshot["multi_agent_reasoning_effort"] == "xhigh"
    assert snapshot["production_model_definition"]["source_model_metadata"] == {
        "reference_commit": "822e58cc3d666166c7446c5b1ea2e52f5d09594c", "catalog_file": "codex-rs/models-manager/models.json"}
    assert len(snapshot["production_definition_sha256"]) == 64
    assert profile["features"]["agent_message_board"] is True and profile["agent"]["max_turn_seconds"] == 5400
    assert profile["features"]["multi_agent_v2"]["tool_namespace"] == snapshot["namespace"] == tool_namespace
    assert snapshot["namespace_preflight"]["namespace"] == tool_namespace
    assert snapshot["namespace_preflight"]["tool_names"] == proof["tool_names"]
    catalog_text = (tmp_path / ".minicode/models.json").read_text()
    assert config.llm.api_key not in catalog_text and config.llm.api_key not in json.dumps(snapshot)
    model = json.loads(catalog_text)["providers"]["custom"]["models"][0]
    assert model["multi_agent_reasoning_effort"] == "xhigh"
    assert not (tmp_path / ".minicode/models-store.json").exists()


@pytest.mark.parametrize("fault", ["old-list-only", "wrong-namespace", "missing-board-name", "duplicate-name",
    "missing-board-schema", "wrong-wire-namespace", "changed-board-schema", "missing-result-request"])
def test_configure_rejects_incomplete_or_different_namespace_inventory_before_mutation(tmp_path, fault):
    config = AppConfig(llm=LLMSettings(api_key="", model="gpt-6.1-sol", wire_api="responses", base_url="https://audit.invalid/v1"))
    profile = {"agent": {"max_turn_seconds": 5400}}
    original = deepcopy(profile)
    proof = full_proof(config.llm, "agents")
    if fault == "old-list-only":
        proof = {"passed": True, **provider_identity(config.llm)}
    elif fault == "wrong-namespace":
        proof["namespace"] = "collaboration"
    elif fault == "missing-board-name":
        proof["tool_names"].remove("create_channel")
    elif fault == "duplicate-name":
        proof["tool_names"].append("list_agents")
    elif fault == "missing-board-schema":
        proof["requests"][0]["tools"][0]["tools"] = [item for item in proof["requests"][0]["tools"][0]["tools"] if item["name"] != "create_channel"]
    elif fault == "wrong-wire-namespace":
        proof["requests"][0]["tools"][0]["name"] = "collaboration"
    elif fault == "changed-board-schema":
        next(item for item in proof["requests"][0]["tools"][0]["tools"] if item["name"] == "post")["parameters"]["required"] = []
    else:
        proof["requests"].pop()
    with pytest.raises(AssertionError):
        configure_v2_board_audit(tmp_path, profile, config, proof, "agents")
    assert profile == original and not (tmp_path / ".minicode").exists()


@pytest.mark.parametrize("model,wire_api,override,error", [
    ("audit-model", "responses", None, "does not advertise V2"),
    ("gpt-5.6-luna", "responses", None, "does not advertise V2"),
    ("gpt-6.1-sol", "responses", "v1", "does not advertise V2"),
    ("gpt-6.1-sol", "chat", None, "does not advertise the configured Responses model"),
])
def test_namespace_proof_cannot_invent_v2_or_override_the_original_model_catalog(tmp_path, monkeypatch, model, wire_api, override, error):
    production_path = tmp_path / "production-models.json"
    monkeypatch.setattr("backend.llm.model_runtime._DEFAULT_MODELS_CONFIG_FILE", production_path)
    if override is not None:
        production_path.write_text(json.dumps({"providers": {"custom": {"model_overrides": {
            model: {"multi_agent_version": override}}}}}), encoding="utf-8")
    config = AppConfig(llm=LLMSettings(api_key="never-persist-this", model=model, wire_api="responses", base_url="https://audit.invalid/v1"),
        config_layer_stack=SimpleNamespace(effective_config=lambda: {"llm": {"provider": "custom", "custom": {
            "model": model, "wire_api": wire_api, "available_models": [model]}}}))
    profile = {"llm": {"provider": "custom", "custom": {"model": model, "wire_api": "responses"}},
        "agent": {"max_turn_seconds": 5400}}
    original = json.dumps(profile)
    with pytest.raises(AssertionError, match=error):
        configure_v2_board_audit(tmp_path, profile, config, full_proof(config.llm))
    assert json.dumps(profile) == original and not (tmp_path / ".minicode").exists()


def board_case(tmp_path, tool_namespace="collaboration"):
    path = tmp_path / "agent_message_board_1.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE channels(board,name,author); CREATE TABLE posts(seq,board,id,channel,root,author,payload,request_id,timestamp);")
        db.execute("INSERT INTO channels VALUES('conversation','audit','/root')")
        for seq, identity, author, text, turn in [(1, "root-post", "/root", "ROOT_BOARD token", "root-turn"),
                (2, "child-post", "/root/reviewer", "CHILD_BOARD token", "child-turn"),
                (3, "child-followup", "/root/reviewer", "CHILD_FOLLOWUP_BOARD token", "child-turn")]:
            db.execute("INSERT INTO posts VALUES(?,?,?,?,?,?,?,?,?)", (seq, "conversation", identity, "audit", "root-post", author,
                json.dumps({"text": text}), f"{author}:{turn}:call-{seq}", seq * 1_000_000))
    events = []
    for name in ("create_channel", "post", "spawn_agent", "followup_task", "read_thread"):
        events += [{"type": "tool_call", "id": name, "name": name, "conversation_id": "conversation", "message_id": "answer",
            "turn_id": "root-turn", "native_namespace": tool_namespace, "args": {"thread_id": "root-post"}, "started_at": 4000},
            {"type": "tool_result", "id": name, "conversation_id": "conversation", "message_id": "answer", "turn_id": "root-turn", "is_error": False}]
    events.append({"type": "subagent.start", "subagent_id": "child-turn", "run_id": "child-turn", "conversation_id": "conversation",
        "parent_message_id": "answer", "parent_tool_call_id": "spawn_agent", "parent_run_id": "root-turn", "agent_path": "root-turn/reviewer"})
    return events, path


@pytest.mark.parametrize("fault", [None, "wrong-namespace", "wrong-read-namespace", "failed-spawn", "wrong-parent-turn", "wrong-result-message", "stale-read", "legacy"])
@pytest.mark.parametrize("tool_namespace", ["collaboration", "agents"])
def test_v2_board_acceptance_requires_actual_success_original_tree_turn_and_latest_child_result(tmp_path, fault, tool_namespace):
    events, _path = board_case(tmp_path, tool_namespace)
    if fault == "wrong-namespace":
        events[0]["native_namespace"] = "functions"
    elif fault == "wrong-read-namespace":
        events[8]["native_namespace"] = "functions"
    elif fault == "failed-spawn":
        events[5]["is_error"] = True
    elif fault == "wrong-parent-turn":
        events[-1]["parent_run_id"] = "different"
    elif fault == "wrong-result-message":
        events[1]["message_id"] = "different"
    elif fault == "stale-read":
        events[8]["started_at"] = 1000
    elif fault == "legacy":
        events[8]["name"] = "task"
    if fault:
        with pytest.raises(AssertionError):
            v2_board_evidence(events, tmp_path, conversation_id="conversation", message_id="answer", channel="audit", nonce="token", child_probe=True, tool_namespace=tool_namespace)
    else:
        evidence = v2_board_evidence(events, tmp_path, conversation_id="conversation", message_id="answer", channel="audit", nonce="token", child_probe=True, tool_namespace=tool_namespace)
        assert evidence["namespace"] == tool_namespace
        assert evidence["child_id"] == "child-turn" and evidence["child_post_ids"] == ["child-post", "child-followup"]


@pytest.mark.parametrize("explicit_namespace", [None, "agents"])
def test_cli_preserves_default_or_explicit_namespace_without_loading_real_config(tmp_path, monkeypatch, explicit_namespace):
    from scripts import audit_long_task_session as audit
    config = AppConfig(llm=LLMSettings(api_key="fixture-only", model="fixture-model", wire_api="responses"))
    received = []
    async def preflight(settings, probe_root, tool_namespace):
        received.append((settings, probe_root, tool_namespace))
        return {"model": settings.model, "wire_api": settings.wire_api, "passed": True,
            "namespace": tool_namespace, "tool_names": sorted(SOURCE_V2_COORDINATION_TOOLS | SOURCE_MESSAGE_BOARD_TOOLS)}
    monkeypatch.setattr("backend.config.load_config", lambda cwd: config)
    monkeypatch.setattr(audit, "namespace_preflight", preflight)
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "initial"))
    output = tmp_path / "output"
    argv = ["audit_long_task_session.py", "--out", str(output), "--configured-state", str(tmp_path / "fixture-state"), "--namespace-preflight-only"]
    if explicit_namespace is not None:
        argv.extend(["--tool-namespace", explicit_namespace])
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit) as stopped:
        audit.main()
    assert stopped.value.code == 0
    assert received == [(config.llm, output / "namespace-runtime", explicit_namespace or "collaboration")]
    assert json.loads((output / "namespace-preflight.json").read_text())["namespace"] == received[0][2]
