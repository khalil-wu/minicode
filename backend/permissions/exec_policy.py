"""Source policy ownership and trusted-layer loading, independent of WS readers.

This module does not register itself with existing tool callers. Callers must
capture the final argv and issuing model before asking for a requirement, then
execute that exact capture after approval. No string/glob policy interpreter
exists here; all rule decisions and amendments use the actual native engine.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import TYPE_CHECKING, Any, Literal

from backend.async_cleanup import to_thread_cancel_safe
from backend.config_layers import ConfigLayerStack
from backend.config_requirements import ConfigRequirements, ConfigRequirementsError
from backend.permissions.native_execpolicy import NativeExecPolicyError, NativeExecPolicyProcessError, NativeExecPolicyRuntime
from backend.sandbox.policy import PermissionProfile, ResolvedSandboxPolicy, SandboxEnforcement, SandboxPolicy

if TYPE_CHECKING:
    from backend.permissions.environment_command import EnvironmentCommandPolicy
    from backend.permissions.environment_scope import EnvironmentPermissionScope
    from backend.workspace.executor_paths import ExecutorPath
    from backend.agent.final_tool_request import FinalExecutableToolRequest
    from backend.agent.model_execution import ModelExecutionSnapshot
    from backend.permissions.context import PermissionContext


@dataclass(frozen=True, slots=True)
class ExecPolicySource:
    path: str
    contents: str

    def payload(self) -> dict[str, str]:
        return {"path": self.path, "contents": self.contents}


def config_folders(stack: ConfigLayerStack, *, ignore_user_and_project_rules: bool = False) -> tuple[Path, ...]:
    folders = []
    for layer in stack.get_layers():
        source = layer.source
        if ignore_user_and_project_rules and source.kind in {"user", "project"}:
            continue
        if source.kind in {"system", "user"}:
            folders.append(Path(source.file).parent)
        elif source.kind == "project":
            folders.append(Path(source.project_config_folder))
    return tuple(folders)


def collect_policy_sources(folder: Path) -> tuple[ExecPolicySource, ...]:
    # Source ignores an absent optional rules directory; all other IO and UTF-8
    # failures retain their actual error instead of becoming an empty policy.
    try:
        entries = os.scandir(folder / "rules")
    except FileNotFoundError:
        return ()
    with entries:
        paths = sorted(Path(entry.path) for entry in entries
            if Path(entry.name).suffix == ".rules" and entry.is_file(follow_symlinks=False))
    return tuple(ExecPolicySource(str(path), path.read_text(encoding="utf-8")) for path in paths)


def load_trusted_policy_sources(stack: ConfigLayerStack, *, ignore_user_and_project_rules: bool = False) -> tuple[ExecPolicySource, ...]:
    return tuple(source for folder in config_folders(stack, ignore_user_and_project_rules=ignore_user_and_project_rules)
        for source in collect_policy_sources(folder))


@dataclass(frozen=True, slots=True)
class ExecPolicyLoadWarning:
    message: str
    error: NativeExecPolicyError


@dataclass(frozen=True, slots=True)
class FrozenExecCommand:
    tool_request_digest: str
    argv: tuple[str, ...]
    policy_argv: tuple[str, ...]
    cwd: Path | ExecutorPath
    configured_shell: Path | str
    approval_policy_json: str
    profile_kind: str
    managed_profile: bool
    full_disk_write: bool
    denied_reads: bool
    windows_sandbox_level: str
    sandbox_permissions: str
    prefix_rule: tuple[str, ...] | None
    environment_id: str
    shell_mode: str
    execution_digest: str
    policy: SandboxPolicy | EnvironmentCommandPolicy
    additional_permissions_present: bool

    @classmethod
    def capture(cls, request: FinalExecutableToolRequest, *, argv: Sequence[str], policy_argv: Sequence[str], cwd: Path,
        configured_shell: Path, permission: PermissionContext, profile: PermissionProfile,
        resolved_profile: ResolvedSandboxPolicy, windows_sandbox_level: str, sandbox_permissions: str,
        prefix_rule: Sequence[str] | None, environment_id: str, policy: SandboxPolicy,
        additional_permissions_present: bool, shell_mode: str = "normal",
        environment_scope: EnvironmentPermissionScope | None = None) -> FrozenExecCommand:
        approval_policy_json = json.dumps(permission.approval_policy_value(), sort_keys=True, separators=(",", ":"))
        values = {"tool_request_digest": request.arguments_digest or request.digest, "argv": tuple(argv), "policy_argv": tuple(policy_argv),
            "cwd": cwd if environment_scope is not None else Path(cwd),
            "configured_shell": str(configured_shell) if environment_scope is not None else Path(configured_shell), "approval_policy_json": approval_policy_json,
            "profile_kind": profile.file_system.kind.value, "managed_profile": profile.enforcement is SandboxEnforcement.MANAGED,
            "full_disk_write": environment_scope.has_full_disk_write_access() if environment_scope is not None else resolved_profile.full_disk_write,
            "denied_reads": environment_scope.has_denied_read_restrictions() if environment_scope is not None else resolved_profile.has_denied_read_restrictions,
            "windows_sandbox_level": windows_sandbox_level, "sandbox_permissions": sandbox_permissions,
            "prefix_rule": tuple(prefix_rule) if prefix_rule is not None else None,
            "environment_id": environment_id, "shell_mode": shell_mode,
            "additional_permissions_present": additional_permissions_present}
        identity = json.dumps({**values, "request_digest": request.digest,
            "network_policy": policy.network_policy.identity() if environment_scope is not None and policy.network_policy is not None else None,
            "resolved_profile": environment_scope.identity() if environment_scope is not None else asdict(resolved_profile)},
            default=str, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(**values, policy=policy, execution_digest=hashlib.sha256(identity.encode("utf-8")).hexdigest())

    def native_payload(self) -> dict[str, Any]:
        return {"command": list(self.policy_argv), "configured_shell": str(self.configured_shell), "shell_mode": self.shell_mode,
            "approval_policy": json.loads(self.approval_policy_json),
            "permission_profile": {"managed": self.managed_profile,
                "file_system": {"kind": self.profile_kind, "full_disk_write": self.full_disk_write}},
            "windows_sandbox_level": self.windows_sandbox_level, "sandbox_permissions": self.sandbox_permissions,
            "prefix_rule": list(self.prefix_rule) if self.prefix_rule is not None else None}


@dataclass(frozen=True, slots=True)
class ExecPolicyDecision:
    command: FrozenExecCommand
    kind: Literal["skip", "needs_approval", "forbidden"]
    reason: str | None
    bypass_sandbox: bool
    proposed_amendment: tuple[str, ...] | None
    parsed_commands: tuple[tuple[str, ...], ...]
    evidence_json: str

    def amendment_array(self) -> list[str] | None:
        # Source ExecPolicyAmendment is serde-transparent Array<string>.
        return list(self.proposed_amendment) if self.proposed_amendment is not None else None

    def available_decisions(self) -> list[Any]:
        # protocol/approvals.rs::default_available_decisions for ordinary
        # command and inline additional-permission requests, without network.
        decisions: list[Any] = ["approved"]
        if not self.command.additional_permissions_present and self.proposed_amendment is not None:
            decisions.append({"approved_execpolicy_amendment": {"proposed_execpolicy_amendment": self.amendment_array()}})
        decisions.append("abort")
        return decisions

    def first_attempt_override(self, *, already_approved: bool) -> str:
        # core/tools/sandboxing.rs::sandbox_override_for_first_attempt.
        if self.command.denied_reads:
            return "escalated_sandbox_with_restrictions" if (
                self.command.sandbox_permissions == "require_escalated" and already_approved) else "no_override"
        if self.kind == "skip" and self.bypass_sandbox:
            return "bypass_sandbox_first_attempt"
        return "bypass_sandbox_first_attempt" if self.command.sandbox_permissions == "require_escalated" else "no_override"


class ExecPolicyOwner:
    def __init__(self, *, home: Path, stack: ConfigLayerStack,
        runtime: NativeExecPolicyRuntime | None = None, ignore_user_and_project_rules: bool = False):
        self.home = Path(home)
        self.runtime = runtime if runtime is not None else NativeExecPolicyRuntime()
        self.folders = config_folders(stack, ignore_user_and_project_rules=ignore_user_and_project_rules)
        self.ignore_user_and_project_rules = ignore_user_and_project_rules
        self.allow_login_shell = stack.requirements.value_for("allow_login_shell", stack.effective_config().get("allow_login_shell", True))
        self._requirements_rules = deepcopy(stack.requirements.value_for("rules"))
        self._approved_prefixes: tuple[tuple[str, ...], ...] = ()
        self._state_lock = threading.Lock()
        self._update_lock = threading.Lock()
        self.revision = 0
        self.load_warning: ExecPolicyLoadWarning | None = None
        # Requirements were compiled before Source loads user files. Validate
        # them separately so a malformed managed rule can never become a user
        # parse warning or be replaced with an empty policy.
        if self._requirements_rules is not None:
            try:
                self.runtime.invoke({"operation": "inspect", "sources": [],
                    "requirements_rules": self._requirements_rules, "ignore_allow_prefix_rules": False})
            except (NativeExecPolicyError, NativeExecPolicyProcessError) as error:
                raise ConfigRequirementsError(f"Invalid execution rules in {stack.requirements.source_for('rules')}: {error}") from error
        self.sources = load_trusted_policy_sources(stack, ignore_user_and_project_rules=ignore_user_and_project_rules)
        try:
            self._current_inspection = self.runtime.invoke(self._inspection_payload())
        except NativeExecPolicyError as error:
            # Source load_exec_policy_with_warning drops the whole malformed
            # user policy while retaining managed requirements and evidence.
            self.load_warning = ExecPolicyLoadWarning(str(error), error)
            self.sources = ()
            self._current_inspection = self.runtime.invoke(self._inspection_payload())

    @property
    def default_policy_path(self) -> Path:
        return self.home / "rules" / "default.rules"

    def _policy_payload(self) -> dict[str, Any]:
        with self._state_lock:
            return {"sources": [source.payload() for source in self.sources],
                "requirements_rules": deepcopy(self._requirements_rules),
                "approved_prefixes": [list(prefix) for prefix in self._approved_prefixes]}

    def _inspection_payload(self) -> dict[str, Any]:
        return {"operation": "inspect", **self._policy_payload(), "ignore_allow_prefix_rules": False}

    @staticmethod
    def _ignore_for_model(model: ModelExecutionSnapshot) -> bool:
        from backend.agent.model_activation import _ignores_prefix_rules
        stack = model.config.config_layer_stack
        return _ignores_prefix_rules(model, stack.requirements if stack is not None else ConfigRequirements())

    def inspect_for_model(self, model: ModelExecutionSnapshot) -> dict[str, Any]:
        if not self._ignore_for_model(model):
            with self._state_lock:
                return deepcopy(self._current_inspection["policy"])
        return self.runtime.invoke({"operation": "inspect", **self._policy_payload(),
            "ignore_allow_prefix_rules": self._ignore_for_model(model)})["policy"]

    def command_requirement(self, command: FrozenExecCommand, *, model: ModelExecutionSnapshot,
        environment_sources: Sequence[ExecPolicySource] = (),
        environment_requirements: dict[str, Any] | None = None) -> ExecPolicyDecision:
        payload = self._policy_payload()
        if environment_requirements is not None:
            # Both inputs go through the pinned Source requirements parser.
            # Requirements permit only prompt/forbidden, so model filtering
            # of allow prefixes commutes with this restrictive overlay.
            thread_rules = payload["requirements_rules"]
            payload["requirements_rules"] = {**deepcopy(environment_requirements), "prefix_rules": [
                *(thread_rules["prefix_rules"] if thread_rules is not None else ()),
                *deepcopy(environment_requirements["prefix_rules"])]}
        result = self.runtime.invoke({"operation": "command_requirement", **payload,
            **command.native_payload(), "environment_sources": [source.payload() for source in environment_sources],
            "ignore_allow_prefix_rules": self._ignore_for_model(model)})
        requirement = result["requirement"]
        amendment = requirement.get("proposed_execpolicy_amendment")
        return ExecPolicyDecision(command=command, kind=requirement["kind"], reason=requirement.get("reason"),
            bypass_sandbox=requirement.get("bypass_sandbox", False),
            proposed_amendment=tuple(amendment["command"]) if amendment is not None else None,
            parsed_commands=tuple(tuple(argv) for argv in result["commands"]),
            evidence_json=json.dumps(result, ensure_ascii=False, sort_keys=True))

    def _append_and_update(self, prefix: tuple[str, ...]) -> None:
        with self._update_lock:
            self.runtime.invoke({"operation": "append_allow_prefix", "path": str(self.default_policy_path), "prefix": list(prefix)})
            candidate = self._inspection_payload()
            candidate["approved_prefixes"].append(list(prefix))
            inspection = self.runtime.invoke(candidate)
            with self._state_lock:
                if inspection != self._current_inspection:
                    self._approved_prefixes = (*self._approved_prefixes, prefix)
                    self._current_inspection = inspection
                    self.revision += 1

    async def persist_approved_amendment(self, decision: ExecPolicyDecision) -> None:
        if decision.proposed_amendment is None:
            raise ValueError("This captured execution request has no proposed execpolicy amendment")
        await to_thread_cancel_safe(self._append_and_update, decision.proposed_amendment)

    def _append_network_and_update(self, runtime, *, host: str, protocol: str, action: str) -> None:
        with self._update_lock:
            runtime.append_network_amendment(path=self.default_policy_path, host=host, protocol=protocol, action=action)
            sources = tuple(source for folder in self.folders for source in collect_policy_sources(folder))
            candidate = {**self._inspection_payload(), "sources": [source.payload() for source in sources]}
            inspection = self.runtime.invoke(candidate)
            with self._state_lock:
                if sources != self.sources or inspection != self._current_inspection:
                    self.sources = sources
                    self._current_inspection = inspection
                    self.revision += 1

    async def persist_network_amendment(self, runtime, *, host: str, protocol: str, action: str) -> None:
        await to_thread_cancel_safe(self._append_network_and_update, runtime, host=host, protocol=protocol, action=action)

    async def apply_approved_amendment(self, decision: ExecPolicyDecision,
        warn: Callable[[str], Awaitable[None]]) -> bool:
        # Source session/handlers.rs::exec_approval still notifies this exact
        # approved request when persistence fails; only reuse is unavailable.
        try:
            await self.persist_approved_amendment(decision)
        except (NativeExecPolicyError, NativeExecPolicyProcessError, OSError) as error:
            await warn(f"Failed to apply execpolicy amendment: {error}")
            return False
        return True
