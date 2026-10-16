from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.context_window import ContextWindowConfig
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, LLMSettings, PermissionSettings, TokenBudget
from backend.conversations.public_projection import project_public_conversation, project_public_tool_call
from backend.conversations.repository import ConversationRepository
from backend.feature_flags import FeatureFlags
from backend.llm.openai_adapter import OpenAIAdapter, _responses_function_call_input_item, _responses_function_call_output_item
from backend.permissions.context import PermissionContext
from backend.sdk import SDKSession
from backend.tests.test_history_notes_service import notes_runtime
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.history_notes import history_notes_actions
from backend.tools.registry import ToolRegistry


PRIVATE_PATH = "/root/notes/fixture-private-state.md"
PRIVATE_CIPHER = "explicit-private-service-ciphertext"
PRIVATE_TEXT = "explicit-private-service-plaintext"
HINT_PREFIX = "Recent notes: private fixture hint"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jVZkAAAAASUVORK5CYII="
PRIVATE_NAMES = {action.runtime_name for action in history_notes_actions()}


def call(name, arguments, *, namespace="", call_id="private-notes-call"):
    return {"type": "function_call", "id": call_id + "-item", "call_id": call_id,
        "name": name, "arguments": json.dumps(arguments), "status": "completed",
        **({"namespace": namespace} if namespace else {})}


def response(output, response_id):
    return {"type": "response.completed", "response": {"id": response_id,
        "status": "completed", "output": output,
        "usage": {"input_tokens": 200, "output_tokens": 20}}}


def final(response_id="final"):
    return response([{"type": "message", "id": response_id + "-message", "role": "assistant",
        "phase": "final_answer", "content": [{"type": "output_text", "text": "The fixture operation is complete."}]}], response_id)


def streamed_calls(items, *, terminal_only=False):
    if terminal_only:
        return [response(items, "calls-completed")]
    events = []
    for index, item in enumerate(items):
        events.append({"type": "response.output_item.added", "output_index": index,
            "item": {**item, "status": "in_progress", "arguments": ""}})
        encoded = item["arguments"]
        for offset in range(0, len(encoded), 11):
            events.append({"type": "response.function_call_arguments.delta", "item_id": item["id"],
                "output_index": index, "delta": encoded[offset:offset + 11]})
        done = {"type": "response.function_call_arguments.done", "item_id": item["id"],
            "output_index": index, "arguments": encoded}
        events.extend([done, deepcopy(done), {"type": "response.output_item.done", "output_index": index, "item": item}])
    events.append(response(items, "calls-completed"))
    return events


@contextmanager
def service_chain(batches, *, note_result=None, note_status=200, hint_status=200):
    model_requests, note_requests = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            record = {"path": self.path, "body": body,
                "headers": {key.lower(): value for key, value in self.headers.items()}}
            if self.path == "/backend-api/codex/responses":
                model_requests.append(record)
                events = batches[len(model_requests) - 1]
                payload = ("\n\n".join("data: " + json.dumps(event) for event in events) + "\n\n").encode("utf-8")
                status, content_type = 200, "text/event-stream"
            else:
                note_requests.append(record)
                if self.path == "/backend-api/codex/alpha/notes/v2/thread_hint":
                    hints = sum(request["path"].endswith("/thread_hint") for request in note_requests)
                    value, status = {"text": f"{HINT_PREFIX} {hints}: {PRIVATE_PATH}"}, hint_status
                else:
                    assert self.path in {"/backend-api/codex/" + action.endpoint for action in history_notes_actions()}
                    value, status = note_result, note_status
                payload = json.dumps(value).encode("utf-8")
                content_type = "application/json"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}",
            model_requests=model_requests, note_requests=note_requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class OrdinaryRead(BaseTool):
    name = "read_file"
    description = "Read an ordinary public fixture file."
    permission = PermissionLevel.AUTO
    read_only = True

    def __init__(self, *, error=False):
        self.error = error
        self.calls = 0

    def get_schema(self):
        return ToolSchema(self.name, self.description, {"type": "object", "properties": {
            "file_path": {"type": "string"}}, "required": ["file_path"], "additionalProperties": False})

    async def execute(self, args, context=None):
        self.calls += 1
        return ToolResult("ordinary fixture failure" if self.error else "ordinary fixture file contents",
            is_error=self.error, runtime_metadata={"private_model_only": True})


@asynccontextmanager
async def sdk_fixture(tmp_path, server, *, ordinary_error=False, captured=True, session_id="notes-source-sdk"):
    model_runtime, model, storage = notes_runtime(tmp_path, server.url)
    await model_runtime.refresh_provider_auth(model.provider, publish_snapshot=False)
    spec = model_runtime.resolve_adapter_spec(model.provider, model.id)
    settings = LLMSettings(api_key=spec.api_key, provider=model.provider, model=model.id,
        base_url=spec.base_url, default_headers=tuple(spec.headers.items()), proxy_mode=spec.proxy_mode,
        wire_api="responses", tool_mode="code_mode", supports_custom_tools=True, max_tokens=1024,
        truncation_policy=dict(model.truncation_policy))
    adapter = OpenAIAdapter(settings)
    budget = TokenBudget(total=96000, response_reserve=4096)
    config = AppConfig(llm=settings, token_budget=budget,
        permissions=PermissionSettings(require_confirm=[], require_diff_review=[]),
        agent=AgentSettings(max_iterations=8, max_turn_seconds=20, stream_max_attempts=0, code_mode_only=False),
        feature_flags=FeatureFlags({"token_budget": True}),
        context_window_config=ContextWindowConfig(model_context_window=96000, use_history_notes_extension=True))
    registry = ToolRegistry()
    ordinary = OrdinaryRead(error=ordinary_error)
    for tool in (ordinary, ToolExecTool(), ToolWaitTool()):
        registry.register(tool)
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl",
        swarm_store_dir=tmp_path / "runtime/swarm", enable_lease_heartbeat=False)
    journal = ExecutionJournal("notes", base_dir=tmp_path / "journal")
    snapshot = ModelExecutionSnapshot(config=config, llm=adapter, provider=model.provider,
        model=model.id, model_runtime=model_runtime, model_info=model) if captured else None
    owner = RunContext(agent_runtime=runtime, execution_journal=journal, model_execution=snapshot)
    builder = ContextBuilder(llm=adapter, token_budget=budget, workspace_root=tmp_path,
        conversation_id="notes-source-conversation")
    sdk = SDKSession(session_id=session_id, llm=adapter, config=config, tool_registry=registry,
        workspace_root=tmp_path, artifact_store=artifacts,
        permission_context=PermissionContext(mode="bypass"), context_builder=builder,
        run_context=owner, metadata={"conversation_id": "notes-source-conversation"})
    fixture = SimpleNamespace(sdk=sdk, owner=owner, registry=registry, builder=builder, adapter=adapter,
        config=config, model_runtime=model_runtime, model=model, storage=storage,
        ordinary=ordinary, runtime=runtime, journal=journal, artifacts=artifacts)
    try:
        yield fixture
    finally:
        await sdk.aclose()
        await adapter.aclose()
        artifacts.shutdown()
        runtime.close(release_lease=True)


async def submit(fixture, text="Complete the fixture", *, session=None, **overrides):
    selected = session or fixture.sdk
    state = AgentState(user_message=text, workspace_root=fixture.builder._workspace_root,
        conversation_id=selected.metadata["conversation_id"])
    events = [event async for event in selected.query(text, state=state, **overrides)]
    assert state.terminal_status == "completed", (state.stopped_reason, [event.data for event in events])
    return state, events


def input_texts(request):
    result = [request["body"]["instructions"]] if request["body"].get("instructions") else []
    for item in request["body"]["input"]:
        content = item.get("content")
        if isinstance(content, str):
            result.append(content)
        elif isinstance(content, list):
            result.extend(block["text"] for block in content if block.get("type") == "input_text")
    return result


def source_metadata(request):
    client = request["body"]["client_metadata"]
    header = request["headers"]["x-codex-turn-metadata"]
    assert client["x-codex-turn-metadata"] == header
    assert header.isascii()
    source = json.loads(header)
    assert {"agent_name", "context_window_id", "window_number", "history_ingest_requested"} <= set(source)
    for name, value in source.items():
        assert client[name] == (value if isinstance(value, str) else json.dumps(value, ensure_ascii=True, separators=(",", ":")))
    return client, source


def assert_public(events, registry, *, allow_ordinary=True):
    def public_facts(value, path="data"):
        if isinstance(value, dict):
            for key, item in value.items():
                yield path + "." + key, key
                yield from public_facts(item, path + "." + key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from public_facts(item, f"{path}[{index}]")
        else:
            yield path, json.dumps(value)

    secrets = [*PRIVATE_NAMES, PRIVATE_PATH, PRIVATE_CIPHER, PRIVATE_TEXT, HINT_PREFIX, "private-notes-call"]
    leaks = [(secret, event.type, path) for event in events for path, value in public_facts(event.data)
        for secret in secrets if secret in value]
    assert not leaks, leaks
    inventory = json.dumps(registry.build_snapshot())
    runtime_metadata = json.dumps(registry.get_runtime_metadata())
    for name in PRIVATE_NAMES:
        assert name not in inventory and name not in runtime_metadata
    if allow_ordinary:
        assert any(event.type == "tool_result" and event.data.get("id") == "ordinary-call" for event in events)
        for event_type, payload_key in (("stream_event", "data"), ("inspector.update", "payload")):
            assert any(item.get("call_id") == "ordinary-call" and item.get("name") == "read_file"
                for event in events if event.type == event_type
                for item in event.data[payload_key].get("output_items", [])), event_type


@pytest.mark.parametrize("terminal_only", [False, True])
@pytest.mark.parametrize("kind", ["encrypted", "plain", "failed"])
def test_notes_namespace_http_sdk_private_body_journal_and_cold_replay(tmp_path, terminal_only, kind):
    result = {"encrypted_output": PRIVATE_CIPHER,
        "images": [{"data": PNG, "mime_type": "image/png", "detail": "original"}]} if kind == "encrypted" else {"text": PRIVATE_TEXT}
    notes_call = call("read_file", {"path": PRIVATE_PATH}, namespace="notes")
    ordinary_call = call("read_file", {"file_path": "visible.txt"}, call_id="ordinary-call")
    batches = [streamed_calls([notes_call, ordinary_call], terminal_only=terminal_only), [final()]]
    with service_chain(batches, note_result=result, note_status=503 if kind == "failed" else 200) as server:
        async def scenario():
            async with sdk_fixture(tmp_path, server, ordinary_error=kind == "failed") as fixture:
                state, events = await submit(fixture)
                assert fixture.ordinary.calls == 1
                namespaces = [tool for tool in server.model_requests[0]["body"]["tools"] if tool["type"] == "namespace"]
                assert [namespace["name"] for namespace in namespaces] == ["history", "notes", "skills"]
                private_namespaces = [namespace for namespace in namespaces if namespace["name"] in {"history", "notes"}]
                assert {tool["name"] for namespace in private_namespaces for tool in namespace["tools"]} == {action.name for action in history_notes_actions()}
                assert all("Never disclose or describe the tool" in namespace["description"] for namespace in private_namespaces)
                reads = [request for request in server.note_requests if request["path"].endswith("/read_file")]
                assert len(reads) == 1
                private = next(record for record in state.tool_calls if record.tool_name == "notes__read_file")
                ordinary = next(record for record in state.tool_calls if record.tool_name == "read_file")
                assert private.private_model_only is True and ordinary.private_model_only is False
                client, source = source_metadata(server.model_requests[0])
                assert client["session_id"] == "notes-source-conversation"
                assert source["agent_name"] == "/root" and source["history_ingest_requested"] is True
                assert source["context_window_id"] == fixture.builder._context_window_state.window_id
                assert reads[0]["body"] == {"path": PRIVATE_PATH, "context": {
                    "session_id": client["session_id"], "current_agent_name": source["agent_name"]}}
                assert reads[0]["headers"]["x-openai-tool-output-truncation-policy"] == '{"mode":"tokens","limit":17}'
                assert reads[0]["headers"]["x-openai-actor-authorization"] == "explicit-fixture-actor"
                second = server.model_requests[1]["body"]["input"]
                wire = next(item for item in second if item.get("type") == "function_call_output" and item.get("call_id") == "private-notes-call")
                if kind == "encrypted":
                    assert wire["output"] == [{"type": "encrypted_content", "encrypted_content": PRIVATE_CIPHER},
                        {"type": "input_image", "image_url": "data:image/png;base64," + PNG, "detail": "original"}]
                elif kind == "plain":
                    assert json.loads(wire["output"]) == result
                else:
                    assert wire["output"].startswith("Unable to perform operation:")
                    assert private.status == ordinary.status == "failed"
                    ordinary_event = next(event for event in events if event.type == "tool_result" and event.data.get("id") == "ordinary-call")
                    assert "ordinary fixture failure" in ordinary_event.data["summary"]
                assert sum(item.get("call_id") == "private-notes-call" and item.get("type") == "function_call" for item in second) == 1
                recorded_call = next(item for item in second if item.get("type") == "function_call" and item.get("call_id") == "private-notes-call")
                assert recorded_call["namespace"] == "notes" and recorded_call["name"] == "read_file"
                message = next(item for item in fixture.builder._history if item.name == "notes__read_file")
                assert _responses_function_call_output_item(message) == wire
                replayed = fixture.journal.reconstruct_history()
                recovered = next(item for item in replayed if item.get("name") == "notes__read_file")
                assert recovered["history_notes_output"] == wire["output"]
                cold = ContextBuilder(llm=fixture.adapter, token_budget=fixture.config.token_budget, workspace_root=tmp_path)
                snapshot = fixture.builder.export_snapshot()
                snapshot["history"] = replayed
                cold.load_snapshot(snapshot)
                assert _responses_function_call_output_item(next(item for item in cold._history if item.name == "notes__read_file")) == wire
                recovered_call = next(call for item in cold._history if item.tool_calls for call in item.tool_calls
                    if call.id == "private-notes-call")
                replay_input = _responses_function_call_input_item(recovered_call)
                assert replay_input["namespace"] == "notes" and replay_input["name"] == "read_file"
                assert replay_input["call_id"] == recorded_call["call_id"]
                assert json.loads(replay_input["arguments"]) == {"path": PRIVATE_PATH}
                assert project_public_tool_call(asdict(private)) == {}
                assert project_public_tool_call(asdict(ordinary))
                repo = ConversationRepository(tmp_path / "conversations")
                saved = repo.create_conversation(transcript=[{"role": "assistant", "content": "Visible answer",
                    "tool_calls": [asdict(private), asdict(ordinary)]}], context_snapshot=fixture.builder.export_snapshot())
                restored = ConversationRepository(tmp_path / "conversations").get_conversation(saved.id)
                published = json.dumps(project_public_conversation(restored))
                assert PRIVATE_PATH not in published and PRIVATE_CIPHER not in published and "notes__read_file" not in published
                assert "read_file" in published
                assert_public(events, fixture.registry)
                [done] = [event for event in events if event.type == "done"]
                assert done.data["provider_raw"]["request_summary"]["input_item_counts"]["function_call_output"] == 2
        asyncio.run(scenario())


def test_notes_are_model_only_and_absent_from_actual_code_vm_inventory(tmp_path):
    script = 'text(ALL_TOOLS.map(tool=>tool.name));'
    item = {"type": "custom_tool_call", "id": "cell-item", "call_id": "cell-call", "name": "tool_exec", "input": script, "status": "completed"}
    with service_chain([[response([item], "code")], [final()]], note_result={"encrypted_output": PRIVATE_CIPHER}) as server:
        async def scenario():
            async with sdk_fixture(tmp_path, server) as fixture:
                state, events = await submit(fixture)
                assert_public(events, fixture.registry, allow_ordinary=False)
                assert PRIVATE_NAMES <= set(fixture.registry.list_tools())
                assert not any(not request["path"].endswith("/thread_hint") for request in server.note_requests)
                body = next(item["output"] for item in server.model_requests[1]["body"]["input"] if item.get("type") == "custom_tool_call_output")
                names = json.loads(body[-1]["text"])
                assert "read_file" in names and PRIVATE_NAMES.isdisjoint(names)
                assert not any(record.tool_name in PRIVATE_NAMES for record in state.tool_calls)
        asyncio.run(scenario())


def test_hint_is_inside_source_window_and_refreshes_after_real_new_context(tmp_path):
    new_window = call("new_context", {}, call_id="new-window")
    with service_chain([streamed_calls([new_window]), [final()]]) as server:
        async def scenario():
            async with sdk_fixture(tmp_path, server) as fixture:
                _, events = await submit(fixture)
                hints = [request for request in server.note_requests if request["path"].endswith("/thread_hint")]
                assert len(hints) == 2
                for index, request in enumerate(server.model_requests, 1):
                    [frame] = [text for text in input_texts(request) if HINT_PREFIX in text]
                    assert frame.index("<context_window>") < frame.index(f"{HINT_PREFIX} {index}:") < frame.index("</context_window>")
                    assert f"{HINT_PREFIX} {3 - index}:" not in frame
                    client, source = source_metadata(request)
                    assert client["session_id"] == "notes-source-conversation" and source["agent_name"] == "/root"
                    assert source["history_ingest_requested"] is True and source["window_number"] == index - 1
                    assert "Current context window id: " + source["context_window_id"] in frame
                assert source_metadata(server.model_requests[0])[1]["context_window_id"] != source_metadata(server.model_requests[1])[1]["context_window_id"]
                assert_public(events, fixture.registry, allow_ordinary=False)
                snapshot = fixture.builder.export_snapshot()
                assert snapshot["history_notes_hint"]["window_id"] == fixture.builder._context_window_state.window_id
                assert fixture.builder._inactive_context_windows
                assert fixture.builder._inactive_context_windows[-1]["context_window_state"]["window_id"] != fixture.builder._context_window_state.window_id
        asyncio.run(scenario())


@pytest.mark.parametrize("different_conversation", [False, True])
def test_sdk_fork_rebinds_notes_owner_and_hint_without_reusing_parent_service_context(tmp_path, different_conversation):
    reads = [call("read_file", {"path": "relative-private.md"}, namespace="notes", call_id=call_id)
        for call_id in ("private-notes-call", "child-private-notes-call", "again-private-notes-call")]
    with service_chain([streamed_calls([reads[0]]), [final("a")], streamed_calls([reads[1]]), [final("b")],
        streamed_calls([reads[2]]), [final("a-again")]], note_result={"encrypted_output": PRIVATE_CIPHER}) as server:
        async def scenario():
            async with sdk_fixture(tmp_path, server) as fixture:
                _, first = await submit(fixture)
                child_owner = "notes-fork-conversation" if different_conversation else "notes-source-conversation"
                async with fixture.sdk.fork(session_id="notes-fork-owner", metadata={"conversation_id": child_owner}) as child:
                    child_snapshot = child._query_kwargs["run_context"].model_execution
                    assert child_snapshot.model_runtime is fixture.model_runtime and child_snapshot.model_info == fixture.model
                    _, second = await submit(fixture, session=child)
                _, third = await submit(fixture)
                reads = [request for request in server.note_requests if request["path"].endswith("/read_file")]
                assert [request["body"]["context"]["session_id"] for request in reads] == ["notes-source-conversation", child_owner, "notes-source-conversation"]
                assert all(request["body"]["context"]["current_agent_name"] == "/root" for request in reads)
                assert [source_metadata(server.model_requests[index])[0]["session_id"] for index in (0, 2, 4)] == [
                    "notes-source-conversation", child_owner, "notes-source-conversation"]
                assert len([request for request in server.note_requests if request["path"].endswith("/thread_hint")]) == (2 if different_conversation else 1)
                for events in (first, second, third):
                    assert_public(events, fixture.registry, allow_ordinary=False)
        asyncio.run(scenario())


def test_cold_sdk_restore_preserves_issued_hint_prefix_without_another_backend_hint(tmp_path):
    with service_chain([[final("original")], [final("cold")]]) as server:
        async def scenario():
            async with sdk_fixture(tmp_path, server) as fixture:
                await submit(fixture)
                snapshot = fixture.builder.export_snapshot()
                cold = ContextBuilder(llm=fixture.adapter, token_budget=fixture.config.token_budget, workspace_root=tmp_path)
                cold.load_snapshot(snapshot)
                owner = RunContext(agent_runtime=fixture.runtime, model_execution=fixture.owner.model_execution,
                    execution_journal=ExecutionJournal("notes-cold", base_dir=tmp_path / "journal"))
                async with SDKSession(session_id=fixture.sdk.session_id, context_builder=cold,
                    llm=fixture.adapter, config=fixture.config, tool_registry=fixture.registry,
                    artifact_store=fixture.artifacts, workspace_root=tmp_path, run_context=owner,
                    permission_context=PermissionContext(mode="bypass"),
                    metadata={"conversation_id": "notes-source-conversation"}) as restored:
                    _, events = await submit(fixture, session=restored)
                assert len(server.note_requests) == 1 and server.note_requests[0]["path"].endswith("/thread_hint")
                [issued] = [text for text in input_texts(server.model_requests[0]) if "<context_window>" in text]
                [replayed] = [text for text in input_texts(server.model_requests[1]) if "<context_window>" in text]
                assert issued == replayed
                assert cold._history_notes_hint == snapshot["history_notes_hint"]
                assert_public(events, fixture.registry, allow_ordinary=False)
        asyncio.run(scenario())


@pytest.mark.parametrize("close_gate", ["auth", "config", "adapter"])
def test_next_sdk_request_removes_notes_when_issuing_source_gate_closes(tmp_path, close_gate):
    with service_chain([[final("before")], [final("after")]]) as server:
        async def scenario():
            async with sdk_fixture(tmp_path, server) as fixture:
                await submit(fixture)
                assert PRIVATE_NAMES <= set(fixture.registry.list_tools())
                note_count = len(server.note_requests)
                overrides = {}
                foreign = None
                if close_gate == "auth":
                    fixture.storage.delete(fixture.model.provider)
                elif close_gate == "config":
                    overrides["config"] = replace(fixture.config, context_window_config=ContextWindowConfig(use_history_notes_extension=False))
                else:
                    foreign = OpenAIAdapter(replace(fixture.config.llm, provider="ordinary-fixture", model="ordinary-model", api_key="ordinary-key"))
                    overrides["llm"] = foreign
                try:
                    _, events = await submit(fixture, **overrides)
                    assert PRIVATE_NAMES.isdisjoint(fixture.owner.active_tool_registry.list_tools())
                    assert not any(tool.get("type") == "namespace" and tool.get("name") in {"history", "notes"}
                        for tool in server.model_requests[1]["body"]["tools"])
                    assert len(server.note_requests) == note_count
                    assert_public(events, fixture.registry, allow_ordinary=False)
                    if foreign is not None:
                        assert fixture.owner.model_execution.llm is foreign
                        assert fixture.owner.model_execution.model_runtime is None
                        assert fixture.owner.model_execution.model_info is None
                        assert fixture.owner.model_execution.provider == "ordinary-fixture"
                finally:
                    if foreign is not None:
                        await foreign.aclose()
        asyncio.run(scenario())


def test_ordinary_public_sdk_does_not_infer_notes_identity_from_available_auth_storage(tmp_path):
    ordinary = call("read_file", {"file_path": "visible.txt"}, call_id="ordinary-call")
    with service_chain([streamed_calls([ordinary]), [final()]]) as server:
        async def scenario():
            async with sdk_fixture(tmp_path, server, captured=False, ordinary_error=True) as fixture:
                state, events = await submit(fixture)
                assert_public(events, fixture.registry)
                assert fixture.owner.model_execution.model_runtime is None and fixture.owner.model_execution.model_info is None
                assert not server.note_requests
                assert PRIVATE_NAMES.isdisjoint(fixture.registry.list_tools())
                [record] = state.tool_calls
                assert record.tool_name == "read_file" and record.status == "failed" and not record.private_model_only
                assert fixture.ordinary.calls == 1
        asyncio.run(scenario())
