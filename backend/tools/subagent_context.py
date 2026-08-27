from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from backend.permissions.checker import (
    clamp_permission_mode,
    normalize_permission_mode_token,
)
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.subagent_catalog import BUILTIN_AGENT_TYPES
from backend.tools.toolsets import (
    ACTIVE_TOOLSET_POLICY_METADATA_KEY,
    SESSION_TOOLSET_POLICY_METADATA_KEY,
    ToolsetPolicy,
)

_INTERNAL_RUNTIME_METADATA_KEYS = frozenset(
    {
        "_agent_state",
        "_agent_execution_profile",
        "_current_tool_call_id",
        "_streamed_tool_output_ids",
        # Turn-local input/mailbox phase belongs to exactly one agent.  Handing
        # the parent's owner to a child lets the child's ``begin_turn`` rewrite
        # the parent's phase, starve parallel siblings, and consume user steer
        # input addressed to the main agent.  Children build their own owner.
        "turn_input_queue",
    }
)

# Metadata is a data projection across the parent/child boundary.  These
# entries are host-owned capabilities or mutable lifecycle owners, not child
# configuration.  They must never cross the generic copy path; code that
# genuinely needs one must bind a child-local capability after constructing the
# child permission snapshot (as teammate mode does below).
_RUNTIME_OWNER_METADATA_KEYS = frozenset(
    {
        "agent_runtime",
        "conversation_repository",
        "permission_context_provider",
        "permission_mode_setter",
        "command_prompt_allow_rules_setter",
        "teammate_plan_approval_requester",
        "turn_input_queue",
        "turn_execution_state",
        "workspace_context",
        "persist_consumed_turn_input",
        "acknowledge_consumed_turn_input",
        "artifact_store",
        "llm",
        "tool_registry",
        "_tool_execution_context",
        "_execution_journal",
        "_lifecycle_runtime",
        "_context_builder",
        "_sandbox_policy_factory",
        "_permission_context_normalizer",
    }
)

_APPROVED_SHARED_MCP_METADATA_KEYS = frozenset(
    {
        "_mcp_manager",
        "_mcp_owner_session_id",
    }
)
# The session's tool-visibility filter is immutable data (a frozen
# ToolsetPolicy), not a live runtime owner, and it is a *restriction*. Dropping
# it with the rest of the underscore-prefixed keys made the child fall back to
# ToolsetPolicy.default(), so a parent narrowed to read_file produced a child
# that could also write and run commands — restricted_by() then had nothing to
# restrict. It is carried explicitly so delegation can only narrow.
_INHERITED_CAPABILITY_METADATA_KEYS = frozenset(
    {
        ACTIVE_TOOLSET_POLICY_METADATA_KEY,
        SESSION_TOOLSET_POLICY_METADATA_KEY,
    }
)
_UNSAFE_METADATA_VALUE = object()


def _clone_subagent_metadata_data(value: Any) -> Any:
    """Clone provider-neutral metadata data or reject a capability object.

    Only value shapes that can be reasoned about as data cross this generic
    boundary.  Arbitrary class instances are rejected even when they are not
    themselves callable: their methods and mutable identity would otherwise
    recreate the same ambient-capability problem under a new key.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if callable(value):
        return _UNSAFE_METADATA_VALUE
    if isinstance(value, Mapping):
        cloned: dict[Any, Any] = {}
        for key, item in value.items():
            if not isinstance(key, (str, int, float, bool)):
                return _UNSAFE_METADATA_VALUE
            cloned_item = _clone_subagent_metadata_data(item)
            if cloned_item is _UNSAFE_METADATA_VALUE:
                return _UNSAFE_METADATA_VALUE
            cloned[key] = cloned_item
        return cloned
    if isinstance(value, list):
        cloned_list: list[Any] = []
        for item in value:
            cloned_item = _clone_subagent_metadata_data(item)
            if cloned_item is _UNSAFE_METADATA_VALUE:
                return _UNSAFE_METADATA_VALUE
            cloned_list.append(cloned_item)
        return cloned_list
    if isinstance(value, tuple):
        cloned_tuple: list[Any] = []
        for item in value:
            cloned_item = _clone_subagent_metadata_data(item)
            if cloned_item is _UNSAFE_METADATA_VALUE:
                return _UNSAFE_METADATA_VALUE
            cloned_tuple.append(cloned_item)
        return tuple(cloned_tuple)
    if isinstance(value, (set, frozenset)):
        cloned_set: set[Any] = set()
        for item in value:
            cloned_item = _clone_subagent_metadata_data(item)
            if cloned_item is _UNSAFE_METADATA_VALUE:
                return _UNSAFE_METADATA_VALUE
            try:
                cloned_set.add(cloned_item)
            except TypeError:
                return _UNSAFE_METADATA_VALUE
        return frozenset(cloned_set) if isinstance(value, frozenset) else cloned_set
    return _UNSAFE_METADATA_VALUE

AGENT_EXECUTION_PROFILE_METADATA_KEY = "_agent_execution_profile"

AgentExecutionRole = Literal["subagent", "teammate"]
AgentDelivery = Literal["foreground", "background", "persistent"]
AgentDelegation = Literal["none", "foreground", "any"]


@dataclass(frozen=True, slots=True)
class AgentExecutionProfile:
    """Provider-neutral execution capabilities for one child agent.

    Role, delivery, and delegation are independent dimensions. This prevents
    provider identity or tool spelling from selecting a different agent loop,
    and avoids inferring security behavior from a
    ``PermissionContext.source`` string.
    """

    role: AgentExecutionRole = "subagent"
    delivery: AgentDelivery = "foreground"
    delegation: AgentDelegation = "none"
    task_coordination: bool = True
    message_coordination: bool = True
    agent_lifecycle: bool = False
    scheduled_triggers: bool = False
    constrained_async_surface: bool = False

    @property
    def team_mode(self) -> bool:
        return self.role == "teammate"

    @property
    def can_delegate_foreground(self) -> bool:
        return self.delegation in {"foreground", "any"}

    @property
    def can_delegate_background(self) -> bool:
        return self.delegation == "any"

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "delivery": self.delivery,
            "delegation": self.delegation,
            "task_coordination": self.task_coordination,
            "message_coordination": self.message_coordination,
            "agent_lifecycle": self.agent_lifecycle,
            "scheduled_triggers": self.scheduled_triggers,
            "constrained_async_surface": self.constrained_async_surface,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AgentExecutionProfile":
        def _bool(value: Any, *, default: bool = False) -> bool:
            """Parse durable booleans without Python's ``bool('false')`` trap.

            Profiles can cross a journal/checkpoint boundary where JSON values
            are sometimes supplied by older integrations as strings.  An
            unrecognised value is deliberately treated as false so malformed
            data can only narrow an agent surface, never widen it.
            """

            if isinstance(value, bool):
                return value
            if value is None:
                return default
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return bool(value) if value in {0, 1} else default
            normalized = str(value).strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off", ""}:
                return False
            return default

        role = str(raw.get("role") or "subagent").strip().lower()
        delivery = str(raw.get("delivery") or "background").strip().lower()
        delegation = str(raw.get("delegation") or "none").strip().lower()
        if role not in {"subagent", "teammate"}:
            role = "subagent"
        if delivery not in {"foreground", "background", "persistent"}:
            delivery = "background"
        if delegation not in {"none", "foreground", "any"}:
            delegation = "none"
        return cls(
            role=role,  # type: ignore[arg-type]
            delivery=delivery,  # type: ignore[arg-type]
            delegation=delegation,  # type: ignore[arg-type]
            task_coordination=_bool(raw.get("task_coordination"), default=False),
            message_coordination=_bool(raw.get("message_coordination"), default=False),
            agent_lifecycle=_bool(raw.get("agent_lifecycle"), default=False),
            scheduled_triggers=_bool(raw.get("scheduled_triggers"), default=False),
            constrained_async_surface=_bool(
                raw.get("constrained_async_surface"),
                default=delivery != "foreground",
            ),
        )


AGENT_HARD_DENIED_TOOLS = frozenset(
    {
        "task_stop",
        "task_status",
        "team_create",
        "team_list",
        "team_delete",
        "task_output",
        "ask_user",
        "update_plan",
        "enter_plan_mode",
        "exit_plan_mode",
    }
)

AGENT_DELEGATION_TOOLS = frozenset({"task"})
AGENT_TASK_COORDINATION_TOOLS = frozenset(
    {"task_create", "task_get", "task_list", "task_update"}
)
AGENT_MESSAGE_TOOLS = frozenset({"send_message", "message_list"})
AGENT_LIFECYCLE_TOOLS = frozenset({"task_status", "task_stop"})
AGENT_SCHEDULE_TOOLS = frozenset(
    {"schedule_cron", "schedule_cron_list", "schedule_cron_delete"}
)

# MiniCode's constrained background-agent allowlist is expressed with
# capabilities rather than provider/tool-name branches. ``workspace.edit`` includes
# both edit_file and the stronger atomic apply_patch implementation.
ASYNC_AGENT_ALLOWED_CAPABILITIES = frozenset(
    {
        "artifact.read",
        "shell.execute",
        "tool.discovery",
        "web.fetch",
        "web.search",
        "workspace.edit",
        "workspace.glob",
        "workspace.grep",
        "workspace.list",
        "workspace.read",
        "workspace.write",
    }
)
ASYNC_AGENT_ALLOWED_TOOLSETS = frozenset({"mcp"})
ASYNC_AGENT_ALLOWED_TOOLS = frozenset(
    {
        "apply_patch",
        "edit_file",
        "glob_files",
        "grep_files",
        "notebook_edit",
        "read_artifact",
        "read_file",
        "run_command",
        "tool_search",
        "web_fetch",
        "web_search",
        "write_file",
    }
)

# Backwards-compatible exported baseline: an ordinary *foreground* child can
# use the shared task list and agent messaging, but cannot recursively delegate
# or create scheduled work unless the corresponding feature is enabled.
SUBAGENT_DENIED_TOOLS = frozenset(
    AGENT_HARD_DENIED_TOOLS
    | AGENT_DELEGATION_TOOLS
    | AGENT_LIFECYCLE_TOOLS
    | AGENT_SCHEDULE_TOOLS
)

# Persistent teammates are workers rather than ordinary bounded subagents.
# They keep the same hard agent-context exclusions, but
# may coordinate through the shared task list and may synchronously launch one
# ordinary subagent.  ExitPlanMode is exposed only while the teammate is
# actually in Plan mode; EnterPlanMode remains unavailable in every agent
# context.
TEAMMATE_ALLOWED_TOOLS = frozenset(
    AGENT_DELEGATION_TOOLS
    | AGENT_TASK_COORDINATION_TOOLS
    | AGENT_MESSAGE_TOOLS
)


def build_agent_execution_profile(
    *,
    team_mode: bool = False,
    background: bool = False,
    agent_triggers_enabled: bool = False,
) -> AgentExecutionProfile:
    if team_mode:
        return AgentExecutionProfile(
            role="teammate",
            delivery="persistent",
            delegation="foreground",
            task_coordination=True,
            message_coordination=True,
            agent_lifecycle=False,
            scheduled_triggers=bool(agent_triggers_enabled),
            constrained_async_surface=True,
        )
    if background:
        return AgentExecutionProfile(
            role="subagent",
            delivery="background",
            delegation="none",
            task_coordination=False,
            message_coordination=False,
            agent_lifecycle=False,
            scheduled_triggers=False,
            constrained_async_surface=True,
        )
    return AgentExecutionProfile(
        role="subagent",
        delivery="foreground",
        delegation="none",
        task_coordination=True,
        message_coordination=True,
        agent_lifecycle=False,
        scheduled_triggers=bool(agent_triggers_enabled),
        constrained_async_surface=False,
    )


def build_delegating_agent_execution_profile(
    *,
    background: bool = True,
) -> AgentExecutionProfile:
    """Profile for a child that owns and coordinates a descendant tree.

    The shape is independent of whichever model-facing dialect exposed the
    spawn operation.  It runs through the same AgentRuntime/QueryEngine kernel
    as ordinary children; only explicit delegation and lifecycle capabilities
    differ.
    """

    return AgentExecutionProfile(
        role="subagent",
        delivery="background" if background else "foreground",
        delegation="any",
        task_coordination=False,
        message_coordination=True,
        agent_lifecycle=True,
        scheduled_triggers=False,
        constrained_async_surface=False,
    )


def execution_profile_for_background_resume(
    profile: AgentExecutionProfile,
) -> AgentExecutionProfile:
    """Move a durable child into background delivery without widening it.

    A completed foreground child resumes through the async controller and must
    receive the narrow positive background surface. A delegating child
    already owns explicit delegation/lifecycle capabilities, so retain those
    independently modelled capabilities while changing only its delivery.
    Persistent teammates keep their persistent ownership unchanged.
    """

    if profile.delivery in {"background", "persistent"}:
        return profile
    if profile.delegation == "none" and not profile.agent_lifecycle:
        return build_agent_execution_profile(background=True)
    return replace(profile, delivery="background")


def resolve_agent_execution_profile(
    permission: PermissionContext | None,
    metadata: Mapping[str, Any] | None = None,
) -> AgentExecutionProfile | None:
    raw_metadata = metadata if isinstance(metadata, Mapping) else {}
    raw_profile = raw_metadata.get(AGENT_EXECUTION_PROFILE_METADATA_KEY)
    if isinstance(raw_profile, AgentExecutionProfile):
        return raw_profile
    if isinstance(raw_profile, Mapping):
        return AgentExecutionProfile.from_mapping(raw_profile)

    source = str(getattr(permission, "source", "") or "")
    if source.startswith("teammate:"):
        return build_agent_execution_profile(team_mode=True)
    if source.startswith("subagent:"):
        # Old persisted contexts did not record delivery.  Default to the
        # narrower async surface unless an explicit foreground marker exists.
        foreground = str(raw_metadata.get("delivery") or "").strip().lower() == "foreground"
        return build_agent_execution_profile(background=not foreground)
    return None


def _child_denied_tools(
    *,
    execution_profile: AgentExecutionProfile,
    permission_mode: str = "",
) -> frozenset[str]:
    denied = set(AGENT_HARD_DENIED_TOOLS)
    if execution_profile.delegation == "none":
        denied.update(AGENT_DELEGATION_TOOLS)
    if not execution_profile.task_coordination:
        denied.update(AGENT_TASK_COORDINATION_TOOLS)
    if not execution_profile.message_coordination:
        denied.update(AGENT_MESSAGE_TOOLS)
    if not execution_profile.agent_lifecycle:
        denied.update(AGENT_LIFECYCLE_TOOLS)
    if not execution_profile.scheduled_triggers:
        denied.update(AGENT_SCHEDULE_TOOLS)
    if (
        execution_profile.team_mode
        and str(permission_mode or "").strip().lower() == "plan"
    ):
        denied.discard("exit_plan_mode")
    return frozenset(denied)


def subagent_toolset_policy(
    *,
    team_mode: bool = False,
    background: bool = False,
    permission_mode: str = "",
    agent_triggers_enabled: bool = False,
    execution_profile: AgentExecutionProfile | None = None,
) -> ToolsetPolicy:
    profile = execution_profile or build_agent_execution_profile(
        team_mode=team_mode,
        background=background,
        agent_triggers_enabled=agent_triggers_enabled,
    )
    policy = ToolsetPolicy.from_iterables(
        disabled_tools=_child_denied_tools(
            execution_profile=profile,
            permission_mode=permission_mode,
        )
    )
    if not profile.constrained_async_surface:
        return policy

    allowed_tools = set(ASYNC_AGENT_ALLOWED_TOOLS)
    if profile.delegation != "none":
        allowed_tools.update(AGENT_DELEGATION_TOOLS)
    if profile.task_coordination:
        allowed_tools.update(AGENT_TASK_COORDINATION_TOOLS)
    if profile.message_coordination:
        allowed_tools.update(AGENT_MESSAGE_TOOLS)
    if profile.agent_lifecycle:
        allowed_tools.update(AGENT_LIFECYCLE_TOOLS)
    if profile.scheduled_triggers:
        allowed_tools.update(AGENT_SCHEDULE_TOOLS)
    if profile.team_mode and str(permission_mode or "").strip().lower() == "plan":
        allowed_tools.add("exit_plan_mode")
    return policy.with_availability_filter(
        tools=allowed_tools,
        toolsets=ASYNC_AGENT_ALLOWED_TOOLSETS,
        capabilities=ASYNC_AGENT_ALLOWED_CAPABILITIES,
    )


def is_subagent_permission_context(
    permission: PermissionContext | None,
    metadata: Mapping[str, Any] | None = None,
) -> bool:
    # Some tool-owned permission hooks receive the full ToolExecutionContext,
    # while loop/session policy code passes PermissionContext directly.
    # Normalize both shapes at this single boundary.
    if permission is not None and isinstance(
        getattr(permission, "permission", None),
        PermissionContext,
    ):
        if metadata is None:
            candidate_metadata = getattr(permission, "metadata", None)
            metadata = candidate_metadata if isinstance(candidate_metadata, Mapping) else None
        permission = getattr(permission, "permission")
    raw_metadata = metadata if isinstance(metadata, Mapping) else {}
    if isinstance(raw_metadata.get(AGENT_EXECUTION_PROFILE_METADATA_KEY), (AgentExecutionProfile, Mapping)):
        return True
    source = str(getattr(permission, "source", "") or "")
    return source.startswith(("subagent:", "teammate:"))


def _append_unique_rules(rules: list[str], additions: Iterable[str]) -> list[str]:
    seen = set(rules)
    for rule in sorted(additions):
        if rule not in seen:
            rules.append(rule)
            seen.add(rule)
    return rules


def _clone_filesystem_constraints(raw: Any) -> dict[str, list[str]]:
    """Clone child filesystem policy without string-splitting or aliasing.

    A malformed constraint is a security boundary error, so unsupported value
    types fail closed instead of being silently dropped.
    """

    if not isinstance(raw, Mapping):
        raise ValueError("filesystem_constraints must be an object")
    cloned: dict[str, list[str]] = {}
    for raw_key, raw_values in raw.items():
        key = str(raw_key or "").strip()
        if not key:
            raise ValueError("filesystem constraint names must be non-empty")
        if raw_values is None:
            cloned[key] = []
            continue
        if isinstance(raw_values, str):
            cloned[key] = [raw_values]
            continue
        if not isinstance(raw_values, (list, tuple, set, frozenset)):
            raise ValueError(
                f"filesystem constraint {key!r} must be a string or list of paths"
            )
        cloned[key] = [str(value) for value in raw_values]
    return cloned


def sanitize_subagent_runtime_metadata(parent_metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Project parent metadata into child data without leaking capabilities.

    A metadata dictionary is deliberately not treated as a capability channel.
    In particular, checking only a small list of callback names is unsafe:
    integrations can add a new callable under an otherwise innocuous key and
    accidentally hand the child a live parent owner.  Unknown callables and
    known mutable runtime owners are therefore rejected by default.  Approved
    MCP ownership metadata remains available for the provider/tool boundary;
    the child still receives its own runtime, permission context and tool
    session explicitly below this projection.
    """
    source = parent_metadata if isinstance(parent_metadata, dict) else {}
    projected: dict[str, Any] = {}
    for raw_key, value in source.items():
        key = str(raw_key)
        if key in _APPROVED_SHARED_MCP_METADATA_KEYS:
            # MCP manager/session ownership is an explicit shared resource.
            # It is intentionally the sole exception to the data-only rule.
            projected[key] = value
            continue
        if key in _INHERITED_CAPABILITY_METADATA_KEYS:
            # An inherited capability restriction: carried so the child cannot
            # regain a surface the parent gave up.
            if isinstance(value, ToolsetPolicy):
                projected[key] = value
            continue
        if (
            key in _INTERNAL_RUNTIME_METADATA_KEYS
            or key in _RUNTIME_OWNER_METADATA_KEYS
            or key.startswith("_")
        ):
            continue
        cloned = _clone_subagent_metadata_data(value)
        if cloned is not _UNSAFE_METADATA_VALUE:
            projected[key] = cloned
    return projected


def build_subagent_permission_context(
    agent_type: str,
    parent_context: ToolExecutionContext | None,
    *,
    read_only: bool = False,
    extra_deny_rules: list[str] | None = None,
    team_mode: bool = False,
    background: bool = False,
    plan_mode_required: bool = False,
    requested_mode: str = "",
    agent_triggers_enabled: bool = False,
    execution_profile: AgentExecutionProfile | None = None,
) -> PermissionContext:
    parent_permission = parent_context.permission if parent_context else PermissionContext()
    profile = execution_profile or build_agent_execution_profile(
        team_mode=team_mode,
        background=background,
        agent_triggers_enabled=agent_triggers_enabled,
    )
    team_mode = profile.team_mode
    # ``requested_mode`` is model input. The canonical token normalizer rejects
    # bogus modes; clamping it against the parent keeps delegation from widening
    # authority the caller does not hold.
    if requested_mode is None or not str(requested_mode).strip():
        normalized_requested_mode = ""
    else:
        normalized_requested_mode = clamp_permission_mode(
            requested_mode,
            parent_permission.mode,
        )

    forced_read_only = bool(read_only or agent_type in {"explore", "plan"})
    if team_mode:
        # A teammate is an independent worker and carries its own permission
        # mode rather than inheriting the leader's, so a leader in Plan mode
        # still spawns a write-capable teammate. An explicitly requested mode is
        # clamped to the parent above, and required Plan mode or a read-only
        # contract still overrides everything.
        if plan_mode_required or forced_read_only:
            mode = "plan"
        else:
            mode = normalized_requested_mode or "confirm"
    elif parent_permission.mode == "plan" or forced_read_only:
        # Ordinary subagents are stricter: they are bounded helpers of the
        # current turn, not independent workers, so they do inherit Plan mode.
        mode = "plan"
    else:
        mode = parent_permission.mode

    child_denied_tools = _child_denied_tools(
        execution_profile=profile,
        permission_mode=mode,
    )
    # A read-only teammate is a MiniCode extension, not CC's required-plan
    # workflow.  It must not be able to turn a hard read-only delegation into
    # a write-capable one by calling ExitPlanMode.
    if team_mode and forced_read_only and not plan_mode_required:
        child_denied_tools = frozenset({*child_denied_tools, "exit_plan_mode"})

    deny_rules = _append_unique_rules(
        list(parent_permission.tool_deny_rules),
        child_denied_tools,
    )

    if extra_deny_rules:
        deny_rules = _append_unique_rules(deny_rules, list(extra_deny_rules))

    if team_mode:
        source = (
            f"teammate:{agent_type}:required_plan"
            if plan_mode_required
            else f"teammate:{agent_type}"
        )
        approval_policy = (
            "never" if mode == "bypass" else "on-request"
        )
        sandbox_mode = (
            "danger-full-access"
            if mode == "bypass"
            else "read-only"
            if mode == "plan"
            else "workspace-write"
        )
        pre_plan_mode = None
    else:
        source = f"subagent:{agent_type}"
        approval_policy = (
            parent_permission.approval_policy
        )
        sandbox_mode = (
            "read-only" if mode == "plan" else parent_permission.sandbox_mode
        )
        pre_plan_mode = (
            parent_permission.pre_plan_mode
            if parent_permission.mode == "plan"
            else parent_permission.mode
            if mode == "plan"
            else None
        )

    # Start from the full immutable parent context so owner identity, managed
    # requirements, sandbox failure semantics, command prompt grants and future
    # PermissionContext fields cannot silently disappear at the child boundary.
    # Clone mutable containers so a child cannot mutate the parent's policy by
    # retaining a shared list/dict reference.
    return replace(
        parent_permission,
        mode=mode,
        session_overrides=dict(parent_permission.session_overrides),
        command_prompt_allow_rules=tuple(parent_permission.command_prompt_allow_rules),
        tool_deny_rules=deny_rules,
        filesystem_constraints=_clone_filesystem_constraints(
            parent_permission.filesystem_constraints
        ),
        source=source,
        pre_plan_mode=pre_plan_mode,
        approval_policy=approval_policy,
        sandbox_mode=sandbox_mode,
        sandbox_excluded_commands=tuple(parent_permission.sandbox_excluded_commands),
    )


def transition_subagent_permission_mode(
    agent_type: str,
    parent_context: ToolExecutionContext | None,
    current_context: PermissionContext,
    mode: str,
    *,
    read_only: bool = False,
    extra_deny_rules: list[str] | None = None,
    plan_mode_required: bool = False,
    agent_triggers_enabled: bool = False,
    execution_profile: AgentExecutionProfile | None = None,
) -> PermissionContext:
    """Rebuild one live child permission owner for a mode transition.

    Generated deny rules must be recomputed from the parent/profile rather
    than edited in place.  This lets temporary capabilities appear and vanish
    with the mode (notably teammate ``exit_plan_mode``) while retaining every
    explicit parent, managed, and custom-agent denial.
    """

    # Normalize, do not silently rewrite: the old allowlist also omitted
    # "confirm"/"auto", so a legal stricter target remains explicit.
    # The parent ceiling is applied by build_subagent_permission_context below.
    target_mode = normalize_permission_mode_token(mode)
    profile = execution_profile or resolve_agent_execution_profile(
        current_context,
    ) or build_agent_execution_profile(team_mode=True)
    rebuilt = build_subagent_permission_context(
        agent_type,
        parent_context,
        read_only=read_only,
        extra_deny_rules=extra_deny_rules,
        team_mode=profile.team_mode,
        background=profile.delivery == "background",
        # Required Plan mode is an admission gate. Once the leader approves
        # an exit, the approved target must not be forced straight back to
        # Plan mode.
        plan_mode_required=bool(plan_mode_required and target_mode == "plan"),
        requested_mode=target_mode,
        agent_triggers_enabled=agent_triggers_enabled,
        execution_profile=profile,
    )
    return replace(
        rebuilt,
        source=(
            f"teammate:{agent_type}:required_plan"
            if profile.team_mode and plan_mode_required
            else rebuilt.source
        ),
        pre_plan_mode=(
            current_context.mode
            if target_mode == "plan" and current_context.mode != "plan"
            else None
        ),
    )


def build_subagent_prompt(
    agent_type: str,
    prompt: str,
    *,
    get_custom_agent: Callable[[str], Any | None] | None = None,
) -> str:
    if agent_type not in BUILTIN_AGENT_TYPES and get_custom_agent is not None:
        custom = get_custom_agent(agent_type)
        if custom and getattr(custom, "prompt", ""):
            return f"{custom.prompt}\n\nTask:\n{prompt}".strip()
    if agent_type == "explore":
        # cc exploreAgent.ts system prompt: read-only specialist with
        # explicit prohibitions and parallel-search guidance.
        role_note = (
            "You are a read-only exploration agent and a file search specialist. You excel at thoroughly navigating and exploring codebases.\n\n=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===\nThis is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:\n- Creating new files (no Write, touch, or file creation of any kind)\n- Modifying existing files (no Edit operations)\n- Deleting files (no rm or deletion)\n- Moving or copying files (no mv or cp)\n- Creating temporary files anywhere, including /tmp\n- Using redirect operators (>, >>, |) or heredocs to write to files\n- Running ANY commands that change system state\n\nYour role is EXCLUSIVELY to search and analyze existing code.\n\nYour strengths:\n- Rapidly finding files using glob patterns\n- Searching code and text with powerful regex patterns\n- Reading and analyzing file contents\n\nGuidelines:\n- Use glob_files for broad file pattern matching\n- Use grep_files for searching file contents with regex\n- Use read_file when you know the specific file path you need to read\n- Use run_command ONLY for read-only operations (ls, git status, git log, git diff, find, grep, cat, head, tail)\n- NEVER use run_command for: mkdir, touch, rm, cp, mv, git add, git commit, npm install, pip install, or any file creation/modification\n- Adapt your search approach based on the thoroughness level specified by the caller\n- Communicate your final report directly as a regular message - do NOT attempt to create files\n\nNOTE: You are meant to be a fast agent that returns output as quickly as possible:\n- Make efficient use of the tools at your disposal; be smart about how you search\n- Wherever possible, spawn multiple parallel tool calls for grepping and reading files\n\nComplete the user's search request efficiently and report your findings clearly."
        )
    elif agent_type == "plan":
        # cc planAgent.ts system prompt semantics.
        role_note = (
            'You are a planning agent for implementation tasks. You excel at breaking down complex programming requests into concrete, ordered implementation steps.\n\n=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===\nThis is a READ-ONLY planning task. You are STRICTLY PROHIBITED from:\n- Creating, modifying, deleting, moving, or copying files\n- Running ANY commands that change system state\n\nYour role is EXCLUSIVELY to:\n1. Research the relevant code until you fully understand the request context\n2. Produce a step-by-step implementation plan the caller can follow\n\nGuidelines:\n- Explore with glob_files, grep_files, and read_file before writing any plan step\n- Reference concrete file paths and function names in each step\n- Order steps so independent work is parallelizable where safe\n- Keep the plan actionable and minimal; skip steps the request does not need\n- Present the plan clearly for user review'
        )
    else:
        role_note = (
            "You are a general-purpose agent. Handle the assigned task with the available tools "
            "and return the result to the parent agent."
        )
    return f"{role_note}\n\nTask:\n{prompt}".strip()
