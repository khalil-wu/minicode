"""Connection-owned sampling on an inherited, private QueryEngine thread."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from backend.agent.context import ContextBuilder
from backend.agent.conversation_query_guard import conversation_query_guards
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.query_engine import AgentSession, QuerySubmission
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.thread_sample import SAMPLE_TIMEOUT, SampleCapture, SampleError, SampleParams, sample_turn
from backend.agent.turn_input import user_input_origin_from_trigger
from backend.artifact.store import ArtifactStore
from backend.async_cleanup import retain_cleanup_task
from backend.atomic_io import await_task_despite_cancellation
from backend.llm.model_metadata import source_guardian_model_metadata
from backend.llm.model_registry import create_session_llm
from backend.llm.model_selection import apply_model_thinking_level, config_with_model_budget
from backend.permissions.checker import PermissionChecker


def request_key(request_id: str | int) -> tuple[type, str | int]:
    if type(request_id) not in {str, int}:
        raise SampleError("sample requestId must be a string or integer", invalid_request=True)
    if type(request_id) is int and not -(2 ** 63) <= request_id < 2 ** 63:
        raise SampleError("sample integer requestId must fit a signed 64-bit integer", invalid_request=True)
    return type(request_id), request_id


def check_source(execution, record, expected_turn_id: str) -> None:
    if execution.shutting_down or record is None or record.archived:
        raise SampleError("sample source must be a loaded, unarchived thread", invalid_request=True)
    if record.thread_source == "subagent":
        raise SampleError("sample source must be a persistent root thread", invalid_request=True)
    if (record.id in execution.host.run_manager.run_tasks
            or conversation_query_guards().active_claim(record.id) is not None):
        raise SampleError("sample source must be idle", invalid_request=True)
    last = next((row for row in reversed(record.transcript) if row.get("role") in {"assistant", "user"}), None)
    if (last is None or last["role"] != "assistant"
            or str(last.get("turn_id") or last["id"]) != expected_turn_id
            or last.get("terminal_status") not in {"completed", "failed", "cancelled", "interrupted", "partial"}):
        raise SampleError("sample source turn must be the latest finished turn", invalid_request=True)


def source_history(record, thread):
    return record.content_revision, thread._model_snapshot(record.context_snapshot)


def sample_model_config(request, model_runtime, params):
    selection = request.model_selection
    provider = selection.get("provider") or request.config.llm.provider
    model = selection.get("model") or request.config.llm.model
    definition = model_runtime.get_model(provider, model) if model_runtime is not None else None
    info = definition.extra if definition is not None else source_guardian_model_metadata(model)
    effort = selection.get("reasoning_effort", request.config.llm.reasoning_effort)
    if provider == "openai" and info.get("supports_reasoning_effort_updates") and params.effort is not None:
        effort = "off" if params.effort == "none" else params.effort
    config = replace(request.config, llm=replace(request.config.llm, provider=provider, model=model, reasoning_effort=effort))
    config = config_with_model_budget(config, model_runtime=model_runtime, provider=provider, model=model)
    return config, definition, info


async def generate_sample(session, params: SampleParams, cancel: asyncio.Event, deadline: float) -> dict[str, Any]:
    execution = session.ws_manager.execution_contexts.context_for(params.thread_id) if session.ws_manager is not None else None
    if execution is None:
        raise SampleError("thread/sample requires a loaded source", invalid_request=True)
    host, thread = execution.host, execution.thread_runtime
    record = await asyncio.to_thread(host.conversation_repo.get_conversation, params.thread_id)
    check_source(execution, record, params.expected_turn_id)
    revision = source_history(record, thread)
    request = host.capture_agent_run_request(params.thread_id, config=thread.model_config)
    model_runtime = host._model_runtime_for_conversation(params.thread_id)
    config, definition, model_info = sample_model_config(request, model_runtime, params)
    sample_id = "sample-" + uuid4().hex
    capture = SampleCapture()
    run = RunContext(session_source="thread_sample", response_capture=capture, user_shell=deepcopy(thread.user_shell),
        environment_manager=host.environment_manager, cancel_event=asyncio.Event(),
        responses_session_id=sample_id, cost_session_id=sample_id, mcp_owner_session_id=sample_id,
        mcp_manager=request.mcp_manager, skill_manager=request.skill_manager,
        requires_explicit_workspace=True)
    async with AsyncExitStack() as scope:
        scope.push_async_callback(run.close_environment_snapshots)
        if thread.thread_environments is not None:
            run.inherited_turn_environments = await thread.thread_environments.snapshot()
        # The operation task is shielded by the connection owner. Startup is
        # allowed to settle so every acquired resource has a cleanup owner.
        temp = scope.enter_context(TemporaryDirectory(prefix="minicode-sample-"))
        root = Path(temp)
        runtime = await asyncio.to_thread(AgentRuntime, metrics_file=root / "metrics.jsonl",
            swarm_store_dir=root / "swarm", runtime_instance_id=sample_id)
        scope.push_async_callback(runtime.aclose)
        artifacts = ArtifactStore(storage_dir=root / "artifacts")
        scope.push_async_callback(artifacts.aclose)
        adapter = await asyncio.to_thread(create_session_llm, config, provider_override=config.llm.provider,
            model_override=config.llm.model, model_runtime=model_runtime)
        scope.push_async_callback(adapter.aclose)
        effort = apply_model_thinking_level(adapter, definition, config.llm.reasoning_effort)
        run.model_execution = ModelExecutionSnapshot(config=config, llm=adapter, provider=config.llm.provider,
            model=config.llm.model, thinking_level=effort, model_runtime=model_runtime, model_info=definition)
        run.agent_runtime = runtime
        retained = thread.model_context
        builder = ContextBuilder(token_budget=config.token_budget, agent_settings=config.agent,
            llm=adapter, conversation_id=sample_id, workspace_root=request.workspace_root,
            skill_manager=request.skill_manager, skill_executor=request.skill_executor,
            memory_manager=request.memory_manager)
        await asyncio.to_thread(builder.load_snapshot, record.context_snapshot)
        builder.auto_compaction_disabled = True
        run.mcp_servers_disabled = params.disable_mcp and bool(model_info.get("use_responses_lite")) and any(
            item.get("type") == "additional_tools" for message in builder._history for item in message.provider_items)
        agent = AgentSession(llm=adapter, tool_registry=request.tool_registry, artifact_store=artifacts,
            permission_checker=PermissionChecker(config.permissions), agent_settings=config.agent, token_budget=config.token_budget,
            context_builder=builder, approval_handler=capture.reject_interaction)
        active = host._extension_runtime_states.get(params.thread_id, {}).get("active_tool_names")
        agent.active_tool_names = tuple(active) if active is not None else None
        scope.push_async_callback(agent.aclose)
        async def drain_turn_resources():
            if run.hook_manager is not None:
                await run.hook_manager.finalize_async_hooks()
            while pending := {task for task in run.lifecycle_cleanup_tasks if not task.done()}:
                await asyncio.wait(pending)
            await run.close_environment_snapshots()
        scope.push_async_callback(drain_turn_resources)
        from backend.mcp.client import sample_interaction_handler
        interaction_token = sample_interaction_handler.set(capture.reject_interaction)
        scope.callback(sample_interaction_handler.reset, interaction_token)
        metadata = {"conversation_id": sample_id, "query_source": "thread_sample", "agent_mode": request.agent_mode,
            "input_origin": user_input_origin_from_trigger(params.turn_trigger),
            "_source_responses_metadata": {**(params.responsesapi_client_metadata or {}), "thread_source": params.thread_source,
                **({"turn_trigger": params.turn_trigger} if params.turn_trigger is not None else {})}}
        if cancel.is_set() or asyncio.get_running_loop().time() >= deadline:
            raise SampleError("sample canceled" if cancel.is_set() else "sample timed out")
        result = await sample_turn(QuerySubmission(user_message=params.prompt, session=agent,
            state=AgentState(user_message=params.prompt, conversation_id=sample_id, workspace_root=request.workspace_root),
            runtime=AgentLoopSessionContext(run_context=run, metadata=metadata, session_id=sample_id,
                workspace_root=request.workspace_root, skill_manager=request.skill_manager,
                permission_context=replace(deepcopy(request.permission), conversation_id=sample_id),
                cancel_event=run.cancel_event, emit_event=capture.observe)), cancel=cancel, deadline=deadline)
    current = await asyncio.to_thread(host.conversation_repo.get_conversation, params.thread_id)
    if (current is None or source_history(current, thread) != revision
            or conversation_query_guards().active_claim(params.thread_id) is not None
            or retained is not None and retained[0]._history_store.revision != retained[2]):
        raise SampleError("sample source changed", invalid_request=True)
    return {"sourceTurnId": params.expected_turn_id, **result}


@dataclass
class SampleRequest:
    cancel: asyncio.Event
    task: asyncio.Task


class ThreadSampleRequests:
    def __init__(self, session):
        self.session = session
        self.requests: dict[tuple[int, type, str | int], SampleRequest] = {}

    def cancel(self, request_id, generation):
        request = self.requests.get((generation, *request_key(request_id)))
        if request is not None:
            request.cancel.set()

    def disconnect(self, generation):
        for key, request in self.requests.items():
            if key[0] == generation:
                request.cancel.set()

    async def sample(self, request_id, params, generation):
        key = (generation, *request_key(request_id))
        if key in self.requests:
            raise SampleError("sample requestId is already in use", invalid_request=True)
        deadline = asyncio.get_running_loop().time() + SAMPLE_TIMEOUT
        cancel = asyncio.Event()
        task = asyncio.create_task(generate_sample(self.session, params, cancel, deadline), name="thread-sample-owner")
        self.requests[key] = SampleRequest(cancel, task)
        retain_cleanup_task(task, self.session.cleanup_tasks)
        def settled(completed):
            self.requests.pop(key)
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(settled)
        try:
            done, _ = await asyncio.wait({task}, timeout=SAMPLE_TIMEOUT)
            if not done:
                cancel.set()
                raise SampleError("sample timed out")
            return task.result()
        except asyncio.CancelledError:
            cancel.set()
            # Disconnect may cancel the RPC while fork startup is suspended.
            # Its owner must finish startup and drain the private thread.
            try:
                await await_task_despite_cancellation(task)
            except SampleError:
                pass  # No response is delivered to a retired connection.
            raise
