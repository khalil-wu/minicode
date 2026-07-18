"""Hook manager — lifecycle hooks modeled after Claude Code's hook system.

Configuration in settings.json:

    {
      "hooks": {
        "user_prompt_submit": [
          {"matcher": ".*", "command": "echo 'prompt received'"}
        ],
        "pre_tool_use": [
          {"matcher": "write_file|edit_file", "command": "echo $TOOL_NAME >> .claude/audit.log"}
        ],
        "post_tool_use": [
          {"matcher": "write_file", "command": "prettier --write $TOOL_PATH"}
        ],
        "stop": [
          {"matcher": ".*", "command": "python .claude/quality_check.py"}
        ]
      }
    }

Hook events (Claude Code lifecycle):
  - session_start:       fires once when agent session begins
  - user_prompt_submit:  fires after user message received, before model call
  - pre_tool_use:        fires before each tool execution
  - post_tool_use:       fires after each tool execution
  - pre_compact:         fires before context compaction
  - stop:               fires when model produces final reply (no more tool calls)

Exit code semantics:
  - 0: hook succeeded, no effect on flow
  - 1: error (pre_tool_use: block the tool; others: log warning)
  - 2: feedback injection — stdout becomes a user-role message injected into
       the next model iteration. The model retries with this feedback.
       This is NOT answer generation — it's guidance for the model.

Environment variables available to hooks:
  - HOOK_EVENT: the event type (e.g. "pre_tool_use")
  - TOOL_NAME: tool name (tool events only)
  - TOOL_ARGS_JSON: JSON tool arguments (tool events only)
  - TOOL_PATH: convenience path argument (tool events only)
  - TOOL_RESULT: tool output, truncated (post_tool_use only)
  - USER_PROMPT: user message (user_prompt_submit, stop)
  - DRAFT_REPLY: model's draft final reply (stop only)
  - TOOL_RESULTS_JSON: JSON array of recent tool call records (stop only)
    Each entry: {tool_name, status, evidence_type?, extraction_status?, source_url?, preview?}
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from backend.runtime_env import sanitized_subprocess_env

logger = logging.getLogger(__name__)

_HOOK_TIMEOUT_S = 10.0
_RESULT_TRUNCATE = 4096
_PROMPT_TRUNCATE = 2000
_DRAFT_TRUNCATE = 4096
_TOOL_RESULTS_LIMIT = 10
_PREVIEW_TRUNCATE = 200

_CC_EVENT_NAMES: dict["HookEvent", str] = {}


def _serialize_tool_results(tool_results: list[Any], limit: int = _TOOL_RESULTS_LIMIT) -> str:
    """Serialize recent tool call records to JSON for hook environment."""
    entries: list[dict[str, Any]] = []
    for record in tool_results[-limit:]:
        entry: dict[str, Any] = {
            "tool_name": getattr(record, "tool_name", ""),
            "status": getattr(record, "status", ""),
        }
        ev_type = getattr(record, "evidence_type", None)
        if ev_type:
            entry["evidence_type"] = ev_type
        ext_status = getattr(record, "extraction_status", None)
        if ext_status:
            entry["extraction_status"] = ext_status
        source_url = getattr(record, "source_url", None)
        if source_url:
            entry["source_url"] = source_url
        preview = getattr(record, "content_preview", None) or ""
        if not preview:
            output = str(getattr(record, "tool_output", "") or "")
            preview = output[:_PREVIEW_TRUNCATE]
        else:
            preview = preview[:_PREVIEW_TRUNCATE]
        if preview:
            entry["preview"] = preview
        entries.append(entry)
    try:
        return json.dumps(entries, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "[]"


def _parse_json_stdout(stdout: str) -> dict[str, Any] | None:
    """Parse hook stdout as JSON if it looks like a JSON object.

    Returns the parsed dict, or None if stdout isn't JSON. Tolerates leading/
    trailing whitespace. A non-object (e.g. a bare number) is treated as None.
    """
    stripped = stdout.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_hook_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value != 0
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _coerce_async_timeout(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


def _json_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _normalize_permission_decision(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"allow", "deny", "ask"} else ""


def _extract_permission_fields(json_result: dict[str, Any]) -> tuple[str, str]:
    decision = _normalize_permission_decision(
        json_result.get("permissionDecision") or json_result.get("permission_decision")
    )
    reason = _json_text(
        json_result.get("permissionDecisionReason")
        or json_result.get("permission_decision_reason")
        or json_result.get("reason")
    )
    hook_specific = json_result.get("hookSpecificOutput") or json_result.get("hook_specific_output")
    if isinstance(hook_specific, dict):
        decision = _normalize_permission_decision(
            hook_specific.get("permissionDecision")
            or hook_specific.get("permission_decision")
            or decision
        )
        reason = _json_text(
            hook_specific.get("permissionDecisionReason")
            or hook_specific.get("permission_decision_reason")
            or reason
        )
    return decision, reason


def _extract_json_output_fields(json_result: dict[str, Any]) -> tuple[str, Any]:
    additional_context = str(
        json_result.get("additionalContext")
        or json_result.get("additional_context")
        or ""
    )
    updated_input = json_result.get("updatedInput", json_result.get("updated_input", ""))
    hook_specific = json_result.get("hookSpecificOutput") or json_result.get("hook_specific_output")
    if isinstance(hook_specific, dict):
        additional_context = str(
            hook_specific.get("additionalContext")
            or hook_specific.get("additional_context")
            or additional_context
        )
        updated_input = (
            hook_specific["updatedInput"]
            if "updatedInput" in hook_specific
            else hook_specific.get("updated_input", updated_input)
        )
    return additional_context, updated_input


class HookEvent(str, Enum):
    SESSION_START = "session_start"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    POST_TOOL_USE_FAILURE = "post_tool_use_failure"
    NOTIFICATION = "notification"
    PRE_COMPACT = "pre_compact"
    POST_COMPACT = "post_compact"
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_DENIED = "permission_denied"
    SETUP = "setup"
    STOP_FAILURE = "stop_failure"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_STOP = "subagent_stop"
    TEAMMATE_IDLE = "teammate_idle"
    TASK_CREATED = "task_created"
    TASK_COMPLETED = "task_completed"
    ELICITATION = "elicitation"
    ELICITATION_RESULT = "elicitation_result"
    CONFIG_CHANGE = "config_change"
    WORKTREE_CREATE = "worktree_create"
    WORKTREE_REMOVE = "worktree_remove"
    INSTRUCTIONS_LOADED = "instructions_loaded"
    CWD_CHANGED = "cwd_changed"
    FILE_CHANGED = "file_changed"
    SESSION_END = "session_end"
    STOP = "stop"


_CC_EVENT_NAMES = {
    HookEvent.PRE_TOOL_USE: "PreToolUse",
    HookEvent.POST_TOOL_USE: "PostToolUse",
    HookEvent.POST_TOOL_USE_FAILURE: "PostToolUseFailure",
    HookEvent.NOTIFICATION: "Notification",
    HookEvent.USER_PROMPT_SUBMIT: "UserPromptSubmit",
    HookEvent.SESSION_START: "SessionStart",
    HookEvent.SESSION_END: "SessionEnd",
    HookEvent.STOP: "Stop",
    HookEvent.STOP_FAILURE: "StopFailure",
    HookEvent.SUBAGENT_START: "SubagentStart",
    HookEvent.SUBAGENT_STOP: "SubagentStop",
    HookEvent.PRE_COMPACT: "PreCompact",
    HookEvent.POST_COMPACT: "PostCompact",
    HookEvent.PERMISSION_REQUEST: "PermissionRequest",
    HookEvent.PERMISSION_DENIED: "PermissionDenied",
    HookEvent.SETUP: "Setup",
    HookEvent.TEAMMATE_IDLE: "TeammateIdle",
    HookEvent.TASK_CREATED: "TaskCreated",
    HookEvent.TASK_COMPLETED: "TaskCompleted",
    HookEvent.ELICITATION: "Elicitation",
    HookEvent.ELICITATION_RESULT: "ElicitationResult",
    HookEvent.CONFIG_CHANGE: "ConfigChange",
    HookEvent.WORKTREE_CREATE: "WorktreeCreate",
    HookEvent.WORKTREE_REMOVE: "WorktreeRemove",
    HookEvent.INSTRUCTIONS_LOADED: "InstructionsLoaded",
    HookEvent.CWD_CHANGED: "CwdChanged",
    HookEvent.FILE_CHANGED: "FileChanged",
}


def cc_event_name(event: HookEvent) -> str:
    return _CC_EVENT_NAMES.get(event, event.value)


def _hook_config_keys() -> dict[str, HookEvent]:
    keys: dict[str, HookEvent] = {
        "pre_tool": HookEvent.PRE_TOOL_USE,
        "post_tool": HookEvent.POST_TOOL_USE,
    }
    for event, camel in _CC_EVENT_NAMES.items():
        keys[event.value] = event
        keys[camel] = event
    return keys


@dataclass
class HookResult:
    blocked: bool = False
    message: str = ""
    feedback: str = ""
    stdout: str = ""
    # Hook-provided permission decision: allow, deny, or ask.
    permission_decision: str = ""
    permission_decision_reason: str = ""
    # Extra context injected into the conversation (cc's additionalContext).
    additional_context: str = ""
    # Replacement text for user_prompt_submit hooks (cc's updatedInput).
    updated_input: Any = ""

    @property
    def has_feedback(self) -> bool:
        return bool(self.feedback.strip())

    @property
    def has_additional_context(self) -> bool:
        return bool(self.additional_context.strip())

    @property
    def has_updated_input(self) -> bool:
        if isinstance(self.updated_input, str):
            return bool(self.updated_input.strip())
        return self.updated_input is not None

    @property
    def has_permission_decision(self) -> bool:
        return self.permission_decision in {"allow", "deny", "ask"}


@dataclass
class _HookEntry:
    matcher: re.Pattern[str]
    command: str
    raw_matcher: str = ""
    run_async: bool = False
    async_timeout: float | None = None
    owner: str = ""


@dataclass
class HookManager:
    hooks: dict[HookEvent, list[_HookEntry]] = field(default_factory=dict)
    workspace_root: Path | None = None
    # Session IDs for which session_start has already fired. Lets the hook
    # fire once per session (on the first agent turn) without per-session state
    # in the per-turn AgentState.
    _session_start_fired: set[str] = field(default_factory=set)
    _async_tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    # Legacy compatibility properties
    @property
    def pre_tool(self) -> list[_HookEntry]:
        return self.hooks.get(HookEvent.PRE_TOOL_USE, [])

    @property
    def post_tool(self) -> list[_HookEntry]:
        return self.hooks.get(HookEvent.POST_TOOL_USE, [])

    @classmethod
    def from_settings(cls, settings: dict[str, Any], workspace_root: Path | None = None) -> "HookManager":
        mgr = cls(workspace_root=workspace_root)
        hooks_cfg = settings.get("hooks") if isinstance(settings, dict) else None
        if not isinstance(hooks_cfg, dict):
            return mgr

        for key, event in _hook_config_keys().items():
            entries = hooks_cfg.get(key)
            if not isinstance(entries, list):
                continue
            if event not in mgr.hooks:
                mgr.hooks[event] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                matcher_raw = str(entry.get("matcher", ".*"))
                command = str(entry.get("command", "")).strip()
                if not command:
                    continue
                try:
                    pattern = re.compile(matcher_raw)
                except re.error as exc:
                    logger.warning("Skipping hook with invalid matcher %r: %s", matcher_raw, exc)
                    continue
                run_async = _coerce_hook_bool(entry.get("async", entry.get("runAsync", False)))
                async_timeout = _coerce_async_timeout(
                    entry.get("asyncTimeout", entry.get("async_timeout"))
                )
                mgr.hooks[event].append(
                    _HookEntry(
                        matcher=pattern,
                        command=command,
                        raw_matcher=matcher_raw,
                        run_async=run_async,
                        async_timeout=async_timeout,
                    )
                )
        return mgr

    def add_temporary_hooks(self, owner: str, entries: list[dict[str, Any]]) -> int:
        """Register owner-scoped hooks, typically supplied by an invoked Skill."""
        clean_owner = str(owner or "").strip()
        if not clean_owner:
            return 0
        self.remove_temporary_hooks(clean_owner)

        registered = 0
        event_keys = _hook_config_keys()
        for raw in entries:
            if not isinstance(raw, dict):
                continue
            event_raw = str(
                raw.get("event")
                or raw.get("hook_event")
                or raw.get("hookEvent")
                or raw.get("name")
                or ""
            ).strip()
            event = event_keys.get(event_raw)
            if event is None:
                event = event_keys.get(event_raw.lower())
            if event is None:
                logger.warning("Skipping temporary hook with unknown event %r", event_raw)
                continue

            matcher_raw = str(raw.get("matcher", ".*"))
            command = str(raw.get("command", "")).strip()
            if not command:
                continue
            try:
                pattern = re.compile(matcher_raw)
            except re.error as exc:
                logger.warning("Skipping temporary hook with invalid matcher %r: %s", matcher_raw, exc)
                continue
            run_async = _coerce_hook_bool(raw.get("async", raw.get("runAsync", False)))
            async_timeout = _coerce_async_timeout(raw.get("asyncTimeout", raw.get("async_timeout")))
            self.hooks.setdefault(event, []).append(
                _HookEntry(
                    matcher=pattern,
                    command=command,
                    raw_matcher=matcher_raw,
                    run_async=run_async,
                    async_timeout=async_timeout,
                    owner=clean_owner,
                )
            )
            registered += 1
        return registered

    def remove_temporary_hooks(self, owner: str) -> int:
        """Remove previously registered owner-scoped hooks."""
        clean_owner = str(owner or "").strip()
        if not clean_owner:
            return 0
        removed = 0
        for event, entries in list(self.hooks.items()):
            kept = [entry for entry in entries if entry.owner != clean_owner]
            removed += len(entries) - len(kept)
            if kept:
                self.hooks[event] = kept
            else:
                self.hooks.pop(event, None)
        return removed

    def has_hooks(self, event: HookEvent) -> bool:
        return bool(self.hooks.get(event))

    @property
    def pending_async_hooks(self) -> int:
        return len(self._async_tasks)

    async def drain_async_hooks(self) -> None:
        while self._async_tasks:
            tasks = tuple(self._async_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_pre_tool(self, tool_name: str, args: dict[str, Any]) -> HookResult:
        return await self._run_event(
            HookEvent.PRE_TOOL_USE, match_target=tool_name,
            env_extras=self._tool_env(tool_name, args),
        )

    async def run_post_tool(self, tool_name: str, args: dict[str, Any], result_content: str) -> HookResult:
        extras = self._tool_env(tool_name, args)
        extras["TOOL_RESULT"] = result_content[:_RESULT_TRUNCATE]
        return await self._run_event(
            HookEvent.POST_TOOL_USE, match_target=tool_name, env_extras=extras,
        )

    async def run_post_tool_failure(self, tool_name: str, args: dict[str, Any], error_content: str) -> HookResult:
        """Fires when a tool call errors — useful for logging/alerting/telemetry."""
        extras = self._tool_env(tool_name, args)
        extras["TOOL_ERROR"] = error_content[:_RESULT_TRUNCATE]
        return await self._run_event(
            HookEvent.POST_TOOL_USE_FAILURE, match_target=tool_name, env_extras=extras,
        )

    async def run_notification(
        self,
        message: str,
        *,
        title: str = "",
        notification_type: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.NOTIFICATION,
            match_target=notification_type or title or "notification",
            env_extras={
                "NOTIFICATION_MESSAGE": message[:_RESULT_TRUNCATE],
                "NOTIFICATION_TITLE": title,
                "NOTIFICATION_TYPE": notification_type,
            },
        )

    async def run_post_compact(self) -> HookResult:
        """Fires after context compaction completes (symmetric with pre_compact)."""
        return await self._run_event(HookEvent.POST_COMPACT, match_target="compact")

    async def run_permission_request(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        reason: str = "",
        permission_level: str = "",
    ) -> HookResult:
        extras = self._tool_env(tool_name, args)
        extras["PERMISSION_REASON"] = reason
        extras["PERMISSION_LEVEL"] = permission_level
        return await self._run_event(
            HookEvent.PERMISSION_REQUEST,
            match_target=tool_name,
            env_extras=extras,
        )

    async def run_permission_denied(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        reason: str = "",
        permission_level: str = "",
    ) -> HookResult:
        extras = self._tool_env(tool_name, args)
        extras["PERMISSION_DENIED_REASON"] = reason
        extras["PERMISSION_LEVEL"] = permission_level
        return await self._run_event(
            HookEvent.PERMISSION_DENIED,
            match_target=tool_name,
            env_extras=extras,
        )

    async def run_setup(self, *, trigger: str = "init") -> HookResult:
        return await self._run_event(
            HookEvent.SETUP,
            match_target=trigger or "setup",
            env_extras={"SETUP_TRIGGER": trigger},
        )

    async def run_stop_failure(
        self,
        error: str,
        *,
        error_details: str = "",
        last_assistant_message: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.STOP_FAILURE,
            match_target=error or "unknown",
            env_extras={
                "STOP_FAILURE_ERROR": error,
                "STOP_FAILURE_ERROR_DETAILS": error_details[:_RESULT_TRUNCATE],
                "LAST_ASSISTANT_MESSAGE": last_assistant_message[:_DRAFT_TRUNCATE],
            },
        )

    async def run_subagent_start(
        self,
        *,
        subagent_id: str = "",
        agent_type: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.SUBAGENT_START,
            match_target=agent_type or subagent_id or "subagent",
            env_extras={
                "SUBAGENT_ID": subagent_id,
                "SUBAGENT_AGENT_TYPE": agent_type,
            },
        )

    async def run_subagent_stop(
        self,
        *,
        subagent_id: str = "",
        agent_type: str = "",
        status: str = "",
        summary: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.SUBAGENT_STOP,
            match_target=agent_type or subagent_id or status or "subagent",
            env_extras={
                "SUBAGENT_ID": subagent_id,
                "SUBAGENT_AGENT_TYPE": agent_type,
                "SUBAGENT_STATUS": status,
                "SUBAGENT_SUMMARY": summary[:_RESULT_TRUNCATE],
            },
        )

    async def run_teammate_idle(
        self,
        *,
        teammate_name: str = "",
        team_name: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.TEAMMATE_IDLE,
            match_target=teammate_name or team_name or "teammate",
            env_extras={
                "TEAMMATE_NAME": teammate_name,
                "TEAM_NAME": team_name,
            },
        )

    async def run_task_created(
        self,
        *,
        task_id: str = "",
        subject: str = "",
        description: str = "",
        teammate_name: str = "",
        team_name: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.TASK_CREATED,
            match_target=task_id or subject or "task",
            env_extras={
                "TASK_ID": task_id,
                "TASK_SUBJECT": subject,
                "TASK_DESCRIPTION": description[:_RESULT_TRUNCATE],
                "TEAMMATE_NAME": teammate_name,
                "TEAM_NAME": team_name,
            },
        )

    async def run_task_completed(
        self,
        *,
        task_id: str = "",
        subject: str = "",
        description: str = "",
        teammate_name: str = "",
        team_name: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.TASK_COMPLETED,
            match_target=task_id or subject or "task",
            env_extras={
                "TASK_ID": task_id,
                "TASK_SUBJECT": subject,
                "TASK_DESCRIPTION": description[:_RESULT_TRUNCATE],
                "TEAMMATE_NAME": teammate_name,
                "TEAM_NAME": team_name,
            },
        )

    async def run_elicitation(
        self,
        prompt: str,
        *,
        response: str = "",
        elicitation_id: str = "",
        mcp_server_name: str = "",
        mode: str = "",
        url: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.ELICITATION,
            match_target=mcp_server_name or elicitation_id or "elicitation",
            env_extras={
                "MCP_SERVER_NAME": mcp_server_name,
                "ELICITATION_ID": elicitation_id,
                "ELICITATION_PROMPT": prompt[:_PROMPT_TRUNCATE],
                "ELICITATION_RESPONSE": response[:_RESULT_TRUNCATE],
                "ELICITATION_MODE": mode,
                "ELICITATION_URL": url,
            },
        )

    async def run_elicitation_result(
        self,
        *,
        mcp_server_name: str = "",
        elicitation_id: str = "",
        action: str = "",
        content: dict[str, Any] | None = None,
        mode: str = "",
    ) -> HookResult:
        try:
            content_json = json.dumps(content or {}, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            content_json = "{}"
        return await self._run_event(
            HookEvent.ELICITATION_RESULT,
            match_target=mcp_server_name or elicitation_id or "elicitation",
            env_extras={
                "MCP_SERVER_NAME": mcp_server_name,
                "ELICITATION_ID": elicitation_id,
                "ELICITATION_ACTION": action,
                "ELICITATION_MODE": mode,
                "ELICITATION_CONTENT_JSON": content_json[:_RESULT_TRUNCATE],
            },
        )

    async def run_config_change(
        self,
        *,
        source: str,
        file_path: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.CONFIG_CHANGE,
            match_target=source,
            env_extras={
                "CONFIG_CHANGE_SOURCE": source,
                "CONFIG_FILE_PATH": file_path,
            },
        )

    async def run_worktree_create(
        self,
        *,
        path: str,
        branch: str = "",
        base: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.WORKTREE_CREATE,
            match_target=path,
            env_extras={
                "WORKTREE_PATH": path,
                "WORKTREE_BRANCH": branch,
                "WORKTREE_BASE": base,
            },
        )

    async def run_worktree_remove(
        self,
        *,
        path: str,
        reason: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.WORKTREE_REMOVE,
            match_target=path,
            env_extras={
                "WORKTREE_PATH": path,
                "WORKTREE_REMOVE_REASON": reason,
            },
        )

    async def run_instructions_loaded(
        self,
        *,
        file_path: str,
        memory_type: str = "",
        load_reason: str = "",
        trigger_file_path: str = "",
        parent_file_path: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.INSTRUCTIONS_LOADED,
            match_target=load_reason or memory_type or file_path,
            env_extras={
                "INSTRUCTIONS_FILE_PATH": file_path,
                "INSTRUCTIONS_MEMORY_TYPE": memory_type,
                "INSTRUCTIONS_LOAD_REASON": load_reason,
                "INSTRUCTIONS_TRIGGER_FILE_PATH": trigger_file_path,
                "INSTRUCTIONS_PARENT_FILE_PATH": parent_file_path,
            },
        )

    async def run_cwd_changed(
        self,
        *,
        old_cwd: str,
        new_cwd: str,
    ) -> HookResult:
        return await self._run_event(
            HookEvent.CWD_CHANGED,
            match_target=new_cwd,
            env_extras={
                "OLD_CWD": old_cwd,
                "NEW_CWD": new_cwd,
            },
        )

    async def run_file_changed(
        self,
        path: str,
        *,
        event: str = "modified",
        tool_name: str = "",
        tool_call_id: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.FILE_CHANGED,
            match_target=path,
            env_extras={
                "FILE_PATH": path,
                "FILE_EVENT": event,
                "TOOL_NAME": tool_name,
                "TOOL_CALL_ID": tool_call_id,
            },
        )

    async def run_session_end(
        self,
        *,
        session_id: str = "",
        reason: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.SESSION_END,
            match_target=session_id or "session",
            env_extras={
                "SESSION_ID": session_id,
                "SESSION_END_REASON": reason,
            },
        )

    async def run_session_start(self) -> HookResult:
        return await self._run_event(HookEvent.SESSION_START, match_target="session")

    async def run_session_start_once(self, session_id: str) -> HookResult:
        """Fire session_start once per session (first agent turn). No-op on
        subsequent turns of the same session."""
        if not session_id or session_id in self._session_start_fired:
            return HookResult()
        self._session_start_fired.add(session_id)
        return await self.run_session_start()

    async def run_user_prompt_submit(self, user_message: str) -> HookResult:
        return await self._run_event(
            HookEvent.USER_PROMPT_SUBMIT, match_target="prompt",
            env_extras={"USER_PROMPT": user_message[:_PROMPT_TRUNCATE]},
        )

    async def run_pre_compact(self) -> HookResult:
        return await self._run_event(HookEvent.PRE_COMPACT, match_target="compact")

    async def run_stop(
        self,
        user_message: str,
        draft_reply: str,
        tool_results: list[Any] | None = None,
    ) -> HookResult:
        extras: dict[str, str] = {
            "USER_PROMPT": user_message[:_PROMPT_TRUNCATE],
            "DRAFT_REPLY": draft_reply[:_DRAFT_TRUNCATE],
        }
        if tool_results:
            extras["TOOL_RESULTS_JSON"] = _serialize_tool_results(tool_results)
        return await self._run_event(
            HookEvent.STOP, match_target="stop", env_extras=extras,
        )

    async def _run_event(
        self,
        event: HookEvent,
        *,
        match_target: str,
        env_extras: dict[str, str] | None = None,
    ) -> HookResult:
        entries = self.hooks.get(event, [])
        if not entries:
            return HookResult()

        outputs: list[str] = []
        for entry in entries:
            if not entry.matcher.search(match_target):
                continue
            if entry.run_async:
                self._schedule_async_hook(entry, event, env_extras)
                continue

            stdout, exit_code = await self._exec(entry.command, event, env_extras)
            if stdout:
                outputs.append(stdout)

            # JSON stdout contract (cc-aligned): a hook may emit a JSON object
            # with {"decision": "block"|"allow", "feedback": "...",
            # "additionalContext": "...", "updatedInput": "..."} for richer
            # control than exit codes.
            json_result = _parse_json_stdout(stdout)
            if json_result is not None:
                permission_decision, permission_decision_reason = _extract_permission_fields(json_result)
                additional_context, updated_input = _extract_json_output_fields(json_result)
                if permission_decision:
                    blocked = permission_decision == "deny"
                    message = (
                        permission_decision_reason
                        or str(json_result.get("feedback") or json_result.get("message") or "")
                    )
                    return HookResult(
                        blocked=blocked,
                        message=message or ("Hook denied permission" if blocked else ""),
                        permission_decision=permission_decision,
                        permission_decision_reason=permission_decision_reason,
                        additional_context=additional_context,
                        updated_input=updated_input,
                        stdout="\n".join(outputs),
                    )
                if json_result.get("decision") == "block" or exit_code == 2:
                    feedback = json_result.get("feedback") or (stdout.strip() if exit_code == 2 else "")
                    return HookResult(
                        blocked=json_result.get("decision") == "block",
                        message=str(feedback or json_result.get("message") or "Hook blocked event"),
                        feedback=str(feedback or ""),
                        permission_decision=permission_decision,
                        permission_decision_reason=permission_decision_reason,
                        additional_context=additional_context,
                        updated_input=updated_input,
                        stdout="\n".join(outputs),
                    )
                if json_result.get("feedback"):
                    return HookResult(
                        feedback=str(json_result["feedback"]),
                        permission_decision=permission_decision,
                        permission_decision_reason=permission_decision_reason,
                        additional_context=additional_context,
                        updated_input=updated_input,
                        stdout="\n".join(outputs),
                    )
                if additional_context or updated_input:
                    return HookResult(
                        permission_decision=permission_decision,
                        permission_decision_reason=permission_decision_reason,
                        additional_context=additional_context,
                        updated_input=updated_input,
                        stdout="\n".join(outputs),
                    )

            # Exit code 2: feedback injection (Claude Code semantics)
            if exit_code == 2 and stdout.strip():
                return HookResult(
                    feedback=stdout.strip(),
                    stdout="\n".join(outputs),
                )

            # Exit code 1 (non-zero, not 2): blocking for pre_tool_use, warning for others
            if exit_code != 0 and exit_code != 2:
                if event == HookEvent.PRE_TOOL_USE:
                    msg = stdout[len("BLOCK:"):].strip() if stdout.startswith("BLOCK:") else stdout.strip()
                    return HookResult(
                        blocked=True,
                        message=msg or f"Hook blocked tool '{match_target}'",
                        stdout="\n".join(outputs),
                    )
                logger.warning("Hook exited %d for event %s: %s", exit_code, event.value, stdout[:200])

        return HookResult(stdout="\n".join(outputs))

    def _schedule_async_hook(
        self,
        entry: _HookEntry,
        event: HookEvent,
        env_extras: dict[str, str] | None,
    ) -> None:
        task = asyncio.create_task(
            self._exec(entry.command, event, env_extras, timeout=entry.async_timeout),
            name=f"hook:{event.value}:{entry.raw_matcher or '*'}",
        )
        self._async_tasks.add(task)

        def _done(done: asyncio.Task[Any]) -> None:
            self._async_tasks.discard(done)
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    stdout, exit_code = done.result()
                except Exception as exc:
                    logger.warning("Async hook failed for event %s: %s", event.value, exc)
                    return
                if exit_code not in (0, None):
                    logger.warning("Async hook exited %d for event %s: %s", exit_code, event.value, stdout[:200])

        task.add_done_callback(_done)

    def _tool_env(self, tool_name: str, args: dict[str, Any]) -> dict[str, str]:
        env: dict[str, str] = {"TOOL_NAME": tool_name}
        try:
            env["TOOL_ARGS_JSON"] = json.dumps(args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            env["TOOL_ARGS_JSON"] = "{}"
        path_arg = args.get("path") or args.get("file_path") or args.get("directory")
        if isinstance(path_arg, str):
            env["TOOL_PATH"] = path_arg
        return env

    async def _exec(
        self,
        command: str,
        event: HookEvent,
        env_extras: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[str, int]:
        env = sanitized_subprocess_env()
        env["HOOK_EVENT"] = event.value
        env["HOOK_EVENT_CC"] = cc_event_name(event)
        if env_extras:
            env.update(env_extras)
        hook_input: dict[str, Any] = {
            "hook_event_name": cc_event_name(event),
            "hook_event_name_snake": event.value,
        }
        if env_extras:
            hook_input["env"] = env_extras
        try:
            env["HOOK_INPUT_JSON"] = json.dumps(hook_input, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            env["HOOK_INPUT_JSON"] = json.dumps(
                {"hook_event_name": cc_event_name(event), "hook_event_name_snake": event.value},
                ensure_ascii=False,
            )
        cwd = str(self.workspace_root) if self.workspace_root else None
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=cwd,
            )
        except Exception as exc:
            logger.warning("Failed to spawn hook %r: %s", command, exc)
            return f"hook spawn failed: {exc}", 1
        try:
            effective_timeout = timeout if timeout is not None else _HOOK_TIMEOUT_S
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return f"hook timed out after {effective_timeout}s", 124
        stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
        return stdout, proc.returncode if proc.returncode is not None else 0


_active_manager: HookManager | None = None


def get_hook_manager() -> HookManager | None:
    return _active_manager


def set_hook_manager(manager: HookManager | None) -> None:
    global _active_manager
    _active_manager = manager
