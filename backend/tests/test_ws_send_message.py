from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.agent.runtime import AgentRuntime
from backend.agent.run_context import RunContext
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.agent_tools import TaskTool
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec
from backend.tools.registry import ToolRegistry
from backend.tools.toolsets import ToolsetPolicy
from backend.ws.handlers.misc import handle_send_message, handle_subagent_transcript


class _TaskTool:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def resume_background_subtask(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return str(kwargs["subagent_id"])


class _Registry:
    def __init__(self, task_tool: _TaskTool) -> None:
        self.task_tool = task_tool

    def get_tool(self, name: str):
        return self.task_tool if name == "task" else None


class _Session:
    def __init__(self, tmp_path, task_tool: _TaskTool) -> None:
        self.active_conversation_id = "conversation-1"
        self.permission_context = PermissionContext(mode="confirm")
        self.session_id = "session-1"
        self.tool_registry = _Registry(task_tool)
        self._workspace_root = tmp_path
        self.command_results: list[dict] = []
        self.events: list[dict] = []
        self.session_lifecycle = SimpleNamespace(
            current_workspace_root=lambda: self._workspace_root,
            workspace_root=self._workspace_root,
            workspace_root_for_conversation=lambda _conversation=None: self._workspace_root,
        )

    def current_workspace_root(self):
        return self._workspace_root

    def resolve_requested_workspace(self, requested_workspace=None):
        from pathlib import Path

        return Path(requested_workspace or self._workspace_root).expanduser().resolve()

    async def send_event(self, event) -> None:
        self.events.append(event.to_ws_message())

    async def emit_command_result(self, command: str, message: str, **kwargs) -> None:
        self.command_results.append({"command": command, "message": message, **kwargs})


class _ResumeProbeTool(BaseTool):
    name = "resume_probe"
    read_only = True

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="workspace.read",
            toolset="core",
            exposure="core",
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description="Probe a resumed child capability surface",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args, context=None) -> ToolResult:
        return ToolResult(content="probe")


def test_ui_send_message_resumes_stopped_agent(monkeypatch, tmp_path) -> None:
    task_tool = _TaskTool()
    session = _Session(tmp_path, task_tool)
    subagent = SimpleNamespace(
        parent_run_id="parent-run",
        status="completed",
        task_id="task-1",
    )
    runtime = SimpleNamespace(
        get_subagent=lambda subagent_id: subagent,
        get_run=lambda run_id: SimpleNamespace(conversation_id="conversation-1"),
    )
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: runtime)

    handled = asyncio.run(handle_send_message(session, {
        "recipient": "subagent-ended",
        "message": "Check one more edge case.",
        "message_id": "message-1",
    }))

    assert handled is True
    assert len(task_tool.calls) == 1
    call = task_tool.calls[0]
    assert call["subagent_id"] == "subagent-ended"
    assert call["prompt"] == "Check one more edge case."
    assert call["context"].conversation_id == "conversation-1"
    assert call["context"].run_context.agent_runtime is runtime
    assert session.command_results[-1] == {
        "command": "send_message",
        "message": "Stopped subagent resumed with the message.",
        "data": {
            "recipient": "subagent-ended",
            "message_id": "message-1",
            "resumed": True,
        },
    }


def test_ui_send_message_resumes_evicted_agent_from_durable_record(monkeypatch, tmp_path) -> None:
    task_tool = _TaskTool()
    session = _Session(tmp_path, task_tool)
    subagent = SimpleNamespace(
        subagent_id="subagent-evicted",
        parent_run_id="parent-run",
        session_id="session-1",
        status="completed",
        task_id="task-1",
    )
    runtime = SimpleNamespace(
        get_subagent=lambda subagent_id: None,
        load_persisted_subagent=lambda subagent_id: subagent,
        get_run=lambda run_id: SimpleNamespace(conversation_id="conversation-1"),
    )
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: runtime)

    asyncio.run(handle_send_message(session, {
        "recipient": "subagent-evicted",
        "message": "Resume from disk.",
        "message_id": "message-2",
    }))

    assert len(task_tool.calls) == 1
    assert task_tool.calls[0]["context"].metadata["run_id"] == "parent-run"
    assert session.command_results[-1]["data"] == {
        "recipient": "subagent-evicted",
        "message_id": "message-2",
        "resumed": True,
    }


def test_ui_send_message_real_resume_restores_persisted_tool_policy(
    monkeypatch,
    tmp_path,
) -> None:
    class _ResumeLLM(LLMAdapter):
        def __init__(self) -> None:
            self.tool_names: list[list[str]] = []

        async def stream_chat(self, messages, tools=None, metadata=None):
            self.tool_names.append(
                [str((schema.get("function") or {}).get("name") or "") for schema in (tools or [])]
            )
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="resumed")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

        async def simple_chat(
            self,
            messages: list[LLMMessage],
            *,
            max_tokens: int | None = None,
        ) -> str:
            return ""

    llm = _ResumeLLM()
    registry = ToolRegistry()
    registry.register(_ResumeProbeTool())
    task_tool = TaskTool(
        llm_provider=llm,
        tool_registry_provider=registry,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker_provider=lambda: PermissionChecker(
            PermissionSettings(), workspace_root=tmp_path
        ),
        agent_settings_provider=lambda: AgentSettings(max_iterations=1),
        token_budget_provider=lambda: TokenBudget(),
    )
    registry.register(task_tool)
    session = _Session(tmp_path, task_tool)
    session.tool_registry = registry
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False,
    )
    runtime.start_run(run_id="parent-run", conversation_id="conversation-1")
    emitted: list[tuple[str, dict]] = []

    async def emit(event_type: str, payload: dict) -> None:
        emitted.append((event_type, payload))

    parent_context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        workspace_root=tmp_path,
        session_id="session-1",
        task_id="parent-task",
        conversation_id="conversation-1",
        emit_event=emit,
        metadata={
            "agent_runtime": runtime,
            "run_id": "parent-run",
            "_tool_registry": registry,
            "_session_toolset_policy": ToolsetPolicy.from_iterables(
                enabled_toolsets=(),
                enabled_tools=["resume_probe"],
            ),
        },
        run_context=RunContext(agent_runtime=runtime),
        tool_registry=registry,
    )
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: runtime)
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))

    try:
        async def run() -> tuple[bool, str]:
            initial = await task_tool.execute(
                {
                    "description": "resume probe",
                    "prompt": "Run once before the UI resumes this child.",
                    "agent_type": "general-purpose",
                },
                context=parent_context,
            )
            assert initial.status == "completed"
            starts = [
                payload
                for event_type, payload in emitted
                if event_type == "subagent.start"
            ]
            assert starts
            subagent_id = str(starts[-1]["subagent_id"])

            handled = await handle_send_message(
                session,
                {
                    "recipient": subagent_id,
                    "message": "continue",
                    "message_id": "message-real-resume",
                },
            )
            for _ in range(500):
                resumed = runtime.get_subagent(subagent_id)
                if resumed is not None and resumed.status == "completed":
                    return handled, subagent_id
                await asyncio.sleep(0.01)
            raise AssertionError(runtime.get_subagent_snapshot(subagent_id, include_result=True))

        handled, subagent_id = asyncio.run(run())

        assert handled is True
        assert runtime.get_subagent(subagent_id).status == "completed"
        assert llm.tool_names == [["resume_probe"], ["resume_probe"]]
        assert session.command_results[-1]["data"]["resumed"] is True
    finally:
        runtime.close(release_lease=True)


def test_ui_send_message_rejects_transcript_without_durable_record(monkeypatch, tmp_path) -> None:
    task_tool = _TaskTool()
    session = _Session(tmp_path, task_tool)
    runtime = SimpleNamespace(
        get_subagent=lambda subagent_id: None,
        load_persisted_subagent=lambda subagent_id: None,
        load_agent_transcript=lambda subagent_id: {
            "history": [{"role": "assistant", "content": "untrusted history"}],
        },
    )
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: runtime)

    asyncio.run(handle_send_message(session, {
        "recipient": "subagent-transcript-only",
        "message": "Do not recover from this transcript.",
        "message_id": "message-transcript-only",
    }))

    assert task_tool.calls == []
    assert session.command_results[-1]["level"] == "error"
    assert "No subagent found" in session.command_results[-1]["message"]


def test_ui_send_message_failure_echoes_message_id(monkeypatch, tmp_path) -> None:
    task_tool = _TaskTool()
    session = _Session(tmp_path, task_tool)
    runtime = SimpleNamespace(
        get_subagent=lambda subagent_id: None,
        load_agent_transcript=lambda subagent_id: {"history": [], "events": []},
    )
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: runtime)

    asyncio.run(handle_send_message(session, {
        "recipient": "subagent-missing",
        "message": "Hello",
        "message_id": "message-failed",
    }))

    assert session.command_results[-1]["level"] == "error"
    assert session.command_results[-1]["data"] == {
        "recipient": "subagent-missing",
        "message_id": "message-failed",
    }


def test_subagent_transcript_replays_an_evicted_owned_agent(monkeypatch, tmp_path) -> None:
    task_tool = _TaskTool()
    session = _Session(tmp_path, task_tool)
    transcript = {"events": [{
        "event_type": "user_prompt",
        "event_id": "user-1",
        "ts_ms": 1,
        "payload": {
            "content": "Inspect the implementation",
            "conversation_id": "conversation-1",
            "session_id": "session-1",
        },
    }]}
    runtime = SimpleNamespace(
        get_subagent=lambda subagent_id: None,
        load_persisted_subagent=lambda subagent_id: SimpleNamespace(
            subagent_id=subagent_id,
            parent_run_id="parent-run",
            session_id="session-1",
        ),
        get_subagent_task_metadata=lambda subagent_id: None,
        get_subagent_snapshot=lambda subagent_id, include_result=False: None,
        get_run=lambda run_id: SimpleNamespace(conversation_id="conversation-1"),
        load_agent_transcript=lambda subagent_id: transcript,
    )
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: runtime)

    handled = asyncio.run(handle_subagent_transcript(session, {
        "subagent_id": "subagent-evicted",
        "conversation_id": "conversation-1",
    }))

    assert handled is True
    assert session.command_results[-1]["command"] == "subagent.transcript"
    assert session.command_results[-1]["data"]["messages"][0]["content"] == "Inspect the implementation"


def test_subagent_transcript_rejects_an_evicted_agent_from_another_session(
    monkeypatch,
    tmp_path,
) -> None:
    task_tool = _TaskTool()
    session = _Session(tmp_path, task_tool)
    transcript = {"events": [{
        "event_type": "user_prompt",
        "payload": {
            "content": "private child work",
            "conversation_id": "conversation-1",
            "session_id": "another-session",
        },
    }]}
    runtime = SimpleNamespace(
        get_subagent=lambda subagent_id: None,
        load_persisted_subagent=lambda subagent_id: SimpleNamespace(
            subagent_id=subagent_id,
            parent_run_id="parent-run",
            session_id="another-session",
        ),
        get_subagent_task_metadata=lambda subagent_id: None,
        get_subagent_snapshot=lambda subagent_id, include_result=False: None,
        get_run=lambda run_id: SimpleNamespace(conversation_id="conversation-1"),
        load_agent_transcript=lambda subagent_id: transcript,
    )
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: runtime)

    asyncio.run(handle_subagent_transcript(session, {
        "subagent_id": "subagent-private",
        "conversation_id": "conversation-1",
    }))

    assert session.command_results == []
    assert session.events[-1]["level"] == "error"
    assert "different session" in session.events[-1]["message"]
