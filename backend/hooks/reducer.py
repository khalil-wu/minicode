from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from backend.hooks.dispatcher import HookExecution
from backend.hooks.policy import SuccessfulOutput, event_policy

logger = logging.getLogger(__name__)

_DEFAULT_CONTEXT_TOKEN_LIMIT = 2_500
_CHARS_PER_APPROX_TOKEN = 4
_CODEX_EVENTS = {
    "session_start", "session_end", "subagent_start", "subagent_stop",
    "user_prompt_submit", "pre_tool_use", "post_tool_use", "permission_request", "stop",
    "pre_model_request", "post_model_request",
}


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
    additional_context_items: tuple[tuple[str, Any], ...] = ()
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
    if event_key in {"pre_model_request", "post_model_request"}:
        return _reduce_model_observers(executions)
    if event_key in _CODEX_EVENTS:
        return _reduce_codex_executions(event_key, executions, blocking_allowed=blocking_allowed)
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
        json_result = execution.json_output
        suppress_output = bool(
            json_result is not None and json_result.get("suppress_output") is True
        )
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
        if stdout and not suppress_output:
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

        if event_key == "interrupt":
            summaries[-1].update(stdout=stdout, stderr=stderr)
            invalid = bool(stdout) and (
                json_result is None or bool(set(json_result) - {"systemMessage"})
                or json_result.get("systemMessage") is not None
                and not isinstance(json_result["systemMessage"], str)
            )
            if execution.execution_failed or exit_code != 0 or invalid:
                failed = True
                summaries[-1]["status"] = "failed"
                errors.append(
                    stderr or (
                        f"hook exited with code {exit_code}" if exit_code != 0
                        else "hook returned invalid Interrupt hook JSON output"
                        if stdout.startswith(("{", "[")) else "Interrupt hook returned non-JSON stdout"
                    )
                )
            elif json_result is not None and json_result.get("systemMessage"):
                system_messages.append(json_result["systemMessage"])
            continue

        if execution.execution_failed:
            # A launcher/runtime failure is visible but does not silently block
            # the operation. Only exit 2 or a valid JSON block/deny decision can
            # stop the current lifecycle event.
            error = stderr or stdout or "Hook execution failed"
            errors.append(error)
            if event_key == "worktree_create":
                failed = True
            continue

        if event_key in {"pre_compact", "post_compact"} and exit_code == 0:
            # Source's compact output schema accepts only these universal fields,
            # in camelCase. Invalid JSON is evidence, never a guessed control.
            label = "PreCompact" if event_key == "pre_compact" else "PostCompact"
            invalid = json_result is None and stdout.startswith(("{", "["))
            if json_result is not None:
                invalid = bool(set(json_result) - {"continue", "stopReason", "suppressOutput", "systemMessage"}) or any(
                    name in json_result and not isinstance(json_result[name], bool)
                    for name in ("continue", "suppressOutput")
                ) or any(json_result.get(name) is not None and not isinstance(json_result[name], str)
                         for name in ("stopReason", "systemMessage"))
            if invalid:
                errors.append(f"hook returned invalid {label} hook JSON output")
                summaries[-1]["status"] = "failed"
                failed = True
                continue
            if json_result is not None:
                json_result = {"continue": json_result.get("continue", True),
                    "stop_reason": json_result.get("stopReason") or "",
                    "suppress_output": json_result.get("suppressOutput", False),
                    "system_message": json_result.get("systemMessage") or ""}
                if json_result["continue"] is False and execution_blocking_allowed and blocking_allowed:
                    summaries[-1]["status"] = "stopped"

        event_matches = True
        if json_result is not None:
            actual_event = str(json_result.get("event") or "")
            event_matches = not actual_event or actual_event == expected_event_name
            if not event_matches:
                errors.append(
                    f"Hook returned {actual_event!r}; expected {expected_event_name!r}"
                )
        if json_result is not None and exit_code == 0:
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
            if decision == "allow" and not execution.permission_grant_allowed:
                decision = ""
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
            if event_key in {"pre_compact", "post_compact"}:
                failed = True
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
    if str(getattr(event, "value", event) or "") in _CODEX_EVENTS | {"interrupt", "pre_compact", "post_compact"}:
        return not reduce_hook_executions(
            event, [execution], expected_event_name=expected_event_name,
        ).failed
    payload = execution.json_output
    if payload is None:
        return True
    actual = str(payload.get("event") or "")
    if actual and actual != expected_event_name:
        return False
    if str(getattr(event, "value", event) or "") == "worktree_create":
        path = payload.get("worktree_path")
        return bool(str(path or "").strip())
    return True


def _parse_codex_output(event: str, payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Apply the command output shapes in codex-rs/hooks/src/schema.rs."""
    if payload is None:
        return None
    universal = {"continue", "stopReason", "suppressOutput", "systemMessage"}
    fields = {
        "session_start": {"hookSpecificOutput"},
        "subagent_start": {"hookSpecificOutput"},
        "user_prompt_submit": {"decision", "reason", "hookSpecificOutput"},
        "pre_tool_use": {"decision", "reason", "hookSpecificOutput"},
        "post_tool_use": {"decision", "reason", "hookSpecificOutput"},
        "permission_request": {"hookSpecificOutput"},
        "stop": {"decision", "reason"},
        "subagent_stop": {"decision", "reason"},
    }
    if set(payload) - universal - fields[event]:
        return None
    if any(key in payload and not isinstance(payload[key], bool) for key in ("continue", "suppressOutput")):
        return None
    if any(payload.get(key) is not None and not isinstance(payload[key], str)
           for key in ("stopReason", "systemMessage", "reason")):
        return None
    decision = payload.get("decision")
    if decision is not None and (not isinstance(decision, str) or decision not in ({"approve", "block"} if event == "pre_tool_use" else {"block"})):
        return None
    specific = payload.get("hookSpecificOutput")
    if specific is None:
        return payload
    if not isinstance(specific, dict):
        return None
    allowed = {
        "session_start": {"hookEventName", "additionalContext"},
        "subagent_start": {"hookEventName", "additionalContext"},
        "user_prompt_submit": {"hookEventName", "additionalContext"},
        "pre_tool_use": {"hookEventName", "permissionDecision", "permissionDecisionReason", "updatedInput", "additionalContext"},
        "post_tool_use": {"hookEventName", "additionalContext", "updatedMCPToolOutput"},
        "permission_request": {"hookEventName", "decision"},
    }
    event_names = {"PreToolUse", "PermissionRequest", "PostToolUse", "PreCompact", "PostCompact", "SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop", "Stop", "Interrupt"}
    event_name = specific.get("hookEventName")
    if set(specific) - allowed[event] or not isinstance(event_name, str) or event_name not in event_names:
        return None
    if any(specific.get(key) is not None and not isinstance(specific[key], str)
           for key in ("additionalContext", "permissionDecisionReason")):
        return None
    permission = specific.get("permissionDecision")
    if permission is not None and (not isinstance(permission, str) or permission not in {"allow", "deny", "ask"}):
        return None
    if event == "permission_request" and specific.get("decision") is not None:
        permission_result = specific["decision"]
        if not isinstance(permission_result, dict) or set(permission_result) - {"behavior", "updatedInput", "updatedPermissions", "message", "interrupt"}:
            return None
        behavior = permission_result.get("behavior")
        if not isinstance(behavior, str) or behavior not in {"allow", "deny"}:
            return None
        if permission_result.get("message") is not None and not isinstance(permission_result["message"], str):
            return None
        if "interrupt" in permission_result and not isinstance(permission_result["interrupt"], bool):
            return None
    return payload


def _codex_semantic_error(event: str, payload: dict[str, Any]) -> str:
    specific = payload.get("hookSpecificOutput") or {}
    if event in {"pre_tool_use", "permission_request"}:
        label = "PreToolUse" if event == "pre_tool_use" else "PermissionRequest"
        if payload.get("continue") is False:
            return f"{label} hook returned unsupported continue:false"
        if payload.get("stopReason") is not None:
            return f"{label} hook returned unsupported stopReason"
        if payload.get("suppressOutput") is True:
            return f"{label} hook returned unsupported suppressOutput"
    if event == "pre_tool_use":
        permission = specific.get("permissionDecision")
        reason = specific.get("permissionDecisionReason")
        updated = specific.get("updatedInput")
        if any(value is not None for value in (permission, reason, updated)):
            if updated is not None and permission != "allow":
                return "PreToolUse hook returned updatedInput without permissionDecision:allow"
            if permission == "allow" and updated is None:
                return "PreToolUse hook returned unsupported permissionDecision:allow"
            if permission == "ask":
                return "PreToolUse hook returned unsupported permissionDecision:ask"
            if permission == "deny" and not (reason or "").strip():
                return "PreToolUse hook returned permissionDecision:deny without a non-empty permissionDecisionReason"
            if permission is None and reason is not None:
                return "PreToolUse hook returned permissionDecisionReason without permissionDecision"
        elif payload.get("decision") == "approve":
            return "PreToolUse hook returned unsupported decision:approve"
        elif payload.get("decision") is None and payload.get("reason") is not None:
            return "PreToolUse hook returned reason without decision"
    elif event == "permission_request":
        decision = specific.get("decision") or {}
        if decision.get("updatedInput") is not None:
            return "PermissionRequest hook returned unsupported updatedInput"
        if decision.get("updatedPermissions") is not None:
            return "PermissionRequest hook returned unsupported updatedPermissions"
        if decision.get("interrupt") is True:
            return "PermissionRequest hook returned unsupported interrupt:true"
    elif event == "post_tool_use":
        if payload.get("suppressOutput") is True:
            return "PostToolUse hook returned unsupported suppressOutput"
        if specific.get("updatedMCPToolOutput") is not None:
            return "PostToolUse hook returned unsupported updatedMCPToolOutput"
        if payload.get("decision") is None and payload.get("continue", True) and payload.get("reason") is not None:
            return "PostToolUse hook returned reason without decision"
    if payload.get("decision") == "block" and not (payload.get("reason") or "").strip():
        if event != "pre_tool_use" or not any(specific.get(key) is not None for key in ("permissionDecision", "permissionDecisionReason", "updatedInput")):
            label = "".join(part.title() for part in event.split("_"))
            return f"{label} hook returned decision:block without a non-empty reason"
    return ""


def _reduce_model_observers(executions: Iterable[HookExecution]) -> HookReduction:
    """Model request hooks observe the request; stdout never controls it."""
    summaries = []
    errors = []
    failed = False
    for execution in executions:
        is_failed = execution.execution_failed or execution.exit_code != 0
        failed |= is_failed
        stderr = execution.stderr.strip()
        if stderr:
            errors.append(stderr)
        elif is_failed:
            errors.append(f"hook exited with code {execution.exit_code}")
        summaries.append({
            "key": execution.entry.entry_id, "source": execution.entry.source,
            "source_path": execution.entry.source_path, "display_order": execution.entry.display_order,
            "configured_order": execution.configured_order, "completion_order": execution.completion_order,
            "duration_ms": execution.duration_ms, "exit_code": execution.exit_code,
            "status": "failed" if is_failed else "succeeded",
            "status_message": execution.entry.status_message,
            **({"runtime_error": True} if execution.execution_failed else {}),
        })
    return HookReduction(failed=failed, errors=tuple(errors), run_summaries=tuple(summaries))


def _reduce_codex_executions(event: str, executions: Iterable[HookExecution], *, blocking_allowed: bool) -> HookReduction:
    outputs: list[str] = []
    contexts: list[tuple[str, Any]] = []
    feedback: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []
    summaries: list[dict[str, Any]] = []
    blocked = False
    stopped = False
    stop_reason = ""
    block_reason = ""
    permission_decision = ""
    permission_reason = ""
    latest_input: tuple[int, Any] | None = None
    for execution in executions:
        stdout = execution.stdout.strip()
        semantic_stdout = (execution.semantic_stdout if execution.semantic_stdout is not None else stdout).strip()
        stderr = execution.stderr.strip()
        summary = {
            "key": execution.entry.entry_id, "source": execution.entry.source,
            "source_path": execution.entry.source_path, "display_order": execution.entry.display_order,
            "configured_order": execution.configured_order, "completion_order": execution.completion_order,
            "duration_ms": execution.duration_ms, "exit_code": execution.exit_code,
            "status": "succeeded", "status_message": execution.entry.status_message,
            **({"runtime_error": True} if execution.execution_failed else {}),
        }
        summaries.append(summary)
        if stdout:
            outputs.append(stdout)
        if execution.execution_failed:
            summary["status"] = "failed"
            errors.append(stderr or "Hook execution failed")
            continue
        if execution.exit_code != 0:
            if execution.exit_code == 2 and event in {"pre_tool_use", "post_tool_use", "permission_request", "user_prompt_submit", "stop", "subagent_stop"} and blocking_allowed:
                if stderr:
                    summary["status"] = "blocked"
                    blocked = True
                    if not block_reason:
                        block_reason = stderr
                    if event == "permission_request":
                        if permission_decision != "deny":
                            permission_decision, permission_reason = "deny", stderr
                    else:
                        feedback.append(stderr)
                else:
                    summary["status"] = "failed"
                    label = "".join(part.title() for part in event.split("_"))
                    detail = "denial reason" if event == "permission_request" else "continuation prompt" if event in {"stop", "subagent_stop"} else "feedback" if event == "post_tool_use" else "blocking reason"
                    errors.append(f"{label} hook exited with code 2 but did not write a {detail} to stderr")
            else:
                summary["status"] = "failed"
                errors.append(stderr or f"hook exited with code {execution.exit_code}")
            continue
        if event == "session_end" or not semantic_stdout:
            continue
        payload = _parse_codex_output(event, execution.json_output)
        if payload is None:
            if semantic_stdout.startswith(("{", "[")) or event in {"stop", "subagent_stop"}:
                summary["status"] = "failed"
                errors.append(f"hook returned invalid {event.replace('_', ' ')} JSON output")
            elif event in {"session_start", "subagent_start", "user_prompt_submit"}:
                contexts.append((semantic_stdout, execution.entry))
            continue
        if payload.get("suppressOutput") is True and stdout:
            outputs.pop()
        if payload.get("systemMessage"):
            warnings.append(payload["systemMessage"])
        semantic_error = _codex_semantic_error(event, payload)
        # PostToolUse, prompt and stop honor continue:false before their
        # semantic decision errors; PreToolUse and PermissionRequest reject it.
        stops = payload.get("continue") is False and event in {"session_start", "user_prompt_submit", "post_tool_use", "stop", "subagent_stop"} and blocking_allowed
        if semantic_error and not stops:
            summary["status"] = "failed"
            errors.append(semantic_error)
            continue
        specific = payload.get("hookSpecificOutput") or {}
        additional = specific.get("additionalContext")
        if additional and not semantic_error:
            contexts.append((additional, execution.entry))
        if stops:
            summary["status"] = "stopped"
            if event == "post_tool_use":
                feedback.append((payload.get("reason") or payload.get("stopReason") or "PostToolUse hook stopped execution").strip())
            else:
                stopped = True
                if not stop_reason:
                    stop_reason = payload.get("stopReason") or ""
            continue
        if not blocking_allowed:
            continue
        decision = payload.get("decision")
        reason = payload.get("reason") or ""
        if event == "pre_tool_use":
            permission = specific.get("permissionDecision")
            if permission == "deny":
                decision, reason = "block", specific["permissionDecisionReason"]
            elif permission == "allow" and specific.get("updatedInput") is not None:
                candidate = (execution.completion_order, specific["updatedInput"])
                if latest_input is None or candidate[0] > latest_input[0]:
                    latest_input = candidate
                decision = None
        elif event == "permission_request":
            request_decision = specific.get("decision") or {}
            behavior = request_decision.get("behavior")
            if behavior == "deny":
                message = (request_decision.get("message") or "").strip() or "PermissionRequest hook denied approval"
                if permission_decision != "deny":
                    permission_decision, permission_reason = "deny", message
                decision, reason = "block", message
            elif behavior == "allow" and permission_decision != "deny" and execution.permission_grant_allowed:
                permission_decision = "allow"
        if decision == "block":
            summary["status"] = "blocked"
            blocked = True
            if not block_reason:
                block_reason = reason.strip()
            if event != "permission_request":
                feedback.append(reason.strip())
    if event in {"session_start", "user_prompt_submit"} and stopped:
        blocked = True
        block_reason = stop_reason or "Hook stopped execution"
    if event == "pre_tool_use" and blocked:
        latest_input = None
        permission_decision, permission_reason = "deny", block_reason
    return HookReduction(
        blocked=blocked, failed=any(item["status"] == "failed" for item in summaries),
        message=block_reason, feedback="\n".join(feedback), stdout="\n".join(outputs),
        errors=tuple(errors), permission_decision=permission_decision,
        permission_decision_reason=permission_reason, additional_context="\n\n".join(text for text, _ in contexts),
        additional_context_items=tuple(contexts),
        updated_input=latest_input[1] if latest_input is not None else None,
        prevent_continuation=stopped, stop_reason=stop_reason,
        system_message="\n".join(warnings), run_summaries=tuple(summaries),
    )


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
