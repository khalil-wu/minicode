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
from urllib.parse import urlparse
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.agent.query_engine import QuerySubmission
from backend.agent.run_events import normalize_agent_event, run_event_to_agent_event
from backend.config import get_available_models, get_llm_provider, load_config
from backend.ws.utils import (
    build_conversation_summary,
    build_effective_user_message,
    extract_turn_facts,
    merge_conversation_facts,
)

if TYPE_CHECKING:
    pass


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
    ) -> None:
        async with self._agent_run_lock:
            await self._run_agent_locked(user_message, attachments=attachments)

    async def _run_agent_locked(
        self,
        user_message: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        self._interrupted = False
        if not self.active_conversation_id:
            self._ensure_active_conversation()
        conversation = self.active_conversation
        if conversation is None:
            return
        normalized_attachments = list(attachments or [])
        effective_user_message = build_effective_user_message(user_message, normalized_attachments)

        # Track active streaming metadata for reconnection recovery
        self._streaming_conversation_id = self.active_conversation_id
        self._streaming_message_id = f"assistant_{uuid.uuid4().hex[:8]}"
        self._streaming_accumulated_text = ""

        try:
            from backend.llm.model_registry import create_session_llm

            previous_provider = self.provider
            self.config = load_config()
            self.provider = get_llm_provider()
            config_model = getattr(self.config.llm, "model", "").strip()
            self.available_models = get_available_models(self.provider)
            if self.provider != previous_provider:
                self._model_override_active = False
                self.selected_model = config_model
            elif not self._model_override_active:
                self.selected_model = config_model
            if self.available_models and self.selected_model and self.selected_model not in self.available_models:
                self.selected_model = ""
            if not self.selected_model and self.available_models:
                self.selected_model = self.available_models[0]

            self.llm = create_session_llm(self.config, model_override=self.selected_model or None)
            self.context_builder._llm = self.llm
        except Exception as exc:
            await self._send_event(
                AgentEvent.error(f"LLM 初始化失败: {exc}", recoverable=False, error_type="api")
            )
            return

        # 回填压缩摘要为持久备忘，保证模型轮次即使丢掉 snapshot 也能读到高层结论
        compaction_summary = (conversation.compaction_summary or "").strip()
        if compaction_summary:
            existing_notes = getattr(self.context_builder, "_persistent_notes", [])
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
        agent_state.workspace_context = getattr(self, '_workspace_context', None)
        agent_state.attachments = normalized_attachments
        agent_state.checkpoint_manager = getattr(self, "checkpoint_manager", None)
        agent_state.conversation_id = conversation.id
        self._last_agent_state = agent_state

        conv_id = self.active_conversation_id or ""

        async def _stream_callback(line: str) -> None:
            await self._send_ws_payload(
                {
                    "type": "command_output_chunk",
                    "conversation_id": conv_id,
                    "content": line,
                },
                log_context="command_output_chunk",
            )

        async def _emit_runtime_event(event_type: str, data: dict[str, Any]) -> None:
            await self._send_event(AgentEvent(type=event_type, data=data))

        assistant_blocks: list[dict[str, Any]] = []
        assistant_artifacts: list[dict[str, Any]] = []
        assistant_citations: list[dict[str, Any]] = []
        seen_citation_urls: set[str] = set()
        usage_payload: dict[str, int] | None = None
        run_failed_message = ""
        assistant_message_id = self._streaming_message_id or f"assistant_{uuid.uuid4().hex[:8]}"

        def _now_ms() -> int:
            return int(time.time() * 1000)

        def _append_text_block(content: str) -> None:
            if not content:
                return
            if assistant_blocks and assistant_blocks[-1].get("type") == "text":
                assistant_blocks[-1]["content"] = f"{assistant_blocks[-1].get('content', '')}{content}"
            else:
                assistant_blocks.append({"type": "text", "content": content})

        def _append_thinking_block(content: str) -> None:
            if not content:
                return
            if assistant_blocks and assistant_blocks[-1].get("type") == "thinking":
                assistant_blocks[-1]["content"] = f"{assistant_blocks[-1].get('content', '')}{content}"
            else:
                assistant_blocks.append({"type": "thinking", "content": content})

        def _replace_tool_call_record(record: dict[str, Any]) -> None:
            for block in assistant_blocks:
                if (
                    block.get("type") == "tool_call"
                    and isinstance(block.get("record"), dict)
                    and block["record"].get("id") == record.get("id")
                ):
                    block["record"] = record
                    return
            assistant_blocks.append({"type": "tool_call", "record": record})

        def _find_tool_call_record(tool_id: str) -> dict[str, Any] | None:
            for block in assistant_blocks:
                if (
                    block.get("type") == "tool_call"
                    and isinstance(block.get("record"), dict)
                    and block["record"].get("id") == tool_id
                ):
                    return block["record"]
            return None

        def _extract_tool_call_records() -> list[dict[str, Any]]:
            records: list[dict[str, Any]] = []
            for block in assistant_blocks:
                if block.get("type") == "tool_call" and isinstance(block.get("record"), dict):
                    records.append(dict(block["record"]))
            return records

        def _extract_text_content() -> str:
            return "".join(
                str(block.get("content") or "")
                for block in assistant_blocks
                if block.get("type") == "text"
            )

        def _source_label(url: str) -> str:
            host = urlparse(url).netloc.lower().removeprefix("www.")
            return host or url

        async def _maybe_emit_source_citation(data: dict[str, Any]) -> None:
            if bool(data.get("is_error")):
                return
            if str(data.get("evidence_type") or "").strip() != "fetched":
                return
            if str(data.get("extraction_status") or "").strip() == "failed":
                return
            source_url = str(data.get("source_url") or "").strip()
            if not source_url or source_url in seen_citation_urls:
                return
            seen_citation_urls.add(source_url)
            label = _source_label(source_url)
            citation = {
                "source": source_url,
                "url": source_url,
                "label": label,
                "title": _source_label(source_url),
                "range": (0, 0),
            }
            assistant_citations.append(citation)
            await self._send_event(AgentEvent(type="citation.add", data={
                "message_id": assistant_message_id,
                **citation,
            }))

        def _upsert_progress_block(progress: dict[str, Any]) -> None:
            progress_id = str(progress.get("id") or "").strip()
            if not progress_id:
                return
            for index, block in enumerate(assistant_blocks):
                if block.get("type") == "progress" and block.get("id") == progress_id:
                    assistant_blocks[index] = progress
                    return
            assistant_blocks.append(progress)

        try:
            async for event in self.query_engine.submit(
                QuerySubmission(
                    user_message=effective_user_message,
                    llm=self.llm,
                    tool_registry=self.tool_registry,
                    artifact_store=self.artifact_store,
                    permission_checker=self.permission_checker,
                    agent_settings=self.config.agent,
                    token_budget=self.config.token_budget,
                    context_builder=self.context_builder,
                    approval_handler=self._approval_handler,
                    skill_manager=self.skill_manager,
                    vector_memory=self.vector_memory,
                    state=agent_state,
                    permission_context=self.permission_context,
                    workspace_root=self._current_workspace_root(),
                    session_id=self.session_id,
                    task_id=self._active_task_id or "",
                    task_manager=self.task_manager,
                    background_manager=getattr(self, "background_manager", None),
                    emit_event=_emit_runtime_event,
                    metadata={"workspace_context": getattr(self, "_workspace_context", None)},
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
                    continue

                run_event = normalize_agent_event(event)
                if run_event is None:
                    continue

                if run_event.type == "message.delta":
                    if run_event.data.get("image_data"):
                        image_data = str(run_event.data.get("image_data") or "").strip()
                        media_type = str(run_event.data.get("media_type") or "image/png").strip() or "image/png"
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
                    _append_text_block(str(run_event.data.get("content", "")))
                elif run_event.type == "reasoning.delta":
                    thinking_chunk = str(run_event.data.get("content", ""))
                    _append_thinking_block(thinking_chunk)
                elif run_event.type == "tool.started":
                    tool_id = str(run_event.data.get("id") or "").strip()
                    tool_name = str(run_event.data.get("name") or "").strip()
                    tool_args = run_event.data.get("args") if isinstance(run_event.data.get("args"), dict) else {}
                    if tool_id and tool_name:
                        _replace_tool_call_record(
                            {
                                "id": tool_id,
                                "name": tool_name,
                                "args": tool_args,
                                "status": str(run_event.data.get("status") or "running"),
                                "startedAt": int(run_event.data.get("started_at") or _now_ms()),
                                "displayHint": str(run_event.data.get("display_hint") or ""),
                                "inputSummary": str(run_event.data.get("input_summary") or ""),
                            }
                        )
                elif run_event.type == "tool.completed":
                    tool_id = str(run_event.data.get("id") or "").strip()
                    if tool_id:
                        existing_record = _find_tool_call_record(tool_id)
                        if existing_record is not None:
                            updated_record = dict(existing_record)
                            updated_record["status"] = (
                                str(run_event.data.get("status") or "")
                                or ("failed" if bool(run_event.data.get("is_error")) else "success")
                            )
                            updated_record["summary"] = str(run_event.data.get("summary") or "")
                            for source_key, target_key in (
                                ("display_summary", "displaySummary"),
                                ("result_kind", "resultKind"),
                                ("limitation", "limitation"),
                            ):
                                if run_event.data.get(source_key):
                                    updated_record[target_key] = str(run_event.data.get(source_key) or "")
                            if run_event.data.get("duration_ms") is not None:
                                updated_record["durationMs"] = int(run_event.data.get("duration_ms") or 0)
                            if run_event.data.get("artifact_id"):
                                updated_record["artifactId"] = run_event.data.get("artifact_id")
                            if run_event.data.get("diff") is not None:
                                updated_record["diff"] = run_event.data.get("diff")
                            for source_key, target_key in (
                                ("source_url", "sourceUrl"),
                                ("extraction_status", "extractionStatus"),
                                ("content_preview", "contentPreview"),
                                ("evidence_type", "evidenceType"),
                            ):
                                if run_event.data.get(source_key):
                                    updated_record[target_key] = run_event.data.get(source_key)
                            updated_record["finishedAt"] = _now_ms()
                            _replace_tool_call_record(updated_record)
                    await _maybe_emit_source_citation(run_event.data)
                elif run_event.type == "status":
                    message = str(run_event.data.get("message") or "").strip()
                    progress_id = str(
                        run_event.data.get("id") or f"{run_event.data.get('stage', 'status')}:{message}"
                    ).strip()
                    if progress_id and message:
                        progress_block: dict[str, Any] = {
                            "type": "progress",
                            "id": progress_id,
                            "stage": str(run_event.data.get("stage") or "status"),
                            "status": str(run_event.data.get("status") or "info"),
                            "message": message,
                            "timestamp": _now_ms(),
                        }
                        for source_key, target_key in (
                            ("phase", "phase"),
                            ("label", "label"),
                            ("summary", "summary"),
                            ("visibility", "visibility"),
                            ("group_id", "groupId"),
                            ("step_id", "stepId"),
                        ):
                            if run_event.data.get(source_key):
                                progress_block[target_key] = str(run_event.data.get(source_key) or "")
                        if run_event.data.get("detail"):
                            progress_block["detail"] = str(run_event.data.get("detail") or "")
                        if run_event.data.get("count") is not None:
                            progress_block["count"] = int(run_event.data.get("count") or 0)
                        _upsert_progress_block(progress_block)
                elif run_event.type == "turn.completed":
                    usage_payload = dict(run_event.data.get("usage") or {})
                    tracker.record_usage(
                        input_tokens=usage_payload.get("input_tokens", 0),
                        output_tokens=usage_payload.get("output_tokens", 0),
                        cache_creation_input_tokens=usage_payload.get("cache_creation_input_tokens", 0),
                        cache_read_input_tokens=usage_payload.get("cache_read_input_tokens", 0),
                        elapsed_sec=time.monotonic() - start_time,
                        model_id=getattr(self.llm, "_model", None) or getattr(getattr(self.llm, "_settings", None), "model", None),
                    )

                await self._send_event(run_event_to_agent_event(run_event))
        except asyncio.CancelledError:
            self._interrupted = True
            await self._send_event(
                AgentEvent.error(
                    "The user interrupted the current run",
                    recoverable=True,
                    error_type="budget",
                )
            )
            await self._send_event(AgentEvent.done())
        except Exception as exc:
            run_failed_message = f"Chat run failed: {exc}"
            await self._send_event(
                AgentEvent.error(
                    run_failed_message,
                    recoverable=True,
                    error_type="api",
                )
            )
            await self._send_event(AgentEvent.done())
        finally:
            # Clear streaming metadata
            self._streaming_conversation_id = None
            self._streaming_message_id = None
            self._streaming_accumulated_text = ""

            terminal_status = "failed" if self._interrupted or run_failed_message else "completed"
            if assistant_blocks:
                normalized_blocks: list[dict[str, Any]] = []
                for block in assistant_blocks:
                    next_block = dict(block)
                    if next_block.get("type") == "progress" and next_block.get("status") == "running":
                        next_block["status"] = terminal_status
                        next_block["timestamp"] = _now_ms()
                    elif next_block.get("type") == "tool_call" and isinstance(next_block.get("record"), dict):
                        record = dict(next_block["record"])
                        if record.get("status") == "running":
                            record["status"] = "failed" if terminal_status == "failed" else "success"
                            record["finishedAt"] = _now_ms()
                        next_block["record"] = record
                    normalized_blocks.append(next_block)
                assistant_blocks = normalized_blocks

            assistant_content = _extract_text_content()
            if run_failed_message and not assistant_content.strip():
                assistant_content = f"Error: {run_failed_message}"

            assistant_tool_calls = _extract_tool_call_records()
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
                    self.conversation_repo.update_facts(
                        conversation.id,
                        local_facts=new_local_facts,
                    )
                    await self._send_ws_payload(
                        {
                            "type": "conversation.summary.updated",
                            "conversation_id": conversation.id,
                            "summary": new_summary,
                        },
                        log_context="conversation.summary.updated",
                    )

            self.conversation_repo.save_context_snapshot(
                conversation.id,
                self.context_builder.export_snapshot(),
            )
