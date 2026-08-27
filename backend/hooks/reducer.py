from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from backend.hooks.dispatcher import HookExecution
from backend.hooks.policy import SuccessfulOutput, event_policy

logger = logging.getLogger(__name__)

_DEFAULT_CONTEXT_TOKEN_LIMIT = 2_500
_CHARS_PER_APPROX_TOKEN = 4


@dataclass
class HookReduction:
    blocked: bool = False
    failed: bool = False
    message: str = ""
    feedback: str = ""
    stdout: str = ""
    errors: tuple[str, ...] = ()
    permission_decision: str = ""
    permission_decision_reason: str = ""
    additional_context: str = ""
    custom_instructions: str = ""
    updated_input: Any = None
    updated_mcp_tool_output: Any = None
    retry: bool = False
    prevent_continuation: bool = False
    stop_reason: str = ""
    system_message: str = ""
    initial_user_message: str = ""
    watch_paths: tuple[str, ...] = ()
    worktree_path: str = ""
    elicitation_action: str = ""
    elicitation_content: Any = None
    run_summaries: tuple[dict[str, Any], ...] = ()


def reduce_hook_executions(
    event: Any,
    executions: Iterable[HookExecution],
    *,
    expected_event_name: str,
    blocking_allowed: bool = True,
) -> HookReduction:
    event_key = str(getattr(event, "value", event) or "")
    policy = event_policy(event_key)
    if policy.fire_and_forget:
        return HookReduction()

    outputs: list[str] = []
    feedback_parts: list[str] = []
    context_parts: list[str] = []
    compact_instructions: list[str] = []
    errors: list[str] = []
    system_messages: list[str] = []
    watch_paths: list[str] = []
    summaries: list[dict[str, Any]] = []
    permission_decision = ""
    permission_reason = ""
    decision_rank = {"": 0, "allow": 1, "ask": 2, "deny": 3}
    blocked = False
    block_message = ""
    failed = False
    updated_input: Any = None
    updated_mcp_tool_output: Any = None
    retry = False
    prevent_continuation = False
    stop_reason = ""
    initial_user_message = ""
    worktree_path = ""
    elicitation_action = ""
    elicitation_content: Any = None

    for execution in executions:
        stdout = execution.stdout.strip()
        stderr = execution.stderr.strip()
        exit_code = execution.exit_code
        source_value = str(getattr(execution.entry, "source", "") or "").casefold()
        execution_blocking_allowed = not (
            event_key == "config_change"
            and source_value in {
                "policy",
                "managed_requirements",
                "enterprise_managed",
                "mdm",
                "legacy_managed_config_file",
                "legacy_managed_config_mdm",
            }
        )
        if stdout:
            outputs.append(stdout)
        summaries.append(
            {
                "key": execution.entry.entry_id,
                "source": execution.entry.source,
                "source_path": execution.entry.source_path,
                "display_order": execution.entry.display_order,
                "configured_order": execution.configured_order,
                "completion_order": execution.completion_order,
                "duration_ms": execution.duration_ms,
                "exit_code": exit_code,
                "status": "succeeded" if exit_code == 0 else "failed",
                "status_message": execution.entry.status_message,
                **({"runtime_error": True} if execution.execution_failed else {}),
            }
        )

        if execution.execution_failed:
            # A launcher/runtime failure is visible but does not silently block
            # the operation. Only exit 2 or a valid JSON block/deny decision can
            # stop the current lifecycle event.
            error = stderr or stdout or "Hook execution failed"
            errors.append(error)
            if event_key == "worktree_create":
                failed = True
            continue

        json_result = _parse_json_object(stdout)
        event_matches = True
        if json_result is not None:
            actual_event = str(json_result.get("event") or "")
            event_matches = not actual_event or actual_event == expected_event_name
            if not event_matches:
                errors.append(
                    f"Hook returned {actual_event!r}; expected {expected_event_name!r}"
                )
            if event_matches and json_result.get("continue") is False and not policy.ignore_blocking and execution_blocking_allowed:
                prevent_continuation = True
                stop_reason = str(json_result.get("stop_reason") or "")
            if event_matches and json_result.get("system_message"):
                system_messages.append(str(json_result["system_message"]))

            decision, reason, candidate_input = (
                _permission_fields(json_result) if event_matches else ("", "", None)
            )
            common_decision = (
                str(json_result.get("decision") or "").strip().lower()
                if event_matches
                else ""
            )
            if common_decision == "approve" and not decision:
                decision = "allow"
            elif common_decision == "block" and policy.json_can_block and execution_blocking_allowed:
                decision = "deny"
                reason = str(json_result.get("reason") or reason or "")
            if candidate_input is not None:
                updated_input = candidate_input
            if decision and decision_rank.get(decision, 0) >= decision_rank[permission_decision]:
                permission_decision = decision
                permission_reason = reason
            if decision == "deny" and policy.json_can_block and not policy.ignore_blocking and execution_blocking_allowed:
                blocked = True
                block_message = reason or "Hook denied permission"

            top_level_context = json_result.get("additional_context")
            if (
                event_matches
                and policy.additional_context
                and isinstance(top_level_context, str)
                and top_level_context.strip()
            ):
                context_parts.append(
                    limit_additional_context(top_level_context.strip(), execution.entry)
                )

            if event_matches:
                if event_key == "session_start" and json_result.get("initial_user_message"):
                    initial_user_message = str(json_result["initial_user_message"])
                if event_key in {"session_start", "cwd_changed", "file_changed"}:
                    raw_paths = json_result.get("watch_paths")
                    if isinstance(raw_paths, list):
                        watch_paths.extend(
                            str(path)
                            for path in raw_paths
                            if isinstance(path, str) and path.strip()
                        )
                if event_key == "post_tool_use" and "updated_mcp_tool_output" in json_result:
                    updated_mcp_tool_output = json_result["updated_mcp_tool_output"]
                if event_key == "permission_denied" and json_result.get("retry") is True:
                    retry = True
                if event_key in {"elicitation", "elicitation_result"}:
                    action = str(json_result.get("action") or "").strip().lower()
                    if action in {"accept", "decline", "cancel"}:
                        elicitation_action = action
                        elicitation_content = json_result.get("content")
                        if action == "decline" and policy.json_can_block and execution_blocking_allowed:
                            blocked = True
                            block_message = str(
                                json_result.get("reason")
                                or "Elicitation denied by hook"
                            )
                if event_key == "worktree_create" and json_result.get("worktree_path"):
                    worktree_path = str(json_result["worktree_path"]).strip()

            if event_matches and json_result.get("feedback"):
                feedback = str(json_result["feedback"]).strip()
                if feedback and feedback not in feedback_parts:
                    feedback_parts.append(feedback)
            if common_decision == "block" and policy.json_can_block and execution_blocking_allowed:
                feedback = str(
                    json_result.get("reason")
                    or json_result.get("feedback")
                    or "Hook blocked event"
                ).strip()
                if feedback and feedback not in feedback_parts:
                    feedback_parts.append(feedback)

        if exit_code == 0 and stdout and json_result is None:
            if policy.successful_output == SuccessfulOutput.MODEL:
                context_parts.append(limit_additional_context(stdout, execution.entry))
            elif policy.successful_output == SuccessfulOutput.COMPACT_INSTRUCTIONS:
                compact_instructions.append(stdout)
            elif policy.successful_output == SuccessfulOutput.USER:
                system_messages.append(stdout)
            elif policy.successful_output == SuccessfulOutput.WORKTREE_PATH and not worktree_path:
                worktree_path = stdout

        if (
            event_matches
            and exit_code == 2
            and policy.exit_two_blocks
            and not policy.ignore_blocking
            and execution_blocking_allowed
        ):
            feedback = stderr or stdout or "Hook blocked event"
            blocked = True
            block_message = feedback
            permission_decision = "deny"
            permission_reason = feedback
            if feedback not in feedback_parts:
                feedback_parts.append(feedback)
        elif exit_code != 0:
            error = stderr or stdout or f"Hook exited with status {exit_code}"
            errors.append(error)
            if event_key == "worktree_create":
                failed = True

    if prevent_continuation and event_key != "stop" and not policy.ignore_blocking:
        blocked = True
        block_message = stop_reason or block_message or "Hook prevented continuation"
    if not blocking_allowed or policy.ignore_blocking:
        blocked = False
        prevent_continuation = False
        stop_reason = ""
        if permission_decision == "deny":
            permission_decision = ""
            permission_reason = ""
    if event_key == "worktree_create" and not worktree_path:
        failed = True
        if not block_message:
            block_message = errors[0] if errors else "WorktreeCreate hook produced no path"

    return HookReduction(
        blocked=blocked,
        failed=failed,
        message=block_message,
        feedback="\n".join(feedback_parts),
        stdout="\n".join(outputs),
        errors=tuple(errors),
        permission_decision=("deny" if blocked else permission_decision),
        permission_decision_reason=permission_reason,
        additional_context="\n\n".join(context_parts),
        custom_instructions="\n\n".join(compact_instructions),
        updated_input=updated_input,
        updated_mcp_tool_output=updated_mcp_tool_output,
        retry=retry,
        prevent_continuation=prevent_continuation,
        stop_reason=stop_reason,
        system_message="\n".join(system_messages),
        initial_user_message=initial_user_message,
        watch_paths=tuple(dict.fromkeys(watch_paths)),
        worktree_path=worktree_path,
        elicitation_action=elicitation_action,
        elicitation_content=elicitation_content,
        run_summaries=tuple(summaries),
    )


def execution_succeeded_for_once(
    event: Any,
    execution: HookExecution,
    *,
    expected_event_name: str,
) -> bool:
    """Consume a once hook only after a semantically valid success."""
    if execution.exit_code != 0:
        return False
    payload = _parse_json_object(execution.stdout)
    if payload is None:
        return True
    actual = str(payload.get("event") or "")
    if actual and actual != expected_event_name:
        return False
    if str(getattr(event, "value", event) or "") == "worktree_create":
        path = payload.get("worktree_path")
        return bool(str(path or "").strip())
    return True


def _parse_json_object(value: str) -> dict[str, Any] | None:
    stripped = value.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _permission_fields(
    payload: dict[str, Any],
) -> tuple[str, str, Any]:
    return (
        _normalize_permission(payload.get("permission_decision")),
        str(payload.get("permission_decision_reason") or ""),
        payload.get("updated_input"),
    )


def _normalize_permission(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"allow", "deny", "ask"} else ""


def limit_additional_context(value: str, entry: Any) -> str:
    """Apply the hook/model context boundary independently of transport capture."""

    limit = getattr(entry, "additional_context_limit", None)
    if limit is None:
        limit = _DEFAULT_CONTEXT_TOKEN_LIMIT
    try:
        token_limit = max(0, int(limit))
    except (TypeError, ValueError, OverflowError):
        token_limit = _DEFAULT_CONTEXT_TOKEN_LIMIT
    if token_limit == 0:
        return value
    char_limit = max(1, token_limit * _CHARS_PER_APPROX_TOKEN)
    if len(value) <= char_limit:
        return value
    omitted = len(value) - char_limit
    return f"{value[:char_limit]}\n\n[Hook output truncated: {omitted} characters omitted.]"
