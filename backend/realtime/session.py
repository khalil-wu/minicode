"""Bind Source realtime commands to the existing thread and query consumers."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

from backend.agent.message import AgentEvent, UserCommand
from backend.config import load_config
from backend.realtime.protocol import Start
from backend.realtime.runtime import RealtimeRuntime
from backend.realtime.transport import RealtimeConfig


def capture_config(session, conversation_id: str, *, credentials: bool) -> RealtimeConfig:
    conversation = session.conversation_repo.get_conversation_summary(conversation_id) if conversation_id else None
    workspace = session.session_lifecycle.workspace_root_for_conversation(conversation)
    config = load_config(cwd=workspace, resolve_credentials=credentials)
    settings = config.config_layer_stack.effective_config() if config.config_layer_stack is not None else {}
    realtime = settings.get("realtime", {})
    enabled = config.feature_flags.enabled("realtime_conversation", True)
    llm = config.llm
    runtime = session._model_runtime_for_conversation(conversation_id)
    selection = conversation.model_selection if conversation is not None else {}
    provider = selection.get("provider", llm.provider)
    model = selection.get("model", llm.model)
    configured_provider = bool(llm.api_key or llm.default_headers)
    if runtime is not None:
        if credentials:
            available = runtime.get_provider(provider)
            configured_provider = available.configured if available is not None else configured_provider
        else:
            configured_provider = any(model.provider == provider for model in runtime.get_available_snapshot())
        definition = runtime.get_model(provider, model) if model else None
        if definition is not None:
            wire = {"openai-responses": "responses", "openai-completions": "chat", "anthropic-messages": "anthropic"}[definition.api]
            llm = replace(llm, provider=provider, model=model, base_url=definition.base_url, wire_api=wire)
        if credentials and definition is not None:
            spec = runtime.resolve_adapter_spec(provider, model)
            llm = replace(llm, provider=provider, model=model, base_url=spec.base_url,
                api_key=spec.api_key, default_headers=tuple(spec.headers.items()), auth_header=spec.auth_header,
                proxy_mode=spec.proxy_mode, aws=spec.aws)
    if provider != llm.provider:
        raise ValueError("The realtime thread's selected provider is unavailable")
    headers = dict(llm.default_headers)
    if credentials and llm.api_key and not any(key.lower() == "authorization" for key in headers):
        headers["Authorization"] = "Bearer " + llm.api_key
    explicit_endpoint = settings.get("experimental_realtime_ws_base_url")
    base_url = explicit_endpoint or llm.base_url
    configured = bool(base_url and (explicit_endpoint or llm.wire_api in {"chat", "responses"})
        and (configured_provider or explicit_endpoint))
    if llm.aws is not None and not explicit_endpoint:
        configured = False
    return RealtimeConfig(base_url=base_url, headers=headers if credentials else {}, proxy_mode=llm.proxy_mode,
        version=realtime.get("version", "v2"), model=settings.get("experimental_realtime_ws_model"),
        voice=realtime.get("voice"), session_type=realtime.get("type", "conversational"),
        call_base_url=settings.get("experimental_realtime_webrtc_call_base_url") or llm.base_url, enabled=enabled,
        prompt=settings.get("experimental_realtime_ws_backend_prompt") or "", configured=configured,
        startup_context=settings.get("experimental_realtime_ws_startup_context"),
        start_instructions=settings.get("experimental_realtime_start_instructions"), network_policy=config.application_network_policy)


def thread_runtime(session, conversation_id: str):
    if session.ws_manager is None:
        raise ValueError("thread realtime requires a thread-owned execution manager")
    return session.ws_manager.execution_contexts.thread_runtime(session.conversation_repo.store_instance_id(), conversation_id)


def startup_context(conversation) -> str:
    # Source's startup-context allowance is 5,300 estimated tokens. Keep the
    # actual retained messages in source order within the same UTF-8 budget.
    remaining = 5300 * 4
    selected = []
    for message in reversed(conversation.context_snapshot.get("history", [])):
        content = message.get("content", "")
        if not isinstance(content, str) or not content:
            continue
        rendered = f"{message['role']}: {content}"
        encoded = rendered.encode("utf-8")
        if len(encoded) > remaining:
            break
        selected.append(rendered)
        remaining -= len(encoded)
    return "\n\n".join(reversed(selected))


async def start_runtime(session, params: Start, *, desktop_voice=None):
    conversation_id = params.thread_id
    conversation = session.conversation_repo.get_conversation(conversation_id)
    if conversation is None or conversation.archived:
        raise ValueError("The realtime thread does not exist or is archived")
    owner = thread_runtime(session, conversation_id)
    if owner.realtime_runtime is not None:
        await owner.realtime_runtime.aclose()
    config = capture_config(session, conversation_id, credentials=True)
    if not config.configured:
        raise ValueError("The selected provider has no configured realtime endpoint/authentication")
    session.ws_manager.load_conversation_execution(conversation_id, session)

    async def emit(payload):
        # The service belongs to this thread across window navigation and WS
        # reattachment. Media stays live; timeline completion is durable below.
        with session.event_outbox.bind_client_command("", ""), session.event_outbox.bind_connection_generation(None):
            await session.send_payload(payload, log_context=payload["type"], wait_for_delivery=False)

    async def commit(item):
        kind = item["type"]
        message = {"id": item["id"], "role": item["role"] if kind == "transcriptSegment" else "assistant",
            "content": item["text"] if kind == "transcriptSegment" else "",
            "blocks": [{"type": "realtime", "item": item}], "timestamp": datetime.now(timezone.utc).isoformat()}
        saved = await asyncio.to_thread(session.conversation_repo.upsert_transcript_message, conversation_id, message)
        if saved is None:
            raise ValueError("The realtime thread disappeared before its timeline item was saved")

    async def delegate(prompt, delegation):
        running = session.running_agent_task_for(conversation_id)
        data = {"conversation_id": conversation_id, "content": prompt,
            "assistant_message_id": delegation.assistant_message_id, "user_message_id": delegation.user_message_id}
        if running is not None:
            state = session._conversation_streams[conversation_id]
            data.update(streaming_behavior="steer", expected_turn_id=state["turn_id"])
            delegation.turn_id = state["turn_id"]
        with session.event_outbox.bind_client_command("", ""), session.event_outbox.bind_connection_generation(None):
            await session.command_dispatcher._handle_command(UserCommand(type="user_message", data=data))

    async def record_tail(text, identity):
        from backend.agent.context import ContextBuilder
        async with session._conversation_projection_lock(conversation_id):
            current = await asyncio.to_thread(session.conversation_repo.get_conversation, conversation_id)
            if current is None:
                raise ValueError("The realtime thread disappeared before its transcript context was saved")
            run = session.run_manager.context_for(conversation_id)
            builder = run.context_builder if run is not None else None
            if builder is None:
                builder = ContextBuilder(conversation_id=conversation_id,
                    workspace_root=session.session_lifecycle.workspace_root_for_conversation(current))
                builder.load_snapshot(current.context_snapshot)
                if run is not None:
                    run.realtime_history_fragments.append((identity, text))
            builder.append_realtime_transcript_tail(text, identity)
            committed = await asyncio.to_thread(session.conversation_repo.commit_turn_projection,
                conversation_id, assistant_message=None, context_snapshot=builder.export_snapshot(), expected_revision=current.revision)
            if committed is None:
                raise ValueError("The realtime thread disappeared before its transcript context was saved")
            if run is not None:
                run.conversation_revision = committed.revision

    def activity_changed():
        session.ws_manager.note_execution_activity(conversation_id)

    service = RealtimeRuntime(config, params, emit=emit, commit=commit, delegate=delegate, record_tail=record_tail,
        track_admission=lambda task: session.command_dispatcher.track_execution_command_task(task, conversation_id=conversation_id),
        startup_context=config.startup_context if config.startup_context is not None else startup_context(conversation),
        active_changed=activity_changed)
    if desktop_voice is not None:
        from backend.realtime.desktop_client import DesktopVoiceClient
        service.desktop_client = DesktopVoiceClient(session, desktop_voice)
    owner.realtime_runtime = service
    service.launch()
    return service
