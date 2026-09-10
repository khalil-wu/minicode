from __future__ import annotations

import asyncio
from contextlib import aclosing
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import config_helpers, config_providers, sdk
from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.query_engine import QueryEngine
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import _flush_queue, execute_tool_batch
from backend.agent.tool_execution import _execution_arguments_for_tool
from backend.artifact.store import ArtifactPersistenceError, ArtifactStore
from backend.atomic_io import canonical_file_path_key
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.evals.minicode_driver import _eval_max_turn_seconds, _runtime_occupied_ms
from backend.extensions.loader import ExtensionLoader
from backend.extensions.runtime import _as_tool_result
from backend.extensions.types import ToolResultEvent
from backend.hooks.discovery import discover_hook_snapshot
from backend.hooks.http_executor import _SSRFGuardedNetworkBackend
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.llm.base import LLMAdapter, ToolCallEvent
from backend.owner_scope import OwnerScope, grant_owner_scope
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.plugins.identity import version_satisfies
from backend.runtime_env import sanitized_subprocess_env
from backend.services import llm_adapter_factory
from backend.tools.apply_patch_parser import apply_update_hunks, parse_patch
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.file_tools_common import content_hash
from backend.tools.registry import ToolRegistry
from backend.tools.write_file import WriteFileTool
from backend.workspace.recent_projects import RecentProjectPersistenceError, RecentProjectStore


class Provider(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        if False:
            yield

    async def simple_chat(self, messages):
        return ""


@pytest.mark.asyncio
@pytest.mark.parametrize("stateful", [False, True])
async def test_sdk_close_releases_nested_query_before_return(tmp_path, monkeypatch, stateful):
    closed = asyncio.Event()

    async def events():
        try:
            yield AgentEvent(type="probe", data={})
        finally:
            closed.set()

    monkeypatch.setattr(QueryEngine, "submit", lambda *_args: events())
    kwargs = dict(llm=Provider(), tool_registry=ToolRegistry(), config=AppConfig(llm=LLMSettings(api_key="fixture")), workspace_root=tmp_path)
    stream = sdk.SDKSession(**kwargs).resume_with_context("probe") if stateful else sdk.query("probe", **kwargs)
    await anext(stream)
    await stream.aclose()
    assert closed.is_set()


def test_sdk_future_annotations_and_permission_contract():
    @sdk.tool
    def add(value: int, enabled: bool = False):
        return value if enabled else 0

    properties = add.get_schema().parameters["properties"]
    assert properties["value"]["type"] == "integer"
    assert properties["enabled"]["type"] == "boolean"
    with pytest.raises(ValueError, match="Unsupported tool permission"):
        sdk.tool(lambda: None, permission="confim")


@pytest.mark.asyncio
async def test_failed_extension_factory_leaves_no_callbacks_flags_or_providers(tmp_path):
    observed = []

    def broken(api):
        api.events.on("probe", lambda *_args: observed.append(True))
        api.register_flag("ghost", {"type": "boolean", "default": True})
        api.register_provider("ghost", {"api": "openai-completions", "base_url": "https://example.invalid"})
        raise RuntimeError("factory failed")

    result = await ExtensionLoader(cwd=tmp_path).load_factory(broken)
    try:
        await result.runner.event_bus.emit("probe", {})
        assert not observed
        assert not result.runtime.pending_provider_registrations
        assert "ghost" not in result.runtime.flag_values
    finally:
        await result.runner.shutdown()


@pytest.mark.asyncio
async def test_extension_inplace_and_rich_results_survive_projection(tmp_path):
    def factory(api):
        def mutate(event):
            event.content = "changed"
            event.details["count"] = 2
        api.on("tool_result", mutate)

    loaded = await ExtensionLoader(cwd=tmp_path).load_factory(factory)
    try:
        result = await loaded.runner.emit_tool_result(ToolResultEvent(content="original", details={"count": 1}))
        assert result.content == "changed"
        assert result.details == {"count": 2}
    finally:
        await loaded.runner.shutdown()
    rich = _as_tool_result({"content": [{"type": "image", "data": "PIXELS", "mimeType": "image/png"}], "artifact_id": "image", "cleanup_receipt": {"pending": 1}})
    assert rich.images == [{"data": "PIXELS", "media_type": "image/png"}]
    assert "PIXELS" not in rich.content
    assert rich.artifact_id == "image"
    assert rich.cleanup_receipt["pending"] == 1


def test_factory_uses_explicit_session_config(monkeypatch):
    requested = LLMSettings(api_key="fixture", provider="custom", model="selected", wire_api="anthropic", thinking_budget=2048)
    monkeypatch.setattr(llm_adapter_factory, "build_wire_adapter", lambda settings, **_kwargs: settings)
    actual = llm_adapter_factory.create_session_llm(AppConfig(llm=requested))
    assert actual == requested


def test_provider_candidate_is_validated_before_publishing(monkeypatch):
    stored = {"llm": {"provider": "openai", "openai": {"model": "gpt-5.4", "reasoning_effort": "high", "base_url": "https://example.invalid/v1"}}}
    writes = []
    monkeypatch.setattr(config_providers, "_load_settings_json", lambda: deepcopy(stored))
    monkeypatch.setattr(config_providers, "_write_settings_json", lambda value: writes.append(deepcopy(value)))
    save = config_providers.save_llm_settings.__wrapped__
    with pytest.raises(config_helpers.SettingsError):
        save({"openai": {"headers": {"X-Test": "invalid\nheader"}}})
    assert not writes
    save({"openai": {"reasoning_effort": ""}})
    assert writes[-1]["llm"]["openai"]["reasoning_effort"] == ""
    history = {}
    config_providers._upsert_llm_history(history, "openai", {"model": "gpt-5.4", "base_url": "https://example.invalid/v1", "headers": {"X-Tenant": "fixture"}, "auth_header": True})
    assert history["llm"]["provider_history"][0]["headers"] == {"X-Tenant": "fixture"}
    assert history["llm"]["provider_history"][0]["auth_header"] is True
    assert config_helpers._history_profile_identity("custom", "https://example.invalid/Team", "chat") != config_helpers._history_profile_identity("custom", "https://example.invalid/team", "chat")


@pytest.mark.parametrize("constraint,version,expected", [("~1", "1.5.0", True), ("~1", "2.0.0", False), ("^0", "0.5.0", True), ("^0.0", "0.0.9", True), ("^0.0.1", "0.0.2", False)])
def test_partial_plugin_ranges(constraint, version, expected):
    assert version_satisfies(version, constraint) is expected


def test_hook_discovery_preserves_distinct_matchers():
    hook_map = {"pre_tool_use": [{"matcher": matcher, "hooks": [{"type": "command", "command": "same-command"}]} for matcher in ("read_file", "write_file")]}
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"), {"hooks": hook_map}),))
    result = discover_hook_snapshot(config_stack=stack, workspace_root=None, workspace_trusted=True, plugin_sources=())
    assert [entry.matcher for entry in result.entries] == ["read_file", "write_file"]


@pytest.mark.asyncio
async def test_http_hook_cancellation_does_not_connect_to_another_address(monkeypatch):
    calls = []

    async def addresses(*_args):
        return ("1.1.1.1", "8.8.8.8")

    async def connect(address, *_args, **_kwargs):
        calls.append(address)
        raise asyncio.CancelledError()

    monkeypatch.setattr("backend.hooks.http_executor._resolve_safe_addresses", addresses)
    backend = _SSRFGuardedNetworkBackend()
    backend._backend = SimpleNamespace(connect_tcp=connect)
    with pytest.raises(asyncio.CancelledError):
        await backend.connect_tcp("example.invalid", 443)
    assert calls == ["1.1.1.1"]


def test_snapshot_preserves_literal_prompts_and_different_images():
    rows = [{"role": "user", "content": "same text", "images": [{"data": image, "media_type": "image/png"}]} for image in ("one", "two")]
    rows.append({"role": "user", "content": "<system-reminder>literal user example</system-reminder>"})
    builder = ContextBuilder()
    builder.load_snapshot({"history": rows})
    restored = builder.export_snapshot()["history"]
    assert len(restored) == 3
    assert restored[0]["images"] != restored[1]["images"]
    assert restored[2]["content"] == rows[2]["content"]


def test_same_conversation_workspace_grants_and_explicit_projectless_root(tmp_path):
    original = (OwnerScope("conversation", canonical_file_path_key(tmp_path / "A")),)
    moved = grant_owner_scope(original, source_conversation_id="conversation", target_conversation_id="conversation", target_workspace_root=tmp_path / "B")
    assert OwnerScope("conversation", canonical_file_path_key(tmp_path / "B")) in moved
    projectless = grant_owner_scope(original, source_conversation_id="conversation", target_conversation_id="fork", target_workspace_root="")
    assert OwnerScope("fork", "") in projectless


def test_cleanup_can_retry_content_deletion(tmp_path, monkeypatch):
    store = ArtifactStore(storage_dir=tmp_path / "artifacts", ttl_seconds=1)
    artifact = store.save("content", source="fixture", conversation_id="owner", workspace_root=tmp_path)
    content = tmp_path / "artifacts" / f"{artifact}.txt"
    metadata = tmp_path / "artifacts" / f"{artifact}.meta.json"
    original = Path.unlink

    def fail_content(path, *args, **kwargs):
        if path == content:
            raise PermissionError("busy")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as local:
        local.setattr(Path, "unlink", fail_content)
        with pytest.raises(ArtifactPersistenceError):
            store.cleanup_expired(now=10**12)
    assert metadata.exists()
    assert store.cleanup_expired(now=10**12) == 1
    assert not content.exists()


def test_recent_project_read_failure_does_not_replace_store(tmp_path, monkeypatch):
    path = tmp_path / "recent.json"
    store = RecentProjectStore(path)
    store.add(str(tmp_path / "A"), "A")
    before = path.read_bytes()
    read = Path.read_text

    def fail(path_arg, *args, **kwargs):
        if path_arg == path:
            raise PermissionError("temporary read failure")
        return read(path_arg, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail)
    with pytest.raises(RecentProjectPersistenceError):
        store.add(str(tmp_path / "B"), "B")
    assert path.read_bytes() == before


def test_host_environment_does_not_inherit_scoped_model_credentials(monkeypatch):
    for name in ("OPENAI_API_KEY_ABC123", "MINICODE_CUSTOM_IMAGE_API_KEY_ABC123"):
        monkeypatch.setenv(name, "fixture")
    child = sanitized_subprocess_env()
    assert "OPENAI_API_KEY_ABC123" not in child
    assert "MINICODE_CUSTOM_IMAGE_API_KEY_ABC123" not in child


@pytest.mark.parametrize("patch,expected", [("@@\n+two", "one\ntwo\n"), ("@@\n-one\n+two\n*** End of File", "two\n")])
def test_codex_append_and_eof_patch_contract(patch, expected):
    parsed = parse_patch(f"*** Begin Patch\n*** Update File: file.txt\n{patch}\n*** End Patch")
    assert apply_update_hunks("one\n", parsed[0].hunks, "file.txt") == expected


@pytest.mark.asyncio
async def test_write_does_not_bless_external_changes_or_model_supplied_hash(tmp_path, monkeypatch):
    target = tmp_path / "file.txt"
    target.write_text("original", encoding="utf-8")
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path, metadata={"_read_file_hashes": {canonical_file_path_key(target): content_hash("original")}})
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    builder = ContextBuilder()
    state = AgentState(user_message="write", workspace_root=tmp_path)

    async def external_change(*_args, **_kwargs):
        target.write_text("external", encoding="utf-8")

    monkeypatch.setattr("backend.tools.write_file._emit_write_diff", external_change)
    call = ToolCallEvent(id="first", name="write_file", arguments={"file_path": target.name, "content": "owned"})
    async with aclosing(execute_tool_batch([call], ctx=builder, state=state, tool_registry=registry, permission_checker=PermissionChecker(PermissionSettings(), tmp_path), approval_handler=None, skill_manager=None, permission_context=context.permission, tool_ctx=context)) as events:
        async for _ in events:
            pass
    assert target.read_text(encoding="utf-8") == "external"
    forged = ToolCallEvent(id="second", name="write_file", arguments={"file_path": target.name, "content": "replacement", "expected_hash": content_hash("external")})
    args = _execution_arguments_for_tool(forged, tool_registry=registry, tool_ctx=context)
    assert args["expected_hash"] == content_hash("owned")
    assert (await WriteFileTool().execute(args, context)).is_error
    assert target.read_text(encoding="utf-8") == "external"


@pytest.mark.parametrize("raw", ["invalid", "nan", "inf", "-1"])
def test_invalid_eval_deadline_is_rejected(monkeypatch, raw):
    monkeypatch.setenv("MINICODE_EVAL_MAX_TURN_SECONDS", raw)
    with pytest.raises(ValueError):
        _eval_max_turn_seconds()


def test_eval_service_time_union_excludes_parallel_overlap():
    spans = [{"event": "complete", "started_at": 100, "ended_at": 300}, {"event": "complete", "started_at": 200, "ended_at": 400}]
    assert _runtime_occupied_ms(spans, {"complete"}) == 300
