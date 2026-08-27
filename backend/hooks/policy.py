from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from backend.hooks.models import HookScope


class SuccessfulOutput(str, Enum):
    IGNORE = "ignore"
    MODEL = "model"
    USER = "user"
    COMPACT_INSTRUCTIONS = "compact_instructions"
    WORKTREE_PATH = "worktree_path"


@dataclass(frozen=True)
class HookEventPolicy:
    matcher_applies: bool = True
    exit_two_blocks: bool = False
    json_can_block: bool = False
    ignore_blocking: bool = False
    fire_and_forget: bool = False
    http_allowed: bool = True
    successful_output: SuccessfulOutput = SuccessfulOutput.IGNORE
    scope: HookScope = HookScope.TURN
    default_timeout_seconds: float = 600.0
    max_timeout_seconds: float | None = None
    additional_context: bool = False


_OBSERVER = HookEventPolicy()
_POLICIES: dict[str, HookEventPolicy] = {
    "session_start": HookEventPolicy(
        ignore_blocking=True,
        http_allowed=False,
        successful_output=SuccessfulOutput.MODEL,
        scope=HookScope.THREAD,
        additional_context=True,
    ),
    "user_prompt_submit": HookEventPolicy(
        matcher_applies=False,
        exit_two_blocks=True,
        json_can_block=True,
        successful_output=SuccessfulOutput.MODEL,
        additional_context=True,
    ),
    "pre_tool_use": HookEventPolicy(
        exit_two_blocks=True,
        json_can_block=True,
        additional_context=True,
    ),
    "post_tool_use": HookEventPolicy(
        exit_two_blocks=True,
        json_can_block=True,
        additional_context=True,
    ),
    "post_tool_use_failure": HookEventPolicy(
        exit_two_blocks=True,
        additional_context=True,
    ),
    "notification": HookEventPolicy(matcher_applies=True),
    "pre_compact": HookEventPolicy(
        exit_two_blocks=True,
        json_can_block=True,
        successful_output=SuccessfulOutput.COMPACT_INSTRUCTIONS,
    ),
    "post_compact": HookEventPolicy(successful_output=SuccessfulOutput.USER),
    "permission_request": HookEventPolicy(json_can_block=True),
    "permission_denied": HookEventPolicy(matcher_applies=True),
    "stop_failure": HookEventPolicy(
        matcher_applies=True,
        ignore_blocking=True,
        fire_and_forget=True,
    ),
    "subagent_start": HookEventPolicy(
        ignore_blocking=True,
        successful_output=SuccessfulOutput.MODEL,
        scope=HookScope.THREAD,
        additional_context=True,
    ),
    "subagent_stop": HookEventPolicy(exit_two_blocks=True, json_can_block=True),
    "teammate_idle": HookEventPolicy(matcher_applies=False, exit_two_blocks=True, json_can_block=True),
    "task_created": HookEventPolicy(matcher_applies=False, exit_two_blocks=True, json_can_block=True),
    "task_completed": HookEventPolicy(matcher_applies=False, exit_two_blocks=True, json_can_block=True),
    "elicitation": HookEventPolicy(exit_two_blocks=True, json_can_block=True),
    "elicitation_result": HookEventPolicy(exit_two_blocks=True, json_can_block=True),
    "config_change": HookEventPolicy(exit_two_blocks=True, json_can_block=True),
    # Worktree hooks are lifecycle adapters, not event filters. A matcher here
    # would make a path or branch value accidentally suppress a handler.
    "worktree_create": HookEventPolicy(
        matcher_applies=False,
        successful_output=SuccessfulOutput.WORKTREE_PATH,
        scope=HookScope.THREAD,
    ),
    "worktree_remove": HookEventPolicy(matcher_applies=False, scope=HookScope.THREAD),
    "instructions_loaded": HookEventPolicy(matcher_applies=True, ignore_blocking=True),
    "cwd_changed": HookEventPolicy(matcher_applies=False, ignore_blocking=True, scope=HookScope.THREAD),
    "file_changed": HookEventPolicy(matcher_applies=True, ignore_blocking=True, scope=HookScope.THREAD),
    "session_end": HookEventPolicy(
        matcher_applies=True,
        ignore_blocking=True,
        scope=HookScope.THREAD,
        default_timeout_seconds=1.0,
        max_timeout_seconds=3.0,
    ),
    "stop": HookEventPolicy(
        matcher_applies=False,
        exit_two_blocks=True,
        json_can_block=True,
    ),
}


def event_policy(event: Any) -> HookEventPolicy:
    value = str(getattr(event, "value", event) or "")
    return _POLICIES.get(value, _OBSERVER)
