"""Source step_activation admission for an explicitly targeted running turn."""
from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.agent.guardian.prompt import POLICY, POLICY_TEMPLATE
from backend.config_requirements import ConfigRequirements
from backend.permissions.context import PermissionContext
from backend.sandbox.policy import FileSystemAccessMode, FileSystemSandboxKind, FileSystemSpecialPath

_NODE_REPL_POLICY = (Path(__file__).parent / "guardian/resources/node-repl-policy.md").read_text(encoding="utf-8")
_V2_FIELDS = ("async_classifier_mode", "async_classifier_conversation_token_limit", "classifier_instructions",
    "review_threshold_basis_points", "max_tool_call_lag", "reasoning_effort", "transcript", "max_action_tokens",
    "max_classifier_instruction_tokens", "reuse_parent_compaction", "max_parent_compaction_tokens")
_TRANSCRIPT_FIELDS = ("sources", "include_images", "max_message_entry_tokens", "max_tool_entry_tokens",
    "max_message_transcript_tokens", "max_tool_transcript_tokens", "max_recent_non_user_entries")


class ModelActivationRejected(ValueError):
    pass


class ModelActivationTargetUnavailable(ValueError):
    def __init__(self):
        super().__init__("The originally targeted turn is no longer running")


def _settings(config):
    return config.config_layer_stack.effective_config() if config.config_layer_stack is not None else {}


def _requirements(config):
    return config.config_layer_stack.requirements if config.config_layer_stack is not None else ConfigRequirements()


def _extra(snapshot):
    return snapshot.model_info.extra if snapshot.model_info is not None else {}


def _is_cyber(snapshot):
    return _extra(snapshot).get("model_specialty") == "cyber"


def _ignores_prefix_rules(snapshot, requirements):
    ignored = requirements.value_for("auto_review", {}).get("ignore_rules") or []
    return _is_cyber(snapshot) or snapshot.model in ignored


def _computer_review(snapshot):
    extra = _extra(snapshot)
    guardian = extra.get("guardian")
    return guardian.get("computer_use", "disabled") != "disabled" if guardian is not None else extra.get("node_repl_auto_review_required", False)


def _auto_review(snapshot):
    return (_extra(snapshot).get("model_messages") or {}).get("auto_review") or {}


def _message(snapshot, name, default):
    value = _auto_review(snapshot).get(name)
    return default if value is None else value


def _classification_settings(snapshot):
    declared = (_extra(snapshot).get("model_messages") or {}).get("guardian_v2") or {}
    result = {name: deepcopy(declared[name]) for name in _V2_FIELDS if declared.get(name) is not None}
    if "transcript" in result:
        transcript = result["transcript"]
        transcript = {name: transcript[name] for name in _TRANSCRIPT_FIELDS if transcript.get(name) is not None}
        if transcript:
            result["transcript"] = transcript
        else:
            result.pop("transcript")
    return result


def _paths_share_target(left, right):
    if left.kind == right.kind:
        return left == right
    literal, special = (left, right) if left.kind == "path" else (right, left)
    if literal.kind != "path" or special.kind != "special" or special.subpath is not None:
        return False
    path = Path(literal.value)
    if special.value == FileSystemSpecialPath.ROOT:
        return path.is_absolute() and path.parent == path
    return special.value == FileSystemSpecialPath.SLASH_TMP and os.name != "nt" and path == Path("/tmp")


def _full_disk_write(tool_context):
    filesystem = tool_context.sandbox_policy.permission_profile.file_system
    if filesystem.kind in {FileSystemSandboxKind.UNRESTRICTED, FileSystemSandboxKind.EXTERNAL}:
        return True
    root_write = any(entry.path.kind == "special" and entry.path.value == FileSystemSpecialPath.ROOT
        and entry.path.subpath is None and entry.access.can_write
        for entry in filesystem.entries)
    if not root_write:
        return False
    # Source permissions.rs::has_write_narrowing_entries. A read entry that
    # is shadowed by a same-target write is not an effective restriction.
    for entry in filesystem.entries:
        if entry.access.can_write:
            continue
        if entry.path.kind == "glob_pattern":
            return False
        if entry.path.kind == "special":
            if entry.path.value == FileSystemSpecialPath.ROOT:
                if entry.access == FileSystemAccessMode.DENY:
                    return False
                continue
            if entry.path.value == FileSystemSpecialPath.MINIMAL or entry.path.value == FileSystemSpecialPath.SLASH_TMP and os.name == "nt":
                continue
        if entry.access == FileSystemAccessMode.READ and any(candidate.access.can_write
                and _paths_share_target(entry.path, candidate.path) for candidate in filesystem.entries):
            continue
        return False
    return True


def validate_model_activation(*, admitted, current, destination, admitted_permission, current_permission, live_config, tool_context):
    """The exact legacy-turn restrictions retained by Source step_activation."""
    if not admitted.config.feature_flags.enabled("step_model_switching"):
        raise ModelActivationRejected("turn settings updates require the step_model_switching feature")
    requirements = _requirements(live_config)
    requirements.ensure_approval_policy(current_permission.approval_policy_value())
    reviewer = _settings(current.config).get("approvals_reviewer", "user")
    allowed_reviewers = requirements.value_for("allowed_approvals_reviewers")
    if allowed_reviewers is not None and reviewer not in allowed_reviewers:
        raise ModelActivationRejected("the selected approvals reviewer is no longer allowed")
    required = requirements.auto_review_required_for_model(destination.model)
    if required and (reviewer != "auto_review" or _full_disk_write(tool_context)
            or not admitted.config.feature_flags.enabled("guardian_approval")):
        raise ModelActivationRejected(f"Automatic approval review is required for model {destination.model}")
    if current_permission.approval_policy_value() != admitted_permission.approval_policy_value():
        raise ModelActivationRejected("the destination changes the admitted approval policy")
    if required != requirements.auto_review_required_for_model(current.model) or required != _requirements(admitted.config).auto_review_required_for_model(admitted.model):
        raise ModelActivationRejected("the destination changes model-required approval authority")
    ignores = _ignores_prefix_rules(destination, requirements)
    if ignores != _ignores_prefix_rules(current, requirements) or ignores != _ignores_prefix_rules(admitted, _requirements(admitted.config)):
        raise ModelActivationRejected("the destination changes the admitted prefix-rule policy")
    if any(snapshot.model_info is None or snapshot.model_info.id != snapshot.model or snapshot.model_info.source_model_metadata is None for snapshot in (admitted, current)):
        raise ModelActivationRejected("the active model has only fallback metadata")
    if destination.model_info is None or destination.model_info.id != destination.model or destination.model_info.source_model_metadata is None:
        raise ModelActivationRejected("the destination model has only fallback metadata")
    retained = (admitted, current)
    if any(_is_cyber(snapshot) != _is_cyber(destination) for snapshot in retained):
        raise ModelActivationRejected("the destination changes the admitted Guardian rejection policy")
    if any(_computer_review(snapshot) != _computer_review(destination) for snapshot in retained):
        raise ModelActivationRejected("the destination changes the admitted node REPL review requirement")
    for name, reason in (("guardian", "Guardian coverage"), ("node_repl_disabled", "node REPL availability restriction"),
            ("auto_review_model_override", "explicit Guardian reviewer model")):
        default = False if name == "node_repl_disabled" else None
        if any(_extra(snapshot).get(name, default) != _extra(destination).get(name, default) for snapshot in retained):
            raise ModelActivationRejected(f"the destination changes the {'admitted ' if name != 'auto_review_model_override' else ''}{reason}")
    if (admitted.config.feature_flags.enabled("guardianv2") or _extra(destination).get("guardian") is not None) and admitted.config.feature_flags.enabled("guardian_approval"):
        if any(_classification_settings(snapshot) != _classification_settings(destination) for snapshot in retained):
            raise ModelActivationRejected("the destination changes the admitted Guardian V2 classification settings")
    if _extra(destination).get("auto_review_model_override") is None:
        if any(_message(snapshot, "node_repl_policy", _NODE_REPL_POLICY) != _message(destination, "node_repl_policy", _NODE_REPL_POLICY) for snapshot in retained):
            raise ModelActivationRejected("the destination changes the Guardian parent-fallback node REPL policy")
        for config in (admitted.config, live_config):
            configured = _settings(config).get("guardian_policy_config")
            target = configured if configured is not None else _message(destination, "policy", POLICY)
            if any((configured if configured is not None else _message(snapshot, "policy", POLICY)) != target for snapshot in retained):
                raise ModelActivationRejected("the destination changes the Guardian parent-fallback policy")
        if any(_message(snapshot, "policy_template", POLICY_TEMPLATE).rstrip() != _message(destination, "policy_template", POLICY_TEMPLATE).rstrip() for snapshot in retained):
            raise ModelActivationRejected("the destination changes the Guardian parent-fallback policy template")


@dataclass(frozen=True)
class ModelActivationTicket:
    state: Any
    current: Any


@dataclass
class ModelActivationState:
    owner: Any
    task: asyncio.Task
    cancel_event: asyncio.Event | None
    admitted: Any
    admitted_permission: PermissionContext
    turn_id: str
    updates: asyncio.Lock = field(default_factory=asyncio.Lock)

    def capture(self):
        self.require_current()
        return ModelActivationTicket(self, self.owner.model_execution)

    def require_current(self):
        if (self.owner.model_activation is not self or self.owner.model_owner_task is not self.task
                or self.task.done() or self.task.cancelling()
                or self.cancel_event is not None and self.cancel_event.is_set()):
            raise ModelActivationTargetUnavailable()

    def commit(self, ticket, destination, *, live_config, is_current):
        self.require_current()
        if ticket.state is not self or not is_current():
            raise ModelActivationTargetUnavailable()
        if self.owner.model_execution is not ticket.current:
            raise ModelActivationRejected("the running turn's model settings changed during resolution")
        tool_context = self.owner.context_builder._tool_execution_context
        permission = self.owner.permission_context_provider() if self.owner.permission_context_provider is not None else tool_context.permission
        validate_model_activation(admitted=self.admitted, current=ticket.current, destination=destination,
            admitted_permission=self.admitted_permission, current_permission=permission, live_config=live_config, tool_context=tool_context)
        # No await separates the final task/requirements checks and actual
        # publication. Turn settings never overwrite thread defaults.
        self.owner.model_execution = destination


def admit_model_activation(owner, *, task, cancel_event, permission, turn_id):
    owner.model_activation = ModelActivationState(owner, task, cancel_event, owner.model_execution, permission, turn_id)
