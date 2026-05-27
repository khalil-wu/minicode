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


class HookEvent(str, Enum):
    SESSION_START = "session_start"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    PRE_COMPACT = "pre_compact"
    STOP = "stop"


@dataclass
class HookResult:
    blocked: bool = False
    message: str = ""
    feedback: str = ""
    stdout: str = ""

    @property
    def has_feedback(self) -> bool:
        return bool(self.feedback.strip())


@dataclass
class _HookEntry:
    matcher: re.Pattern[str]
    command: str
    raw_matcher: str = ""


@dataclass
class HookManager:
    hooks: dict[HookEvent, list[_HookEntry]] = field(default_factory=dict)
    workspace_root: Path | None = None

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

        # Map legacy keys to new event types
        key_map = {
            "pre_tool": HookEvent.PRE_TOOL_USE,
            "post_tool": HookEvent.POST_TOOL_USE,
            "pre_tool_use": HookEvent.PRE_TOOL_USE,
            "post_tool_use": HookEvent.POST_TOOL_USE,
            "session_start": HookEvent.SESSION_START,
            "user_prompt_submit": HookEvent.USER_PROMPT_SUBMIT,
            "pre_compact": HookEvent.PRE_COMPACT,
            "stop": HookEvent.STOP,
        }

        for key, event in key_map.items():
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
                mgr.hooks[event].append(_HookEntry(matcher=pattern, command=command, raw_matcher=matcher_raw))
        return mgr

    def has_hooks(self, event: HookEvent) -> bool:
        return bool(self.hooks.get(event))

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

    async def run_session_start(self) -> HookResult:
        return await self._run_event(HookEvent.SESSION_START, match_target="session")

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
            stdout, exit_code = await self._exec(entry.command, event, env_extras)
            if stdout:
                outputs.append(stdout)

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
    ) -> tuple[str, int]:
        env = sanitized_subprocess_env()
        env["HOOK_EVENT"] = event.value
        if env_extras:
            env.update(env_extras)
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
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=_HOOK_TIMEOUT_S)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return f"hook timed out after {_HOOK_TIMEOUT_S}s", 124
        stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
        return stdout, proc.returncode if proc.returncode is not None else 0


_active_manager: HookManager | None = None


def get_hook_manager() -> HookManager | None:
    return _active_manager


def set_hook_manager(manager: HookManager | None) -> None:
    global _active_manager
    _active_manager = manager
