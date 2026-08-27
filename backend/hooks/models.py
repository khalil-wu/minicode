from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class HookEvent(str, Enum):
    """Canonical MiniCode lifecycle event identifiers."""

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


class HookSource(str, Enum):
    SYSTEM = "system"
    USER = "user"
    PROJECT = "project"
    POLICY = "policy"
    MDM = "mdm"
    ENTERPRISE_MANAGED = "enterprise_managed"
    SESSION_FLAGS = "session_flags"
    PLUGIN = "plugin"
    MANAGED_REQUIREMENTS = "managed_requirements"
    LEGACY_MANAGED_CONFIG_FILE = "legacy_managed_config_file"
    LEGACY_MANAGED_CONFIG_MDM = "legacy_managed_config_mdm"
    UNKNOWN = "unknown"


class HookTrustStatus(str, Enum):
    MANAGED = "managed"
    TRUSTED = "trusted"
    MODIFIED = "modified"
    UNTRUSTED = "untrusted"


class HookScope(str, Enum):
    THREAD = "thread"
    TURN = "turn"


@dataclass(frozen=True)
class HookDefinition:
    key: str
    event: str
    handler_type: str
    matcher: str | None
    source_path: str
    source: HookSource
    display_order: int
    command: str = ""
    prompt: str = ""
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    allowed_env_vars: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float | None = None
    run_async: bool = False
    async_rewake: bool = False
    once: bool = False
    condition: str = ""
    model: str = ""
    shell: str = ""
    status_message: str = ""
    additional_context_limit: int | None = None
    plugin_id: str = ""
    plugin_root: str = ""
    plugin_data_root: str = ""
    is_managed: bool = False
    enabled: bool = True
    current_hash: str = ""
    trusted_hash: str = ""
    trust_status: HookTrustStatus = HookTrustStatus.UNTRUSTED

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))

    @property
    def executable(self) -> bool:
        return self.enabled and self.trust_status in {
            HookTrustStatus.MANAGED,
            HookTrustStatus.TRUSTED,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "event": self.event,
            "handler_type": self.handler_type,
            "matcher": self.matcher,
            "command": self.command or None,
            "url": self.url or None,
            "timeout_seconds": self.timeout_seconds,
            "status_message": self.status_message or None,
            "additional_context_limit": self.additional_context_limit,
            "source_path": self.source_path,
            "source": self.source.value,
            "plugin_id": self.plugin_id or None,
            "display_order": self.display_order,
            "enabled": self.enabled,
            "is_managed": self.is_managed,
            "current_hash": self.current_hash,
            "trusted_hash": self.trusted_hash or None,
            "trust_status": self.trust_status.value,
            "execution_mode": "async" if self.run_async else "sync",
        }


@dataclass(frozen=True)
class HookSnapshot:
    entries: tuple[HookDefinition, ...] = ()
    warnings: tuple[str, ...] = ()
    config_fingerprint: str = ""
    workspace_trusted: bool = True
    disabled_reason: str = ""
    allowed_http_hook_urls: tuple[str, ...] | None = None
    http_hook_allowed_env_vars: tuple[str, ...] | None = None

    @property
    def fingerprint(self) -> str:
        payload = {
            "config": self.config_fingerprint,
            "trusted": self.workspace_trusted,
            "disabled": self.disabled_reason,
            "entries": [
                {
                    "key": entry.key,
                    "hash": entry.current_hash,
                    "enabled": entry.enabled,
                    "trust": entry.trust_status.value,
                }
                for entry in self.entries
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def executable_entries(self) -> tuple[HookDefinition, ...]:
        if self.disabled_reason:
            return ()
        return tuple(entry for entry in self.entries if entry.executable)

    def for_event(self, event: str, *, executable_only: bool = True) -> tuple[HookDefinition, ...]:
        entries = self.executable_entries if executable_only else self.entries
        return tuple(entry for entry in entries if entry.event == event)

    def to_payload(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "workspace_trusted": self.workspace_trusted,
            "disabled_reason": self.disabled_reason or None,
            "warnings": list(self.warnings),
            "hooks": [entry.to_payload() for entry in self.entries],
        }


def normalized_hook_hash(
    *,
    event: str,
    matcher: str | None,
    handler: Mapping[str, Any],
) -> str:
    payload = {
        "event": event,
        "matcher": matcher,
        "handler": _json_safe(dict(handler)),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
