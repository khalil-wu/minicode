"""Retained terminal authority, following Codex unified_exec/stdin_approval.rs."""
from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from backend.sandbox.policy import AdditionalPermissionProfile, NetworkSandboxPolicy, PermissionProfile, SandboxPolicy


SandboxIntent = Literal["use_default", "with_additional_permissions", "require_escalated"]


class StdinAuthorityError(RuntimeError):
    """The retained terminal cannot perform this reviewed interaction."""


@dataclass(frozen=True, slots=True)
class TerminalPolicySnapshot:
    permissions: PermissionProfile
    cwd: Path | None
    workspace_roots: tuple[Path, ...]
    protect_workspace_metadata: bool
    proxy_environment: tuple[tuple[str, str], ...]

    @classmethod
    def capture(cls, policy: SandboxPolicy) -> "TerminalPolicySnapshot":
        # Source keeps network proxy settings private. They participate in
        # policy drift, never in the displayed approval action/reason.
        proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
        environment = {**os.environ, **policy.env_overrides}
        return cls(policy.permission_profile, policy.policy_cwd, policy.workspace_roots,
                   policy.protect_workspace_metadata,
                   tuple((key, environment[key]) for key in proxy_keys if key in environment))

    def filesystem_context(self) -> tuple:
        return (replace(self.permissions, network=NetworkSandboxPolicy.RESTRICTED),
                self.cwd, self.workspace_roots, self.protect_workspace_metadata)


def _with_retained_permissions(policy: SandboxPolicy, *profiles: AdditionalPermissionProfile | None) -> SandboxPolicy:
    for profile in profiles:
        if profile is not None:
            policy = policy.with_additional_permissions(profile)
    return policy


@dataclass(frozen=True, slots=True)
class TerminalLaunchAuthority:
    policy: TerminalPolicySnapshot
    launch_permissions: SandboxIntent
    additional_permissions: AdditionalPermissionProfile | None
    internal_permissions: AdditionalPermissionProfile | None
    environment_id: str
    filesystem_escalated: bool = False

    @classmethod
    def capture(
        cls, *, baseline_policy: SandboxPolicy, effective_policy: SandboxPolicy,
        launch_permissions: SandboxIntent = "use_default",
        additional_permissions: AdditionalPermissionProfile | None = None,
        internal_permissions: AdditionalPermissionProfile | None = None,
        environment_id: str = "local", filesystem_escalated: bool = False,
    ) -> "TerminalLaunchAuthority":
        # Preserve the baseline plus its retained grants separately from the
        # successful override. An already-running process cannot be resandboxed.
        additional_permissions = additional_permissions if additional_permissions is not None and not additional_permissions.is_empty else None
        internal_permissions = internal_permissions if internal_permissions is not None and not internal_permissions.is_empty else None
        policy = _with_retained_permissions(baseline_policy, additional_permissions, internal_permissions)
        if launch_permissions == "use_default" and not filesystem_escalated:
            policy = effective_policy
        return cls(TerminalPolicySnapshot.capture(policy), launch_permissions,
                   additional_permissions, internal_permissions, environment_id,
                   filesystem_escalated)

    def review_requirement(self, current_policy: SandboxPolicy, *, environment_id: str) -> SandboxIntent:
        if environment_id != self.environment_id:
            raise StdinAuthorityError("cannot access the terminal's original environment; select it before retrying")
        current = TerminalPolicySnapshot.capture(_with_retained_permissions(
            current_policy, self.additional_permissions, self.internal_permissions))
        bypassed = self.launch_permissions == "require_escalated"
        if current_policy.permission_profile.file_system.has_denied_read_restrictions() and (
            bypassed or self.policy.filesystem_context() != current.filesystem_context()
        ):
            raise StdinAuthorityError("this terminal cannot enforce the current denied-read restrictions; start a new terminal")
        if bypassed or self.filesystem_escalated or self.policy != current:
            return "require_escalated"
        ordinary = TerminalPolicySnapshot.capture(_with_retained_permissions(current_policy, self.internal_permissions))
        if self.policy.permissions == ordinary.permissions:
            return "use_default"
        return "with_additional_permissions"

    def approval_reason(self, intent: SandboxIntent) -> str:
        if self.launch_permissions == "require_escalated":
            authority = "This terminal was launched outside the sandbox, bypassing any managed network proxy."
        elif self.filesystem_escalated:
            authority = "This terminal has approved broader filesystem access; denied reads are still enforced."
        elif self.policy.permissions.enforcement.value == "disabled":
            authority = "This terminal runs without a filesystem sandbox."
        elif intent == "use_default":
            authority = "This terminal uses the current permissions."
        elif intent == "with_additional_permissions":
            authority = "This terminal retains additional permissions."
        else:
            authority = "This terminal retains sandbox or network settings that differ from the current permissions."
        reason = "Send input to an existing terminal. " + authority
        if self.internal_permissions is not None:
            reason += " It also has an internal filesystem grant."
        reason += " The cwd is its launch directory; the terminal's current directory and state may have changed."
        if self.additional_permissions is not None:
            from backend.permissions.grants import permission_profile_payload

            reason += " Retained grants: " + json.dumps(permission_profile_payload(self.additional_permissions), ensure_ascii=False, separators=(",", ":")) + "."
        return reason


@dataclass(frozen=True, slots=True)
class TerminalInputApproval:
    id: str
    approval_id: str
    environment_id: str
    command_id: str
    process_id: int
    input: str
    cwd: str
    tty: bool
    sandbox_permissions: SandboxIntent
    additional_permissions: AdditionalPermissionProfile | None
    reason: str

    def to_payload(self) -> dict:
        from backend.permissions.grants import permission_profile_payload

        return {
            "id": self.id, "approval_id": self.approval_id,
            "environment_id": self.environment_id, "command_id": self.command_id,
            "process_id": self.process_id, "input": self.input,
            "cwd": self.cwd, "tty": self.tty, "sandbox_permissions": self.sandbox_permissions,
            "additional_permissions": permission_profile_payload(self.additional_permissions) if self.additional_permissions is not None else None,
            "reason": self.reason,
            "display_command": shlex.join(["write_stdin", "--session-id", str(self.process_id), self.input]),
        }

    def permission_hook_input(self) -> dict:
        payload = self.to_payload()
        return {"session_id": self.process_id, "chars": self.input, "parent_call_id": self.id,
                **{key: payload[key] for key in ("approval_id", "environment_id", "cwd", "tty",
                "sandbox_permissions", "additional_permissions")}}

    def guardian_review_payload(self) -> dict:
        from backend.permissions.grants import permission_profile_payload

        payload = {"tool": "write_stdin", "session_id": self.process_id, "chars": self.input,
                   "cwd": self.cwd, "sandbox_permissions": self.sandbox_permissions, "tty": self.tty,
                   "environment_id": self.environment_id}
        if self.additional_permissions is not None:
            payload["additional_permissions"] = permission_profile_payload(self.additional_permissions)
        return payload

    def verify_reviewable(self) -> None:
        if "\0" in self.input:
            raise StdinAuthorityError("terminal input contains a NUL byte and cannot be reviewed safely")
        # Source bounds the full serialized action and reason, including escapes;
        # it never reviews a prefix then executes the unreviewed tail.
        encoded = json.dumps(self.guardian_review_payload(), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        if len(encoded) + len(self.reason.encode("utf-8")) > 8000:
            raise StdinAuthorityError("terminal input and permission details are too large to review safely; use a smaller input or start a new terminal with fewer grants")
