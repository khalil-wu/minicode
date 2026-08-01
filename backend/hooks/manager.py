"""Claude Code-compatible lifecycle hook runner.

Configuration in settings.json:

    {
      "hooks": {
        "UserPromptSubmit": [
          {"matcher": ".*", "hooks": [{"type": "command", "command": "python .claude/on_prompt.py"}]}
        ],
        "PreToolUse": [
          {"matcher": "write_file|edit_file", "hooks": [{"type": "command", "command": "python .claude/check_write.py"}]}
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

Hooks receive one Claude Code-shaped JSON object on stdin. Command output may
use Claude Code's ``hookSpecificOutput`` contract. Exit code 2 supplies
feedback; a non-zero PreToolUse exit blocks the call.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from backend.runtime_env import sanitized_subprocess_env
from backend.subprocesses import communicate, spawn_shell

logger = logging.getLogger(__name__)

# Claude Code's command/tool hook default. Formatters, linters, and project
# validation hooks routinely exceed ten seconds; timing them out early changes a
# valid hook into a misleading non-zero result. Prompt/agent hooks have separate
# shorter limits in CC, but MiniCode currently exposes command hooks only.
_HOOK_TIMEOUT_S = 10 * 60.0
_RESULT_TRUNCATE = 4096
_PROMPT_TRUNCATE = 2000
_DRAFT_TRUNCATE = 4096

_CC_EVENT_NAMES: dict["HookEvent", str] = {}


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


def _extract_permission_fields(
    json_result: dict[str, Any],
    event: "HookEvent",
) -> tuple[str, str]:
    decision = _normalize_permission_decision(
        json_result.get("permissionDecision") or json_result.get("permission_decision")
    )
    reason = _json_text(
        json_result.get("permissionDecisionReason")
        or json_result.get("permission_decision_reason")
        or json_result.get("reason")
    )
    hook_specific = json_result.get("hookSpecificOutput") or json_result.get("hook_specific_output")
    if (
        isinstance(hook_specific, dict)
        and hook_specific.get("hookEventName", hook_specific.get("hook_event_name"))
        == cc_event_name(event)
    ):
        if event == HookEvent.PERMISSION_REQUEST:
            request_decision = hook_specific.get("decision")
            if isinstance(request_decision, dict):
                behavior = _normalize_permission_decision(request_decision.get("behavior"))
                if behavior in {"allow", "deny"}:
                    decision = behavior
                    reason = _json_text(request_decision.get("message") or reason)
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


def _extract_json_output_fields(
    json_result: dict[str, Any],
    event: "HookEvent",
) -> tuple[str, Any]:
    additional_context = str(
        json_result.get("additionalContext")
        or json_result.get("additional_context")
        or ""
    )
    updated_input = json_result.get("updatedInput", json_result.get("updated_input", ""))
    hook_specific = json_result.get("hookSpecificOutput") or json_result.get("hook_specific_output")
    if (
        isinstance(hook_specific, dict)
        and hook_specific.get("hookEventName", hook_specific.get("hook_event_name"))
        == cc_event_name(event)
    ):
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
        if event == HookEvent.PERMISSION_REQUEST:
            request_decision = hook_specific.get("decision")
            if (
                isinstance(request_decision, dict)
                and request_decision.get("behavior") == "allow"
            ):
                updated_input = request_decision.get("updatedInput", updated_input)
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


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _cc_hook_input(
    event: HookEvent,
    fields: dict[str, str] | None,
    workspace_root: Path | None,
) -> dict[str, Any]:
    """Translate internal call-site values to Claude Code's stdin schema."""
    raw = dict(fields or {})
    payload: dict[str, Any] = {
        "session_id": raw.get("SESSION_ID", ""),
        "transcript_path": raw.get("TRANSCRIPT_PATH", ""),
        "cwd": str(workspace_root or Path.cwd()),
        "hook_event_name": cc_event_name(event),
    }
    permission_mode = raw.get("PERMISSION_MODE", "")
    if permission_mode:
        payload["permission_mode"] = permission_mode

    tool_name = raw.get("TOOL_NAME", "")
    tool_input = _json_object(raw.get("TOOL_ARGS_JSON", ""))
    tool_use_id = raw.get("TOOL_CALL_ID", "")
    if event in {
        HookEvent.PRE_TOOL_USE,
        HookEvent.POST_TOOL_USE,
        HookEvent.POST_TOOL_USE_FAILURE,
        HookEvent.PERMISSION_REQUEST,
        HookEvent.PERMISSION_DENIED,
    }:
        payload.update({"tool_name": tool_name, "tool_input": tool_input})
        if event != HookEvent.PERMISSION_REQUEST:
            payload["tool_use_id"] = tool_use_id

    if event == HookEvent.POST_TOOL_USE:
        payload["tool_response"] = raw.get("TOOL_RESULT", "")
    elif event == HookEvent.POST_TOOL_USE_FAILURE:
        payload.update({"error": raw.get("TOOL_ERROR", ""), "is_interrupt": False})
    elif event == HookEvent.PERMISSION_REQUEST:
        payload["permission_suggestions"] = []
    elif event == HookEvent.PERMISSION_DENIED:
        payload["reason"] = raw.get("PERMISSION_DENIED_REASON", "")
    elif event == HookEvent.NOTIFICATION:
        payload.update({
            "message": raw.get("NOTIFICATION_MESSAGE", ""),
            "title": raw.get("NOTIFICATION_TITLE", ""),
            "notification_type": raw.get("NOTIFICATION_TYPE", ""),
        })
    elif event == HookEvent.SETUP:
        payload["trigger"] = raw.get("SETUP_TRIGGER", "")
    elif event == HookEvent.STOP_FAILURE:
        payload.update({
            "error": raw.get("STOP_FAILURE_ERROR", "") or "unknown",
            "error_details": raw.get("STOP_FAILURE_ERROR_DETAILS", ""),
            "last_assistant_message": raw.get("LAST_ASSISTANT_MESSAGE", ""),
        })
    elif event == HookEvent.STOP:
        payload.update({
            "stop_hook_active": False,
            "last_assistant_message": raw.get("DRAFT_REPLY", ""),
        })
    elif event == HookEvent.SUBAGENT_START:
        payload.update({
            "agent_id": raw.get("SUBAGENT_ID", ""),
            "agent_type": raw.get("SUBAGENT_AGENT_TYPE", ""),
        })
    elif event == HookEvent.SUBAGENT_STOP:
        payload.update({
            "stop_hook_active": False,
            "agent_id": raw.get("SUBAGENT_ID", ""),
            "agent_transcript_path": raw.get("SUBAGENT_TRANSCRIPT_PATH", ""),
            "agent_type": raw.get("SUBAGENT_AGENT_TYPE", ""),
            "last_assistant_message": raw.get("SUBAGENT_SUMMARY", ""),
        })
    elif event == HookEvent.TEAMMATE_IDLE:
        payload.update({"teammate_name": raw.get("TEAMMATE_NAME", ""), "team_name": raw.get("TEAM_NAME", "")})
    elif event in {HookEvent.TASK_CREATED, HookEvent.TASK_COMPLETED}:
        payload.update({
            "task_id": raw.get("TASK_ID", ""),
            "task_subject": raw.get("TASK_SUBJECT", ""),
            "task_description": raw.get("TASK_DESCRIPTION", ""),
            "teammate_name": raw.get("TEAMMATE_NAME", ""),
            "team_name": raw.get("TEAM_NAME", ""),
        })
    elif event == HookEvent.USER_PROMPT_SUBMIT:
        payload["prompt"] = raw.get("USER_PROMPT", "")
    elif event == HookEvent.SESSION_START:
        payload["source"] = raw.get("SESSION_SOURCE", "startup")
    elif event == HookEvent.SESSION_END:
        payload["reason"] = raw.get("SESSION_END_REASON", "")
    elif event in {HookEvent.PRE_COMPACT, HookEvent.POST_COMPACT}:
        payload["trigger"] = raw.get("COMPACT_TRIGGER", "manual")
        if event == HookEvent.PRE_COMPACT:
            payload["custom_instructions"] = raw.get("COMPACT_CUSTOM_INSTRUCTIONS") or None
        else:
            payload["compact_summary"] = raw.get("COMPACT_SUMMARY", "")
    elif event == HookEvent.CONFIG_CHANGE:
        payload.update({"source": raw.get("CONFIG_CHANGE_SOURCE", ""), "file_path": raw.get("CONFIG_FILE_PATH", "")})
    elif event == HookEvent.CWD_CHANGED:
        payload.update({"old_cwd": raw.get("OLD_CWD", ""), "new_cwd": raw.get("NEW_CWD", "")})
    elif event == HookEvent.FILE_CHANGED:
        payload.update({"file_path": raw.get("FILE_PATH", ""), "event": raw.get("FILE_EVENT", "")})
    elif event == HookEvent.INSTRUCTIONS_LOADED:
        payload.update({
            "file_path": raw.get("INSTRUCTIONS_FILE_PATH", ""),
            "memory_type": raw.get("INSTRUCTIONS_MEMORY_TYPE", ""),
            "load_reason": raw.get("INSTRUCTIONS_LOAD_REASON", ""),
            "trigger_file_path": raw.get("INSTRUCTIONS_TRIGGER_FILE_PATH", ""),
            "parent_file_path": raw.get("INSTRUCTIONS_PARENT_FILE_PATH", ""),
        })
    elif event == HookEvent.ELICITATION:
        payload.update({
            "mcp_server_name": raw.get("MCP_SERVER_NAME", ""),
            "message": raw.get("ELICITATION_PROMPT", ""),
            "mode": raw.get("ELICITATION_MODE", ""),
            "url": raw.get("ELICITATION_URL", ""),
            "elicitation_id": raw.get("ELICITATION_ID", ""),
        })
    elif event == HookEvent.ELICITATION_RESULT:
        payload.update({
            "mcp_server_name": raw.get("MCP_SERVER_NAME", ""),
            "elicitation_id": raw.get("ELICITATION_ID", ""),
            "mode": raw.get("ELICITATION_MODE", ""),
            "action": raw.get("ELICITATION_ACTION", ""),
            "content": _json_object(raw.get("ELICITATION_CONTENT_JSON", "")),
        })
    elif event == HookEvent.WORKTREE_CREATE:
        payload["name"] = raw.get("WORKTREE_BRANCH", "") or Path(raw.get("WORKTREE_PATH", "worktree")).name
    elif event == HookEvent.WORKTREE_REMOVE:
        payload["worktree_path"] = raw.get("WORKTREE_PATH", "")
    return payload


def _hook_config_keys() -> dict[str, HookEvent]:
    return {camel: event for event, camel in _CC_EVENT_NAMES.items()}


def _read_settings_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _merge_hook_settings(settings_layers: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge CC hook groups across scopes without inventing precedence rules.

    Claude Code runs matching hooks from every active settings scope. Hook
    groups therefore append in scope order instead of replacing one another.
    """
    merged: dict[str, list[Any]] = {}
    for settings in settings_layers:
        hooks = settings.get("hooks") if isinstance(settings, dict) else None
        if not isinstance(hooks, dict):
            continue
        for event_name in _hook_config_keys():
            groups = hooks.get(event_name)
            if isinstance(groups, list):
                merged.setdefault(event_name, []).extend(groups)
    return {"hooks": merged}


def _trusted_workspace_roots() -> set[str]:
    """Read the desktop main process' authoritative workspace-trust ledger.

    Project hooks execute arbitrary commands. Claude Code only enables them
    after workspace trust, so the backend must not infer trust merely because
    a path arrived in a WebSocket payload. The Electron main process persists
    native-picker approvals in this ledger under the shared state root.
    """
    from backend.config import DATA_ROOT

    ledger = Path(DATA_ROOT) / "trusted_workspaces.json"
    try:
        payload = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return set()
    values = payload if isinstance(payload, list) else payload.get("roots", []) if isinstance(payload, dict) else []
    if not isinstance(values, list):
        return set()
    trusted: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            candidate = Path(value).expanduser().resolve()
            if candidate.is_dir():
                trusted.add(str(candidate).casefold())
        except OSError:
            continue
    return trusted


def is_workspace_trusted_for_hooks(workspace_root: Path | None) -> bool:
    """Return whether project command hooks may execute for ``workspace_root``."""
    if workspace_root is None:
        return False
    try:
        resolved = Path(workspace_root).expanduser().resolve()
    except OSError:
        return False
    return resolved.is_dir() and str(resolved).casefold() in _trusted_workspace_roots()


def load_hook_manager_for_workspace(workspace_root: Path | None) -> "HookManager":
    """Load app/user hooks and trusted project/local hook scopes for a turn."""
    from backend.config import SETTINGS_FILE

    paths = [Path(SETTINGS_FILE), Path.home() / ".claude" / "settings.json"]
    resolved_root = Path(workspace_root).resolve() if workspace_root is not None else None
    if resolved_root is not None and is_workspace_trusted_for_hooks(resolved_root):
        paths.extend(
            [
                resolved_root / ".claude" / "settings.json",
                resolved_root / ".claude" / "settings.local.json",
            ]
        )
    layers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        layers.append(_read_settings_file(path))
    try:
        from backend.services.plugin_settings_service import (
            load_enabled_plugin_hook_settings,
        )

        layers.extend(load_enabled_plugin_hook_settings())
    except Exception as exc:
        logger.warning("Failed to load enabled plugin hooks: %s", exc)
    return HookManager.from_settings(
        _merge_hook_settings(layers),
        workspace_root=resolved_root,
    )


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
    updated_input: Any = None
    updated_mcp_tool_output: Any = None
    retry: bool = False
    prevent_continuation: bool = False
    stop_reason: str = ""
    system_message: str = ""
    initial_user_message: str = ""
    watch_paths: tuple[str, ...] = ()

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
    plugin_root: str = ""


@dataclass
class HookManager:
    hooks: dict[HookEvent, list[_HookEntry]] = field(default_factory=dict)
    workspace_root: Path | None = None
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
            for group in entries:
                if not isinstance(group, dict):
                    continue
                matcher_raw = str(group.get("matcher", ".*"))
                try:
                    pattern = re.compile(matcher_raw)
                except re.error as exc:
                    logger.warning("Skipping hook with invalid matcher %r: %s", matcher_raw, exc)
                    continue
                hook_defs = group.get("hooks")
                if not isinstance(hook_defs, list):
                    continue
                for hook in hook_defs:
                    if not isinstance(hook, dict) or str(hook.get("type") or "command") != "command":
                        continue
                    command = str(hook.get("command", "")).strip()
                    if not command:
                        continue
                    run_async = _coerce_hook_bool(hook.get("async", False))
                    async_timeout = _coerce_async_timeout(hook.get("timeout"))
                    plugin_root = str(hook.get("_plugin_root") or "").strip()
                    mgr.hooks[event].append(
                        _HookEntry(
                            matcher=pattern,
                            command=command,
                            raw_matcher=matcher_raw,
                            run_async=run_async,
                            async_timeout=async_timeout,
                            plugin_root=plugin_root,
                        )
                    )
        return mgr

    def has_hooks(self, event: HookEvent) -> bool:
        return bool(self.hooks.get(event))

    @property
    def pending_async_hooks(self) -> int:
        return len(self._async_tasks)

    async def drain_async_hooks(self) -> None:
        while self._async_tasks:
            tasks = tuple(self._async_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_pre_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        tool_call_id: str = "",
        session_id: str = "",
        permission_mode: str = "",
    ) -> HookResult:
        fields = self._tool_env(tool_name, args)
        fields["TOOL_CALL_ID"] = tool_call_id
        fields["SESSION_ID"] = session_id
        fields["PERMISSION_MODE"] = permission_mode
        return await self._run_event(
            HookEvent.PRE_TOOL_USE, match_target=tool_name,
            env_extras=fields,
        )

    async def run_post_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        result_content: str,
        *,
        tool_call_id: str = "",
        session_id: str = "",
        permission_mode: str = "",
    ) -> HookResult:
        extras = self._tool_env(tool_name, args)
        extras["TOOL_CALL_ID"] = tool_call_id
        extras["SESSION_ID"] = session_id
        extras["PERMISSION_MODE"] = permission_mode
        extras["TOOL_RESULT"] = result_content[:_RESULT_TRUNCATE]
        return await self._run_event(
            HookEvent.POST_TOOL_USE, match_target=tool_name, env_extras=extras,
        )

    async def run_post_tool_failure(
        self,
        tool_name: str,
        args: dict[str, Any],
        error_content: str,
        *,
        tool_call_id: str = "",
        session_id: str = "",
        permission_mode: str = "",
    ) -> HookResult:
        """Fires when a tool call errors — useful for logging/alerting/telemetry."""
        extras = self._tool_env(tool_name, args)
        extras["TOOL_CALL_ID"] = tool_call_id
        extras["SESSION_ID"] = session_id
        extras["PERMISSION_MODE"] = permission_mode
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

    async def run_post_compact(
        self,
        *,
        summary: str = "",
        trigger: str = "auto",
    ) -> HookResult:
        """Fires after context compaction completes (symmetric with pre_compact)."""
        return await self._run_event(
            HookEvent.POST_COMPACT,
            match_target=trigger,
            env_extras={
                "COMPACT_TRIGGER": trigger,
                "COMPACT_SUMMARY": summary[:_RESULT_TRUNCATE],
            },
        )

    async def run_permission_request(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        reason: str = "",
        permission_level: str = "",
        tool_call_id: str = "",
        session_id: str = "",
        permission_mode: str = "",
    ) -> HookResult:
        extras = self._tool_env(tool_name, args)
        extras["TOOL_CALL_ID"] = tool_call_id
        extras["PERMISSION_REASON"] = reason
        extras["PERMISSION_LEVEL"] = permission_level
        extras["SESSION_ID"] = session_id
        extras["PERMISSION_MODE"] = permission_mode
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
        tool_call_id: str = "",
        session_id: str = "",
        permission_mode: str = "",
    ) -> HookResult:
        extras = self._tool_env(tool_name, args)
        extras["TOOL_CALL_ID"] = tool_call_id
        extras["PERMISSION_DENIED_REASON"] = reason
        extras["PERMISSION_LEVEL"] = permission_level
        extras["SESSION_ID"] = session_id
        extras["PERMISSION_MODE"] = permission_mode
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
        try:
            return await self._run_event(
                HookEvent.SESSION_END,
                match_target=session_id or "session",
                env_extras={
                    "SESSION_ID": session_id,
                    "SESSION_END_REASON": reason,
                },
            )
        finally:
            if session_id:
                _session_hook_managers.pop(session_id, None)

    async def run_session_start(self, session_id: str = "") -> HookResult:
        return await self._run_event(
            HookEvent.SESSION_START,
            match_target="session",
            env_extras={"SESSION_ID": session_id, "SESSION_SOURCE": "startup"},
        )

    async def run_session_start_once(self, session_id: str) -> HookResult:
        """Fire session_start once per session (first agent turn). No-op on
        subsequent turns of the same session."""
        if not session_id or session_id in _session_hook_managers:
            return HookResult()
        _session_hook_managers[session_id] = self
        return await self.run_session_start(session_id)

    async def run_user_prompt_submit(self, user_message: str) -> HookResult:
        return await self._run_event(
            HookEvent.USER_PROMPT_SUBMIT, match_target="prompt",
            env_extras={"USER_PROMPT": user_message[:_PROMPT_TRUNCATE]},
        )

    async def run_pre_compact(
        self,
        *,
        trigger: str = "auto",
        custom_instructions: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.PRE_COMPACT,
            match_target=trigger,
            env_extras={
                "COMPACT_TRIGGER": trigger,
                "COMPACT_CUSTOM_INSTRUCTIONS": custom_instructions[:_PROMPT_TRUNCATE],
            },
        )

    async def run_stop(
        self,
        user_message: str,
        draft_reply: str,
        tool_results: list[Any] | None = None,
    ) -> HookResult:
        del user_message, tool_results
        extras: dict[str, str] = {
            "DRAFT_REPLY": draft_reply[:_DRAFT_TRUNCATE],
        }
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
        feedback_parts: list[str] = []
        context_parts: list[str] = []
        updated_input: Any = None
        permission_decision = ""
        permission_decision_reason = ""
        decision_rank = {"": 0, "allow": 1, "ask": 2, "deny": 3}
        blocked = False
        block_message = ""
        updated_mcp_tool_output: Any = None
        retry = False
        prevent_continuation = False
        stop_reason = ""
        system_messages: list[str] = []
        initial_user_message = ""
        watch_paths: list[str] = []
        for entry in entries:
            if not entry.matcher.search(match_target):
                continue
            if entry.run_async:
                self._schedule_async_hook(entry, event, env_extras)
                continue

            stdout, stderr, exit_code = await self._exec(
                entry.command,
                event,
                env_extras,
                timeout=entry.async_timeout,
                plugin_root=entry.plugin_root,
            )
            if stdout:
                outputs.append(stdout)

            # JSON stdout contract (cc-aligned). Common ``decision`` accepts
            # approve/block; event-specific fields live in hookSpecificOutput.
            json_result = _parse_json_stdout(stdout)
            if json_result is not None:
                if json_result.get("continue") is False:
                    prevent_continuation = True
                    stop_reason = str(json_result.get("stopReason") or "")
                if json_result.get("systemMessage"):
                    system_messages.append(str(json_result["systemMessage"]))
                decision, decision_reason = _extract_permission_fields(json_result, event)
                additional_context, candidate_input = _extract_json_output_fields(json_result, event)
                common_decision = str(json_result.get("decision") or "").strip().lower()
                if common_decision == "approve" and not decision:
                    decision = "allow"
                elif common_decision == "block":
                    decision = "deny"
                    decision_reason = str(json_result.get("reason") or decision_reason or "")
                if additional_context:
                    context_parts.append(additional_context)
                if candidate_input not in (None, ""):
                    # CC applies hook input updates in registration order.
                    updated_input = candidate_input
                if decision:
                    if decision_rank[decision] >= decision_rank[permission_decision]:
                        permission_decision = decision
                        permission_decision_reason = decision_reason
                    if decision == "deny":
                        blocked = True
                        block_message = (
                            decision_reason
                            or str(json_result.get("feedback") or json_result.get("message") or "")
                            or "Hook denied permission"
                        )
                if common_decision == "block":
                    feedback = str(
                        json_result.get("reason")
                        or json_result.get("feedback")
                        or json_result.get("message")
                        or "Hook blocked event"
                    )
                    blocked = True
                    block_message = feedback
                    feedback_parts.append(feedback)
                if json_result.get("feedback"):
                    feedback = str(json_result["feedback"])
                    if feedback not in feedback_parts:
                        feedback_parts.append(feedback)
                hook_specific = json_result.get("hookSpecificOutput")
                if (
                    isinstance(hook_specific, dict)
                    and hook_specific.get("hookEventName") == cc_event_name(event)
                ):
                    if event == HookEvent.SESSION_START:
                        if hook_specific.get("initialUserMessage"):
                            initial_user_message = str(hook_specific["initialUserMessage"])
                        raw_watch_paths = hook_specific.get("watchPaths")
                        if isinstance(raw_watch_paths, list):
                            watch_paths.extend(
                                str(path) for path in raw_watch_paths
                                if isinstance(path, str) and path.strip()
                            )
                    if event == HookEvent.POST_TOOL_USE and "updatedMCPToolOutput" in hook_specific:
                        updated_mcp_tool_output = hook_specific["updatedMCPToolOutput"]
                    if event == HookEvent.PERMISSION_DENIED and hook_specific.get("retry") is True:
                        retry = True

            # Claude Code reserves exit code 2 for a blocking hook and reads the
            # feedback from stderr (falling back to stdout). Streams are captured
            # separately so a hook's JSON decision on stdout is never corrupted
            # by warnings/diagnostics it also writes to stderr.
            if exit_code == 2:
                feedback = stderr.strip() or stdout.strip() or "Hook blocked event"
                blocked = True
                block_message = feedback
                if feedback not in feedback_parts:
                    feedback_parts.append(feedback)

            # Other non-zero statuses are non-blocking hook failures in CC.
            if exit_code != 0 and exit_code != 2:
                logger.warning(
                    "Hook exited %d for event %s: stdout=%s stderr=%s",
                    exit_code, event.value, stdout[:200], stderr[:200],
                )

        if prevent_continuation and event != HookEvent.STOP:
            blocked = True
            block_message = stop_reason or block_message or "Hook prevented continuation"

        return HookResult(
            blocked=blocked,
            message=block_message,
            feedback="\n".join(feedback_parts),
            stdout="\n".join(outputs),
            permission_decision=("deny" if blocked else permission_decision),
            permission_decision_reason=permission_decision_reason,
            additional_context="\n\n".join(context_parts),
            updated_input=updated_input,
            updated_mcp_tool_output=updated_mcp_tool_output,
            retry=retry,
            prevent_continuation=prevent_continuation,
            stop_reason=stop_reason,
            system_message="\n".join(system_messages),
            initial_user_message=initial_user_message,
            watch_paths=tuple(dict.fromkeys(watch_paths)),
        )

    def _schedule_async_hook(
        self,
        entry: _HookEntry,
        event: HookEvent,
        env_extras: dict[str, str] | None,
    ) -> None:
        task = asyncio.create_task(
            self._exec(
                entry.command,
                event,
                env_extras,
                timeout=entry.async_timeout,
                plugin_root=entry.plugin_root,
            ),
            name=f"hook:{event.value}:{entry.raw_matcher or '*'}",
        )
        self._async_tasks.add(task)

        def _done(done: asyncio.Task[Any]) -> None:
            self._async_tasks.discard(done)
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    stdout, stderr, exit_code = done.result()
                except Exception as exc:
                    logger.warning("Async hook failed for event %s: %s", event.value, exc)
                    return
                if exit_code not in (0, None):
                    logger.warning(
                        "Async hook exited %d for event %s: stdout=%s stderr=%s",
                        exit_code, event.value, stdout[:200], stderr[:200],
                    )

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
        plugin_root: str = "",
    ) -> tuple[str, str, int]:
        env = sanitized_subprocess_env()
        env["HOOK_EVENT"] = event.value
        env["HOOK_EVENT_CC"] = cc_event_name(event)
        if self.workspace_root is not None:
            # CC exposes the stable project root to every command hook. Hook
            # scripts use this instead of relying on the process install cwd.
            env["CLAUDE_PROJECT_DIR"] = str(self.workspace_root.resolve())
        if plugin_root:
            resolved_plugin_root = str(Path(plugin_root).resolve())
            env["CLAUDE_PLUGIN_ROOT"] = resolved_plugin_root
            command = command.replace("${CLAUDE_PLUGIN_ROOT}", resolved_plugin_root)
        hook_input = _cc_hook_input(event, env_extras, self.workspace_root)
        try:
            # Keep the shell boundary ASCII-only on Windows. PowerShell 5.1
            # transcodes native-process pipelines through the active codepage;
            # JSON unicode escapes preserve the exact CC payload regardless of
            # that codepage.
            hook_input_bytes = json.dumps(hook_input, ensure_ascii=True, default=str).encode("ascii")
        except (TypeError, ValueError):
            hook_input_bytes = json.dumps(
                _cc_hook_input(event, {}, self.workspace_root),
                ensure_ascii=True,
            ).encode("ascii")
        cwd = str(self.workspace_root) if self.workspace_root else None
        shell_command = command
        if __import__("sys").platform == "win32":
            # Reuse the same UTF-16LE EncodedCommand wrapper as run_command.
            # cmd.exe corrupts non-ASCII workspace paths before the hook
            # process even starts; PowerShell's encoded form preserves both
            # command text and native exit status.
            from backend.tools.command_tool import _host_shell_command

            shell_command = _host_shell_command(
                "$minicodeHookInput = [Console]::In.ReadToEnd(); "
                f"$minicodeHookInput | & {{ $input | {command} }}"
            )
        try:
            proc = await spawn_shell(
                shell_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
            )
        except Exception as exc:
            logger.warning("Failed to spawn hook %r: %s", command, exc)
            return f"hook spawn failed: {exc}", "", 1
        try:
            effective_timeout = timeout if timeout is not None else _HOOK_TIMEOUT_S
            stdout_b, stderr_b = await communicate(proc, hook_input_bytes, timeout=effective_timeout)
        except asyncio.TimeoutError:
            return f"hook timed out after {effective_timeout}s", "", 124
        stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
        stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()
        return stdout, stderr, proc.returncode if proc.returncode is not None else 0


_active_manager: HookManager | None = None
_bound_manager: contextvars.ContextVar[HookManager | None] = contextvars.ContextVar(
    "minicode_hook_manager",
    default=None,
)
_session_hook_managers: dict[str, HookManager] = {}


def get_hook_manager() -> HookManager | None:
    return _bound_manager.get() or _active_manager


def get_hook_manager_for_session(session_id: str) -> HookManager | None:
    clean_session_id = str(session_id or "").strip()
    return _session_hook_managers.get(clean_session_id) if clean_session_id else None


def register_hook_manager_for_session(session_id: str, manager: HookManager) -> None:
    clean_session_id = str(session_id or "").strip()
    if clean_session_id:
        _session_hook_managers[clean_session_id] = manager


def bind_hook_manager(manager: HookManager) -> contextvars.Token[HookManager | None]:
    return _bound_manager.set(manager)


def unbind_hook_manager(token: contextvars.Token[HookManager | None]) -> None:
    _bound_manager.reset(token)


def set_hook_manager(manager: HookManager | None) -> None:
    global _active_manager
    _active_manager = manager
