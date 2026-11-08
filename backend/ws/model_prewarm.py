"""Idle-thread auth and connection setup using the normal model adapter owner."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import logging

from backend.async_cleanup import to_thread_cancel_safe
from backend.llm.base import LLMAdapter

logger = logging.getLogger(__name__)


def _release_unused_thread(registry, thread):
    if (registry.context_for(thread.conversation_id) is None and thread.thread_environments is None
            and thread.model_context is None and thread.model_context_preparation is None and not thread.pending_tasks()):
        registry.release_thread_runtime(thread)


def release_prepared_model_context(session, conversation_id=None):
    prepared = session._model_context_prewarm
    if prepared is None or conversation_id is not None and prepared[0].conversation_id != conversation_id:
        return
    session._model_context_prewarm = None
    thread, retained = prepared
    registry = session.ws_manager.execution_contexts
    if thread.model_context_preparation is retained:
        thread.model_context_preparation = None
        retained.cancel()
        retained.add_done_callback(lambda _: _release_unused_thread(registry, thread))
    if thread.model_context is retained:
        thread.model_context = None
        _release_unused_thread(registry, thread)


async def prepare_thread_model_context(session, conversation_id, workspace, config):
    """Load the idle thread's owned context before input admission needs it."""
    if session.ws_manager is None:
        return
    registry = session.ws_manager.execution_contexts
    store_id = await asyncio.to_thread(session.conversation_repo.store_instance_id)
    thread = registry.thread_runtime(store_id, conversation_id)
    if thread.model_config is None:
        from backend.agent.model_execution import capture_model_config
        thread.model_config = capture_model_config(config)
    if thread.model_context is not None or thread.model_context_preparation is not None or thread.pending_tasks():
        return

    def load():
        from backend.agent.context import ContextBuilder
        from backend.runtime_env import default_user_shell

        record = session.conversation_repo.get_execution_context(conversation_id, prepare_resume=True)
        if record is None or record.archived:
            return None
        builder = ContextBuilder(token_budget=config.token_budget, agent_settings=config.agent,
            conversation_id=conversation_id, workspace_root=workspace)
        builder.load_snapshot(record.context_snapshot)
        builder.prepare_recovery_snapshot()
        shell = thread.user_shell if thread is not None and thread.user_shell is not None else default_user_shell()
        return builder, record.context_snapshot, shell, workspace

    work = asyncio.create_task(to_thread_cancel_safe(load), name=f"thread-history-prepare:{conversation_id}")
    thread.model_context_preparation = work
    thread.retain_turn(work, set())
    session._model_context_prewarm = (thread, work)
    try:
        prepared = await asyncio.shield(work)
    except asyncio.CancelledError:
        if thread.model_context_preparation is work:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            release_prepared_model_context(session, conversation_id)
        raise
    except Exception:
        release_prepared_model_context(session, conversation_id)
        raise
    if thread.model_context_preparation is not work:
        return  # A real turn now owns the same preparation and its validation.
    if (prepared is None or session.active_conversation_id != conversation_id or session.session_lifecycle.is_shutting_down
            or thread.pending_tasks()):
        release_prepared_model_context(session, conversation_id)
        return
    if conversation_id in session.run_manager.run_tasks:
        return  # Admission will take the ready candidate at its history boundary.
    thread.model_context_preparation = None
    thread.capture_memory_settings(config.memories)
    builder, snapshot, shell, _ = prepared
    if thread.user_shell is None:
        thread.user_shell = shell
    thread.retain_model_context(builder, snapshot, workspace)
    session._model_context_prewarm = (thread, thread.model_context)


async def close_thread_model_sessions(session, conversation_id):
    pending = session.__dict__.get("_model_prewarm_tasks", {}).pop(conversation_id, None)
    if pending is not None:
        pending[1].cancel()
        await asyncio.gather(pending[1], return_exceptions=True)
    release_prepared_model_context(session, conversation_id)
    adapters = {id(adapter): adapter for collection in ("_llm_adapter_cache", "_retired_llm_adapters")
        for adapter in session.__dict__.get(collection, {}).values()}
    await asyncio.gather(*(adapter.close_thread_session(conversation_id) for adapter in adapters.values() if isinstance(adapter, LLMAdapter)))
    from backend.ws.agent_runner import _clear_session_llm_cache

    _clear_session_llm_cache(session, conversation_id=conversation_id)


def schedule_model_prewarm(session, conversation_id):
    if not conversation_id or session.session_lifecycle.is_shutting_down:
        return
    if conversation_id in session.run_manager.run_tasks:
        return
    selection = (session.provider, session.selected_model, session.config.llm.reasoning_effort)
    if not selection[1]:
        return
    from backend.ws.agent_runner import _llm_settings_identity

    key = (selection, _llm_settings_identity(session.config.llm),
        session.config.config_layer_stack.fingerprint if session.config.config_layer_stack is not None else None,
        id(session._model_runtime_for_conversation(conversation_id)))
    existing = session._model_prewarm_tasks.get(conversation_id)
    if existing is not None and not existing[1].done():
        if existing[0] == key:
            return
        existing[1].cancel()
    previous_view = session._model_prewarm_view_owner
    session._model_prewarm_view_owner = conversation_id
    if previous_view and previous_view != conversation_id and previous_view not in session.run_manager.run_tasks:
        loaded = session.ws_manager.execution_contexts.context_for(previous_view) if session.ws_manager is not None else None
        if loaded is None:
            previous_task = session._model_prewarm_tasks.pop(previous_view, None)
            if previous_task is not None:
                previous_task[1].cancel()
            from backend.ws.agent_runner import _clear_session_llm_cache

            # Speculative views have no loaded core to unload. The normal
            # adapter lease still protects a foreground run admitted now.
            _clear_session_llm_cache(session, conversation_id=previous_view)
            release_prepared_model_context(session, previous_view)

    async def prepare():
        from backend.config import load_config

        record = await asyncio.to_thread(session.conversation_repo.get_conversation_summary, conversation_id)
        if record is None or record.archived:
            return
        workspace = session.session_lifecycle.workspace_root_for_conversation(record)
        config = await to_thread_cancel_safe(load_config, cwd=workspace)
        async with asyncio.TaskGroup() as preparation:
            preparation.create_task(prepare_thread_model_context(session, conversation_id, workspace, config))
            preparation.create_task(prepare_transport(config))

    async def prepare_transport(config):
        from backend.ws.agent_runner import _apply_thinking_level, _config_with_runtime_model_budget, _get_or_create_session_llm
        from backend.agent.provider_lifecycle import LIFECYCLE_RUNTIME_METADATA_KEY

        runtime = session._model_runtime_for_conversation(conversation_id)
        provider, model, effort = selection
        if runtime is not None:
            runtime.refresh(settings_snapshot=config.config_layer_stack.effective_config() if config.config_layer_stack is not None else None)
            await runtime.refresh_oauth_credentials(provider)
            await runtime.refresh_provider_auth(provider)
        if (session.active_conversation_id != conversation_id or session.session_lifecycle.is_shutting_down
                or conversation_id in session.run_manager.run_tasks
                or selection != (session.provider, session.selected_model, session.config.llm.reasoning_effort)):
            return
        config = _config_with_runtime_model_budget(config, model_runtime=runtime, provider=provider, model=model)
        config = replace(config, llm=replace(config.llm, reasoning_effort=effort))
        adapter = _get_or_create_session_llm(session, config=config, provider=provider, model=model, model_runtime=runtime,
            conversation_id=conversation_id)
        definition = runtime.get_model(provider, model) if runtime is not None else None
        _apply_thinking_level(adapter, definition, effort)
        if isinstance(adapter, LLMAdapter):
            metadata = {"session_id": conversation_id, "thread_id": conversation_id,
                "conversation_id": conversation_id, "turn_id": "startup-prewarm", "query_source": "user"}
            extension = session.__dict__.get("_extension_runtime_states", {}).get(conversation_id)
            if extension is not None and extension.runtime is not None:
                metadata[LIFECYCLE_RUNTIME_METADATA_KEY] = extension.runtime
            task = adapter.schedule_session_preconnect(metadata)
            if task is not None:
                await asyncio.shield(task)

    task = asyncio.create_task(prepare(), name=f"thread-model-prewarm:{conversation_id}")
    session._model_prewarm_tasks[conversation_id] = (key, task)
    session.command_dispatcher.track_command_task(task)
    def completed(done):
        if session._model_prewarm_tasks.get(conversation_id) == (key, done):
            session._model_prewarm_tasks.pop(conversation_id)
        if not done.cancelled() and (error := done.exception()) is not None:
            logger.info("Thread model prewarm unavailable (%s)", type(error).__name__)
    task.add_done_callback(completed)
