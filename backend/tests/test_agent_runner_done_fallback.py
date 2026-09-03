import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.query_engine import QueryEngine
from backend.agent.runtime import default_runtime
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMAdapter, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry
from backend.ws.agent_runner import (
    SessionAgentRunnerMixin,
    _commit_automatic_compaction,
    _consume_previous_turn_aborted,
    _llm_adapter_cache_key,
    _llm_settings_identity,
    _reconcile_ui_agent_state_with_runtime,
    _reply_attachments_from_tool_calls,
    _attachment_path_key,
    _is_persistent_reasoning_event,
    _ui_agent_state_for_event,
)
from backend.ws.run_manager import SessionRunManager
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.session_lifecycle import SessionLifecycle


class _NoopLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        if False:
            yield None

    async def simple_chat(self, messages):
        return ""


def test_only_provider_reasoning_summary_is_persistent() -> None:
    raw = AgentEvent.thinking_chunk(
        "raw",
        source="provider",
        provider_reasoning_type="reasoning_content",
    )
    summary = AgentEvent.thinking_chunk(
        "summary",
        source="provider",
        provider_reasoning_type="reasoning_summary_text",
    )

    assert _is_persistent_reasoning_event(raw) is False
    assert _is_persistent_reasoning_event(summary) is True


def test_runner_streams_raw_reasoning_but_persists_only_summary(tmp_path, monkeypatch) -> None:
    events: list[dict] = []
    runner_events = [
        AgentEvent.thinking_chunk(
            "raw body",
            source="provider",
            visibility="timeline",
            provider_reasoning_type="reasoning_content",
        ),
        AgentEvent.thinking_chunk(
            "durable summary",
            source="provider",
            visibility="timeline",
            provider_reasoning_type="reasoning_summary_text",
        ),
        AgentEvent.agent_message_completed("Done.", source="model_final"),
    ]
    session = _Session(tmp_path, events, runner_events=runner_events)
    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(session._run_agent_locked(
        "Inspect reasoning lifecycle",
        conversation_id="conv_runnerdone",
        metadata={
            "agent_runtime": default_runtime(),
            "assistant_message_id": "assistant-reasoning",
            "user_message_id": "user-reasoning",
        },
    ))

    thinking_events = [event for event in events if event.get("type") == "thinking_delta"]
    assert [event["provider_reasoning_type"] for event in thinking_events] == [
        "reasoning_content",
        "reasoning_summary_text",
    ]
    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    thinking_blocks = [
        block
        for block in saved.transcript[-1]["blocks"]
        if block.get("type") == "thinking"
    ]
    assert thinking_blocks == [{
        "type": "thinking",
        "content": "durable summary",
        "source": "provider",
        "visibility": "timeline",
        "provider_reasoning_type": "reasoning_summary_text",
    }]


async def _admit_runner_turn(kwargs) -> None:
    context = kwargs["context_builder"]
    history_start = context.history_length
    context.append_user(kwargs["user_message"])
    await kwargs["metadata"]["commit_turn_admission"](
        boundary_input=SimpleNamespace(consumed_steer=None),
        history_start=history_start,
        history_end=context.history_length,
    )


async def _admit_query_submission(submission) -> None:
    context = submission.session.context_builder
    history_start = context.history_length
    context.append_user(submission.user_message)
    await submission.runtime.metadata["commit_turn_admission"](
        boundary_input=SimpleNamespace(consumed_steer=None),
        history_start=history_start,
        history_end=context.history_length,
    )


class _Session(SessionAgentRunnerMixin):
    def __init__(self, tmp_path: Path, events: list[dict], runner_events: list[AgentEvent] | None = None):
        self.session_id = "session_runner_done"
        self.conversation_repo = ConversationRepository(tmp_path / "conversations")
        conversation = self.conversation_repo.create_conversation(
            conversation_id="conv_runnerdone",
            title="Runner done fallback",
            workspace_root=str(tmp_path),
        )
        self.active_conversation_id = conversation.id
        self.active_conversation = conversation
        self.query_engine = QueryEngine(runner=self._runner)
        self.tool_registry = ToolRegistry()
        self.artifact_store = ArtifactStore(storage_dir=str(tmp_path / "artifacts"))
        self.permission_checker = PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path)
        self.config = AppConfig(llm=LLMSettings(api_key="test-key"))
        self.llm = _NoopLLM()
        # Keep the harness on the same collaborator contract as the real
        # WebSocket session; a one-field namespace hides binding drift.
        self.context_builder = ContextBuilder(
            token_budget=self.config.token_budget,
            agent_settings=self.config.agent,
            llm=self.llm,
        )
        self.provider = "openai"
        self.available_models = ["gpt-test"]
        self.selected_model = "gpt-test"
        self._model_override_active = False
        self.skill_manager = None
        self.vector_memory = None
        self.task_manager = None
        self.background_manager = None
        self.terminal_manager = None
        self.checkpoint_manager = None
        self.approval_handler = None
        self._conversation_streams = {}
        self.session_lifecycle = SessionLifecycle(self)
        # Keep this lightweight host on the same explicit ownership seams as
        # WebSocketSession.  Projection locks remain local because several
        # tests inspect them from worker threads, where a manager-owned
        # asyncio lock cannot be resolved.
        self.ws_manager = None
        self._conversation_projection_locks: dict[str, Any] = {}
        self._conversation_lifecycle_lock = asyncio.Lock()

        def conversation_lifecycle_lock() -> asyncio.Lock:
            return self._conversation_lifecycle_lock

        self.conversation_lifecycle_lock = conversation_lifecycle_lock
        self.command_dispatcher = SessionCommandDispatcher(
            self,
            root_dir=tmp_path / "client-command-log",
        )
        self.run_manager = SessionRunManager(self)
        self._interrupted = False
        self.last_agent_state = None
        self._events = events
        self._runner_events = runner_events

    async def _runner(self, **kwargs):
        await _admit_runner_turn(kwargs)
        if self._runner_events is None:
            yield AgentEvent.agent_message_completed("文件已经写好了。", source="model_final")
            return
        for event in self._runner_events:
            yield event

    async def send_event(self, event):
        self._events.append(event.to_ws_message())

    async def send_payload(self, payload, *, log_context=""):
        self._events.append(dict(payload))

    def refresh_tool_registry_if_mcp_changed(self):
        return False

    def _conversation_tool_registry(
        self,
        conversation_id: str = "",
        *,
        workspace_root=None,
        force_rebuild: bool = False,
    ):
        # Production builds one registry generation per conversation through the
        # app bootstrap. These tests own a fixed registry instead of a bootstrap.
        return self.tool_registry

    def _ensure_active_conversation(self):
        return None

    def _model_runtime_for_conversation(self, conversation_id):
        return None

    def _resolve_llm_provider(self, settings=None):
        return self.provider

    def _resolve_available_models(self, provider, settings=None):
        return list(self.available_models)

    def permission_context_for_conversation(self, conversation, source="agent.run"):
        return self.permission_checker.build_context(mode="bypass", source=source)

    def load_active_conversation_snapshot(self, conversation_id, snapshot, notify=False):
        return False

    def sync_permission_mode_with_active_conversation(self, source=""):
        return None


def test_consume_previous_turn_aborted_is_one_shot() -> None:
    session = SimpleNamespace(_interrupted=True)

    assert _consume_previous_turn_aborted(session) is True
    assert session._interrupted is False
    assert _consume_previous_turn_aborted(session) is False


def test_consume_previous_turn_aborted_is_scoped_to_conversation() -> None:
    session = SimpleNamespace(
        _interrupted=False,
        _interrupted_conversation_ids={"conv-a"},
    )

    assert _consume_previous_turn_aborted(session, "conv-b") is False
    assert _consume_previous_turn_aborted(session, "conv-a") is True
    assert _consume_previous_turn_aborted(session, "conv-a") is False


def test_reply_attachments_are_deduplicated_from_persisted_tool_outputs() -> None:
    attachments = _reply_attachments_from_tool_calls([
        {
            "outputFiles": [
                {"path": r"C:\Desktop\report.pdf", "size": 4096, "isImage": False},
                {"path": r"c:\desktop\REPORT.pdf", "size": 4096, "isImage": False},
            ],
        },
        {"output_files": [{"path": r"C:\Desktop\chart.png", "size": 512, "is_image": True}]},
    ])

    assert attachments == [
        {"path": r"C:\Desktop\report.pdf", "size": 4096, "is_image": False},
        {"path": r"C:\Desktop\chart.png", "size": 512, "is_image": True},
    ]


def test_attachment_path_key_normalizes_windows_paths_on_unix() -> None:
    assert _attachment_path_key(r"C:\Desktop\report.pdf") == _attachment_path_key(
        r"c:\desktop\REPORT.pdf"
    )


def test_runner_marks_external_context_and_disables_memory_generation(tmp_path, monkeypatch):
    events: list[dict] = []
    runner_events = [
        AgentEvent.tool_call(
            "search-1",
            "web_search",
            {"query": "current release"},
        ),
        AgentEvent.tool_result(
            "search-1",
            "External search results",
            status="success",
            result_kind="search",
        ),
        AgentEvent.agent_message_completed(
            "The current release is documented externally.",
            source="model_final",
        ),
    ]
    session = _Session(tmp_path, events, runner_events=runner_events)
    session._model_runtime_for_conversation = lambda _conversation_id: None
    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(
            api_key="test-key",
            provider="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-test",
        )),
    )
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr("backend.ws.agent_runner.get_available_models", lambda provider="openai": ["gpt-test"])
    monkeypatch.setattr(
        "backend.ws.agent_runner._get_or_create_session_llm",
        lambda session, **_kwargs: _NoopLLM(),
    )

    asyncio.run(session._run_agent_locked(
        "Find the current release",
        conversation_id="conv_runnerdone",
        metadata={
            "agent_runtime": default_runtime(),
            "assistant_message_id": "assistant-polluted",
            "user_message_id": "user-polluted",
        },
    ))

    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assert saved.memory_polluted is True
    assert saved.memory_pollution_sources == ["web_search"]
    assert saved.memory_mode == "polluted"
    assert saved.summary
    assert not any(event.get("type") == "conversation.list" for event in events)
    done_index = next(
        index for index, event in enumerate(events) if event.get("type") == "done"
    )
    summary_index = next(
        index
        for index, event in enumerate(events)
        if event.get("type") == "conversation.summary.updated"
    )
    assert done_index < summary_index
    assert events[summary_index]["memory_polluted"] is True
    assert events[summary_index]["memory_pollution_sources"] == ["web_search"]


def test_runner_persists_all_yielded_provider_progress_for_restore(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[dict] = []
    runner_events = [
        AgentEvent.progress(
            "Searching the web",
            id="provider:web-restore",
            stage="tool",
            phase="tool",
            status="running",
            label="Web search",
            visibility="timeline",
            ephemeral=True,
        ),
        AgentEvent.progress(
            "Web search completed",
            id="provider:web-restore",
            stage="tool",
            phase="tool",
            status="completed",
            label="Web search",
            visibility="timeline",
        ),
        AgentEvent.progress(
            "Provider code prepared",
            id="provider:code-restore",
            stage="tool",
            phase="tool",
            status="running",
            label="Code execution",
            detail="Code: 34 characters",
            visibility="timeline",
            ephemeral=True,
        ),
        AgentEvent.progress(
            "Provider code execution completed",
            id="provider:code-restore",
            stage="tool",
            phase="tool",
            status="completed",
            label="Code execution",
            visibility="timeline",
        ),
        AgentEvent.progress(
            "MCP tool call prepared: lookup",
            id="provider:mcp-restore",
            stage="tool",
            phase="tool",
            status="running",
            label="MCP tool",
            detail=(
                "Server: audit-local · Tool: lookup · "
                "Arguments: 38 characters"
            ),
            visibility="timeline",
            ephemeral=True,
        ),
        AgentEvent.progress(
            "MCP tool completed: lookup",
            id="provider:mcp-restore",
            stage="tool",
            phase="tool",
            status="completed",
            label="MCP tool",
            detail="Server: audit-local · Tool: lookup",
            visibility="timeline",
        ),
        AgentEvent.agent_message_completed(
            "Provider projection complete.",
            source="model_final",
        ),
    ]
    session = _Session(tmp_path, events, runner_events=runner_events)
    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_llm_provider",
        lambda: "openai",
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "Audit provider projection",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant-provider-restore",
                "user_message_id": "user-provider-restore",
            },
        )
    )

    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    progress = saved.context_snapshot["ui_agent_state"]["agentProgress"]
    assert [item["id"] for item in progress] == [
        "provider:web-restore",
        "provider:code-restore",
        "provider:mcp-restore",
    ]
    assert [item["status"] for item in progress] == [
        "completed",
        "completed",
        "completed",
    ]
    assert progress[1]["detail"] == "Code: 34 characters"
    assert progress[2]["detail"] == (
        "Server: audit-local · Tool: lookup · Arguments: 38 characters"
    )
    assert saved.context_snapshot["_ui_agent_state_revision"] > 0


def test_ui_agent_snapshot_preserves_partial_subagent_result() -> None:
    state = _ui_agent_state_for_event(None, "subagent.done", {
        "subagent_id": "sa-partial",
        "status": "partial",
        "summary": "Found two causes",
        "termination_reason": "deadline_exceeded",
        "initiator": "runtime",
        "result": {
            "content": "Found two causes",
            "error": "deadline reached",
        },
        "record": {
            "agent_type": "explore",
            "parent_run_id": "run-parent",
            "objective": "Inspect rendering",
            "checkpoint_id": "checkpoint-1",
        },
    })

    assert state is not None
    assert state["subagents"] == [
        {
            "id": "sa-partial",
            "role": "explore",
            "status": "partial",
            "summary": "Found two causes",
            "resultAvailable": True,
            "resultContent": "Found two causes",
            "resultError": "deadline reached",
            "terminationReason": "deadline_exceeded",
            "terminationInitiator": "runtime",
            "checkpointId": "checkpoint-1",
            "objective": "Inspect rendering",
            "parentRunId": "run-parent",
        }
    ]


def test_ui_agent_snapshot_treats_legacy_empty_error_as_success() -> None:
    state = _ui_agent_state_for_event(None, "subagent.done", {
        "subagent_id": "sa-completed",
        "status": "completed",
        "summary": "三个子任务都已完成",
        # Older persisted events used an empty object as "no error".
        "error": {},
        "result": {"status": "completed", "content": "完成结果"},
    })

    assert state is not None
    assert state["subagents"][0]["status"] == "done"
    assert state["subagents"][0]["resultContent"] == "完成结果"
    assert "resultError" not in state["subagents"][0]


def test_ui_agent_snapshot_preserves_typed_progress() -> None:
    state = _ui_agent_state_for_event(None, "agent.progress", {
        "id": "tool:call-1",
        "stage": "tool",
        "status": "running",
        "message": "Running read_file",
    })

    assert state is not None
    assert state["agentProgress"][0]["message"] == "Running read_file"


def test_ui_agent_snapshot_removes_debug_provider_completion() -> None:
    state = _ui_agent_state_for_event(None, "agent.progress", {
        "id": "provider:request-1",
        "stage": "status",
        "status": "running",
        "message": "模型正在响应",
        "provider_state": "responding",
    })
    assert state is not None

    next_state = _ui_agent_state_for_event(state, "agent.progress", {
        "id": "provider:request-1",
        "stage": "status",
        "status": "completed",
        "message": "提供商响应完成",
        "provider_state": "completed",
        "visibility": "debug",
    })

    assert next_state is not None
    assert next_state["agentProgress"] == []


def test_ui_agent_snapshot_preserves_image_and_cache_progress_contract() -> None:
    image_state = _ui_agent_state_for_event(None, "agent.progress", {
        "id": "provider:image-generation-1",
        "stage": "image_generation",
        "status": "running",
        "message": "正在生成图像",
        "phase": "image_generation",
    })
    cache_state = _ui_agent_state_for_event(None, "agent.progress", {
        "id": "cache:lookup-1",
        "stage": "cache",
        "status": "partial",
        "message": "缓存只命中了一部分",
        "phase": "cache",
    })

    assert image_state is not None
    assert image_state["agentProgress"][0]["stage"] == "image_generation"
    assert cache_state is not None
    assert cache_state["agentProgress"][0]["stage"] == "cache"
    assert cache_state["agentProgress"][0]["status"] == "partial"


def test_ui_agent_snapshot_keeps_provider_detail_and_terminal_status() -> None:
    state = _ui_agent_state_for_event(None, "agent.progress", {
        "id": "provider:mcp-sticky",
        "stage": "tool",
        "status": "running",
        "message": "MCP tool call prepared: lookup",
        "detail": (
            "Server: audit-local · Tool: lookup · Arguments: 38 characters"
        ),
    })
    assert state is not None

    state = _ui_agent_state_for_event(state, "agent.progress", {
        "id": "provider:mcp-sticky",
        "stage": "tool",
        "status": "completed",
        "message": "MCP tool completed: lookup",
        "detail": "Server: audit-local · Tool: lookup",
    })
    assert state is not None
    state = _ui_agent_state_for_event(state, "agent.progress", {
        "id": "provider:mcp-sticky",
        "stage": "tool",
        "status": "running",
        "message": "MCP tool in progress: lookup",
    })
    assert state is not None

    progress = state["agentProgress"][0]
    assert progress["status"] == "completed"
    assert progress["message"] == "MCP tool completed: lookup"
    assert progress["detail"] == (
        "Server: audit-local · Tool: lookup · Arguments: 38 characters"
    )


def test_ui_agent_snapshot_never_persists_running_tool_as_summary() -> None:
    state = _ui_agent_state_for_event(None, "subagent.progress", {
        "subagent_id": "sa-running",
        "tool_name": "read_file",
        "detail": "Running read_file",
        "current_activity": "Running read_file",
        "summary": "查询北京天气",
    })

    assert state is not None
    assert state["subagents"][0]["summary"] == "查询北京天气"


def test_ui_agent_snapshot_does_not_regress_terminal_subagent_to_running() -> None:
    state = _ui_agent_state_for_event(None, "subagent.done", {
        "subagent_id": "sa-sticky",
        "status": "completed",
        "summary": "完成天气查询",
    })
    assert state is not None

    next_state = _ui_agent_state_for_event(state, "subagent.progress", {
        "subagent_id": "sa-sticky",
        "summary": "仍在运行",
        "tool_name": "web_search",
    })

    assert next_state is not None
    assert next_state["subagents"][0]["status"] == "done"
    assert next_state["subagents"][0]["summary"] == "完成天气查询"


def test_ui_agent_snapshot_reconciles_stale_running_child_from_durable_runtime() -> None:
    class _Runtime:
        @staticmethod
        def get_subagent_snapshot(subagent_id, *, include_result=True):
            assert subagent_id == "sa-stale"
            assert include_result is True
            return {
                "subagent_id": subagent_id,
                "parent_run_id": "run-parent",
                "agent_type": "general-purpose",
                "status": "completed",
                "summary": "广州天气完成",
                "duration_ms": 14556,
                "iterations": 3,
                "tool_call_count": 2,
                "termination_reason": "success",
                "result": {
                    "status": "completed",
                    "content": "广州晴，31°C。",
                },
            }

        @staticmethod
        def get_run(run_id):
            assert run_id == "run-parent"
            return SimpleNamespace(conversation_id="conv-weather")

    state, changed = _reconcile_ui_agent_state_with_runtime(
        {
            "plan": None,
            "todos": [],
            "subagents": [{
                "id": "sa-stale",
                "role": "general-purpose",
                "status": "running",
                "summary": "调研广州天气",
                "parentRunId": "run-parent",
            }],
            "agentProgress": [],
        },
        runtime=_Runtime(),
        conversation_id="conv-weather",
    )

    assert changed is True
    assert state["subagents"] == [{
        "id": "sa-stale",
        "role": "general-purpose",
        "status": "done",
        "summary": "广州天气完成",
        "parentRunId": "run-parent",
        "resultAvailable": True,
        "resultContent": "广州晴，31°C。",
        "durationMs": 14556,
        "iteration": 3,
        "toolCallCount": 2,
        "terminationReason": "success",
        "terminationInitiator": "runtime",
    }]


def test_persisted_ui_agent_snapshot_is_repaired_before_restore(tmp_path, monkeypatch) -> None:
    session = _Session(tmp_path, [])
    session.conversation_repo.save_context_snapshot(
        "conv_runnerdone",
        {
            "history": [],
            "ui_agent_state": {
                "plan": None,
                "todos": [],
                "subagents": [{
                    "id": "sa-restored",
                    "role": "general-purpose",
                    "status": "running",
                    "summary": "调研广州天气",
                    "parentRunId": "run-parent",
                }],
                "agentProgress": [],
            },
            "_ui_agent_state_revision": 7,
        },
    )

    class _Runtime:
        @staticmethod
        def get_subagent_snapshot(subagent_id, *, include_result=True):
            return {
                "subagent_id": subagent_id,
                "parent_run_id": "run-parent",
                "agent_type": "general-purpose",
                "status": "completed",
                "summary": "广州天气完成",
                "result": {"status": "completed", "content": "广州晴，31°C。"},
            }

        @staticmethod
        def get_run(run_id):
            return SimpleNamespace(conversation_id="conv_runnerdone")

    monkeypatch.setattr("backend.ws.agent_runner.default_runtime", lambda: _Runtime())

    refreshed = asyncio.run(
        session.reconcile_persisted_ui_agent_state("conv_runnerdone")
    )

    assert refreshed is not None
    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    row = saved.context_snapshot["ui_agent_state"]["subagents"][0]
    assert row["status"] == "done"
    assert row["resultContent"] == "广州晴，31°C。"
    assert saved.context_snapshot["_ui_agent_state_revision"] > 7


def test_persisted_ui_agent_reconcile_reuses_loaded_conversation(tmp_path, monkeypatch) -> None:
    session = _Session(tmp_path, [])
    loaded = session.conversation_repo.get_conversation("conv_runnerdone")
    assert loaded is not None

    def unexpected_reload(_conversation_id: str):
        raise AssertionError("already-loaded conversation must not be read again")

    monkeypatch.setattr(session.conversation_repo, "get_conversation", unexpected_reload)

    refreshed = asyncio.run(
        session.reconcile_persisted_ui_agent_state(
            "conv_runnerdone",
            conversation=loaded,
        )
    )

    assert refreshed is loaded


def test_persisted_ui_agent_reconcile_keeps_projection_ownership_through_patch(
    tmp_path,
    monkeypatch,
) -> None:
    session = _Session(tmp_path, [])
    session.conversation_repo.save_context_snapshot(
        "conv_runnerdone",
        {
            "ui_agent_state": {
                "plan": None,
                "todos": [],
                "subagents": [{
                    "id": "sa-owned",
                    "role": "general-purpose",
                    "status": "running",
                    "summary": "owned reconcile",
                    "parentRunId": "run-parent",
                }],
                "agentProgress": [],
            },
            "_ui_agent_state_revision": 1,
        },
    )

    class _Runtime:
        @staticmethod
        def get_subagent_snapshot(subagent_id, *, include_result=True):
            return {
                "subagent_id": subagent_id,
                "parent_run_id": "run-parent",
                "agent_type": "general-purpose",
                "status": "completed",
                "summary": "owned reconcile complete",
                "result": {"status": "completed", "content": "done"},
            }

        @staticmethod
        def get_run(run_id):
            return SimpleNamespace(conversation_id="conv_runnerdone")

    monkeypatch.setattr("backend.ws.agent_runner.default_runtime", lambda: _Runtime())
    original_patch = session.conversation_repo.patch_context_snapshot
    ownership_observed: list[bool] = []

    def owned_patch(*args, **kwargs):
        ownership_observed.append(
            session._conversation_projection_lock("conv_runnerdone").locked()
        )
        return original_patch(*args, **kwargs)

    monkeypatch.setattr(
        session.conversation_repo,
        "patch_context_snapshot",
        owned_patch,
    )

    asyncio.run(session.reconcile_persisted_ui_agent_state("conv_runnerdone"))

    assert ownership_observed == [True]


def test_automatic_compaction_uses_conversation_projection_ownership() -> None:
    ownership_observed: list[bool] = []

    async def scenario() -> None:
        lock = asyncio.Lock()
        builder = SimpleNamespace(export_snapshot=lambda: {"history": [], "compaction_count": 1})

        class _Repository:
            @staticmethod
            def get_conversation(conversation_id: str):
                return SimpleNamespace(revision=3, context_snapshot={})

            @staticmethod
            def commit_compaction(conversation_id: str, **kwargs):
                ownership_observed.append(lock.locked())
                return SimpleNamespace(id=conversation_id)

        await _commit_automatic_compaction(
            _Repository(),
            conversation_id="conversation-auto-compact-owned",
            context_builder=builder,
            summary="owned summary",
            projection_lock=lock,
        )

    asyncio.run(scenario())

    assert ownership_observed == [True]


def test_runner_sends_done_when_filtered_stream_omits_done(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(tmp_path, events)

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_llm_provider",
        lambda: "openai",
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "写一个 html",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant_local",
                "user_message_id": "user_local",
            },
        )
    )

    done_events = [event for event in events if event.get("type") == "done"]
    assert len(done_events) == 1
    assert done_events[0]["conversation_id"] == "conv_runnerdone"
    assert done_events[0]["message_id"] == "assistant_local"
    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assert saved.transcript[0]["id"] == "user_local"
    assert saved.transcript[-1]["role"] == "assistant"
    assert saved.transcript[-1]["content"] == "文件已经写好了。"


def test_new_turn_replays_terminal_projection_before_resetting_context(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[dict] = []
    session = _Session(tmp_path, events)
    mutation_order: list[str] = []

    async def replay_before_reset(repository, journal, *, conversation_id):
        assert repository is session.conversation_repo
        assert conversation_id == "conv_runnerdone"
        assert session._conversation_projection_lock(conversation_id).locked()
        mutation_order.append("replay")

    original_save = session.conversation_repo.save_context_snapshot

    def tracked_save(conversation_id, snapshot):
        mutation_order.append("save")
        return original_save(conversation_id, snapshot)

    monkeypatch.setattr(
        "backend.ws.agent_runner._replay_pending_conversation_projections",
        replay_before_reset,
    )
    monkeypatch.setattr(
        session.conversation_repo,
        "save_context_snapshot",
        tracked_save,
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "start after recovery",
            conversation_id="conv_runnerdone",
            metadata={
                "assistant_message_id": "assistant-after-recovery",
                "user_message_id": "user-after-recovery",
            },
        )
    )

    assert mutation_order[:2] == ["replay", "save"]


def test_runner_persists_partial_work_and_replaces_it_when_user_cancels(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(tmp_path, events)
    tool_started = asyncio.Event()

    async def blocking_runner(**kwargs):
        await _admit_runner_turn(kwargs)
        context_builder = kwargs["context_builder"]
        context_builder.append_assistant_tool_calls([
            ToolCallEvent(
                id="tool-running",
                name="read_file",
                arguments={"file_path": "README.md"},
            ),
        ])
        yield AgentEvent.tool_call(
            "tool-running",
            "read_file",
            {"file_path": "README.md"},
        )
        tool_started.set()
        await asyncio.Event().wait()

    session.query_engine = QueryEngine(runner=blocking_runner)
    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")))
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr("backend.ws.agent_runner.get_available_models", lambda provider="openai": ["gpt-test"])
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", lambda config, model_override=None, **_kwargs: _NoopLLM())

    async def scenario() -> None:
        task = asyncio.create_task(session._run_agent_locked(
            "inspect",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant-cancelled",
                "user_message_id": "user-cancelled",
            },
        ))
        await asyncio.wait_for(tool_started.wait(), timeout=10)

        partial = ConversationRepository(session.conversation_repo._base_dir).get_conversation("conv_runnerdone")
        assert partial is not None
        assert partial.transcript[-1]["id"] == "assistant-cancelled"
        assert partial.transcript[-1]["terminal_status"] == "partial"
        assert partial.transcript[-1]["tool_calls"][0]["status"] == "partial"
        assert [message["role"] for message in partial.context_snapshot["history"]][-2:] == [
            "user",
            "assistant",
        ]
        assert partial.context_snapshot["history"][-1]["tool_calls"][0]["id"] == "tool-running"

        task.cancel()
        await task


    asyncio.run(scenario())

    restored = ConversationRepository(session.conversation_repo._base_dir).get_conversation("conv_runnerdone")
    assert restored is not None
    assistant_messages = [
        message for message in restored.transcript
        if message.get("id") == "assistant-cancelled"
    ]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["terminal_status"] == "cancelled"
    assert assistant_messages[0]["tool_calls"][0]["status"] == "cancelled"
    assert next(event for event in events if event.get("type") == "done")["status"] == "cancelled"


def test_projection_flush_does_not_wait_on_debounce_task_holding_the_same_lock(tmp_path):
    session = _Session(tmp_path, [])

    async def scenario() -> None:
        session._persist_ui_agent_state_event(
            "conv_runnerdone",
            "task.update",
            {
                "message_id": "assistant-old",
                "id": "todo-1",
                "content": "finish projection",
                "status": "in_progress",
            },
        )
        lock = session._conversation_projection_lock("conv_runnerdone")
        async with lock:
            await asyncio.wait_for(
                session._flush_ui_agent_state_now_unlocked("conv_runnerdone"),
                timeout=1,
            )

    asyncio.run(scenario())
    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assert saved.context_snapshot["ui_agent_state"]["todos"][0]["id"] == "todo-1"


def test_projection_flush_keeps_pending_state_when_persistence_fails(tmp_path, monkeypatch):
    session = _Session(tmp_path, [])
    original_patch = session.conversation_repo.patch_context_snapshot
    attempts = 0

    def flaky_patch(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("projection disk unavailable")
        return original_patch(*args, **kwargs)

    monkeypatch.setattr(
        session.conversation_repo,
        "patch_context_snapshot",
        flaky_patch,
    )

    async def scenario() -> None:
        session._persist_ui_agent_state_event(
            "conv_runnerdone",
            "task.update",
            {
                "id": "todo-retry",
                "content": "retry projection",
                "status": "in_progress",
            },
        )
        await session._flush_ui_agent_state_now("conv_runnerdone")
        assert "conv_runnerdone" in session.ui_agent_state_store.pending
        await session._flush_ui_agent_state_now("conv_runnerdone")

    asyncio.run(scenario())
    assert "conv_runnerdone" not in session.ui_agent_state_store.pending
    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assert saved.context_snapshot["ui_agent_state"]["todos"][0]["id"] == "todo-retry"


def test_reconcile_retries_retained_pending_state_before_runtime_merge(tmp_path, monkeypatch):
    session = _Session(tmp_path, [])
    original_patch = session.conversation_repo.patch_context_snapshot
    attempts = 0

    def flaky_patch(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("projection disk unavailable")
        return original_patch(*args, **kwargs)

    monkeypatch.setattr(session.conversation_repo, "patch_context_snapshot", flaky_patch)

    async def scenario() -> object:
        session._persist_ui_agent_state_event(
            "conv_runnerdone",
            "task.update",
            {
                "id": "todo-reconcile",
                "content": "keep this todo",
                "status": "in_progress",
            },
        )
        return await session.reconcile_persisted_ui_agent_state("conv_runnerdone")

    refreshed = asyncio.run(scenario())

    assert refreshed is not None
    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assert saved.context_snapshot["ui_agent_state"]["todos"][0]["id"] == "todo-reconcile"
    assert "conv_runnerdone" not in session.ui_agent_state_store.pending


def test_late_terminal_turn_events_are_fenced_but_next_turn_is_accepted(tmp_path):
    session = _Session(tmp_path, [])

    async def scenario() -> None:
        session._terminal_projection_fences = {
            "conv_runnerdone": "assistant-terminal",
        }
        session._persist_ui_agent_state_event(
            "conv_runnerdone",
            "task.update",
            {
                "message_id": "assistant-terminal",
                "id": "old-todo",
                "content": "late event",
                "status": "in_progress",
            },
        )
        assert "conv_runnerdone" not in getattr(session, "_ui_agent_state_pending", {})

        session._persist_ui_agent_state_event(
            "conv_runnerdone",
            "task.update",
            {
                "message_id": "assistant-next",
                "id": "new-todo",
                "content": "next turn",
                "status": "in_progress",
            },
        )
        await session._flush_ui_agent_state_now("conv_runnerdone")

    asyncio.run(scenario())
    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assert saved.context_snapshot["ui_agent_state"]["todos"][0]["id"] == "new-todo"



def test_runner_sends_done_when_terminal_projections_fail(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(tmp_path, events)
    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")))
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr("backend.ws.agent_runner.get_available_models", lambda provider="openai": ["gpt-test"])
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", lambda config, model_override=None, **_kwargs: _NoopLLM())
    original_append_transcript = session.conversation_repo.append_transcript_message

    def fail_terminal_transcript(conversation_id, message):
        if message.get("role") == "assistant":
            raise OSError("disk full")
        return original_append_transcript(conversation_id, message)

    monkeypatch.setattr(session.conversation_repo, "append_transcript_message", fail_terminal_transcript)
    original_save_snapshot = session.conversation_repo.save_context_snapshot
    snapshot_calls = 0

    def fail_terminal_snapshot(*args, **kwargs):
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls > 1:
            raise OSError("disk full")
        return original_save_snapshot(*args, **kwargs)

    monkeypatch.setattr(session.conversation_repo, "save_context_snapshot", fail_terminal_snapshot)

    asyncio.run(session._run_agent_locked(
        "write",
        conversation_id="conv_runnerdone",
        metadata={
            "agent_runtime": default_runtime(),
            "assistant_message_id": "assistant_projection_failure",
            "user_message_id": "user_projection_failure",
        },
    ))

    types = [event.get("type") for event in events]
    assert types.count("done") == 1
    done_index = next(
        index for index, event in enumerate(events)
        if event.get("type") == "done"
    )
    idle_index = max(
        index for index, event in enumerate(events)
        if event.get("type") == "session.state_changed"
        and event.get("state") == "idle"
    )
    assert done_index < idle_index


def test_runner_emits_no_synthetic_tool_failure_answer_before_failed_done(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(
        tmp_path,
        events,
        runner_events=[
            AgentEvent.tool_call("tool-failed", "run_command", {"command": "exit 1"}),
            AgentEvent.tool_result("tool-failed", "command failed", is_error=True, status="failed"),
            AgentEvent.done(),
        ],
    )

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "运行失败命令",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant_failed",
                "user_message_id": "user_failed",
            },
        )
    )

    done_indexes = [index for index, event in enumerate(events) if event.get("type") == "done"]
    assert len(done_indexes) == 1
    assert not any(
        event.get("type") == "item.completed"
        and event.get("item", {}).get("source") == "fallback"
        for event in events
    )
    assert events[done_indexes[0]]["status"] == "failed"


def test_runner_initialization_failure_emits_idle_and_failed_done(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(tmp_path, events)

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )

    def fail_create_llm(config, model_override=None):
        raise RuntimeError("adapter unavailable")

    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", fail_create_llm)

    asyncio.run(
        session._run_agent_locked(
            "hello",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant_init_failed",
                "user_message_id": "user_init_failed",
            },
        )
    )

    assert [event.get("type") for event in events] == [
        "error",
        "agent.run.completed",
        "done",
        "session.state_changed",
    ]
    assert events[1]["status"] == "failed"
    assert events[1]["terminal_reason"] == "llm_initialization_failed"
    assert events[2]["status"] == "failed"
    assert events[2]["reason"] == "llm_initialization_failed"
    assert events[2]["failure_recoverable"] is True


@pytest.mark.parametrize("recoverable", [True, False])
def test_runner_persists_and_emits_terminal_failure_recoverability(tmp_path, monkeypatch, recoverable):
    events: list[dict] = []
    session = _Session(
        tmp_path,
        events,
        runner_events=[
            AgentEvent.agent_message_completed("Partial response", source="model_final"),
            AgentEvent(
                type="error",
                data={
                    "message": "Model action can be retried",
                    "recoverable": recoverable,
                    "error_type": "invalid_model_action",
                    "error_code": "textual_tool_call_imitation",
                },
            ),
            AgentEvent.done(status="failed", reason="invalid_model_action"),
        ],
    )

    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")))
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr("backend.ws.agent_runner.get_available_models", lambda provider="openai": ["gpt-test"])
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", lambda config, model_override=None, **_kwargs: _NoopLLM())

    asyncio.run(
        session._run_agent_locked(
            "imitate a tool",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant-recoverable",
                "user_message_id": "user-recoverable",
            },
        )
    )

    done = next(event for event in events if event.get("type") == "done")
    assert done["status"] == "failed"
    assert done["failure_recoverable"] is recoverable
    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assistant = saved.transcript[-1]
    assert assistant["terminal_status"] == "failed"
    assert assistant["failure_message"] == "Model action can be retried"
    assert assistant["failure_recoverable"] is recoverable


def test_runner_clears_transient_failure_metadata_after_success(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(
        tmp_path,
        events,
        runner_events=[
            AgentEvent(
                type="error",
                data={
                    "message": "Temporary provider disconnect",
                    "recoverable": True,
                    "error_type": "api",
                    "provider_error_type": "network",
                },
            ),
            AgentEvent.agent_message_completed("Recovered response", source="model_final"),
            AgentEvent.done(status="completed"),
        ],
    )

    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")))
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr("backend.ws.agent_runner.get_available_models", lambda provider="openai": ["gpt-test"])
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", lambda config, model_override=None, **_kwargs: _NoopLLM())

    asyncio.run(
        session._run_agent_locked(
            "recover",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant-recovered",
                "user_message_id": "user-recovered",
            },
        )
    )

    done = next(event for event in events if event.get("type") == "done")
    assert done["status"] == "completed"
    assert "failure_recoverable" not in done
    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assistant = saved.transcript[-1]
    assert assistant["terminal_status"] == "completed"
    assert assistant["content"] == "Recovered response"
    assert "failure_message" not in assistant
    assert "failure_recoverable" not in assistant


def test_runner_preserves_partial_done_status_when_deferring_terminal_event(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(
        tmp_path,
        events,
        runner_events=[AgentEvent.done(status="partial", reason="max_iterations")],
    )

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "continue",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant_partial",
                "user_message_id": "user_partial",
            },
        )
    )

    done = [event for event in events if event.get("type") == "done"]
    assert len(done) == 1
    assert done[0]["status"] == "partial"
    assert done[0]["reason"] == "max_iterations"


def test_runner_projects_durable_terminal_over_conflicting_provider_done(
    tmp_path,
    monkeypatch,
):
    events: list[dict] = []
    session = _Session(
        tmp_path,
        events,
        runner_events=None,
    )

    class _ConflictingQueryEngine:
        async def submit(self, submission):
            await _admit_query_submission(submission)
            yield AgentEvent.agent_run_completed(
                SimpleNamespace(
                    public_dict=lambda: {
                        "run_id": "durable-conflict",
                        "conversation_id": "conv_runnerdone",
                        "status": "failed",
                        "summary": "Run ended: provider_rejected",
                        "terminal_reason": "provider_rejected",
                    }
                )
            )
            yield AgentEvent.agent_message_completed(
                "A durable failure was recorded.", source="model_final"
            )
            yield AgentEvent.done(status="completed", reason="completed")

    session.query_engine = _ConflictingQueryEngine()

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "conflicting terminal",
            conversation_id="conv_runnerdone",
            metadata={
                "assistant_message_id": "assistant-conflict",
                "user_message_id": "user-conflict",
            },
        )
    )

    done = next(event for event in events if event.get("type") == "done")
    assert done["status"] == "failed"
    assert done["reason"] == "provider_rejected"
    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assert saved.transcript[-1]["terminal_status"] == "failed"


def test_runner_downgrades_tool_only_success_to_partial_and_persists_status(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(
        tmp_path,
        events,
        runner_events=[
            AgentEvent.tool_call("tool-success", "read_file", {"file_path": "README.md"}),
            AgentEvent.tool_result(
                "tool-success",
                "README content was read successfully.",
                status="success",
                result_kind="file",
            ),
            AgentEvent.done(status="completed"),
        ],
    )

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "read the project overview",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant_tool_only",
                "user_message_id": "user_tool_only",
            },
        )
    )

    done = [event for event in events if event.get("type") == "done"]
    assert len(done) == 1
    assert done[0]["status"] == "partial"
    run_completed = [event for event in events if event.get("type") == "agent.run.completed"]
    assert len(run_completed) == 1
    assert run_completed[0]["status"] == "partial"
    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assistant = saved.transcript[-1]
    assert assistant["role"] == "assistant"
    assert assistant["terminal_status"] == "partial"
    assert "read_file" in str(assistant.get("tool_calls"))
    assert assistant["content"] == ""


def test_runner_persists_presented_file_and_suppresses_deleted_helper(tmp_path, monkeypatch):
    events: list[dict] = []
    output_path = str(tmp_path / "report.pdf")
    session = _Session(
        tmp_path,
        events,
        runner_events=[
            AgentEvent.tool_call("write-helper", "write_file", {"file_path": "create_report.py"}),
            AgentEvent.tool_result(
                "write-helper",
                "Created helper",
                status="success",
                diff={"plus": 20, "minus": 0, "patch": "+print('report')"},
            ),
            AgentEvent.tool_call("present-report", "present_file", {"path": output_path}),
            AgentEvent.tool_result(
                "present-report",
                "Presented report.pdf",
                status="success",
                output_files=[{
                    "path": output_path,
                    "name": "report.pdf",
                    "size": 4096,
                    "mime_type": "application/pdf",
                    "is_image": False,
                }],
                superseded_tool_call_ids=["write-helper"],
                removed_file_paths=["create_report.py"],
            ),
            AgentEvent.agent_message_completed("报告已创建。", source="model_final"),
            AgentEvent.done(status="completed"),
        ],
    )

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "创建 PDF 报告",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant-deliverable",
                "user_message_id": "user-deliverable",
            },
        )
    )

    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assistant = saved.transcript[-1]
    assert assistant["reply_attachments"] == [{
        "path": output_path,
        "size": 4096,
        "is_image": False,
    }]
    helper, deliverable = assistant["tool_calls"]
    assert helper["temporaryRemoved"] is True
    assert "diff" not in helper
    assert deliverable["outputFiles"][0]["path"] == output_path


def test_runner_preserves_query_done_provider_metadata(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(
        tmp_path,
        events,
        runner_events=[AgentEvent.done(
            input_tokens=12,
            output_tokens=7,
            provider_raw={"finish_reason": "stop", "request_id": "req-123"},
        )],
    )

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "continue",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant_metadata",
                "user_message_id": "user_metadata",
            },
        )
    )

    done = [event for event in events if event.get("type") == "done"]
    assert len(done) == 1
    assert done[0]["provider_raw"] == {"finish_reason": "stop", "request_id": "req-123"}
    assert done[0]["usage"]["input_tokens"] == 12
    assert done[0]["usage"]["output_tokens"] == 7
    done_index = next(index for index, event in enumerate(events) if event.get("type") == "done")
    idle_index = next(
        index
        for index, event in enumerate(events)
        if event.get("type") == "session.state_changed" and event.get("state") == "idle"
    )
    assert done_index < idle_index


def test_runner_keeps_low_value_reply_when_no_tool_summary_exists(tmp_path, monkeypatch):
    events: list[dict] = []
    low_value_reply = "Let me now write the final report."
    session = _Session(
        tmp_path,
        events,
        runner_events=[
            AgentEvent.agent_message_completed(low_value_reply, source="model_final"),
            AgentEvent.done(),
        ],
    )

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "write a report",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant_no_tools",
                "user_message_id": "user_no_tools",
            },
        )
    )

    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assert saved.transcript[-1]["content"] == low_value_reply


def test_runner_reuses_session_llm_adapter_for_consecutive_turns(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(tmp_path, events)
    created: list[_NoopLLM] = []

    async def no_lifecycle_runtime(**_kwargs):
        return None

    monkeypatch.setattr(session, "_ensure_lifecycle_runtime", no_lifecycle_runtime)

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key", model="gpt-test")),
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_llm_provider",
        lambda: "openai",
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )

    def create_llm(config, model_override=None, **_kwargs):
        adapter = _NoopLLM()
        created.append(adapter)
        return adapter

    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", create_llm)

    for message in ("第一轮", "第二轮"):
        asyncio.run(
            session._run_agent_locked(
                message,
                conversation_id="conv_runnerdone",
                metadata={
                    "agent_runtime": default_runtime(),
                    "assistant_message_id": f"assistant_{message}",
                    "user_message_id": f"user_{message}",
                },
            )
        )

    assert len(created) == 1
    assert session.llm is created[0]
    assert session.context_builder._llm is created[0]


def test_llm_adapter_cache_key_ignores_non_wire_transient_llm_fields() -> None:
    base = SimpleNamespace(
        llm=SimpleNamespace(
            api_key="key",
            base_url="https://api.openai.com/v1/",
            model="saved-default-a",
            reasoning_effort="low",
            responses_reasoning_summary="off",
            max_tokens=8192,
            wire_api="RESPONSES",
            prompt_cache_retention="24h",
            provider_history=[{"updated_at": 1}],
            available_models=["saved-default-a"],
        ),
        agent=SimpleNamespace(fallback_providers=()),
    )
    changed_transient = SimpleNamespace(
        llm=SimpleNamespace(
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="saved-default-b",
            reasoning_effort="low",
            responses_reasoning_summary="off",
            max_tokens=8192,
            wire_api="responses",
            prompt_cache_retention="24h",
            provider_history=[{"updated_at": 2}],
            available_models=["saved-default-b"],
        ),
        agent=SimpleNamespace(fallback_providers=()),
    )

    assert _llm_settings_identity(base.llm) == _llm_settings_identity(changed_transient.llm)
    assert _llm_adapter_cache_key(config=base, provider="openai", model="gpt-active") == _llm_adapter_cache_key(
        config=changed_transient,
        provider="openai",
        model="gpt-active",
    )


def test_llm_adapter_cache_key_changes_for_wire_relevant_settings() -> None:
    base = AppConfig(
        llm=LLMSettings(
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="gpt-saved",
            reasoning_effort="low",
            responses_reasoning_summary="off",
            max_tokens=8192,
            wire_api="responses",
            prompt_cache_retention="24h",
        )
    )
    changed = AppConfig(
        llm=LLMSettings(
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="gpt-saved",
            reasoning_effort="medium",
            responses_reasoning_summary="off",
            max_tokens=8192,
            wire_api="responses",
            prompt_cache_retention="24h",
        )
    )

    assert _llm_adapter_cache_key(config=base, provider="openai", model="gpt-active") != _llm_adapter_cache_key(
        config=changed,
        provider="openai",
        model="gpt-active",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("small_fast_model", "glm-4.7-flash"),
        ("reasoning_effort_levels", ("low", "high")),
        ("context_window", 128_000),
        ("context_window_source", "provider"),
        ("context_window_verified", True),
        ("max_context_window", 200_000),
        ("max_output_tokens", 16_384),
        ("max_output_tokens_source", "provider"),
        ("max_output_tokens_verified", True),
        ("default_reasoning_effort", "high"),
        ("default_reasoning_summary", "auto"),
        ("proxy_mode", "direct"),
        ("seed", 29),
    ],
)
def test_llm_adapter_cache_identity_changes_for_model_capability_metadata(
    field: str,
    value: object,
) -> None:
    base = LLMSettings(
        api_key="key",
        provider="custom",
        base_url="https://gateway.example/v1",
        model="glm-5.2",
        wire_api="chat",
    )
    changed = replace(base, **{field: value})

    assert _llm_settings_identity(base) != _llm_settings_identity(changed)


def test_runner_persists_completed_agent_message_as_final_text_block(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(
        tmp_path,
        events,
        runner_events=[
            AgentEvent.agent_message_started(),
            AgentEvent.agent_message_delta("北京今天雷阵雨。"),
            AgentEvent.agent_message_completed("北京今天雷阵雨。", source="model_final"),
            AgentEvent.done(),
        ],
    )

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_llm_provider",
        lambda: "openai",
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "今天北京天气如何",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant_local",
                "user_message_id": "user_weather_completed",
            },
        )
    )

    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assistant = saved.transcript[-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "北京今天雷阵雨。"
    assert assistant["blocks"][-1] == {
        "type": "text",
        "content": "北京今天雷阵雨。",
        "source": "model_final",
        "itemId": "agent-message",
        "status": "completed",
        "isStreaming": False,
    }


def test_runner_settles_in_progress_item_as_partial_when_done_has_no_completed_item(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(
        tmp_path,
        events,
        runner_events=[
            AgentEvent.agent_message_started(),
            AgentEvent.agent_message_delta("北京今天雷阵雨。"),
            AgentEvent.done(),
        ],
    )

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_llm_provider",
        lambda: "openai",
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "今天北京天气如何",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant_local",
                "user_message_id": "user_weather_partial",
            },
        )
    )

    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assistant = saved.transcript[-1]
    assert assistant["role"] == "assistant"
    assert assistant["terminal_status"] == "failed"
    assert assistant["content"] == "北京今天雷阵雨。"
    assert assistant["blocks"][0] == {
        "type": "text",
        "itemId": "agent-message",
        "content": "北京今天雷阵雨。",
        "source": "partial",
        "status": "partial",
        "isStreaming": False,
    }
    assert len(assistant["blocks"]) == 1
    missing_final = [
        event
        for event in events
        if event.get("type") == "error"
        and event.get("error_type") == "missing_final_answer"
    ]
    assert len(missing_final) == 1
    assert next(event for event in events if event.get("type") == "done")["status"] == "failed"


def test_runner_preserves_collaboration_final_report_narration(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(
        tmp_path,
        events,
        runner_events=[
            AgentEvent.tool_call(
                "subagent-report",
                "review",
                {"name": "UX audit"},
                result_kind="subagent",
            ),
            AgentEvent.tool_result(
                "subagent-report",
                "Review completed.\n\n## Findings\nThe panel exposes internal IDs.",
                status="success",
                result_kind="subagent",
            ),
            AgentEvent.agent_message_completed(
                "Let me now write the final report.",
                source="model_final",
            ),
            AgentEvent.done(),
        ],
    )

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_llm_provider",
        lambda: "openai",
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "用子代理分头检查这个 UX",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant_local",
                "user_message_id": "user_collaboration_report",
            },
        )
    )

    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assistant = saved.transcript[-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "Let me now write the final report."
    assert "模型没有生成最终总结" not in assistant["content"]


def test_runner_preserves_collaboration_answer_announcement(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(
        tmp_path,
        events,
        runner_events=[
            AgentEvent.tool_call(
                "subagent-report",
                "review",
                {"name": "UX audit"},
                result_kind="subagent",
            ),
            AgentEvent.tool_result(
                "subagent-report",
                "Review completed.\n\n## Findings\nThe panel hides completed reports.",
                status="success",
                result_kind="subagent",
            ),
            AgentEvent.agent_message_completed(
                "Let me give the user a clear, honest answer based on what I've found.",
                source="model_final",
            ),
            AgentEvent.done(),
        ],
    )

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_llm_provider",
        lambda: "openai",
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "用子代理分头检查这个 UX",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant_local",
                "user_message_id": "user_collaboration_answer",
            },
        )
    )

    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assistant = saved.transcript[-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "Let me give the user a clear, honest answer based on what I've found."
    assert "模型没有生成最终总结" not in assistant["content"]


def test_runner_does_not_emit_tool_only_failure_after_live_final_answer(tmp_path, monkeypatch):
    events: list[dict] = []
    session = _Session(
        tmp_path,
        events,
        runner_events=[
            AgentEvent.agent_message_completed(
                "今天北京雷阵雨，最高 30℃。",
                source="model_final",
            ),
            AgentEvent.tool_call(
                "tool_weather_fallback",
                "web_search",
                {"query": "Beijing weather backup"},
            ),
            AgentEvent.tool_result(
                "tool_weather_fallback",
                "backup source timed out",
                is_error=True,
                status="failed",
            ),
            AgentEvent.done(),
        ],
    )

    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_llm_provider",
        lambda: "openai",
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )

    asyncio.run(
        session._run_agent_locked(
            "今天北京天气如何",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant_local",
                "user_message_id": "user_weather_tool_failure",
            },
        )
    )

    assert not any(
        event.get("type") == "error"
        and event.get("message") == "Tool calls failed before the assistant produced a reply."
        for event in events
    )
    saved = session.conversation_repo.get_conversation("conv_runnerdone")
    assert saved is not None
    assistant = saved.transcript[-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "今天北京雷阵雨，最高 30℃。"
    assert assistant["tool_calls"][0]["status"] == "failed"
    text_block = next(block for block in assistant["blocks"] if block["type"] == "text")
    assert text_block == {
        "type": "text",
        "itemId": "agent-message",
        "content": "今天北京雷阵雨，最高 30℃。",
        "source": "model_final",
        "status": "completed",
        "isStreaming": False,
    }
