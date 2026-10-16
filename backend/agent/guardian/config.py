"""Source reviewer catalog choice and read-only configuration, kept off the parent."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from backend.agent.guardian.contract import GUARDIAN_OUTPUT_SCHEMA
from backend.config import AppConfig, PermissionSettings
from backend.agent.context_window import ContextWindowConfig
from backend.feature_flags import FeatureFlags
from backend.llm.model_request_policy import resolve_model_request_policy
from backend.llm.model_selection import model_thinking_levels
from backend.sandbox.policy import (
    FileSystemAccessMode, FileSystemPath, FileSystemSandboxEntry, FileSystemSandboxPolicy,
    FileSystemSandboxKind, FileSystemSpecialPath, NetworkSandboxPolicy, PermissionProfile,
    SandboxEnforcement, SandboxPolicy,
)


@dataclass(frozen=True)
class ReviewModel:
    model: str
    reasoning_effort: str
    model_info: Any | None
    default_review_model_id: str
    catalog_contains_auto_review: bool
    model_overridden: bool

    @property
    def compaction_hash(self) -> str | None:
        info = self.model_info
        return info.comp_hash if info is not None and info.id == self.model else None


def select_review_model(parent, available_models: tuple[Any, ...], *, default_review_model_id: str = "codex-auto-review") -> ReviewModel:
    info = parent.model_info
    extra = info.extra if info is not None else {}
    override = extra.get("auto_review_model_override")
    preferred = override if override is not None else default_review_model_id
    selected = next((model for model in available_models if model.id == preferred), None)
    contains = any(model.id == default_review_model_id for model in available_models)
    if selected is not None:
        levels = model_thinking_levels(selected)
        effort = "low" if "low" in levels else selected.default_reasoning_effort
        return ReviewModel(preferred, effort, selected, default_review_model_id, contains, override is not None)
    levels = model_thinking_levels(info, parent.llm)
    effort = "low" if "low" in levels else parent.thinking_level or (info.default_reasoning_effort if info is not None else "")
    return ReviewModel(override if override is not None else parent.model, effort, info, default_review_model_id, contains, override is not None)


def review_model_for_execution(parent) -> ReviewModel:
    """One actual selector shared by reviewer startup and parent provenance."""
    models = (parent.model_runtime.get_models(parent.provider) if parent.model_runtime is not None else
        (() if parent.model_info is None else (parent.model_info,)))
    oauth = parent.model_runtime.is_using_oauth(parent.provider) if parent.model_runtime is not None else False
    preferred = "gpt-5.6-luna" if parent.provider == "openai" and parent.config.llm.api_key and not oauth else "codex-auto-review"
    return select_review_model(parent, tuple(models), default_review_model_id=preferred)


def reviewer_permission_profile(profile: PermissionProfile) -> PermissionProfile:
    if profile.enforcement is SandboxEnforcement.EXTERNAL:
        raise ValueError("Guardian requires an executor-managed read-only sandbox")
    if profile.file_system.kind is FileSystemSandboxKind.UNRESTRICTED:
        entries = (FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),)
    else:
        entries = tuple(replace(entry, access=FileSystemAccessMode.READ if entry.access.can_read else FileSystemAccessMode.DENY) for entry in profile.file_system.entries)
    return PermissionProfile.managed(FileSystemSandboxPolicy.restricted(entries,
        glob_scan_max_depth=profile.file_system.glob_scan_max_depth), network=NetworkSandboxPolicy.RESTRICTED)


def reviewer_sandbox_policy(parent: SandboxPolicy) -> SandboxPolicy:
    return replace(parent, permission_profile=reviewer_permission_profile(parent.permission_profile), allow_unsandboxed_commands=False,
        auto_allow_commands_if_sandboxed=True, fail_if_unavailable=True, preflight_required=True, excluded_commands=())


def parent_permission_profile(context, *, environment_id=None):
    from backend.permissions.environment_scope import permission_scope_for_context

    scope = permission_scope_for_context(context, environment_id=environment_id)
    if scope is not None:
        return scope.permission_profile
    if context.run_context is not None and context.run_context.turn_environments is not None:
        return None
    return context.sandbox_policy.permission_profile if context.sandbox_policy is not None else None


def parent_environment_identity(context):
    run = context.run_context
    snapshot = run.turn_environments if run is not None else None
    if snapshot is None:
        return context.environment_id, str(context.workspace_root)
    from backend.agent.environment_selection import TurnEnvironment, StartingTurnEnvironment

    return tuple((environment.selection.environment_id, environment.selection.cwd,
        environment.selection.workspace_roots, str(environment.selection.config),
        id(environment.environment) if isinstance(environment, (TurnEnvironment, StartingTurnEnvironment)) else None)
        for environment in snapshot.environments)


def parent_has_full_access(context):
    if context.permission.approval_policy != "never":
        return False
    run = context.run_context
    snapshot = run.turn_environments if run is not None else None
    if snapshot is not None:
        return bool(snapshot.environments) and all(
            environment.config.permission_profile.enforcement is SandboxEnforcement.DISABLED
            for environment in snapshot.turn_environments()) and len(snapshot.turn_environments()) == len(snapshot.environments)
    profile = parent_permission_profile(context)
    return profile is not None and profile.enforcement is SandboxEnforcement.DISABLED


def build_reviewer_config(parent: AppConfig, review_model: ReviewModel) -> AppConfig:
    flags = dict(parent.feature_flags.flags)
    for feature in ("collab", "multi_agent_v2", "guardianv2", "token_budget", "context_management", "codex_hooks", "apps", "plugins", "web_search_request", "web_search_cached", "request_permissions_tool"):
        flags[feature] = False
    request_policy = resolve_model_request_policy(parent, model=review_model.model, definition=review_model.model_info)
    request_policy = replace(request_policy, final_output_json_schema=GUARDIAN_OUTPUT_SCHEMA, final_output_json_schema_strict=False)
    settings = replace(parent.agent, max_turn_seconds=90, turn_error_budget=1, stream_max_attempts=1,
        stream_connection_retries_enabled=False)
    parent_window = parent.context_window_config or ContextWindowConfig()
    reviewer_window = ContextWindowConfig(model_context_window=parent_window.model_context_window,
        model_auto_compact_token_limit=parent_window.model_auto_compact_token_limit,
        model_auto_compact_token_limit_scope=parent_window.model_auto_compact_token_limit_scope)
    return replace(parent, llm=replace(parent.llm, model=review_model.model, reasoning_effort=review_model.reasoning_effort,
        comp_hash=review_model.compaction_hash),
        agent=settings, feature_flags=FeatureFlags(flags), model_request_policy=request_policy,
        token_budget_startup_config=None, context_window_config=reviewer_window,
        permissions=PermissionSettings(auto_allow=[], require_confirm=[], require_diff_review=[], always_deny=[], path_allowlist=[], path_denylist=[]))
