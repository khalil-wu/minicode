from __future__ import annotations

import subprocess
from functools import partial
from pathlib import Path
from typing import Any

from backend.async_cleanup import to_thread_cancel_safe
from backend.services.code_review_service import code_review_options, prepare_code_review
from backend.ws.command_scope import resolve_command_scope
from backend.ws.command_results import emit_command_error


async def handle_code_review(session: Any, data: dict[str, Any], *, command: str) -> bool:
    try:
        owner = resolve_command_scope(session, data, require_conversation=False, require_workspace=True)
        cwd = Path(owner.workspace_root)
        if command == "review.options":
            payload = await to_thread_cancel_safe(code_review_options, cwd)
        else:
            payload = await to_thread_cancel_safe(prepare_code_review, cwd, data["mode"], data.get("base_branch", ""), data.get("merge_base_sha", ""), data.get("request_message", ""))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        await emit_command_error(session, command, getattr(exc, "stderr", None) or str(exc))
        return True
    await session.emit_command_result(command, "Code review request prepared.", data={**payload, "conversation_id": owner.conversation_id})
    return True


HANDLERS = {command: partial(handle_code_review, command=command) for command in ("review.options", "review.prepare")}
