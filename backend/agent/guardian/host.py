"""Conversation owner for the Source reviewer pool and original evidence."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable
from dataclasses import replace
from copy import deepcopy
import json

from backend.agent.guardian.manual_approval import MANUAL_APPROVAL_DEVELOPER_PREFIX
from backend.agent.guardian.context import GuardianParentCheckpoint
from backend.agent.guardian.retained import (
    MAX_RENDER_TOKENS, GuardianEvidenceOwner, GuardianEvidenceSnapshot, GuardianTranscriptEntry, RetainedMessage, _json_bytes,
)
from backend.agent.guardian.skills import bounded_skill_paths
from backend.agent.guardian.service import GuardianReviewService
from backend.agent.guardian.node_repl import NodeReplReviewEvidence
from backend.agent.guardian.truncation import truncate_guardian_text
from backend.llm.native_compaction import native_compaction_windows

if TYPE_CHECKING:
    from backend.agent.context import ContextBuilder


def _output_text(output) -> str:
    if isinstance(output, str):
        return output
    return "\n".join(part["text"] for part in output if part["type"] in {"input_text", "output_text", "text"})


def live_user_messages(builder: ContextBuilder):
    accepted_sources = _accepted_transcript_sources(builder)
    for message in builder._history:
        if not message.is_user_input:
            windows = native_compaction_windows(message)
            items = [item for window in windows for item in window["output"]]
            for item in items:
                if item["type"] != "message" or item["role"] != "user":
                    continue
                metadata = item.get("internal_chat_message_metadata_passthrough") or {}
                source = accepted_sources.get((item.get("id"), metadata.get("turn_id") or ""))
                if source is None:
                    continue
                content = item["content"]
                single = source.single_text and isinstance(content, list) and len(content) == 1 and content[0]["type"] == "input_text"
                if source.input_origin == "heartbeat":
                    single = single and metadata.get("content_item_kinds") == ["user.heartbeat"]
                origin = "user" if source.input_origin == "heartbeat" and not single else source.input_origin
                yield (source.acceptance_order if not source.inherited else None,
                    RetainedMessage(source.turn_id, source.message_id,
                        truncate_guardian_text(_output_text(content), MAX_RENDER_TOKENS), False, origin))
            continue
        if message.input_origin == "agent":
            continue
        facts = message.accepted_input or {}
        text = builder.admitted_user_text(message)
        origin = "user" if message.input_origin == "heartbeat" and not facts.get("single_text", False) else message.input_origin or "user"
        yield (facts.get("order") if not facts.get("inherited", False) else None,
            RetainedMessage(facts.get("turn_id", ""), facts.get("message_id"),
                truncate_guardian_text(text, MAX_RENDER_TOKENS), False, origin))


def _accepted_transcript_sources(builder):
    return {(entry.model_item_id, entry.turn_id): entry for entry in builder.guardian_evidence.transcript
        if entry.kind == "user" and entry.model_item_id is not None and entry.acceptance_order is not None}


def parent_window_transcript(builder: ContextBuilder) -> tuple[GuardianTranscriptEntry, ...]:
    """Source loaded-checkpoint evidence comes from the current model window.

    The persisted legacy backup stays separate, including for old snapshots
    without a recorded checkpoint cut. No cut or opaque payload is guessed.
    """
    entries = []
    tool_names = {}
    retained = builder.guardian_evidence.retained
    stable_order = len({entry.key for _, entry in retained.ordered_entries()}) == len(retained.ordered_entries())
    accepted_sources = _accepted_transcript_sources(builder)

    def append_text(kind, text, *, message_id=None, turn_id="", protected_assistant=False, facts=None, input_origin="user", model_item_id=None,
        source_allowed=True):
        if text.strip():
            facts = facts or {}
            source = next((entry for entry in retained.user_messages if source_allowed and stable_order and kind == "user" and message_id is not None
                and entry.value.message_id == message_id and entry.value.turn_id == turn_id
                and entry.value.complete and entry.value.text == text), None)
            single_text = facts.get("single_text", False)
            origin = "user" if input_origin == "heartbeat" and not single_text else input_origin
            entries.append(GuardianTranscriptEntry(kind, text, source_order=source.label if source is not None else None,
                source_revision=source.revision if source is not None else None, turn_id=turn_id, message_id=message_id,
                protected_assistant=protected_assistant, input_origin=origin, single_text=single_text,
                acceptance_order=facts.get("order"), inherited=facts.get("inherited", False), model_item_id=model_item_id))

    def append_item(item, *, native_window=False):
        kind = item["type"]
        if kind == "message":
            role = item["role"]
            text = _output_text(item["content"])
            if role not in {"user", "assistant"} and not (role == "developer" and text.startswith(MANUAL_APPROVAL_DEVELOPER_PREFIX)):
                return
            metadata = item.get("internal_chat_message_metadata_passthrough") or {}
            original = accepted_sources.get((item.get("id"), metadata.get("turn_id") or "")) if native_window else None
            single = original is not None and original.single_text and isinstance(item["content"], list) and len(item["content"]) == 1 and item["content"][0]["type"] == "input_text"
            if original is not None and original.input_origin == "heartbeat":
                single = single and metadata.get("content_item_kinds") == ["user.heartbeat"]
            facts = ({"single_text": single, "order": original.acceptance_order,
                "inherited": original.inherited} if original is not None else {})
            append_text(role, text, message_id=original.message_id if original is not None else item.get("id"), turn_id=metadata.get("turn_id") or "",
                protected_assistant=role == "assistant" and item.get("phase") != "commentary", facts=facts,
                input_origin=original.input_origin if original is not None else "user",
                model_item_id=original.model_item_id if original is not None else None, source_allowed=original is not None)
        elif kind == "agent_message":
            if any(part["type"] == "encrypted_content" for part in item["content"]):
                if _json_bytes(item) <= 5000 * 4:
                    entries.append(GuardianTranscriptEntry("assistant", "", provider_item=deepcopy(item)))
                else:
                    append_text("assistant", "Some conversation entries were omitted.")
            else:
                text = _output_text(item["content"])
                if text.strip():
                    append_text("assistant", f"Agent message from {item['author']}:\n{text}")
        elif kind in {"function_call", "custom_tool_call"}:
            name = item["name"]
            tool_names[item["call_id"]] = name
            append_text(f"tool {name} call", item["arguments"] if kind == "function_call" else item["input"],
                message_id=item["call_id"])
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            call_id = item.get("call_id")
            text = _output_text(item["output"])
            name = tool_names.get(call_id)
            if call_id is None:
                name = item.get("name")
                if name is None:
                    return
                if item.get("namespace") is not None:
                    name = f"{item['namespace']}.{name}"
                text = text or "[non-text output]"
            append_text(f"tool {name} result" if name is not None else "tool result", text, message_id=call_id)
        elif kind in {"local_shell_call", "web_search_call"}:
            if item.get("action") is not None:
                name = "shell" if kind == "local_shell_call" else "web_search"
                append_text(f"tool {name} call", json.dumps(item["action"], ensure_ascii=False))

    user_sources = {}
    for message_id, boundary in builder._turn_admissions.items():
        for index in range(boundary["history_start"], boundary["history_end"]):
            if builder._history[index].is_user_input:
                user_sources[index] = (message_id, boundary.get("turn_id", ""))

    for index, message in enumerate(builder._history):
        windows = native_compaction_windows(message)
        if windows:
            for window in windows:
                for item in window["output"]:
                    append_item(item, native_window=True)
            continue
        text = builder._strip_trusted_runtime_context(message)
        native_messages = any(item["type"] in {"message", "agent_message"} for item in message.provider_items)
        if not native_messages and message.role in {"user", "assistant"}:
            facts = message.accepted_input or {}
            message_id, turn_id = ((facts.get("message_id"), facts.get("turn_id", "")) if "order" in facts
                else user_sources.get(index, (None, "")))
            role = "assistant" if message.role == "user" and message.input_origin == "agent" else message.role
            append_text(role, builder.admitted_user_text(message) if message.is_user_input else text, message_id=message_id, turn_id=turn_id,
                protected_assistant=role == "assistant" and message.role == "assistant" and message.phase != "commentary",
                facts=facts, input_origin=message.input_origin or "user", model_item_id=facts.get("model_item_id"))
        elif message.role == "developer" and text.startswith(MANUAL_APPROVAL_DEVELOPER_PREFIX):
            append_text("developer", text)
        elif message.role == "tool":
            body = next((value for value in (message.mcp_output, message.code_mode_output, message.history_notes_output)
                if value is not None), text)
            name = tool_names.get(message.tool_call_id, message.name)
            append_text(f"tool {name} result" if name is not None else "tool result", _output_text(body),
                message_id=message.tool_call_id)
        for item in message.provider_items:
            append_item(item)
        if message.tool_calls:
            native_calls = {item["call_id"] for item in message.provider_items if item["type"] in {"function_call", "custom_tool_call"}}
            for call in message.tool_calls:
                if call.id not in native_calls:
                    tool_names[call.id] = call.name
                    append_text(f"tool {call.name} call", json.dumps(call.arguments, ensure_ascii=False), message_id=call.id)
    return tuple(entries)


def restore_requires_parent_context(owner: GuardianEvidenceOwner, builder: ContextBuilder) -> bool:
    """Source retained facts need the parent window only without a backup."""
    if owner.has_transcript_backup:
        return False
    if not owner.retained.verified_answers_complete() or owner.retained.verified_answers:
        return True
    current = parent_window_transcript(builder)
    for role, records in (("user", owner.retained.user_messages), ("assistant", owner.retained.assistant_messages)):
        for record in records:
            message = record.value
            if not any(entry.kind == role and entry.message_id == message.message_id and entry.turn_id == message.turn_id
                and truncate_guardian_text(entry.text, MAX_RENDER_TOKENS) == message.text for entry in current):
                return True
    return False


def restore_parent_compaction_from_window(owner: GuardianEvidenceOwner, builder: ContextBuilder) -> None:
    """Reconstruct cold opaque state from its issuing window, without capture."""
    window = next((window for message in reversed(builder._history)
        for window in reversed(native_compaction_windows(message))), None)
    item = next((item for item in reversed(window["output"])
        if item["type"] in {"compaction", "context_compaction"}), None) if window is not None else None
    owner.parent_compaction = (GuardianParentCheckpoint(item, window["origin"], window.get("compaction_model_hash"))
        if item is not None else None)


class GuardianHostState:
    def __init__(self, root_thread_id: str, *, root_source: Callable[[], GuardianEvidenceSnapshot] | None = None,
        evidence_checkpoint: dict | None = None):
        self.root_thread_id = root_thread_id
        self.root_source = root_source
        self.builder: ContextBuilder | None = None
        self.node_repl_review_evidence = NodeReplReviewEvidence()
        self._unbound_evidence = (GuardianEvidenceOwner.from_checkpoint(evidence_checkpoint)
            if evidence_checkpoint is not None else GuardianEvidenceOwner())
        self.service = GuardianReviewService(self.snapshot, review_ledger_source=lambda: self.evidence.review_ledger)
        from backend.agent.guardian.async_service import GuardianAsyncService

        self.async_service = GuardianAsyncService(self.snapshot, context_source=self.current_tool_context,
            reviews_source=lambda: self.service.reviews)

    def current_tool_context(self):
        context = self.builder._tool_execution_context if self.builder is not None else None
        if context is None:
            return None
        owner = context.run_context
        snapshot = owner.model_execution if owner is not None and owner.model_execution is not None else context.model_execution
        permission = owner.permission_context_provider() if owner is not None and owner.permission_context_provider is not None else context.permission
        return replace(context, model_execution=snapshot, llm=snapshot.llm if snapshot is not None else context.llm,
            permission=permission)

    def bind(self, builder: ContextBuilder) -> None:
        previous = self.evidence
        incoming = builder.guardian_evidence
        if self.builder is not None and self.builder is not builder:
            current = previous.checkpoint()
            restored = incoming.checkpoint()
            for value in (current, restored):
                value.pop("review_context_revision")
                value["retained_context"].pop("next_order")
            if current == restored:
                # A WS turn creates a fresh model builder, but it borrows the
                # same conversation's reviewer and accepted-input owner.
                builder.guardian_evidence = previous
            else:
                incoming.user_message_revision = max(previous.user_message_revision, incoming.user_message_revision) + 1
                incoming.history_reset_version = max(previous.history_reset_version, incoming.history_reset_version) + 1
                incoming.history_version = max(previous.history_version, incoming.history_version) + 1
        builder.guardian_evidence.retained.next_order = max(previous.retained.next_order, builder.guardian_evidence.retained.next_order)
        self.builder = builder

    @property
    def evidence(self) -> GuardianEvidenceOwner:
        return self.builder.guardian_evidence if self.builder is not None else self._unbound_evidence

    def denied_action(self, review_id: str) -> dict:
        return self.evidence.review_ledger[review_id]

    async def manual_approval(self, review_id: str):
        from backend.agent.guardian.manual_approval import ManualApprovalReceipt

        record = self.evidence.review_ledger[review_id]
        if record["assessment"]["status"] != "denied":
            raise ValueError("Only a denied automatic review can be manually approved")
        if record["history_reset_version"] != self.evidence.history_reset_version:
            raise ValueError("This declined action belongs to history that has been replaced")
        receipt = ManualApprovalReceipt.from_denial(record)
        return receipt

    def commit_manual_approval(self, receipt) -> None:
        record = self.denied_action(receipt.review_id)
        if not record.get("manually_approved", False):
            self.builder.append_guardian_approval(receipt)
            record["manually_approved"] = True

    def snapshot(self) -> GuardianEvidenceSnapshot:
        owner = self.evidence
        root = self.root_source() if self.root_source is not None else None
        context = self.builder._tool_execution_context if self.builder is not None else None
        current_turn_id = (context.turn_id if context is not None else self.builder.latest_admitted_turn_id()
            if self.builder is not None else owner.reconciled_context().latest_user_turn_id)
        skills = bounded_skill_paths((*owner.skills_for_turn(current_turn_id), *(root.trusted_skill_paths if root is not None else ())))
        snapshot = owner.snapshot(root_versions=root.root_versions if root is not None else None,
            root_messages=root.root_messages if root is not None else (), trusted_skill_paths=skills)
        snapshot = replace(snapshot, node_repl=self.node_repl_review_evidence.snapshot_since(0))
        return replace(snapshot, parent_window_transcript=parent_window_transcript(self.builder)) if self.builder is not None else snapshot

    def root_snapshot(self) -> GuardianEvidenceSnapshot:
        if self.root_source is not None:
            return self.root_source()
        owner = self.evidence
        reconciled = owner.reconciled_context(live_user_messages(self.builder) if self.builder is not None else None)
        messages, complete = owner.root_projection(reconciled=reconciled)
        root_versions = (
            self.root_thread_id, owner.history_reset_version, owner.user_message_revision,
            owner.review_context_revision, complete,
        )
        return owner.snapshot(root_versions=root_versions, root_messages=messages,
            trusted_skill_paths=owner.skills_for_turn(reconciled.latest_user_turn_id))

    async def aclose(self) -> None:
        await self.async_service.aclose()
        await self.service.pool.aclose()
