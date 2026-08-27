"""MiniCode lifecycle hook runner.

Configuration is loaded from MiniCode's config.toml/settings hook layers:

    {
      "hooks": {
        "user_prompt_submit": [
          {"matcher": ".*", "hooks": [{"type": "command", "command": "python .minicode/on_prompt.py"}]}
        ],
        "pre_tool_use": [
          {"matcher": "write_file|edit_file", "hooks": [{"type": "command", "command": "python .minicode/check_write.py"}]}
        ]
      }
    }

Hook events:
  - session_start:       fires once when agent session begins
  - user_prompt_submit:  fires after user message received, before model call
  - pre_tool_use:        fires before each tool execution
  - post_tool_use:       fires after each tool execution
  - pre_compact:         fires before context compaction
  - stop:               fires when model produces final reply (no more tool calls)

Hooks receive one MiniCode JSON object on stdin and return one top-level
snake_case JSON object. Exit code 2 supplies feedback; an explicit deny or
blocking exit from pre_tool_use blocks the call.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.hooks.models import HookEvent

logger = logging.getLogger(__name__)

# Hook payloads are passed in full over stdin, so OS environment limits do not
# constrain prompt and tool-result fields.
_RESULT_TRUNCATE = 50_000
_PROMPT_TRUNCATE = 50_000
_DRAFT_TRUNCATE = 50_000

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


def _async_hook_context_messages(stdout: str, entry: Any) -> tuple[str, ...]:
    """Extract model context from one completed async hook response.

    The async registry scans stdout line-by-line, skips the
    ``{"async": true}`` handshake, and gives the model only ``system_message``
    plus ``additional_context``. Plain diagnostic stdout is
    retained for the hook response/UI, but it is not silently promoted into a
    new model steer.
    """

    response: dict[str, Any] | None = None
    for line in str(stdout or "").splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict) or "async" in parsed:
            continue
        response = parsed
        break
    if response is None:
        return ()

    from backend.hooks.reducer import limit_additional_context

    messages: list[str] = []
    system_message = response.get("system_message")
    if isinstance(system_message, str) and system_message.strip():
        messages.append(limit_additional_context(system_message.strip(), entry))
    additional = response.get("additional_context")
    if isinstance(additional, str) and additional.strip():
        messages.append(limit_additional_context(additional.strip(), entry))
    return tuple(messages)


async def _emit_async_hook_response(
    *,
    process_id: str,
    entry: Any,
    event: "HookEvent",
    stdout: str,
    stderr: str,
    exit_code: int | None,
    outcome: str,
    tool_context: Any | None,
) -> None:
    """Project a hook response through MiniCode's item stream."""

    emit = getattr(tool_context, "emit_event", None) if tool_context is not None else None
    if not callable(emit):
        return
    from backend.agent.message import AgentEvent

    title = str(entry.status_message or "").strip() or f"{hook_event_name(event)} hook"
    output = f"{stdout}{stderr}"
    if not output.strip():
        output = (
            f"{title} cancelled"
            if outcome == "cancelled"
            else f"{title} completed with exit code {exit_code if exit_code is not None else 1}"
        )
    status = "completed" if outcome == "success" else "failed"
    response_event = AgentEvent.agent_item(
        id=process_id,
        kind="hook_response",
        content=output,
        role="runtime",
        source="hook",
        status=status,
        title=title,
        summary=(
            "Hook completed"
            if outcome == "success"
            else "Hook cancelled"
            if outcome == "cancelled"
            else f"Hook exited {exit_code if exit_code is not None else 1}"
        ),
        visibility="timeline",
        default_collapsed=True,
        source_level=str(entry.plugin_id or entry.source or "settings"),
        reason=outcome,
    )
    try:
        await emit(response_event.type, dict(response_event.data))
    except Exception as exc:
        logger.debug("Async HookResponse emit failed for %s: %s", process_id, exc)


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


def _canonical_config_change_source(source: str, file_path: str = "") -> str:
    raw = str(source or "").strip().casefold()
    if raw in {"user_settings", "project_settings", "local_settings", "policy_settings", "skills"}:
        return raw
    path = str(file_path or "").casefold()
    if "policy" in raw or "managed" in raw or "requirements" in path:
        return "policy_settings"
    if "skill" in raw or "skill" in path:
        return "skills"
    if "project" in raw or "settings.local" in path:
        return "local_settings" if "local" in path else "project_settings"
    return "user_settings"


def _canonical_session_end_reason(reason: str) -> str:
    value = str(reason or "").strip().casefold()
    allowed = {
        "clear",
        "resume",
        "logout",
        "prompt_input_exit",
        "other",
        "bypass_permissions_disabled",
    }
    return value if value in allowed else "other"


def hook_event_name(event: HookEvent) -> str:
    return event.value


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


def _json_value(value: Any, default: Any = "") -> Any:
    if not isinstance(value, str):
        return value if value is not None else default
    text = value.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return value


def _substitute_hook_arguments(prompt: str, json_input: str) -> str:
    """Apply $ARGUMENTS and indexed argument substitution."""

    try:
        parsed_args = shlex.split(json_input)
    except ValueError:
        parsed_args = [item for item in json_input.split() if item]
    original = prompt
    prompt = re.sub(
        r"\$ARGUMENTS\[(\d+)\]",
        lambda match: parsed_args[int(match.group(1))]
        if int(match.group(1)) < len(parsed_args)
        else "",
        prompt,
    )
    prompt = re.sub(
        r"\$(\d+)(?!\w)",
        lambda match: parsed_args[int(match.group(1))]
        if int(match.group(1)) < len(parsed_args)
        else "",
        prompt,
    )
    prompt = prompt.replace("$ARGUMENTS", json_input)
    if prompt == original and json_input:
        prompt = f"{prompt}\n\nARGUMENTS: {json_input}"
    return prompt


def _hook_verdict(stdout: str) -> tuple[bool, str] | None:
    try:
        payload = json.loads(stdout.strip())
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        return None
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        return None
    return bool(payload["ok"]), str(reason or "")


def _hook_condition_matches(
    condition: str,
    *,
    event: "HookEvent | None" = None,
    match_target: str,
    env_extras: dict[str, str] | None,
    tool_registry: Any | None = None,
) -> bool:
    if not condition:
        return True
    if event not in {
        HookEvent.PRE_TOOL_USE,
        HookEvent.POST_TOOL_USE,
        HookEvent.POST_TOOL_USE_FAILURE,
        HookEvent.PERMISSION_REQUEST,
    }:
        return False
    fields = env_extras or {}
    tool_name = fields.get("TOOL_NAME", match_target)
    arguments = _json_object(fields.get("TOOL_ARGS_JSON", ""))
    get_tool = getattr(tool_registry, "get_tool", None)
    tool = get_tool(tool_name) if callable(get_tool) else None
    if tool is None:
        return False
    from backend.permissions.content_rules import parse_content_rule, rule_matches_call
    from backend.tools.base import validate_tool_input

    if validate_tool_input(tool, arguments):
        return False
    rule = parse_content_rule(condition)
    return bool(rule is not None and rule_matches_call(rule, tool_name, arguments))


def _hook_input(
    event: HookEvent,
    fields: dict[str, str] | None,
    workspace_root: Path | None,
) -> dict[str, Any]:
    """Translate internal call-site values to MiniCode's stdin schema."""
    raw = dict(fields or {})
    payload: dict[str, Any] = {
        "session_id": raw.get("SESSION_ID", ""),
        "transcript_path": raw.get("TRANSCRIPT_PATH", ""),
        "cwd": str(workspace_root or Path.cwd()),
        "event": hook_event_name(event),
    }
    permission_mode = raw.get("PERMISSION_MODE", "")
    if permission_mode:
        payload["permission_mode"] = permission_mode
    for key, field_name in (("AGENT_ID", "agent_id"), ("AGENT_TYPE", "agent_type")):
        value = raw.get(key, "")
        if value:
            payload[field_name] = value

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
        payload["tool_response"] = _json_value(raw.get("TOOL_RESULT", ""), "")
    elif event == HookEvent.POST_TOOL_USE_FAILURE:
        payload.update({
            "error": raw.get("TOOL_ERROR", ""),
            "is_interrupt": _coerce_hook_bool(raw.get("TOOL_IS_INTERRUPT"), False),
        })
    elif event == HookEvent.PERMISSION_REQUEST:
        suggestions = _json_value(raw.get("PERMISSION_SUGGESTIONS_JSON", ""), [])
        payload["permission_suggestions"] = suggestions if isinstance(suggestions, list) else []
    elif event == HookEvent.PERMISSION_DENIED:
        payload["reason"] = raw.get("PERMISSION_DENIED_REASON", "")
    elif event == HookEvent.NOTIFICATION:
        payload.update({
            "message": raw.get("NOTIFICATION_MESSAGE", ""),
            "title": raw.get("NOTIFICATION_TITLE", ""),
            "notification_type": raw.get("NOTIFICATION_TYPE", ""),
        })
    elif event == HookEvent.STOP_FAILURE:
        payload.update({
            "error": raw.get("STOP_FAILURE_ERROR", "") or "unknown",
            "error_details": raw.get("STOP_FAILURE_ERROR_DETAILS", ""),
            "last_assistant_message": raw.get("LAST_ASSISTANT_MESSAGE", ""),
        })
    elif event == HookEvent.STOP:
        payload.update({
            "stop_hook_active": _coerce_hook_bool(
                raw.get("STOP_HOOK_ACTIVE"), False
            ),
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
        payload["source"] = raw.get("SESSION_SOURCE", "")
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
        payload.update({
            "file_path": raw.get("FILE_PATH", ""),
            "change_kind": raw.get("FILE_EVENT", ""),
        })
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
            "requested_schema": _json_value(raw.get("ELICITATION_REQUESTED_SCHEMA_JSON", ""), None),
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
    return {event.value: event for event in HookEvent}


def _read_settings_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _merge_hook_settings(settings_layers: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge hook groups across scopes without inventing precedence rules.

    Matching hooks from every active settings scope append in scope order
    instead of replacing one another.
    """
    merged: dict[str, list[Any]] = {}
    allowed_http_urls: list[str] | None = None
    allowed_env_vars: list[str] | None = None
    disable_all_hooks: bool | None = None
    for settings in settings_layers:
        hooks = settings.get("hooks") if isinstance(settings, dict) else None
        if isinstance(hooks, dict):
            for event_name in _hook_config_keys():
                groups = hooks.get(event_name)
                if isinstance(groups, list):
                    merged.setdefault(event_name, []).extend(groups)
        for names, current in (
            (("allowed_http_hook_urls",), allowed_http_urls),
            (("http_hook_allowed_env_vars",), allowed_env_vars),
        ):
            value = next((settings[name] for name in names if name in settings), None)
            if value is None:
                continue
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                logger.warning("Ignoring invalid %s hook policy", names[0])
                continue
            if current is None:
                current = []
                if names[0] == "allowed_http_hook_urls":
                    allowed_http_urls = current
                else:
                    allowed_env_vars = current
            for item in value:
                if item not in current:
                    current.append(item)
        if "disable_all_hooks" in settings:
            disable_all_hooks = _coerce_hook_bool(
                settings.get("disable_all_hooks"),
                False,
            )
    result: dict[str, Any] = {
        "hooks": merged,
        "disable_all_hooks": bool(disable_all_hooks),
    }
    if allowed_http_urls is not None:
        result["allowed_http_hook_urls"] = allowed_http_urls
    if allowed_env_vars is not None:
        result["http_hook_allowed_env_vars"] = allowed_env_vars
    return result


def _trusted_workspace_roots() -> set[str]:
    """Read the desktop main process' authoritative workspace-trust ledger.

    Project hooks execute arbitrary commands and require workspace trust, so
    the backend must not infer trust merely because
    a path arrived in a WebSocket payload. The Electron main process persists
    native-picker approvals in this ledger under the shared state root.
    """
    from backend.workspace.trust import trusted_workspace_roots

    return {os.path.normcase(str(root)) for root in trusted_workspace_roots()}


def is_workspace_trusted_for_hooks(workspace_root: Path | None) -> bool:
    """Return whether project command hooks may execute for ``workspace_root``."""
    from backend.workspace.trust import is_workspace_trusted

    return is_workspace_trusted(workspace_root)


def load_hook_manager_for_workspace(
    workspace_root: Path | None,
    *,
    requirements: Any | None = None,
    config_layer_stack: Any | None = None,
    plugin_sources: Any | None = None,
    session_id: str = "",
) -> "HookManager":
    """Load hooks exclusively from MiniCode's immutable config snapshot."""
    if config_layer_stack is None:
        from backend.config import load_config_layer_stack

        config_layer_stack = load_config_layer_stack(cwd=workspace_root)

    from backend.hooks.discovery import discover_hook_snapshot

    snapshot = discover_hook_snapshot(
        config_stack=config_layer_stack,
        workspace_root=workspace_root,
        workspace_trusted=(
            workspace_root is None or is_workspace_trusted_for_hooks(workspace_root)
        ),
        plugin_sources=plugin_sources,
    )
    previous = get_hook_manager_for_session(session_id)
    if previous is not None and previous.registry_fingerprint == snapshot.fingerprint:
        return previous.fork_for_turn(workspace_root=workspace_root)
    manager = HookManager.from_snapshot(snapshot, workspace_root=workspace_root)
    if previous is not None:
        manager.adopt_session_runtime(previous)
    return manager


@dataclass
class HookResult:
    blocked: bool = False
    failed: bool = False
    message: str = ""
    feedback: str = ""
    stdout: str = ""
    errors: tuple[str, ...] = ()
    # Hook-provided permission decision: allow, deny, or ask.
    permission_decision: str = ""
    permission_decision_reason: str = ""
    # Extra context injected into the conversation.
    additional_context: str = ""
    custom_instructions: str = ""
    # Replacement text for user_prompt_submit hooks.
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
    hook_type: str = "command"
    command: str = ""
    prompt: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    allowed_env_vars: tuple[str, ...] = ()
    model: str = ""
    condition: str = ""
    once: bool = False
    entry_id: str = ""
    raw_matcher: str = ""
    run_async: bool = False
    async_timeout: float | None = None
    plugin_root: str = ""
    plugin_data_root: str = ""
    plugin_id: str = ""
    shell: str = ""
    status_message: str = ""
    additional_context_limit: int | None = None
    async_rewake: bool = False
    source_path: str = ""
    source: str = "unknown"
    display_order: int = 0
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class _HookSessionRuntime:
    """Session registry kept separate from one config snapshot.

    MiniCode replaces active hook configuration atomically but keeps
    already-started async hook processes in ``AsyncHookRegistry``.  A new
    MiniCode HookManager therefore receives a fresh turn binding and snapshot
    while sharing only session lifetime state with the previous generation.
    """

    async_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    consumed_once_hooks: set[str] = field(default_factory=set)
    inflight_once_hooks: set[str] = field(default_factory=set)
    async_context: list[str] = field(default_factory=list)
    session_started: bool = False


@dataclass
class HookManager:
    hooks: dict[HookEvent, list[_HookEntry]] = field(default_factory=dict)
    workspace_root: Path | None = None
    allowed_http_hook_urls: tuple[str, ...] | None = None
    http_hook_allowed_env_vars: tuple[str, ...] | None = None
    snapshot: Any | None = None
    registry_fingerprint: str = ""
    _session_runtime: _HookSessionRuntime = field(
        default_factory=_HookSessionRuntime,
        repr=False,
    )
    _last_run_summaries: tuple[dict[str, Any], ...] = ()
    owner_session_id: str = ""
    scope_id: str = ""
    _llm: Any | None = field(default=None, repr=False)
    _tool_registry: Any | None = field(default=None, repr=False)
    _tool_context: Any | None = field(default=None, repr=False)

    # Legacy compatibility properties
    @property
    def pre_tool(self) -> list[_HookEntry]:
        return self.hooks.get(HookEvent.PRE_TOOL_USE, [])

    @property
    def post_tool(self) -> list[_HookEntry]:
        return self.hooks.get(HookEvent.POST_TOOL_USE, [])

    @property
    def _async_tasks(self) -> set[asyncio.Task[Any]]:
        return self._session_runtime.async_tasks

    @property
    def _consumed_once_hooks(self) -> set[str]:
        return self._session_runtime.consumed_once_hooks

    @property
    def _inflight_once_hooks(self) -> set[str]:
        return self._session_runtime.inflight_once_hooks

    @property
    def _async_context(self) -> list[str]:
        return self._session_runtime.async_context

    @property
    def _session_started(self) -> bool:
        return self._session_runtime.session_started

    @_session_started.setter
    def _session_started(self, value: bool) -> None:
        self._session_runtime.session_started = bool(value)

    def adopt_session_runtime(self, previous: "HookManager") -> None:
        self._session_runtime = previous._session_runtime
        self.owner_session_id = previous.owner_session_id
        self.scope_id = previous.scope_id

    def fork_for_turn(self, *, workspace_root: Path | None) -> "HookManager":
        """Create one turn-bound manager over the same immutable registry."""

        return HookManager(
            hooks=self.hooks,
            workspace_root=workspace_root,
            allowed_http_hook_urls=self.allowed_http_hook_urls,
            http_hook_allowed_env_vars=self.http_hook_allowed_env_vars,
            snapshot=self.snapshot,
            registry_fingerprint=self.registry_fingerprint,
            _session_runtime=self._session_runtime,
            owner_session_id=self.owner_session_id,
            scope_id=self.scope_id,
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Any,
        *,
        workspace_root: Path | None = None,
    ) -> "HookManager":
        mgr = cls(
            workspace_root=workspace_root,
            allowed_http_hook_urls=snapshot.allowed_http_hook_urls,
            http_hook_allowed_env_vars=snapshot.http_hook_allowed_env_vars,
            snapshot=snapshot,
            registry_fingerprint=snapshot.fingerprint,
        )
        for definition in snapshot.executable_entries:
            try:
                event = HookEvent(definition.event)
            except ValueError:
                continue
            try:
                from backend.hooks.dispatcher import compile_hook_matcher

                matcher = compile_hook_matcher(definition.matcher)
            except re.error as exc:
                logger.warning(
                    "Skipping hook with invalid matcher %r in %s: %s",
                    definition.matcher,
                    definition.source_path,
                    exc,
                )
                continue
            mgr.hooks.setdefault(event, []).append(
                _HookEntry(
                    matcher=matcher,
                    hook_type=definition.handler_type,
                    command=definition.command,
                    prompt=definition.prompt,
                    url=definition.url,
                    headers=dict(definition.headers),
                    allowed_env_vars=definition.allowed_env_vars,
                    model=definition.model,
                    condition=definition.condition,
                    once=definition.once,
                    entry_id=definition.key,
                    raw_matcher=definition.matcher or "",
                    run_async=definition.run_async,
                    async_timeout=definition.timeout_seconds,
                    plugin_root=definition.plugin_root,
                    plugin_data_root=definition.plugin_data_root,
                    plugin_id=definition.plugin_id,
                    shell=definition.shell,
                    status_message=definition.status_message,
                    additional_context_limit=definition.additional_context_limit,
                    async_rewake=definition.async_rewake,
                    source_path=definition.source_path,
                    source=definition.source.value,
                    display_order=definition.display_order,
                    env=dict(definition.env),
                )
            )
        return mgr

    @classmethod
    def from_settings(cls, settings: dict[str, Any], workspace_root: Path | None = None) -> "HookManager":
        from backend.hooks.discovery import snapshot_from_settings

        return cls.from_snapshot(
            snapshot_from_settings(settings, workspace_root=workspace_root),
            workspace_root=workspace_root,
        )

    def bind_runtime(
        self,
        *,
        llm: Any,
        tool_registry: Any,
        tool_context: Any,
    ) -> None:
        self._llm = llm
        self._tool_registry = tool_registry
        self._tool_context = tool_context

    def has_hooks(self, event: HookEvent) -> bool:
        return bool(self.hooks.get(event))

    @property
    def pending_async_hooks(self) -> int:
        return len(self._async_tasks)

    async def drain_async_hooks(self) -> None:
        while self._async_tasks:
            tasks = tuple(self._async_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)

    async def finalize_async_hooks(self) -> None:
        """Cancel and drain unfinished session hooks during final teardown."""

        tasks = tuple(self._async_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._async_tasks.clear()
        self._inflight_once_hooks.clear()

    def take_async_context(self) -> tuple[str, ...]:
        values = tuple(self._async_context)
        self._async_context.clear()
        return values

    def list_hooks(self) -> dict[str, Any]:
        if self.snapshot is None:
            return {"hooks": [], "warnings": []}
        return self.snapshot.to_payload()

    def preview(
        self,
        event: HookEvent,
        *,
        match_target: str = "",
        env_extras: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        from backend.hooks.dispatcher import select_handlers
        from backend.hooks.policy import event_policy

        selected = select_handlers(
            self.hooks.get(event, ()),
            event=event,
            match_target=match_target,
            condition_matches=lambda entry: _hook_condition_matches(
                entry.condition,
                event=event,
                match_target=match_target,
                env_extras=env_extras,
                tool_registry=self._tool_registry,
            ),
        )
        policy = event_policy(event)
        return tuple(
            {
                "key": entry.entry_id,
                "event": hook_event_name(event),
                "handler_type": entry.hook_type,
                "execution_mode": "async" if (entry.run_async or entry.async_rewake) else "sync",
                "scope": policy.scope.value,
                "source": entry.source,
                "source_path": entry.source_path,
                "display_order": entry.display_order,
                "status": "pending",
                "status_message": entry.status_message,
            }
            for entry in selected
        )

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
            match_target=notification_type,
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
        permission_suggestions: list[dict[str, Any]] | None = None,
    ) -> HookResult:
        extras = self._tool_env(tool_name, args)
        extras["TOOL_CALL_ID"] = tool_call_id
        extras["PERMISSION_REASON"] = reason
        extras["PERMISSION_LEVEL"] = permission_level
        extras["SESSION_ID"] = session_id
        extras["PERMISSION_MODE"] = permission_mode
        try:
            extras["PERMISSION_SUGGESTIONS_JSON"] = json.dumps(
                permission_suggestions or [], ensure_ascii=False, default=str
            )
        except (TypeError, ValueError):
            extras["PERMISSION_SUGGESTIONS_JSON"] = "[]"
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

    async def run_stop_failure(
        self,
        error: str,
        *,
        error_details: str = "",
        last_assistant_message: str = "",
    ) -> HookResult:
        return await self._run_event(
            HookEvent.STOP_FAILURE,
            match_target=error,
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
            match_target=agent_type,
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
            match_target=agent_type,
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
            match_target="",
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
            match_target="",
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
            match_target="",
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
        requested_schema: dict[str, Any] | None = None,
    ) -> HookResult:
        return await self._run_event(
            HookEvent.ELICITATION,
            match_target=mcp_server_name,
            env_extras={
                "MCP_SERVER_NAME": mcp_server_name,
                "ELICITATION_ID": elicitation_id,
                "ELICITATION_PROMPT": prompt[:_PROMPT_TRUNCATE],
                "ELICITATION_RESPONSE": response[:_RESULT_TRUNCATE],
                "ELICITATION_MODE": mode,
                "ELICITATION_URL": url,
                "ELICITATION_REQUESTED_SCHEMA_JSON": json.dumps(
                    requested_schema, ensure_ascii=False, default=str
                ) if requested_schema is not None else "",
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
            match_target=mcp_server_name,
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
        canonical_source = _canonical_config_change_source(source, file_path)
        return await self._run_event(
            HookEvent.CONFIG_CHANGE,
            match_target=canonical_source,
            env_extras={
                "CONFIG_CHANGE_SOURCE": canonical_source,
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
            match_target=branch or Path(path).name,
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
            match_target=load_reason,
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
            match_target="",
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
            match_target=Path(path).name,
            env_extras={
                "FILE_PATH": path,
                "FILE_EVENT": event if event in {"change", "add", "unlink"} else "change",
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
        canonical_reason = _canonical_session_end_reason(reason)
        try:
            return await self._run_event(
                HookEvent.SESSION_END,
                match_target=canonical_reason,
                env_extras={
                    "SESSION_ID": session_id,
                    "SESSION_END_REASON": canonical_reason,
                },
            )
        finally:
            if session_id:
                _session_hook_managers.pop(session_id, None)

    async def run_session_start(self, session_id: str = "", *, source: str = "startup") -> HookResult:
        return await self._run_event(
            HookEvent.SESSION_START,
            match_target=source,
            env_extras={"SESSION_ID": session_id, "SESSION_SOURCE": source},
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
        stop_hook_active: bool = False,
    ) -> HookResult:
        del user_message, tool_results
        extras: dict[str, str] = {
            "DRAFT_REPLY": draft_reply[:_DRAFT_TRUNCATE],
            "STOP_HOOK_ACTIVE": "true" if stop_hook_active else "false",
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

        from backend.hooks.dispatcher import execute_handlers, select_handlers
        from backend.hooks.reducer import reduce_hook_executions

        selected = select_handlers(
            entries,
            event=event,
            match_target=match_target,
            condition_matches=lambda entry: _hook_condition_matches(
                entry.condition,
                event=event,
                match_target=match_target,
                env_extras=env_extras,
                tool_registry=self._tool_registry,
            ),
        )
        synchronous: list[_HookEntry] = []
        for entry in selected:
            if entry.once:
                if (
                    entry.entry_id in self._consumed_once_hooks
                    or entry.entry_id in self._inflight_once_hooks
                ):
                    continue
                self._inflight_once_hooks.add(entry.entry_id)
            if entry.run_async or entry.async_rewake:
                self._schedule_async_hook(entry, event, env_extras)
            else:
                synchronous.append(entry)

        if not synchronous:
            return HookResult()
        try:
            executions = await execute_handlers(
                synchronous,
                lambda entry: self._execute_entry(entry, event, env_extras),
            )
        except asyncio.CancelledError:
            for entry in synchronous:
                if entry.once:
                    self._inflight_once_hooks.discard(entry.entry_id)
            raise

        reduction = reduce_hook_executions(
            event,
            executions,
            expected_event_name=hook_event_name(event),
        )
        from backend.hooks.reducer import execution_succeeded_for_once

        for execution in executions:
            entry = execution.entry
            if not entry.once:
                continue
            self._inflight_once_hooks.discard(entry.entry_id)
            if execution_succeeded_for_once(
                event,
                execution,
                expected_event_name=hook_event_name(event),
            ) and not (event == HookEvent.WORKTREE_CREATE and reduction.failed):
                self._consumed_once_hooks.add(entry.entry_id)
        self._last_run_summaries = reduction.run_summaries
        return HookResult(
            blocked=reduction.blocked,
            failed=reduction.failed,
            message=reduction.message,
            feedback=reduction.feedback,
            stdout=reduction.stdout,
            errors=reduction.errors,
            permission_decision=reduction.permission_decision,
            permission_decision_reason=reduction.permission_decision_reason,
            additional_context=reduction.additional_context,
            custom_instructions=reduction.custom_instructions,
            updated_input=reduction.updated_input,
            updated_mcp_tool_output=reduction.updated_mcp_tool_output,
            retry=reduction.retry,
            prevent_continuation=reduction.prevent_continuation,
            stop_reason=reduction.stop_reason,
            system_message=reduction.system_message,
            initial_user_message=reduction.initial_user_message,
            watch_paths=reduction.watch_paths,
            worktree_path=reduction.worktree_path,
            elicitation_action=reduction.elicitation_action,
            elicitation_content=reduction.elicitation_content,
            run_summaries=reduction.run_summaries,
        )

    async def _execute_entry(
        self,
        entry: _HookEntry,
        event: HookEvent,
        env_extras: dict[str, str] | None,
        *,
        runtime: Any | None = None,
    ) -> tuple[str, str, int]:
        from backend.hooks.runners import HookRuntimeBindings, execute_hook

        resolved_runtime = runtime or HookRuntimeBindings(
            workspace_root=self.workspace_root,
            llm=self._llm,
            tool_registry=self._tool_registry,
            tool_context=self._tool_context,
            allowed_http_hook_urls=self.allowed_http_hook_urls,
            http_hook_allowed_env_vars=self.http_hook_allowed_env_vars,
        )
        hook_fields = dict(env_extras or {})
        tool_context = resolved_runtime.tool_context
        metadata = getattr(tool_context, "metadata", {}) if tool_context is not None else {}
        if isinstance(metadata, dict):
            hook_fields.setdefault(
                "SESSION_ID",
                str(
                    metadata.get("hook_session_id")
                    or metadata.get("conversation_id")
                    or metadata.get("session_id")
                    or ""
                ),
            )
            hook_fields.setdefault("TRANSCRIPT_PATH", str(metadata.get("transcript_path") or ""))
            hook_fields.setdefault("AGENT_ID", str(metadata.get("agent_id") or ""))
            hook_fields.setdefault("AGENT_TYPE", str(metadata.get("agent_type") or ""))
        hook_input = _hook_input(event, hook_fields, self.workspace_root)
        # Keeping command-hook JSON ASCII avoids PowerShell/Git
        # Bash code-page reinterpretation on Windows.
        json_input = json.dumps(hook_input, ensure_ascii=True, default=str)
        return await execute_hook(
            entry,
            event=event,
            json_input=json_input,
            event_name=hook_event_name(event),
            runtime=resolved_runtime,
            substitute_arguments=_substitute_hook_arguments,
            parse_verdict=_hook_verdict,
        )

    def _schedule_async_hook(
        self,
        entry: _HookEntry,
        event: HookEvent,
        env_extras: dict[str, str] | None,
    ) -> None:
        from uuid import uuid4

        from backend.hooks.runners import HookRuntimeBindings

        # Snapshot the turn binding before scheduling.  HookManager config
        # generations share async session state, but an old process
        # must never read the LLM/tool context that a later turn bound.
        runtime = HookRuntimeBindings(
            workspace_root=self.workspace_root,
            llm=self._llm,
            tool_registry=self._tool_registry,
            tool_context=self._tool_context,
            allowed_http_hook_urls=self.allowed_http_hook_urls,
            http_hook_allowed_env_vars=self.http_hook_allowed_env_vars,
        )
        task = asyncio.create_task(
            self._run_async_entry(
                f"async_hook_{uuid4().hex}",
                entry,
                event,
                dict(env_extras or {}),
                runtime=runtime,
            ),
            name=f"hook:{event.value}:{entry.raw_matcher or '*'}",
        )
        self._async_tasks.add(task)

        def _done(done: asyncio.Task[Any]) -> None:
            self._async_tasks.discard(done)
            if done.cancelled():
                return
            try:
                done.result()
            except Exception as exc:
                logger.warning("Async hook failed for event %s: %s", event.value, exc)

        task.add_done_callback(_done)

    async def _run_async_entry(
        self,
        process_id: str,
        entry: _HookEntry,
        event: HookEvent,
        env_extras: dict[str, str] | None,
        *,
        runtime: Any,
    ) -> None:
        try:
            stdout, stderr, exit_code = await self._execute_entry(
                entry,
                event,
                env_extras,
                runtime=runtime,
            )
        except asyncio.CancelledError:
            if entry.once:
                self._inflight_once_hooks.discard(entry.entry_id)
            await _emit_async_hook_response(
                process_id=process_id,
                entry=entry,
                event=event,
                stdout="",
                stderr="Hook cancelled",
                exit_code=1,
                outcome="cancelled",
                tool_context=runtime.tool_context,
            )
            raise
        except Exception as exc:
            if entry.once:
                self._inflight_once_hooks.discard(entry.entry_id)
            await _emit_async_hook_response(
                process_id=process_id,
                entry=entry,
                event=event,
                stdout="",
                stderr=str(exc),
                exit_code=1,
                outcome="error",
                tool_context=runtime.tool_context,
            )
            raise

        if entry.once:
            self._inflight_once_hooks.discard(entry.entry_id)
            from backend.hooks.reducer import execution_succeeded_for_once
            from backend.hooks.dispatcher import HookExecution

            synthetic = HookExecution(
                entry=entry,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                configured_order=0,
                completion_order=0,
                duration_ms=0,
            )
            if execution_succeeded_for_once(
                event,
                synthetic,
                expected_event_name=hook_event_name(event),
            ):
                self._consumed_once_hooks.add(entry.entry_id)
        # Regular async hooks enter MiniCode's async registry and are attached
        # to the next model query even when their process exits non-zero.  An
        # asyncRewake hook deliberately bypasses that registry and only wakes
        # the model for exit code 2.
        if not entry.async_rewake and stdout:
            self._async_context.extend(_async_hook_context_messages(stdout, entry))
        if entry.async_rewake or stdout.strip():
            await _emit_async_hook_response(
                process_id=process_id,
                entry=entry,
                event=event,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                outcome="success" if exit_code == 0 else "error",
                tool_context=runtime.tool_context,
            )
        if entry.async_rewake and exit_code == 2:
            self._enqueue_async_rewake(
                process_id,
                entry,
                event,
                stderr or stdout,
                tool_context=runtime.tool_context,
            )
        if exit_code not in (0, None):
            logger.warning(
                "Async hook exited %d for event %s: stdout=%s stderr=%s",
                exit_code,
                event.value,
                stdout[:200],
                stderr[:200],
            )

    def _enqueue_async_rewake(
        self,
        process_id: str,
        entry: _HookEntry,
        event: HookEvent,
        content: str,
        *,
        tool_context: Any | None,
    ) -> None:
        from backend.agent.parent_notification_outbox import enqueue_parent_notification

        metadata = getattr(tool_context, "metadata", {}) if tool_context is not None else {}
        parent_run_id = str(metadata.get("run_id") or "").strip()
        conversation_id = str(getattr(tool_context, "conversation_id", "") or "").strip()
        if not parent_run_id and not conversation_id:
            logger.warning(
                "asyncRewake hook %s completed without a parent run/conversation scope",
                entry.entry_id,
            )
            return
        runtime = metadata.get("agent_runtime")
        blocking_message = (
            f'{hook_event_name(event)} hook blocking error from command "{entry.command}": '
            f"{str(content or 'Hook blocked event')}"
        )
        enqueue_parent_notification(
            parent_run_id=parent_run_id,
            conversation_id=conversation_id,
            session_id=str(
                getattr(tool_context, "session_id", "") or metadata.get("session_id") or ""
            ).strip(),
            subagent_id=f"hook:{entry.entry_id}",
            kind="async_hook",
            payload={
                "status": "blocked",
                "content": blocking_message,
                "hook_id": entry.entry_id,
                "event": hook_event_name(event),
                "command": entry.command,
                "plugin_id": entry.plugin_id,
            },
            idempotency_key=f"async_hook:{process_id}",
            base_dir=getattr(runtime, "_outbox_root", None),
        )

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


def register_hook_manager_for_session(
    session_id: str,
    manager: HookManager,
    *,
    owner_session_id: str = "",
) -> None:
    clean_session_id = str(session_id or "").strip()
    if clean_session_id:
        manager.scope_id = clean_session_id
        manager.owner_session_id = str(owner_session_id or clean_session_id).strip()
        _session_hook_managers[clean_session_id] = manager


def iter_hook_managers_for_owner(
    session_id: str,
) -> tuple[tuple[str, HookManager], ...]:
    """Snapshot conversation hook registries owned by one websocket session."""

    clean_owner = str(session_id or "").strip()
    if not clean_owner:
        return ()
    return tuple(
        (scope_id, manager)
        for scope_id, manager in _session_hook_managers.items()
        if manager.owner_session_id == clean_owner or scope_id == clean_owner
    )


def pop_hook_managers_for_owner(session_id: str) -> list[tuple[str, HookManager]]:
    """Detach every conversation-scoped hook runtime owned by one WS session."""

    clean_owner = str(session_id or "").strip()
    if not clean_owner:
        return []
    selected: list[tuple[str, HookManager]] = []
    seen: set[int] = set()
    for scope_id, manager in list(_session_hook_managers.items()):
        if manager.owner_session_id != clean_owner and scope_id != clean_owner:
            continue
        _session_hook_managers.pop(scope_id, None)
        identity = id(manager._session_runtime)
        if identity in seen:
            continue
        seen.add(identity)
        selected.append((scope_id, manager))
    return selected


def bind_hook_manager(manager: HookManager) -> contextvars.Token[HookManager | None]:
    return _bound_manager.set(manager)


def unbind_hook_manager(token: contextvars.Token[HookManager | None]) -> None:
    _bound_manager.reset(token)


def set_hook_manager(manager: HookManager | None) -> None:
    global _active_manager
    _active_manager = manager
