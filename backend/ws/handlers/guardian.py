from __future__ import annotations

import asyncio
from typing import Any

from backend.agent.context import ContextBuilder, clone_context_builder
from backend.agent.guardian.manual_approval import parse_manual_approval_command, require_owned_denial
from backend.async_cleanup import to_thread_cancel_safe
from backend.atomic_io import await_task_despite_cancellation
from backend.conversations.repository import ConversationWriteConflict
from backend.ws.command_results import emit_command_error

COMMAND = "thread/approveGuardianDeniedAction"


async def handle_approve_guardian_denied_action(session: Any, data: dict[str, Any]) -> bool:
    thread_id = ""
    try:
        thread_id, event = parse_manual_approval_command(data)
        if data.get("conversation_id") != thread_id:
            raise ValueError("The manual approval must target its selected conversation")
        async with session._conversation_projection_lock(thread_id):
            current = await to_thread_cancel_safe(session.conversation_repo.get_conversation, thread_id)
            if current is None:
                raise LookupError("The selected conversation was not found")
            host = session.guardian_host_for_conversation(thread_id)
            if host.builder is None:
                builder = ContextBuilder(conversation_id=thread_id,
                    workspace_root=current.execution_cwd or current.worktree_path or current.workspace_root or None)
                builder.load_snapshot(dict(current.context_snapshot or {}))
                host.bind(builder)
            record = host.denied_action(event["id"])
            require_owned_denial(event, record, thread_id)
            receipt = await host.manual_approval(event["id"])
            if not record.get("manually_approved", False):
                candidate = clone_context_builder(host.builder)
                candidate.append_guardian_approval(receipt)
                candidate.guardian_evidence.review_ledger[receipt.review_id]["manually_approved"] = True
                async def commit_approval() -> None:
                    committed = await asyncio.to_thread(session.conversation_repo.commit_turn_projection,
                        thread_id, assistant_message=None, context_snapshot=candidate.export_snapshot(),
                        expected_revision=current.revision)
                    if committed is None:
                        raise LookupError("The selected conversation disappeared before approval was saved")
                    host.commit_manual_approval(receipt)
                await await_task_despite_cancellation(asyncio.create_task(commit_approval()))
        await session.emit_command_result(COMMAND, "", data={"conversation_id": thread_id,
            "review_id": receipt.review_id, "approval_recorded": True})
    except (KeyError, ValueError, LookupError, OSError, ConversationWriteConflict) as exc:
        await emit_command_error(session, COMMAND, exc, data={"conversation_id": thread_id})
    return True


HANDLERS = {
    "thread/approveGuardianDeniedAction": handle_approve_guardian_denied_action,
}
