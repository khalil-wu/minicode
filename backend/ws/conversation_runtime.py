from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from backend.agent.context import ContextBuilder
from backend.conversations.repository import ConversationRepository

SNAPSHOT_PARTIAL_HISTORY_COUNT = 20


class ConversationRuntime:
    """Owns conversation persistence, hydration, and retry rewind behavior."""

    def __init__(
        self,
        *,
        conversation_repo: ConversationRepository,
        context_builder: ContextBuilder,
        load_profile_memory: Callable[[], str],
        inherit_fact: Callable[..., dict[str, Any] | None],
        merge_facts: Callable[..., list[dict[str, Any]]],
        build_summary_from_facts: Callable[[list[dict[str, Any]], str], str],
        build_inherited_memory_note: Callable[[list[dict[str, Any]], str], str],
        build_effective_transcript_content: Callable[[dict[str, Any]], str],
        build_summary_from_transcript: Callable[..., str],
        rebuild_local_facts_from_transcript: Callable[[str, list[dict[str, Any]]], list[dict[str, Any]]],
    ) -> None:
        self._conversation_repo = conversation_repo
        self._context_builder = context_builder
        self._load_profile_memory = load_profile_memory
        self._inherit_fact = inherit_fact
        self._merge_facts = merge_facts
        self._build_summary_from_facts = build_summary_from_facts
        self._build_inherited_memory_note = build_inherited_memory_note
        self._build_effective_transcript_content = build_effective_transcript_content
        self._build_summary_from_transcript = build_summary_from_transcript
        self._rebuild_local_facts_from_transcript = rebuild_local_facts_from_transcript

        self.active_conversation_id: str | None = None
        self._hydration_task: asyncio.Task[None] | None = None
        self._hydration_generation = 0

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
            if current is not None and not current.archived:
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
            if candidate is not None and not candidate.archived:
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
    ) -> bool:
        if self._hydration_task and not self._hydration_task.done():
            self._hydration_task.cancel()

        self._hydration_generation += 1
        generation = self._hydration_generation
        pending_history = self._context_builder.load_snapshot_partial(
            snapshot,
            recent_history_count=SNAPSHOT_PARTIAL_HISTORY_COUNT,
        )
        if not pending_history:
            self._hydration_task = None
            return False

        self._hydration_task = asyncio.create_task(
            self._hydrate_snapshot(
                conversation_id=conversation_id,
                generation=generation,
                pending_history=pending_history,
                notify=notify,
                on_hydration_complete=on_hydration_complete,
            )
        )
        return True

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
            return

        if generation != self._hydration_generation or conversation_id != self.active_conversation_id:
            return

        self._context_builder.prepend_history_messages(parsed_history)
        if notify and on_hydration_complete is not None:
            await on_hydration_complete(conversation_id)

    def build_inherited_snapshot(
        self,
        memory_mode: str,
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        notes: list[dict[str, str]] = []
        summary = ""
        inherited_facts: list[dict[str, Any]] = []

        if memory_mode == "summary":
            current = self.active_conversation
            if current:
                promoted_inherited = [
                    promoted
                    for promoted in (
                        self._inherit_fact(fact, source_conversation_id=current.id)
                        for fact in getattr(current, "inherited_facts", [])
                    )
                    if promoted is not None
                ]
                promoted_local = [
                    promoted
                    for promoted in (
                        self._inherit_fact(fact, source_conversation_id=current.id)
                        for fact in getattr(current, "local_facts", [])
                    )
                    if promoted is not None
                ]
                inherited_facts = self._merge_facts(promoted_inherited, promoted_local)
                summary = self._build_summary_from_facts(inherited_facts, current.summary.strip())
            if current and (inherited_facts or current.summary.strip()):
                notes.append(
                    {
                        "kind": "summary",
                        "title": "Inherited conversation memory",
                        "content": self._build_inherited_memory_note(
                            inherited_facts,
                            current.summary.strip(),
                        ),
                    }
                )
        elif memory_mode == "profile":
            profile_content = self._load_profile_memory()
            if profile_content:
                notes.append(
                    {
                        "kind": "profile",
                        "title": "Inherited user profile",
                        "content": profile_content,
                    }
                )

        return summary, {"history": [], "persistent_notes": notes, "compaction_count": 0}, inherited_facts

    def rebuild_context_from_transcript(
        self,
        conversation: Any,
        transcript: list[dict[str, Any]],
    ) -> dict[str, Any]:
        previous_snapshot = conversation.context_snapshot or {}
        snapshot = {
            "history": [
                {
                    "role": str(message.get("role", "user")),
                    "content": self._build_effective_transcript_content(message),
                    "name": None,
                    "tool_call_id": None,
                    "tool_calls": [],
                }
                for message in transcript
                if str(message.get("role", "")).strip() in {"user", "assistant"}
            ],
            "persistent_notes": list(previous_snapshot.get("persistent_notes", [])),
            "compaction_count": int(previous_snapshot.get("compaction_count", 0) or 0),
        }
        self._context_builder.load_snapshot(snapshot)
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
        snapshot = self.rebuild_context_from_transcript(conversation, trimmed_transcript)
        self._conversation_repo.replace_transcript(conversation.id, trimmed_transcript)
        self._conversation_repo.update_summary(
            conversation.id,
            self._build_summary_from_transcript(
                trimmed_transcript,
                compaction_summary=conversation.compaction_summary or "",
            ),
        )
        self._conversation_repo.update_facts(
            conversation.id,
            local_facts=self._rebuild_local_facts_from_transcript(conversation.id, trimmed_transcript),
        )
        self._conversation_repo.save_context_snapshot(conversation.id, snapshot)
        return self._conversation_repo.get_conversation(conversation.id)
