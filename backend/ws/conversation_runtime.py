from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from backend.agent.context import ContextBuilder
from backend.async_cleanup import CANCELLATION_DRAIN_TIMEOUT_SECONDS, cancel_and_drain
from backend.conversations.repository import ConversationRepository
from backend.memory.pollution import pollution_sources_from_transcript

logger = logging.getLogger(__name__)

SNAPSHOT_PARTIAL_HISTORY_COUNT = 20
UI_AGENT_STATE_SNAPSHOT_KEY = "ui_agent_state"


class ConversationRuntime:
    """Owns conversation persistence, hydration, and retry rewind behavior."""

    def __init__(
        self,
        *,
        conversation_repo: ConversationRepository,
        context_builder: ContextBuilder,
        build_effective_transcript_content: Callable[[dict[str, Any]], str],
        build_summary_from_transcript: Callable[..., str],
    ) -> None:
        self._conversation_repo = conversation_repo
        self._context_builder = context_builder
        self._build_effective_transcript_content = build_effective_transcript_content
        self._build_summary_from_transcript = build_summary_from_transcript

        self.active_conversation_id: str | None = None
        self._hydration_task: asyncio.Task[None] | None = None
        self._retired_hydration_tasks: set[asyncio.Task[None]] = set()
        self._hydration_generation = 0
        self._pending_hydration: tuple[
            str,
            int,
            list[dict[str, Any]],
            bool,
            Callable[[str], Any] | None,
        ] | None = None

    @property
    def active_conversation(self) -> Any | None:
        if not self.active_conversation_id:
            return None
        return self._conversation_repo.get_conversation(self.active_conversation_id)

    def create_fresh_active_conversation(self) -> None:
        created = self._conversation_repo.create_conversation()
        self.active_conversation_id = created.id
        self.load_active_conversation_snapshot(created.id, created.context_snapshot)

    def ensure_active_conversation(self, preferred_id: str | None = None) -> None:
        """确保有活跃对话，优先使用已存在的对话"""
        active: Any | None = None

        if self.active_conversation_id and not preferred_id:
            current = self._conversation_repo.get_conversation(self.active_conversation_id)
            if current is not None and not current.archived and getattr(current, "conversation_type", "main") == "main":
                self.load_active_conversation_snapshot(current.id, current.context_snapshot)
                return

        if not preferred_id:
            active = self._conversation_repo.create_conversation()
            self.active_conversation_id = active.id
            self.load_active_conversation_snapshot(active.id, active.context_snapshot)
            return

        # 1. 优先使用 preferred_id
        if preferred_id:
            candidate = self._conversation_repo.get_conversation(preferred_id)
            if candidate is not None and not candidate.archived and getattr(candidate, "conversation_type", "main") == "main":
                active = candidate

        # 2. 如果preferred_id无效，创建新会话（不再自动加载历史会话）
        if active is None:
            active = self._conversation_repo.create_conversation()

        self.active_conversation_id = active.id
        self.load_active_conversation_snapshot(active.id, active.context_snapshot)

    def load_active_conversation_snapshot(
        self,
        conversation_id: str,
        snapshot: dict[str, Any] | None,
        *,
        notify: bool = False,
        on_hydration_complete: Callable[[str], Any] | None = None,
        defer_start: bool = False,
    ) -> bool:
        snapshot = self._restore_plan_snapshot(conversation_id, snapshot)
        if self._hydration_task and not self._hydration_task.done():
            previous = self._hydration_task
            previous.cancel()
            self._retired_hydration_tasks.add(previous)
            previous.add_done_callback(self._retired_hydration_tasks.discard)

        self._hydration_generation += 1
        generation = self._hydration_generation
        self._pending_hydration = None
        pending_history = self._context_builder.load_snapshot_partial(
            snapshot,
            recent_history_count=SNAPSHOT_PARTIAL_HISTORY_COUNT,
        )
        if not pending_history:
            self._hydration_task = None
            return False

        if defer_start:
            self._pending_hydration = (
                conversation_id,
                generation,
                pending_history,
                notify,
                on_hydration_complete,
            )
            self._hydration_task = None
            return True

        task = self._create_hydration_task(
            conversation_id=conversation_id,
            generation=generation,
            pending_history=pending_history,
            notify=notify,
            on_hydration_complete=on_hydration_complete,
        )
        self._hydration_task = task

        def clear_current(completed: asyncio.Task[None]) -> None:
            if self._hydration_task is completed:
                self._hydration_task = None

        task.add_done_callback(clear_current)
        return True

    def start_hydration(self, conversation_id: str) -> bool:
        pending = self._pending_hydration
        if pending is None or pending[0] != conversation_id:
            return False
        self._pending_hydration = None
        pending_id, generation, history, notify, callback = pending
        task = self._create_hydration_task(
            conversation_id=pending_id,
            generation=generation,
            pending_history=history,
            notify=notify,
            on_hydration_complete=callback,
        )
        self._hydration_task = task

        def clear_current(completed: asyncio.Task[None]) -> None:
            if self._hydration_task is completed:
                self._hydration_task = None

        task.add_done_callback(clear_current)
        return True

    def _create_hydration_task(
        self,
        *,
        conversation_id: str,
        generation: int,
        pending_history: list[dict[str, Any]],
        notify: bool,
        on_hydration_complete: Callable[[str], Any] | None,
    ) -> asyncio.Task[None]:
        return asyncio.create_task(
            self._hydrate_snapshot(
                conversation_id=conversation_id,
                generation=generation,
                pending_history=pending_history,
                notify=notify,
                on_hydration_complete=on_hydration_complete,
            )
        )

    async def wait_for_hydration(self, conversation_id: str) -> None:
        """Wait until the active conversation has its complete history.

        Partial snapshot loading is a UI optimization only.  A provider turn
        must never observe the recent-only prefix while the older entries are
        still being decoded in the background.
        """

        requested_id = str(conversation_id or "").strip()
        if not requested_id or requested_id != self.active_conversation_id:
            return
        task = self._hydration_task
        if task is None and self._pending_hydration is not None:
            self.start_hydration(requested_id)
            task = self._hydration_task
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            # Conversation switching cancels the old generation.  The new
            # conversation's task, if any, is installed before its next turn.
            if requested_id != self.active_conversation_id:
                return
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Conversation history hydration failed for {requested_id}"
            ) from exc

    def _restore_plan_snapshot(
        self,
        conversation_id: str,
        snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Rebind/recover MiniCode's conversation-owned Plan before hydration."""

        normalized = dict(snapshot or {})
        conversation = self._conversation_repo.get_conversation(conversation_id)
        if conversation is None:
            return normalized
        from backend.agent.plans import ensure_plan_file_for_resume

        before = dict(normalized)
        ensure_plan_file_for_resume(
            normalized,
            list(getattr(conversation, "transcript", []) or []),
            getattr(conversation, "workspace_root", "") or None,
        )
        if normalized != before:
            self._conversation_repo.save_context_snapshot(conversation_id, normalized)
        return normalized

    async def shutdown(self) -> bool:
        """Cancel hydration while retaining every task until bounded drain."""

        tasks = set(self._retired_hydration_tasks)
        current = self._hydration_task
        if current is not None:
            tasks.add(current)
        still_pending = await cancel_and_drain(
            tasks,
            timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
            label="conversation hydration",
        )
        self._retired_hydration_tasks = set(still_pending)
        if current is not None and current not in still_pending:
            self._hydration_task = None
        return not still_pending

    async def _hydrate_snapshot(
        self,
        *,
        conversation_id: str,
        generation: int,
        pending_history: list[dict[str, Any]],
        notify: bool,
        on_hydration_complete: Callable[[str], Any] | None,
    ) -> None:
        try:
            parsed_history = await asyncio.to_thread(
                ContextBuilder.deserialize_snapshot_history,
                pending_history,
            )
        except asyncio.CancelledError:
            return
        except Exception:
            # MiniCode's session hydration skips entries line-by-line but surfaces
            # file-level failures; silently returning here would drop the
            # entire pre-recent history without a trace.
            logger.exception(
                "Failed to hydrate conversation history snapshot for %s",
                conversation_id,
            )
            raise

        if generation != self._hydration_generation or conversation_id != self.active_conversation_id:
            return

        self._context_builder.prepend_history_messages(parsed_history)
        if notify and on_hydration_complete is not None:
            await on_hydration_complete(conversation_id)

    def rebuild_context_from_transcript(
        self,
        conversation: Any,
        transcript: list[dict[str, Any]],
    ) -> dict[str, Any]:
        previous_snapshot = conversation.context_snapshot or {}

        def _tool_call_entry(call: dict[str, Any]) -> dict[str, Any] | None:
            call_id = str(call.get("id") or "").strip()
            call_name = str(call.get("name") or "").strip()
            if not call_id or not call_name:
                return None
            arguments = call.get("args")
            if not isinstance(arguments, dict):
                arguments = call.get("arguments")
            return {
                "id": call_id,
                "name": call_name,
                "arguments": dict(arguments) if isinstance(arguments, dict) else {},
            }

        history: list[dict[str, Any]] = []
        for message in transcript:
            role = str(message.get("role", "")).strip()
            if role not in {"user", "assistant"}:
                continue
            entry = {
                "role": role,
                "content": self._build_effective_transcript_content(message),
                "name": None,
                "tool_call_id": None,
                "tool_calls": [],
            }
            if role == "assistant":
                parsed_calls: list[dict[str, Any]] = []
                tool_results: list[dict[str, Any]] = []
                for raw_call in message.get("tool_calls") or []:
                    if not isinstance(raw_call, dict):
                        continue
                    parsed = _tool_call_entry(raw_call)
                    if parsed is None:
                        continue
                    parsed_calls.append(parsed)
                    result_text = str(
                        raw_call.get("summary")
                        or raw_call.get("outputPreview")
                        or raw_call.get("result")
                        or ""
                    ).strip()
                    tool_results.append(
                        {
                            "role": "tool",
                            "content": result_text or "(tool result omitted by rewind)",
                            "name": None,
                            "tool_call_id": parsed["id"],
                            "tool_calls": [],
                        }
                    )
                if parsed_calls:
                    entry["tool_calls"] = parsed_calls
                    history.append(entry)
                    # Pair each assistant tool_call with its tool-role result
                    # so the rebuilt provider history stays structurally valid
                    # (cc rewinds keep tool_use/tool_result pairs intact).
                    history.extend(tool_results)
                    continue
            # Ordinary user/assistant messages are just as authoritative as
            # tool-call groups.  The rewind path previously appended only the
            # assistant branch above, which silently discarded every normal
            # exchange from the rebuilt provider context.
            history.append(entry)
        snapshot = {
            "history": history,
            "persistent_notes": list(previous_snapshot.get("persistent_notes", [])),
            "compaction_count": int(previous_snapshot.get("compaction_count", 0) or 0),
        }
        if UI_AGENT_STATE_SNAPSHOT_KEY in previous_snapshot:
            snapshot[UI_AGENT_STATE_SNAPSHOT_KEY] = previous_snapshot[UI_AGENT_STATE_SNAPSHOT_KEY]
        # Rebuild only replaces model history. Conversation/session ownership
        # metadata (including MiniCode's Plan slug/reference) survives rewinds.
        for key, value in previous_snapshot.items():
            if key not in snapshot and key not in {"history", "context_ledger"}:
                snapshot[key] = value
        from backend.agent.plans import ensure_plan_file_for_resume

        ensure_plan_file_for_resume(
            snapshot,
            transcript,
            getattr(conversation, "workspace_root", "") or None,
        )
        self._context_builder.load_snapshot(snapshot)
        # A persisted ledger is a projection of the exact history above, not
        # durable ownership metadata.  Reusing the pre-rewind ledger makes the
        # UI report the removed tail (or ``--`` after a reload), so regenerate
        # it only after ContextBuilder has accepted the rebuilt snapshot.
        snapshot["context_ledger"] = self._context_builder.context_ledger()
        return snapshot

    def rewind_to_user_turn(
        self,
        *,
        conversation: Any,
        retry_from_message_id: str,
    ) -> dict[str, Any] | None:
        target_id = retry_from_message_id.strip()
        if not target_id:
            return None

        transcript = list(conversation.transcript or [])
        retry_index = next(
            (
                index
                for index, message in enumerate(transcript)
                if str(message.get("id", "")).strip() == target_id
                and str(message.get("role", "")).strip() == "user"
            ),
            -1,
        )
        if retry_index == -1:
            return None

        trimmed_transcript = transcript[:retry_index]
        pollution_sources = pollution_sources_from_transcript(trimmed_transcript)
        snapshot = self.rebuild_context_from_transcript(conversation, trimmed_transcript)
        self._conversation_repo.replace_transcript(conversation.id, trimmed_transcript)
        self._conversation_repo.update_summary(
            conversation.id,
            self._build_summary_from_transcript(
                trimmed_transcript,
                compaction_summary=conversation.compaction_summary or "",
            ),
        )
        self._conversation_repo.set_memory_pollution(
            conversation.id,
            pollution_sources,
        )
        self._conversation_repo.save_context_snapshot(conversation.id, snapshot)
        return self._conversation_repo.get_conversation(conversation.id)
