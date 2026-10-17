from __future__ import annotations

from functools import partial
from typing import Any

from backend.services.code_review_service import code_review_options_on_executor, prepare_code_review_on_executor
from backend.ws.command_scope import resolve_command_scope


async def handle_code_review(session: Any, data: dict[str, Any], *, command: str) -> bool:
    from backend.ws.handlers.diff import _git_execution, _emit_git_error

    owner = None
    identity = {}
    try:
        owner = resolve_command_scope(session, data, require_conversation=False, require_workspace=True, allow_executor_uri=True)
        async with _git_execution(session, owner, data, identity) as service:
            if command == "review.options":
                payload = await code_review_options_on_executor(owner.workspace_root, service)
            else:
                payload = await prepare_code_review_on_executor(owner.workspace_root, service, data["mode"],
                    data.get("base_branch", ""), data.get("merge_base_sha", ""), data.get("request_message", ""))
    except Exception as error:
        await _emit_git_error(session, command, error, scope=owner, identity=identity)
        return True
    await session.emit_command_result(command, "Code review request prepared.", level="success", data=owner.apply({**payload, **identity}))
    return True


HANDLERS = {command: partial(handle_code_review, command=command) for command in ("review.options", "review.prepare")}
