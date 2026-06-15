"""
Agent run logic mixin extracted from ws/handler.py.

SessionAgentRunnerMixin provides the _run_agent method which orchestrates
LLM refresh, query engine submission, cost tracking, transcript persistence,
and conversation summary/facts updates.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.query_engine import QuerySubmission
from backend.agent.turn_state import AgentTurnState
from backend.config import get_available_models, get_llm_provider, load_config
from backend.llm.errors import classify_llm_error, sanitize_llm_error_message
from backend.ws.conversation_errors import emit_conversation_not_found
from backend.ws.utils import (
    build_conversation_summary,
    build_effective_user_message,
    extract_turn_facts,
    merge_conversation_facts,
)
from backend.ws.stream_state import (
    create_stream_state,
    remove_pending_tool_call,
    upsert_pending_tool_call,
)

_FAILED_TOOL_STATUSES = {"error", "failed", "blocked"}


def _contains_cjk(text: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff" for char in text)


def _failed_tool_call_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if str(record.get("status") or "").strip().lower() in _FAILED_TOOL_STATUSES
    ]


def _tool_record_detail(record: dict[str, Any], *, fallback: str) -> str:
    detail = (
        str(record.get("summary") or "").strip()
        or str(record.get("displaySummary") or "").strip()
        or str(record.get("contentPreview") or "").strip()
        or str(record.get("outputPreview") or "").strip()
        or str(record.get("inputSummary") or "").strip()
        or fallback
    )
    if len(detail) > 700:
        detail = detail[:700].rstrip() + "..."
    return detail


def _format_failed_tool_only_reply(
    records: list[dict[str, Any]],
    *,
    user_message: str,
    failure_message: str = "",
) -> str:
    failed = _failed_tool_call_records(records)
    if not failed:
        return ""
    if _contains_cjk(user_message):
        intro = (
            "\u5de5\u5177\u8c03\u7528\u5931\u8d25\uff0c\u800c\u4e14\u6a21\u578b\u6ca1\u6709\u751f\u6210\u6700\u7ec8\u56de\u590d\u3002"
            "\u8fd9\u8f6e\u4e0d\u80fd\u5f53\u4f5c\u6210\u529f\u5b8c\u6210\uff0c\u5931\u8d25\u70b9\u5982\u4e0b\uff1a"
        )
        failure_label = "\u8fd0\u884c\u9519\u8bef"
        no_details = "\u5de5\u5177\u672a\u8fd4\u56de\u53ef\u7528\u7684\u5931\u8d25\u7ec6\u8282\u3002"
    else:
        intro = (
            "Tool calls failed and the model did not produce a final reply. "
            "This turn cannot be treated as completed; here is what failed:"
        )
        failure_label = "Run error"
        no_details = "The tool did not return usable failure details."

    parts = [intro]
    failure_detail = failure_message.strip()
    if failure_detail:
        parts.append(f"{failure_label}: {failure_detail}")
    for index, record in enumerate(failed[-3:], start=1):
        name = str(record.get("name") or "tool")
        status = str(record.get("status") or "failed")
        detail = _tool_record_detail(record, fallback=no_details)
        parts.append(f"{index}. {name} [{status}]\n{detail}")
    return "\n\n".join(parts)


def _format_tool_activity_without_final_reply(
    records: list[dict[str, Any]],
    *,
    user_message: str,
    failure_message: str = "",
) -> str:
    if not records:
        return ""
    failed_reply = _format_failed_tool_only_reply(
        records,
        user_message=user_message,
        failure_message=failure_message,
    )
    if failed_reply:
        return failed_reply

    if _contains_cjk(user_message):
        intro = (
            "\u5de5\u5177\u5df2\u7ecf\u8fd4\u56de\u7ed3\u679c\uff0c\u4f46\u6a21\u578b\u6ca1\u6709\u751f\u6210\u6700\u7ec8\u56de\u590d\u3002"
            "\u8fd9\u8f6e\u4e0d\u80fd\u5f53\u4f5c\u6210\u529f\u5b8c\u6210\uff1b\u5df2\u4fdd\u7559\u7684\u5de5\u5177\u7ed3\u679c\u5982\u4e0b\uff1a"
        )
        failure_label = "\u8fd0\u884c\u9519\u8bef"
        no_details = "\u5de5\u5177\u672a\u8fd4\u56de\u53ef\u7528\u7684\u6458\u8981\u3002"
    else:
        intro = (
            "Tool calls completed, but the model did not produce a final reply. "
            "This turn cannot be treated as completed; the preserved tool results are:"
        )
        failure_label = "Run error"
        no_details = "The tool did not return a usable summary."

    parts = [intro]
    failure_detail = failure_message.strip()
    if failure_detail:
        parts.append(f"{failure_label}: {failure_detail}")
    for index, record in enumerate(records[-3:], start=1):
        name = str(record.get("name") or "tool")
        status = str(record.get("status") or "completed")
        detail = _tool_record_detail(record, fallback=no_details)
        parts.append(f"{index}. {name} [{status}]\n{detail}")
    return "\n\n".join(parts)


class SessionAgentRunnerMixin:
    """Agent run logic for WebSocketSession.

    Depends on session attributes: ws, query_engine, conversation_repo,
    context_builder, permission_checker, permission_context, config,
    llm, artifact_store, tool_registry, skill_manager, vector_memory,
    _approval_handler, _active_task_id, _interrupted, etc.
    """

    async def _run_agent(
        self,
        user_message: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        target_conversation_id = conversation_id or self.active_conversation_id or ""
        if not target_conversation_id:
            self._ensure_active_conversation()
            target_conversation_id = self.active_conversation_id or ""
        locks = getattr(self, "_conversation_run_locks", None)
        lock = None
        if isinstance(locks, dict) and target_conversation_id:
            lock = locks.setdefault(target_conversation_id, asyncio.Lock())
        async with (lock or self._agent_run_lock):
            await self._run_agent_locked(
                user_message,
                attachments=attachments,
                conversation_id=target_conversation_id or None,
                metadata=metadata,
            )

    async def _run_agent_locked(
        self,
        user_message: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._interrupted = False
        run_interrupted = False
        # Hot-reload any MCP tools connected since this session was created, so
        # this run sees them. Rebuilds a new registry object before the run
        # captures self.tool_registry below; in-flight runs keep their own.
        self.refresh_tool_registry_if_mcp_changed()
        target_conversation_id = conversation_id or self.active_conversation_id or ""
        if not target_conversation_id:
            self._ensure_active_conversation()
            target_conversation_id = self.active_conversation_id or ""
        conversation = self.conversation_repo.get_conversation(target_conversation_id)
        if conversation is None:
            await emit_conversation_not_found(self, target_conversation_id)
            return
        is_active_conversation_run = conversation.id == self.active_conversation_id

        # Track active streaming metadata for reconnection recovery
        assistant_message_id = f"assistant_{uuid.uuid4().hex[:8]}"
        stream_state = create_stream_state(conversation.id, assistant_message_id)
        getattr(self, "_conversation_streams", {})[conversation.id] = stream_state

        # Each run uses a local LLM/provider/model/config snapshot to avoid overwriting
        # session fields that concurrent runs might be reading
        try:
            from backend.llm.model_registry import create_session_llm

            run_config = load_config()
            provider_resolver = getattr(self, "_resolve_llm_provider", get_llm_provider)
            models_resolver = getattr(self, "_resolve_available_models", get_available_models)
            run_provider = provider_resolver()
            run_available_models = list(models_resolver(run_provider))

            # Determine model for this run
            config_model = getattr(run_config.llm, "model", "").strip()
            if run_provider != self.provider:
                # Provider changed: use config model, not any previous override
                run_model = config_model
            elif not self._model_override_active:
                # No override: track config changes
                run_model = config_model
            else:
                # Override active: keep using selected_model
                run_model = self.selected_model

            if run_available_models and run_model and run_model not in run_available_models:
                run_model = ""
            if not run_model and run_available_models:
                run_model = run_available_models[0]

            run_llm = create_session_llm(run_config, model_override=run_model or None)

            # Only update session fields if this is the active conversation run
            # This keeps the UI in sync without breaking concurrent background runs
            if is_active_conversation_run:
                self.config = run_config
                self.provider = run_provider
                self.available_models = run_available_models
                self.selected_model = run_model
                self.llm = run_llm
                self.context_builder._llm = run_llm
        except Exception as exc:
            classification = classify_llm_error(exc)
            await self._send_event(
                AgentEvent(
                    type="error",
                    data={
                        "message": sanitize_llm_error_message(exc, classification),
                        "recoverable": not classification.fatal,
                        "error_type": classification.error_type,
                        "provider_error_type": classification.provider_error_type,
                        "conversation_id": conversation.id,
                    },
                )
            )
            getattr(self, "_conversation_streams", {}).pop(conversation.id, None)
            return

        run_context_builder = ContextBuilder(
            token_budget=run_config.token_budget,
            agent_settings=run_config.agent,
            skill_executor=getattr(self, "skill_executor", None),
            rag_pipeline=getattr(self, "rag_pipeline", None),
            memory_manager=getattr(self, "memory_manager", None),
            llm=run_llm,
            skill_manager=self.skill_manager,
            vector_memory=self.vector_memory,
        )
        run_context_builder.load_snapshot(conversation.context_snapshot or {})
        normalized_attachments = list(attachments or [])
        effective_user_message = build_effective_user_message(user_message, normalized_attachments)
        run_metadata = dict(metadata or {})
        run_workspace_root = self._workspace_root_for_conversation(conversation)
        run_workspace_context = self._workspace_context_for_conversation(conversation)
        run_metadata.setdefault("workspace_context", run_workspace_context)
        run_metadata.setdefault("requires_explicit_workspace", True)

        # 回填压缩摘要为持久备忘，保证模型轮次即使丢掉 snapshot 也能读到高层结论
        compaction_summary = (conversation.compaction_summary or "").strip()
        if compaction_summary:
            existing_notes = getattr(run_context_builder, "_persistent_notes", [])
            # Replace existing compaction_summary note (keep only the latest)
            existing_notes[:] = [
                note for note in existing_notes
                if note.get("kind") != "compaction_summary"
            ]
            existing_notes.append(
                {
                    "kind": "compaction_summary",
                    "title": "Compacted conversation memory",
                    "content": compaction_summary,
                }
            )

        self.conversation_repo.append_transcript_message(
            conversation.id,
            {
                "id": f"user_{uuid.uuid4().hex[:8]}",
                "role": "user",
                "content": user_message,
                "timestamp": datetime.now(UTC).isoformat(),
                "attachments": normalized_attachments,
            },
        )

        from backend.llm.cost_tracker import CostTracker
        tracker = CostTracker.get_instance()
        start_time = time.monotonic()

        # Attach workspace context to agent state so ContextBuilder can inject it
        from backend.agent.state import AgentState
        agent_state = AgentState(user_message=effective_user_message)
        agent_state.workspace_context = run_workspace_context
        agent_state.attachments = normalized_attachments
        agent_state.checkpoint_manager = getattr(self, "checkpoint_manager", None)
        agent_state.conversation_id = conversation.id
        goal = dict(getattr(conversation, "goal", {}) or {})
        goal_text = str(goal.get("text") or "").strip()
        if goal_text:
            goal_status = str(goal.get("status") or "active").strip().lower()
            if goal_status == "paused":
                agent_state.task_summary = (
                    f"Conversation goal is paused: {goal_text}\n"
                    "Do not proactively continue this goal unless the user asks to resume or work on it."
                )
            else:
                agent_state.task_summary = (
                    f"Current conversation goal: {goal_text}\n"
                    "Treat user turns as progress toward this goal unless the user clearly changes direction."
                )
        self._last_agent_state = agent_state

        conv_id = conversation.id

        async def _stream_callback(line: str, stream: str = "stdout") -> None:
            await self._send_ws_payload(
                {
                    "type": "command_output_chunk",
                    "conversation_id": conv_id,
                    "content": line,
                    "stream": stream if stream in {"stdout", "stderr"} else "stdout",
                },
                log_context="command_output_chunk",
            )

        async def _emit_runtime_event(event_type: str, data: dict[str, Any]) -> None:
            payload = dict(data)
            payload.setdefault("conversation_id", conversation.id)
            await self._send_event(AgentEvent(type=event_type, data=payload))

        assistant_artifacts: list[dict[str, Any]] = []
        usage_payload: dict[str, int] | None = None
        run_failed_message = ""
        assistant_message_id = str(stream_state.get("message_id") or assistant_message_id)

        def _now_ms() -> int:
            return int(time.time() * 1000)

        turn_state = AgentTurnState(now_ms=_now_ms)
        synthesized_no_final_reply = False

        async def _maybe_emit_source_citation(data: dict[str, Any]) -> None:
            citation = turn_state.record_source_citation(data)
            if citation is None:
                return
            await self._send_event(AgentEvent(type="citation.add", data={
                "conversation_id": conversation.id,
                "message_id": assistant_message_id,
                **citation,
            }))

        async def _emit_no_final_reply_summary_if_needed() -> None:
            nonlocal run_failed_message, synthesized_no_final_reply
            if synthesized_no_final_reply or run_failed_message or turn_state.content().strip():
                return
            tool_records = turn_state.tool_call_records()
            if not tool_records or _failed_tool_call_records(tool_records):
                return
            fallback_reply = _format_tool_activity_without_final_reply(
                tool_records,
                user_message=user_message,
                failure_message=run_failed_message,
            )
            if not fallback_reply:
                return
            synthesized_no_final_reply = True
            turn_state.append_text(fallback_reply)
            for fallback_event in (
                AgentEvent.final_answer_delta(fallback_reply),
                AgentEvent.final_answer_committed(fallback_reply),
            ):
                fallback_event.data["conversation_id"] = conversation.id
                await self._send_event(fallback_event)
            if not run_failed_message:
                run_failed_message = "Tool calls completed but the model did not produce a final reply."
                turn_state.record_error({"message": run_failed_message})
                error_event = AgentEvent.error(
                    run_failed_message,
                    recoverable=True,
                    error_type="api",
                )
                error_event.data["conversation_id"] = conversation.id
                await self._send_event(error_event)

        try:
            async for event in self.query_engine.submit_filtered(
                QuerySubmission(
                    user_message=effective_user_message,
                    llm=run_llm,
                    tool_registry=self.tool_registry,
                    artifact_store=self.artifact_store,
                    permission_checker=self.permission_checker,
                    agent_settings=run_config.agent,
                    token_budget=run_config.token_budget,
                    context_builder=run_context_builder,
                    approval_handler=self._approval_handler,
                    skill_manager=self.skill_manager,
                    vector_memory=self.vector_memory,
                    state=agent_state,
                    permission_context=self.permission_context,
                    workspace_root=run_workspace_root,
                    session_id=self.session_id,
                    task_id=getattr(self, "_conversation_run_task_ids", {}).get(conversation.id, self._active_task_id or ""),
                    task_manager=self.task_manager,
                    background_manager=getattr(self, "background_manager", None),
                    terminal_manager=getattr(self, "terminal_manager", None),
                    emit_event=_emit_runtime_event,
                    metadata=run_metadata,
                    stream_callback=_stream_callback,
                )
            ):
                if event.type == "context_compacted":
                    self.conversation_repo.update_compaction(
                        conversation.id,
                        "compacted",
                        str(event.data.get("summary", "")),
                    )
                    await self._send_ws_payload(
                        {
                            "type": "conversation.compaction.updated",
                            "conversation_id": conversation.id,
                            "state": "compacted",
                            "summary": event.data.get("summary", ""),
                        },
                        log_context="conversation.compaction.updated",
                    )
                    event.data.setdefault("conversation_id", conversation.id)
                    await self._send_event(event)
                    continue

                if event.type == "text_chunk":
                    if event.data.get("image_data"):
                        image_data = str(event.data.get("image_data") or "").strip()
                        media_type = str(event.data.get("media_type") or "image/png").strip() or "image/png"
                        if image_data:
                            artifact_id = self.artifact_store.save(
                                image_data,
                                source="generated_image",
                                type="image",
                                preview_lines=1,
                            )
                            artifact = {
                                "artifact_id": artifact_id,
                                "artifactId": artifact_id,
                                "kind": "image",
                                "summary": "Generated image",
                                "bytes": len(image_data),
                                "media_type": media_type,
                                "mediaType": media_type,
                                "url": f"data:{media_type};base64,{image_data}",
                            }
                            assistant_artifacts.append(artifact)
                            await self._send_ws_payload(
                                {
                                    "type": "artifact.preview",
                                    "conversation_id": conv_id,
                                    "artifact_id": artifact_id,
                                    "kind": "image",
                                    "summary": "Generated image",
                                    "bytes": len(image_data),
                                    "media_type": media_type,
                                    "url": f"data:{media_type};base64,{image_data}",
                                },
                                log_context="artifact.preview",
                            )
                    turn_state.append_text(str(event.data.get("content", "")))
                elif event.type == "image_chunk":
                    image_data = str(event.data.get("image_data") or "").strip()
                    media_type = str(event.data.get("media_type") or "image/png").strip() or "image/png"
                    if image_data:
                        artifact_id = self.artifact_store.save(
                            image_data,
                            source="generated_image",
                            type="image",
                            preview_lines=1,
                        )
                        artifact = {
                            "artifact_id": artifact_id,
                            "artifactId": artifact_id,
                            "kind": "image",
                            "summary": "Generated image",
                            "bytes": len(image_data),
                            "media_type": media_type,
                            "mediaType": media_type,
                            "url": f"data:{media_type};base64,{image_data}",
                        }
                        assistant_artifacts.append(artifact)
                        await self._send_ws_payload(
                            {
                                "type": "artifact.preview",
                                "conversation_id": conv_id,
                                "artifact_id": artifact_id,
                                "kind": "image",
                                "summary": "Generated image",
                                "bytes": len(image_data),
                                "media_type": media_type,
                                "url": f"data:{media_type};base64,{image_data}",
                            },
                            log_context="artifact.preview",
                        )
                elif event.type == "final_answer_started":
                    pass  # no-op: streaming state is managed by final_answer_delta
                elif event.type == "final_answer_delta":
                    turn_state.append_text(str(event.data.get("content", "")))
                elif event.type == "final_answer_retracted":
                    turn_state.clear_text()
                elif event.type == "final_answer_committed":
                    # Fallback: if streaming was interrupted, use committed content
                    turn_state.commit_text(str(event.data.get("content", "")))
                elif event.type in {"thinking_delta", "thinking"}:
                    thinking_chunk = str(event.data.get("content", ""))
                    thinking_metadata = {
                        key: event.data[key]
                        for key in ("source", "visibility", "is_raw_provider_reasoning")
                        if key in event.data
                    }
                    turn_state.append_thinking(thinking_chunk, thinking_metadata)
                elif event.type == "tool_call":
                    record = turn_state.record_tool_call(event.data)
                    if record is not None:
                        tool_id = str(record.get("id") or "")
                        upsert_pending_tool_call(
                            stream_state,
                            tool_id,
                            record,
                        )
                elif event.type == "tool_output_delta":
                    # Preserve incremental tool output so restored transcripts keep command previews.
                    turn_state.record_tool_output_delta(event.data)
                elif event.type == "tool_result":
                    tool_id = str(event.data.get("id") or "").strip()
                    if tool_id:
                        remove_pending_tool_call(stream_state, tool_id)
                        turn_state.record_tool_result(event.data)
                    await _maybe_emit_source_citation(event.data)
                elif event.type == "agent.progress":
                    turn_state.record_progress(event.data)
                elif event.type == "done":
                    usage_payload = turn_state.record_done(event.data)
                    tracker.record_usage(
                        input_tokens=usage_payload.get("input_tokens", 0),
                        output_tokens=usage_payload.get("output_tokens", 0),
                        cache_creation_input_tokens=usage_payload.get("cache_creation_input_tokens", 0),
                        cache_read_input_tokens=usage_payload.get("cache_read_input_tokens", 0),
                        elapsed_sec=time.monotonic() - start_time,
                        model_id=getattr(run_llm, "_model", None) or getattr(getattr(run_llm, "_settings", None), "model", None),
                    )
                    await _emit_no_final_reply_summary_if_needed()
                elif event.type == "error":
                    run_failed_message = turn_state.record_error(event.data)

                event.data.setdefault("conversation_id", conversation.id)
                await self._send_event(event)
        except asyncio.CancelledError:
            run_interrupted = True
            self._interrupted = True
            await self._send_event(
                AgentEvent(
                    type="error",
                    data={
                        "message": "The user interrupted the current run",
                        "recoverable": True,
                        "error_type": "budget",
                        "conversation_id": conversation.id,
                    },
                )
            )
            done_event = AgentEvent.done()
            done_event.data["conversation_id"] = conversation.id
            await self._send_event(done_event)
        except Exception as exc:
            run_failed_message = f"Chat run failed: {exc}"
            await self._send_event(
                AgentEvent(
                    type="error",
                    data={
                        "message": run_failed_message,
                        "recoverable": True,
                        "error_type": "api",
                        "conversation_id": conversation.id,
                    },
                )
            )
            done_event = AgentEvent.done()
            done_event.data["conversation_id"] = conversation.id
            await self._send_event(done_event)
        finally:
            # Clear streaming metadata
            getattr(self, "_conversation_streams", {}).pop(conversation.id, None)

            terminal_status = "failed" if run_interrupted or run_failed_message else "completed"
            turn_snapshot = turn_state.finalize(terminal_status=terminal_status)
            assistant_blocks = turn_snapshot.blocks
            assistant_citations = turn_snapshot.citations
            if not usage_payload:
                usage_payload = turn_snapshot.usage
            assistant_content = turn_snapshot.content

            assistant_tool_calls = turn_snapshot.tool_calls
            failed_tool_only_reply = ""
            if not assistant_content.strip():
                failed_tool_only_reply = _format_failed_tool_only_reply(
                    assistant_tool_calls,
                    user_message=user_message,
                    failure_message=run_failed_message,
                )
            if failed_tool_only_reply and not run_failed_message:
                run_failed_message = "Tool calls failed before the assistant produced a reply."
                terminal_status = "failed"
                turn_snapshot = turn_state.finalize(terminal_status=terminal_status)
                assistant_blocks = turn_snapshot.blocks
                assistant_citations = turn_snapshot.citations
                assistant_content = turn_snapshot.content
                assistant_tool_calls = turn_snapshot.tool_calls
                failed_tool_only_reply = _format_failed_tool_only_reply(
                    assistant_tool_calls,
                    user_message=user_message,
                    failure_message=run_failed_message,
                ) or failed_tool_only_reply
                if usage_payload is None:
                    await self._send_event(
                        AgentEvent(
                            type="error",
                            data={
                                "message": run_failed_message,
                                "recoverable": True,
                                "error_type": "tool_error",
                                "conversation_id": conversation.id,
                            },
                        )
                    )
                    done_event = AgentEvent.done()
                    done_event.data["conversation_id"] = conversation.id
                    await self._send_event(done_event)
            if failed_tool_only_reply and not assistant_content.strip():
                assistant_content = failed_tool_only_reply
                assistant_blocks = [
                    *assistant_blocks,
                    {"type": "text", "content": assistant_content},
                ]
            if not assistant_content.strip() and assistant_tool_calls:
                no_final_reply = _format_tool_activity_without_final_reply(
                    assistant_tool_calls,
                    user_message=user_message,
                    failure_message=run_failed_message,
                )
                if no_final_reply:
                    if not run_failed_message:
                        run_failed_message = "Tool calls completed but the model did not produce a final reply."
                        terminal_status = "failed"
                    assistant_content = no_final_reply
                    assistant_blocks = [
                        *assistant_blocks,
                        {"type": "text", "content": assistant_content},
                    ]
            if assistant_content or assistant_blocks or assistant_tool_calls or assistant_artifacts:
                assistant_message: dict[str, Any] = {
                    "id": assistant_message_id,
                    "role": "assistant",
                    "content": assistant_content,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "usage": usage_payload or {},
                }
                if assistant_tool_calls:
                    assistant_message["tool_calls"] = assistant_tool_calls
                if assistant_blocks:
                    assistant_message["blocks"] = assistant_blocks
                if assistant_artifacts:
                    assistant_message["artifacts"] = assistant_artifacts
                if assistant_citations:
                    assistant_message["citations"] = assistant_citations

                self.conversation_repo.append_transcript_message(conversation.id, assistant_message)

                if assistant_content:
                    new_summary = build_conversation_summary(
                        user_message=user_message,
                        attachments=normalized_attachments,
                        assistant_content=assistant_content,
                        compaction_summary=conversation.compaction_summary or "",
                    )
                    new_local_facts = merge_conversation_facts(
                        getattr(conversation, "local_facts", []),
                        extract_turn_facts(
                            conversation_id=conversation.id,
                            user_message=user_message,
                            attachments=normalized_attachments,
                            assistant_content=assistant_content,
                        ),
                    )
                    self.conversation_repo.update_summary(conversation.id, new_summary)
                    updated_conversation = self.conversation_repo.update_facts(
                        conversation.id,
                        local_facts=new_local_facts,
                    ) or self.conversation_repo.get_conversation(conversation.id)
                    await self._send_ws_payload(
                        {
                            "type": "conversation.summary.updated",
                            "conversation_id": conversation.id,
                            "summary": new_summary,
                            "title": getattr(updated_conversation, "title", conversation.title),
                            "updated_at": getattr(updated_conversation, "updated_at", conversation.updated_at),
                        },
                        log_context="conversation.summary.updated",
                    )

            saved_snapshot = run_context_builder.export_snapshot()
            self.conversation_repo.save_context_snapshot(conversation.id, saved_snapshot)
            if conversation.id == self.active_conversation_id:
                self._load_active_conversation_snapshot(conversation.id, saved_snapshot)
