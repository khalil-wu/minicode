"""Tests for subagent permission and prompt context."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.agent.state import AgentState
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import MAX_TOOL_RESULT_BYTES, BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec
from backend.tools.registry import ToolRegistry
from backend.tools.subagent_context import (
    AGENT_MESSAGE_TOOLS,
    AGENT_TASK_COORDINATION_TOOLS,
    AgentExecutionProfile,
    SUBAGENT_DENIED_TOOLS,
    TEAMMATE_ALLOWED_TOOLS,
    build_agent_execution_profile,
    build_subagent_permission_context,
    build_subagent_prompt,
    is_subagent_permission_context,
    resolve_agent_execution_profile,
    sanitize_subagent_runtime_metadata,
    subagent_toolset_policy,
    transition_subagent_permission_mode,
)
from backend.tools.agent_tools import TaskTool, _externalize_large_subagent_result
from backend.artifact.store import ArtifactStore
from backend.tools.tool_search import ToolSearchTool
from backend.tools.toolsets import (
    SESSION_TOOLSET_POLICY_METADATA_KEY,
    ToolsetPolicy,
)


def test_normal_subagent_inherits_parent_deny_rules() -> None:
    parent = ToolExecutionContext(
        permission=PermissionContext(
            mode="auto",
            tool_deny_rules=["run_command", "task"],
            filesystem_constraints={"denylist": ["/secret"]},
            source="runtime",
        ),
    )

    ctx = build_subagent_permission_context("implement", parent)

    assert ctx.mode == "auto"
    assert "run_command" in ctx.tool_deny_rules
    assert "task" in ctx.tool_deny_rules
    assert "send_message" not in ctx.tool_deny_rules
    assert "message_list" not in ctx.tool_deny_rules
    assert ctx.filesystem_constraints == {"denylist": ["/secret"]}
    assert ctx.source == "subagent:implement"


def test_subagent_preserves_complete_parent_permission_owner_without_aliasing(
    tmp_path: Path,
) -> None:
    parent_permission = PermissionContext(
        mode="auto",
        session_overrides={"write_file": PermissionLevel.CONFIRM},
        command_prompt_allow_rules=("run tests",),
        tool_deny_rules=["custom_tool"],
        filesystem_constraints={
            "denylist": ["secrets/**"],
            "write_allowlist": ["src/**"],
        },
        workspace_scope="worktree",
        source="session",
        approval_policy="confirm",
        sandbox_mode="external-sandbox",
        requirements_source="managed-requirements.toml",
        allow_unsandboxed_commands=False,
        sandbox_fail_if_unavailable=False,
        sandbox_auto_allow_commands=True,
        sandbox_excluded_commands=("dangerous-command",),
        conversation_id="conversation-owner",
        workspace_root=tmp_path,
    )
    parent = ToolExecutionContext(permission=parent_permission)

    ctx = build_subagent_permission_context("implement", parent)

    assert ctx.mode == "auto"
    assert ctx.source == "subagent:implement"
    assert ctx.session_overrides == parent_permission.session_overrides
    assert ctx.command_prompt_allow_rules == ("run tests",)
    assert ctx.workspace_scope == "worktree"
    assert ctx.approval_policy == "confirm"
    assert ctx.sandbox_mode == "external-sandbox"
    assert ctx.requirements_source == "managed-requirements.toml"
    assert ctx.allow_unsandboxed_commands is False
    assert ctx.sandbox_fail_if_unavailable is False
    assert ctx.sandbox_auto_allow_commands is True
    assert ctx.sandbox_excluded_commands == ("dangerous-command",)
    assert ctx.conversation_id == "conversation-owner"
    assert ctx.workspace_root == tmp_path

    assert ctx.session_overrides is not parent_permission.session_overrides
    assert ctx.tool_deny_rules is not parent_permission.tool_deny_rules
    assert ctx.filesystem_constraints is not parent_permission.filesystem_constraints
    assert (
        ctx.filesystem_constraints["denylist"]
        is not parent_permission.filesystem_constraints["denylist"]
    )
    ctx.filesystem_constraints["denylist"].append("child-only/**")
    assert parent_permission.filesystem_constraints["denylist"] == ["secrets/**"]


def test_child_permission_context_recognizes_subagents_and_teammates() -> None:
    assert is_subagent_permission_context(
        PermissionContext(source="subagent:explore")
    )
    assert is_subagent_permission_context(
        PermissionContext(source="teammate:reviewer")
    )
    assert is_subagent_permission_context(
        PermissionContext(source="teammate:reviewer:required_plan")
    )
    assert not is_subagent_permission_context(PermissionContext(source="runtime"))
    assert is_subagent_permission_context(
        PermissionContext(source="runtime"),
        {"_agent_execution_profile": build_agent_execution_profile(background=True)},
    )


def test_execution_profile_mapping_parses_string_booleans_fail_closed() -> None:
    profile = AgentExecutionProfile.from_mapping(
        {
            "role": "subagent",
            "delivery": "background",
            "delegation": "any",
            "task_coordination": "false",
            "message_coordination": "0",
            "scheduled_triggers": "off",
            "constrained_async_surface": "true",
        }
    )

    assert profile.delegation == "any"
    assert profile.task_coordination is False
    assert profile.message_coordination is False
    assert profile.scheduled_triggers is False
    assert profile.constrained_async_surface is True

    malformed = AgentExecutionProfile.from_mapping(
        {
            "delivery": "background",
            "task_coordination": "definitely",
            "message_coordination": object(),
        }
    )
    assert malformed.task_coordination is False
    assert malformed.message_coordination is False


def test_execution_profile_metadata_is_authoritative_over_source_fallback() -> None:
    explicit = AgentExecutionProfile(
        role="subagent",
        delivery="background",
        delegation="any",
        task_coordination=False,
        message_coordination=False,
        constrained_async_surface=True,
    )
    resolved = resolve_agent_execution_profile(
        PermissionContext(source="teammate:legacy"),
        {"_agent_execution_profile": explicit},
    )

    assert resolved is explicit
    assert resolved.team_mode is False
    assert resolved.can_delegate_background is True


def test_teammate_toolset_keeps_sync_delegation_and_task_coordination() -> None:
    ordinary = subagent_toolset_policy().disabled_tools
    teammate_default = subagent_toolset_policy(
        team_mode=True,
        permission_mode="confirm",
    ).disabled_tools
    teammate_plan = subagent_toolset_policy(
        team_mode=True,
        permission_mode="plan",
    ).disabled_tools

    assert "task" in ordinary
    assert AGENT_TASK_COORDINATION_TOOLS.isdisjoint(ordinary)
    assert AGENT_MESSAGE_TOOLS.isdisjoint(ordinary)
    assert TEAMMATE_ALLOWED_TOOLS.isdisjoint(teammate_default)
    assert "task_stop" in teammate_default
    assert "task_status" in teammate_default
    assert "task_output" in teammate_default
    assert "team_create" in teammate_default
    assert "ask_user" in teammate_default
    assert "enter_plan_mode" in teammate_default
    assert "exit_plan_mode" in teammate_default
    assert "exit_plan_mode" not in teammate_plan


def test_default_teammate_has_independent_permission_owner() -> None:
    parent = ToolExecutionContext(
        permission=PermissionContext(
            mode="bypass",
            tool_deny_rules=["custom_tool"],
            approval_policy="never",
            sandbox_mode="danger-full-access",
        )
    )

    ctx = build_subagent_permission_context(
        "implement",
        parent,
        team_mode=True,
    )

    assert ctx.mode == "confirm"
    assert ctx.source == "teammate:implement"
    assert ctx.pre_plan_mode is None
    assert ctx.approval_policy == "on-request"
    assert ctx.sandbox_mode == "workspace-write"
    assert "custom_tool" in ctx.tool_deny_rules
    assert TEAMMATE_ALLOWED_TOOLS.isdisjoint(ctx.tool_deny_rules)
    assert "exit_plan_mode" in ctx.tool_deny_rules


def test_teammate_owns_its_permission_mode_instead_of_inheriting_plan() -> None:
    """A Plan-mode leader still spawns a write-capable teammate.

    A teammate is an independent worker and owns its permission mode, so it is
    not narrowed by the leader's Plan mode. Ordinary subagents deliberately
    differ and do inherit Plan mode, so both halves are pinned here.
    """

    parent = ToolExecutionContext(permission=PermissionContext(mode="plan"))

    teammate = build_subagent_permission_context("implement", parent, team_mode=True)
    assert teammate.mode == "confirm"
    assert teammate.sandbox_mode == "workspace-write"

    subagent = build_subagent_permission_context("implement", parent, team_mode=False)
    assert subagent.mode == "plan"
    assert subagent.sandbox_mode == "read-only"

    # A read-only contract still wins over the teammate's own mode.
    read_only_teammate = build_subagent_permission_context(
        "implement", parent, team_mode=True, read_only=True
    )
    assert read_only_teammate.mode == "plan"
    assert read_only_teammate.sandbox_mode == "read-only"


def test_required_plan_teammate_can_only_exit_through_leader_approval() -> None:
    ctx = build_subagent_permission_context(
        "implement",
        ToolExecutionContext(permission=PermissionContext(mode="bypass")),
        team_mode=True,
        plan_mode_required=True,
        requested_mode="bypass",
    )

    assert ctx.mode == "plan"
    assert ctx.source == "teammate:implement:required_plan"
    assert ctx.pre_plan_mode is None
    assert ctx.approval_policy == "on-request"
    assert ctx.sandbox_mode == "read-only"
    assert "task" not in ctx.tool_deny_rules
    assert "enter_plan_mode" in ctx.tool_deny_rules
    assert "exit_plan_mode" not in ctx.tool_deny_rules


def test_teammate_mode_transition_rebuilds_generated_deny_rules() -> None:
    parent = ToolExecutionContext(
        permission=PermissionContext(
            mode="confirm",
            tool_deny_rules=["custom_tool"],
        )
    )
    profile = build_agent_execution_profile(team_mode=True)
    default_context = build_subagent_permission_context(
        "implement",
        parent,
        team_mode=True,
        execution_profile=profile,
    )

    plan_context = transition_subagent_permission_mode(
        "implement",
        parent,
        default_context,
        "plan",
        execution_profile=profile,
    )
    assert plan_context.mode == "plan"
    assert plan_context.pre_plan_mode == "confirm"
    assert "custom_tool" in plan_context.tool_deny_rules
    assert "exit_plan_mode" not in plan_context.tool_deny_rules

    restored = transition_subagent_permission_mode(
        "implement",
        parent,
        plan_context,
        "confirm",
        execution_profile=profile,
    )
    assert restored.mode == "confirm"
    assert restored.pre_plan_mode is None
    assert "custom_tool" in restored.tool_deny_rules
    assert "exit_plan_mode" in restored.tool_deny_rules


def test_required_plan_transition_can_leave_plan_after_approval() -> None:
    parent = ToolExecutionContext(permission=PermissionContext(mode="bypass"))
    profile = build_agent_execution_profile(team_mode=True)
    required_plan = build_subagent_permission_context(
        "implement",
        parent,
        team_mode=True,
        plan_mode_required=True,
        execution_profile=profile,
    )

    approved = transition_subagent_permission_mode(
        "implement",
        parent,
        required_plan,
        "auto",
        plan_mode_required=True,
        execution_profile=profile,
    )

    assert approved.mode == "auto"
    assert approved.source == "teammate:implement:required_plan"
    assert approved.sandbox_mode == "workspace-write"
    assert "exit_plan_mode" in approved.tool_deny_rules


@pytest.mark.parametrize(
    ("parent_mode", "requested_mode", "expected_mode", "approval_policy", "sandbox_mode"),
    [
        # Delegation may narrow authority.
        ("bypass", "auto", "auto", "on-request", "workspace-write"),
        ("bypass", "plan", "plan", "on-request", "read-only"),
        # A parent that already holds the mode keeps it.
        ("bypass", "bypass", "bypass", "never", "danger-full-access"),
        # Delegation must never widen it: a teammate spawned from a read-only
        # Plan-mode turn cannot request an unsandboxed, never-prompting context.
        ("plan", "bypass", "plan", "on-request", "read-only"),
        ("plan", "auto", "plan", "on-request", "read-only"),
        ("confirm", "bypass", "confirm", "on-request", "workspace-write"),
    ],
)
def test_teammate_requested_mode_is_clamped_to_the_parent_ceiling(
    parent_mode: str,
    requested_mode: str,
    expected_mode: str,
    approval_policy: str,
    sandbox_mode: str,
) -> None:
    ctx = build_subagent_permission_context(
        "implement",
        ToolExecutionContext(permission=PermissionContext(mode=parent_mode)),
        team_mode=True,
        requested_mode=requested_mode,
    )

    assert ctx.mode == expected_mode
    assert ctx.approval_policy == approval_policy
    assert ctx.sandbox_mode == sandbox_mode


def test_teammate_requested_mode_rejects_an_unsupported_token() -> None:
    """An unknown mode is a contract error, not a request for the default.

    Silently downgrading it replaced the caller's explicit permission mode with
    a different one, which is exactly what the clamp exists to prevent.
    """
    with pytest.raises(ValueError, match="Unsupported permission mode"):
        build_subagent_permission_context(
            "implement",
            ToolExecutionContext(permission=PermissionContext(mode="confirm")),
            team_mode=True,
            requested_mode="invalid",
        )


def test_read_only_teammate_cannot_exit_into_write_mode() -> None:
    ctx = build_subagent_permission_context(
        "explore",
        ToolExecutionContext(permission=PermissionContext(mode="bypass")),
        team_mode=True,
        read_only=True,
    )

    assert ctx.mode == "plan"
    assert ctx.source == "teammate:explore"
    assert "exit_plan_mode" in ctx.tool_deny_rules


def test_task_tool_treats_teammate_as_a_child_execution_context() -> None:
    assert TaskTool._is_recursive_subagent_call(
        ToolExecutionContext(
            permission=PermissionContext(source="teammate:implement"),
            task_id="reviewer@release-team",
        )
    )


def test_custom_agent_deny_rules_are_added_to_parent_rules() -> None:
    parent = ToolExecutionContext(
        permission=PermissionContext(
            mode="auto",
            tool_deny_rules=["write_file", "run_command"],
        ),
    )

    ctx = build_subagent_permission_context(
        "implement",
        parent,
        extra_deny_rules=["write_file", "run_command"],
    )

    assert "write_file" in ctx.tool_deny_rules
    assert "run_command" in ctx.tool_deny_rules


def test_foreground_subagent_keeps_coordination_but_denies_delegation_and_plan_tools() -> None:
    parent = ToolExecutionContext(
        permission=PermissionContext(
            mode="bypass",
            tool_deny_rules=["custom_tool"],
        ),
    )

    ctx = build_subagent_permission_context("implement", parent)

    assert "custom_tool" in ctx.tool_deny_rules
    assert SUBAGENT_DENIED_TOOLS <= set(ctx.tool_deny_rules)
    assert AGENT_TASK_COORDINATION_TOOLS.isdisjoint(ctx.tool_deny_rules)
    assert AGENT_MESSAGE_TOOLS.isdisjoint(ctx.tool_deny_rules)
    assert len(ctx.tool_deny_rules) == len(set(ctx.tool_deny_rules))


def test_background_subagent_profile_denies_coordination_and_uses_positive_surface() -> None:
    profile = build_agent_execution_profile(background=True)
    ctx = build_subagent_permission_context(
        "implement",
        ToolExecutionContext(permission=PermissionContext(mode="bypass")),
        background=True,
        execution_profile=profile,
    )
    policy = subagent_toolset_policy(execution_profile=profile)

    assert AGENT_TASK_COORDINATION_TOOLS <= set(ctx.tool_deny_rules)
    assert AGENT_MESSAGE_TOOLS <= set(ctx.tool_deny_rules)
    assert "task" in ctx.tool_deny_rules
    assert policy.availability_filters
    assert policy.is_available(
        ToolSpec(
            name="read_file",
            capability="workspace.read",
            toolset="core",
            exposure="core",
        )
    )
    assert not policy.is_available(
        ToolSpec(
            name="browser_control",
            capability="browser.control",
            toolset="browser",
            exposure="deferred",
        )
    )


def test_filesystem_constraints_parse_strings_and_reject_invalid_scalars() -> None:
    parent = ToolExecutionContext(
        permission=PermissionContext(
            filesystem_constraints={"denylist": "secrets/**"},
        )
    )
    child = build_subagent_permission_context("implement", parent)
    assert child.filesystem_constraints == {"denylist": ["secrets/**"]}

    invalid_parent = ToolExecutionContext(
        permission=PermissionContext(
            filesystem_constraints={"denylist": 42},  # type: ignore[arg-type]
        )
    )
    with pytest.raises(ValueError, match="must be a string or list"):
        build_subagent_permission_context("implement", invalid_parent)


def test_tool_search_cannot_bridge_denied_tools_for_subagent() -> None:
    class _DeferredTool(BaseTool):
        permission = PermissionLevel.AUTO
        description = "deferred test tool"

        def __init__(self, name: str) -> None:
            self.name = name

        def get_spec(self) -> ToolSpec:
            return ToolSpec(
                name=self.name,
                capability="test",
                toolset="agent",
                exposure="core" if self.name == "send_message" else "deferred",
            )

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description=self.description,
                parameters={"type": "object", "properties": {}},
            )

        async def execute(self, args, context=None) -> ToolResult:
            return ToolResult(content="ok")

    registry = ToolRegistry()
    for name in [
        "task_stop",
        "send_message",
        "team_create",
        "ask_user",
        "update_plan",
        "safe_deferred_tool",
    ]:
        registry.register(_DeferredTool(name))

    state = AgentState(user_message="subagent discovery")
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass", source="subagent:implement"),
        metadata={
            "_agent_state": state,
            "_agent_execution_profile": build_agent_execution_profile(background=False),
        },
    )
    result = asyncio.run(
        ToolSearchTool(registry).execute(
            {
                "query": "select:task_stop,send_message,team_create,ask_user,update_plan,safe_deferred_tool",
            },
            context=context,
        )
    )

    assert result.is_error is False
    payload = json.loads(result.content)
    match_names = set(payload["matches"])
    # Coordination messaging is intentionally available to ordinary
    # subagents; the parent/tree authorization check remains in SendMessage.
    assert match_names == {"send_message", "safe_deferred_tool"}
    # send_message is directly visible, so selecting it is a no-op; only the
    # safe deferred tool enters the activation set.
    assert payload["total_deferred_tools"] == 1
    assert state.loaded_deferred_tools == {"safe_deferred_tool"}


def test_tool_search_uses_teammate_policy_instead_of_ordinary_subagent_policy() -> None:
    class _DeferredTool(BaseTool):
        permission = PermissionLevel.AUTO
        description = "deferred teammate test tool"

        def __init__(self, name: str) -> None:
            self.name = name

        def get_spec(self) -> ToolSpec:
            return ToolSpec(
                name=self.name,
                capability=(
                    "workspace.read"
                    if self.name == "safe_deferred_tool"
                    else "test"
                ),
                toolset="agent",
                exposure="deferred",
            )

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description=self.description,
                parameters={"type": "object", "properties": {}},
            )

        async def execute(self, args, context=None) -> ToolResult:
            return ToolResult(content="ok")

    registry = ToolRegistry()
    for name in [
        "task",
        "task_create",
        "task_status",
        "exit_plan_mode",
        "safe_deferred_tool",
    ]:
        registry.register(_DeferredTool(name))

    default_state = AgentState(user_message="teammate discovery")
    default_result = asyncio.run(
        ToolSearchTool(registry).execute(
            {
                "query": (
                    "select:task,task_create,task_status,exit_plan_mode,"
                    "safe_deferred_tool"
                )
            },
            context=ToolExecutionContext(
                permission=PermissionContext(
                    mode="confirm",
                    source="teammate:implement",
                ),
                metadata={"_agent_state": default_state},
            ),
        )
    )
    default_payload = json.loads(default_result.content)

    assert set(default_payload["matches"]) == {
        "task",
        "task_create",
        "safe_deferred_tool",
    }
    assert default_state.loaded_deferred_tools == {
        "task",
        "task_create",
        "safe_deferred_tool",
    }

    plan_state = AgentState(user_message="teammate plan discovery")
    plan_result = asyncio.run(
        ToolSearchTool(registry).execute(
            {"query": "select:exit_plan_mode,task_status"},
            context=ToolExecutionContext(
                permission=PermissionContext(
                    mode="plan",
                    source="teammate:implement:required_plan",
                ),
                metadata={"_agent_state": plan_state},
            ),
        )
    )
    plan_payload = json.loads(plan_result.content)

    assert plan_payload["matches"] == ["exit_plan_mode"]
    assert plan_state.loaded_deferred_tools == {"exit_plan_mode"}


def test_explore_subagent_gets_plan_mode_regardless_of_parent() -> None:
    parent = ToolExecutionContext(
        permission=PermissionContext(
            mode="bypass",
            tool_deny_rules=["write_file", "task"],
        ),
    )

    ctx = build_subagent_permission_context("explore", parent)

    assert ctx.mode == "plan"
    assert "write_file" in ctx.tool_deny_rules
    assert "task" in ctx.tool_deny_rules


def test_subagent_keeps_all_parent_deny_rules() -> None:
    parent = ToolExecutionContext(
        permission=PermissionContext(
            mode="auto",
            tool_deny_rules=["read_file", "write_file", "run_command", "task"],
        ),
        metadata={"agent_mode": "implement"},
    )

    ctx = build_subagent_permission_context("implement", parent)

    assert "read_file" in ctx.tool_deny_rules
    assert "write_file" in ctx.tool_deny_rules
    assert "run_command" in ctx.tool_deny_rules
    assert "task" in ctx.tool_deny_rules


def test_builtin_subagent_prompt_keeps_role_and_assigned_task() -> None:
    prompt = build_subagent_prompt("explore", "Inspect the API routes.")

    assert "Inspect the API routes." in prompt
    # cc exploreAgent.ts role wording (ported verbatim).
    assert "file search specialist" in prompt
    assert "READ-ONLY MODE" in prompt
    assert "Final response contract" not in prompt


def test_subagent_prompt_does_not_inject_language_or_report_contracts() -> None:
    prompt = build_subagent_prompt("explore", "调研成都天气并给出来源。")

    assert "调研成都天气并给出来源。" in prompt
    assert "所有可见进展" not in prompt
    assert "## Result" not in prompt


def test_custom_subagent_prompt_keeps_custom_role_and_task() -> None:
    class _CustomAgent:
        prompt = "You are a security specialist."

    prompt = build_subagent_prompt(
        "security-review",
        "Review auth.",
        get_custom_agent=lambda _name: _CustomAgent(),
    )

    assert "You are a security specialist." in prompt
    assert "Task:\nReview auth." in prompt
    assert "Final response contract" not in prompt


def test_subagent_metadata_sanitizer_drops_internal_runtime_keys() -> None:
    mcp_manager = object()
    cleaned = sanitize_subagent_runtime_metadata(
        {
            "agent_mode": "explore",
            "cwd": "C:/repo",
            "_mcp_manager": mcp_manager,
            "_mcp_owner_session_id": "session-a",
            "_agent_state": object(),
            "_current_tool_call_id": "call-1",
            "_unknown_private_state": object(),
            "normal": "kept",
        }
    )

    assert cleaned["cwd"] == "C:/repo"
    assert cleaned["normal"] == "kept"
    assert cleaned["agent_mode"] == "explore"
    assert cleaned["_mcp_manager"] is mcp_manager
    assert cleaned["_mcp_owner_session_id"] == "session-a"
    assert "_agent_state" not in cleaned
    assert "_current_tool_call_id" not in cleaned
    assert "_unknown_private_state" not in cleaned


def test_subagent_metadata_sanitizer_withholds_parent_turn_input_queue() -> None:
    """A child must never inherit the parent's turn-local input owner.

    The key is public-looking, so neither the explicit-key nor the
    leading-underscore rule caught it: the child's ``begin_turn`` then rewrote
    the parent's mailbox phase and could consume user steer input meant for the
    main agent.
    """
    from backend.agent.turn_input import TurnInputQueue

    parent_queue = TurnInputQueue()
    cleaned = sanitize_subagent_runtime_metadata({"turn_input_queue": parent_queue})

    assert "turn_input_queue" not in cleaned


def test_subagent_metadata_sanitizer_treats_parent_callbacks_as_capabilities() -> None:
    """Generic parent metadata must not become an ambient child capability API."""

    parent_permission_provider = lambda: PermissionContext(mode="bypass")
    parent_permission_setter = lambda _mode: None
    parent_rule_setter = lambda _rules: None

    cleaned = sanitize_subagent_runtime_metadata(
        {
            "permission_context_provider": parent_permission_provider,
            "permission_mode_setter": parent_permission_setter,
            "command_prompt_allow_rules_setter": parent_rule_setter,
            "unknown_host_callback": lambda: "host capability",
            "normal": "kept",
        }
    )

    assert cleaned == {"normal": "kept"}


def test_subagent_metadata_sanitizer_keeps_data_but_drops_runtime_owners() -> None:
    """Child metadata is a data projection, not a shallow runtime-object clone."""

    prompt_cache_data = {"stable_system_hash": "abc", "parts": ["system"]}
    cleaned = sanitize_subagent_runtime_metadata(
        {
            "cwd": "C:/repo",
            "query_source": "user",
            "prompt_cache_safe_params": prompt_cache_data,
            "conversation_repository": object(),
            "agent_runtime": object(),
            "unknown_runtime_owner": object(),
        }
    )

    assert cleaned == {
        "cwd": "C:/repo",
        "query_source": "user",
        "prompt_cache_safe_params": {
            "stable_system_hash": "abc",
            "parts": ["system"],
        },
    }
    assert cleaned["prompt_cache_safe_params"] is not prompt_cache_data
    assert cleaned["prompt_cache_safe_params"]["parts"] is not prompt_cache_data["parts"]


def test_session_toolset_policy_round_trips_as_a_restriction() -> None:
    policy = ToolsetPolicy.from_iterables(
        enabled_toolsets=(),
        enabled_tools=["read_file", "tool_search"],
        disabled_tools=["run_command"],
    )
    restored = ToolsetPolicy.from_mapping(policy.to_mapping())

    assert restored == policy
    projected = sanitize_subagent_runtime_metadata(
        {SESSION_TOOLSET_POLICY_METADATA_KEY: policy}
    )
    assert projected[SESSION_TOOLSET_POLICY_METADATA_KEY] == policy


def test_toolset_policy_rejects_malformed_durable_shapes() -> None:
    with pytest.raises(ValueError, match="enabled_tools"):
        ToolsetPolicy.from_mapping({"enabled_tools": {"read_file": True}})

    with pytest.raises(ValueError, match="unknown field"):
        ToolsetPolicy.from_mapping({"unexpected": []})

    with pytest.raises(ValueError, match="availability filter"):
        ToolsetPolicy.from_mapping(
            {
                "availability_filters": [
                    {"tools": ["read_file"], "unexpected": ["write_file"]}
                ]
            }
        )


def test_large_subagent_result_is_externalized_to_artifact(tmp_path) -> None:
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    raw = "delegated evidence\n" + ("x" * (MAX_TOOL_RESULT_BYTES + 1_000))

    compact, artifact_id = _externalize_large_subagent_result(
        store,
        subagent_id="researcher",
        content=raw,
    )

    assert artifact_id
    assert len(compact) < len(raw)
    assert f"artifact_id: {artifact_id}" in compact
    assert store.get(artifact_id) == raw
