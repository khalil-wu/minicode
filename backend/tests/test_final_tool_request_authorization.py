from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.final_tool_request import canonical_tool_request_digest
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import execute_serial, execute_tool_batch
from backend.agent.tool_execution import (
    _authorize_final_tool_request,
    _bind_final_tool_request,
)
from backend.config import PermissionSettings, TokenBudget
from backend.hooks.manager import HookResult
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import (
    PermissionContext,
    PermissionDecision,
    ToolExecutionContext,
)
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.plan_tool import ExitPlanModeTool
from backend.tools.registry import ToolRegistry
from backend.tools.write_file import WriteFileTool
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.turn_wait_state import TurnWaitState


class _ModeTool(BaseTool):
    name = "mode_tool"
    permission = PermissionLevel.CONFIRM

    def __init__(self, *, mutate_execution_args: bool = False) -> None:
        self.executed: list[dict[str, Any]] = []
        self.mutate_execution_args = mutate_execution_args

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description="Test final-request authorization.",
            parameters={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["read", "write", "deny"]},
                    "payload": {"type": "object"},
                },
                "required": ["mode"],
                "additionalProperties": False,
            },
        )

    def check_permission(self, args=None, context=None):
        mode = str((args or {}).get("mode") or "")
        if mode == "read":
            return PermissionLevel.AUTO
        if mode == "deny":
            return PermissionLevel.ALWAYS_DENY
        return PermissionLevel.CONFIRM

    async def execute(self, args: dict[str, Any], context=None) -> ToolResult:
        self.executed.append(deepcopy(args))
        if self.mutate_execution_args:
            args.setdefault("payload", {})["value"] = "mutated-inside-tool"
        return ToolResult(content="ok")


class _NetworkTool(_ModeTool):
    name = "network_tool"
    open_world = True

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description="Network authorization test.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
                "additionalProperties": False,
            },
        )

    def check_permission(self, args=None, context=None):
        url = str((args or {}).get("url") or "")
        return (
            PermissionLevel.ALWAYS_DENY
            if "127.0.0.1" in url or "localhost" in url
            else PermissionLevel.CONFIRM
        )


class _Hooks:
    def __init__(
        self,
        *,
        pre: HookResult | None = None,
        permission: HookResult | None = None,
    ) -> None:
        self.pre = pre or HookResult()
        self.permission = permission or HookResult()

    async def run_pre_tool(self, *args, **kwargs):
        return self.pre

    async def run_permission_request(self, *args, **kwargs):
        return self.permission

    async def run_permission_denied(self, *args, **kwargs):
        return HookResult()

    async def run_post_tool(self, *args, **kwargs):
        return HookResult()

    async def run_post_tool_failure(self, *args, **kwargs):
        return HookResult()

    async def run_file_changed(self, *args, **kwargs):
        return HookResult()


async def _collect_batch(
    tmp_path: Path,
    tool: BaseTool,
    call: ToolCallEvent,
    *,
    permission: PermissionContext | None = None,
    approval_handler=None,
    metadata: dict[str, Any] | None = None,
    checker: Any | None = None,
    hook_manager: Any | None = None,
    run_context: RunContext | None = None,
):
    registry = ToolRegistry()
    registry.register(tool)
    permission = permission or PermissionContext(mode="confirm", source="test")
    tool_ctx = ToolExecutionContext(
        permission=permission,
        workspace_root=tmp_path,
        conversation_id="conversation-1",
        session_id="session-1",
        metadata=dict(metadata or {}),
        run_context=run_context or RunContext(hook_manager=hook_manager),
    )
    state = AgentState(user_message="test", iterations=1)
    context = ContextBuilder(TokenBudget())
    context.append_assistant_tool_calls([call])
    events = [
        event
        async for event in execute_tool_batch(
            [call],
            ctx=context,
            state=state,
            tool_registry=registry,
            permission_checker=checker
            or PermissionChecker(PermissionSettings(), tmp_path),
            approval_handler=approval_handler,
            skill_manager=None,
            permission_context=permission,
            tool_ctx=tool_ctx,
        )
    ]
    return events, state, tool_ctx


def _event(events, event_type: str):
    return next(event for event in events if event.type == event_type)


def test_pre_tool_updated_input_is_fully_reauthorized_and_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _ModeTool()
    hooks = _Hooks(
        pre=HookResult(updated_input={"mode": "write", "payload": {"value": "final"}})
    )

    async def approve(_call_id: str):
        return {"action": "approve"}

    events, state, _ = asyncio.run(
        _collect_batch(
            tmp_path,
            tool,
            ToolCallEvent(id="pre-update", name=tool.name, arguments={"mode": "read"}),
            approval_handler=approve,
            hook_manager=hooks,
        )
    )

    final_args = {"mode": "write", "payload": {"value": "final"}}
    digest = canonical_tool_request_digest(tool.name, final_args)
    assert tool.executed == [final_args]
    assert _event(events, "approval_request").data["request_digest"] == digest
    assert _event(events, "tool_call").data["request_digest"] == digest
    assert _event(events, "tool_result").data["request_digest"] == digest
    assert state.tool_calls[0].request_digest == digest


def test_permission_request_update_cannot_turn_allowed_request_into_denied_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _ModeTool()
    hooks = _Hooks(
        permission=HookResult(
            updated_input={"mode": "deny"},
            permission_decision="allow",
        )
    )
    approval_calls: list[str] = []

    async def approve(call_id: str):
        approval_calls.append(call_id)
        return {"action": "approve"}

    events, _, _ = asyncio.run(
        _collect_batch(
            tmp_path,
            tool,
            ToolCallEvent(
                id="permission-update", name=tool.name, arguments={"mode": "write"}
            ),
            approval_handler=approve,
            hook_manager=hooks,
        )
    )

    assert tool.executed == []
    assert approval_calls == []
    assert _event(events, "tool_result").data["status"] == "blocked"
    assert _event(events, "tool_result").data["is_error"] is True


def test_permission_request_update_to_auto_does_not_prompt_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _ModeTool()
    hooks = _Hooks(permission=HookResult(updated_input={"mode": "read"}))
    approval_calls: list[str] = []

    async def approve(call_id: str):
        approval_calls.append(call_id)
        return {"action": "approve"}

    events, _, _ = asyncio.run(
        _collect_batch(
            tmp_path,
            tool,
            ToolCallEvent(
                id="auto-update", name=tool.name, arguments={"mode": "write"}
            ),
            approval_handler=approve,
            hook_manager=hooks,
        )
    )

    assert approval_calls == []
    assert not any(event.type == "approval_request" for event in events)
    assert tool.executed == [{"mode": "read"}]


def test_permission_request_update_is_rechecked_against_subagent_write_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    hooks = _Hooks(
        permission=HookResult(
            updated_input={"file_path": "outside.txt", "content": "blocked"},
            permission_decision="allow",
        )
    )
    events, _, _ = asyncio.run(
        _collect_batch(
            tmp_path,
            WriteFileTool(),
            ToolCallEvent(
                id="scope-update",
                name="write_file",
                arguments={"file_path": "allowed/inside.txt", "content": "safe"},
            ),
            approval_handler=lambda _id: None,
            metadata={"write_scope": ["allowed"]},
            hook_manager=hooks,
        )
    )

    result = _event(events, "tool_result")
    assert result.data["is_error"] is True
    assert "write_scope" in result.data["summary"]
    assert not (tmp_path / "outside.txt").exists()


def test_hook_allow_cannot_override_sensitive_path_capability_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = _Hooks(
        permission=HookResult(
            updated_input={"file_path": ".git/HEAD", "content": "blocked"},
            permission_decision="allow",
        )
    )
    events, _, _ = asyncio.run(
        _collect_batch(
            tmp_path,
            WriteFileTool(),
            ToolCallEvent(
                id="capability-update",
                name="write_file",
                arguments={"file_path": "safe.txt", "content": "safe"},
            ),
            approval_handler=lambda _id: None,
            hook_manager=hooks,
        )
    )

    assert _event(events, "tool_result").data["is_error"] is True
    assert not (tmp_path / ".git" / "HEAD").exists()


def test_pre_tool_hook_allow_skips_tool_owned_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: hook allow skips the tool's own interactive floor."""
    tool = _ModeTool()
    hooks = _Hooks(pre=HookResult(permission_decision="allow"))
    approval_calls: list[str] = []

    async def approve(call_id: str):
        approval_calls.append(call_id)
        return {"action": "approve"}

    asyncio.run(
        _collect_batch(
            tmp_path,
            tool,
            ToolCallEvent(
                id="hook-allow-tool-floor",
                name="mode_tool",
                arguments={"mode": "write"},
            ),
            approval_handler=approve,
            hook_manager=hooks,
        )
    )

    assert tool.executed
    assert not approval_calls


def test_pre_tool_hook_allow_cannot_skip_settings_ask_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cc invariant: settings ask rules outrank a pre-tool hook allow.

    ``require_confirm`` produces a CONFIRM decision whose matched_rule_source
    is ``static_policy``, which is deliberately outside the hook-overridable
    sources set, so the user approval must still be surfaced. The tool defers
    (no explicit check_permission decision) so the settings rule is what
    produces the confirm floor.
    """

    class _DeferringTool(_ModeTool):
        def check_permission(self, args=None, context=None):
            return None

    tool = _DeferringTool()
    hooks = _Hooks(pre=HookResult(permission_decision="allow"))
    checker = PermissionChecker(
        PermissionSettings(require_confirm=["mode_tool"]), tmp_path
    )
    approval_calls: list[str] = []

    async def approve(call_id: str):
        approval_calls.append(call_id)
        return {"action": "approve"}

    asyncio.run(
        _collect_batch(
            tmp_path,
            tool,
            ToolCallEvent(
                id="hook-allow-static-ask", name="mode_tool", arguments={"mode": "read"}
            ),
            approval_handler=approve,
            checker=checker,
            hook_manager=hooks,
        )
    )

    assert approval_calls


def test_network_target_changed_by_permission_hook_is_reauthorized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _NetworkTool()
    hooks = _Hooks(
        permission=HookResult(
            updated_input={"url": "http://127.0.0.1/private"},
            permission_decision="allow",
        )
    )
    events, _, _ = asyncio.run(
        _collect_batch(
            tmp_path,
            tool,
            ToolCallEvent(
                id="network-update",
                name=tool.name,
                arguments={"url": "https://example.com/public"},
            ),
            approval_handler=lambda _id: None,
            hook_manager=hooks,
        )
    )
    assert tool.executed == []
    assert _event(events, "tool_result").data["is_error"] is True


def test_approval_response_digest_mismatch_rejects_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _ModeTool()

    async def wrong_digest(_call_id: str):
        return {"action": "approve", "request_digest": "0" * 64}

    events, _, _ = asyncio.run(
        _collect_batch(
            tmp_path,
            tool,
            ToolCallEvent(
                id="wrong-digest", name=tool.name, arguments={"mode": "write"}
            ),
            approval_handler=wrong_digest,
        )
    )
    assert tool.executed == []
    assert "did not match" in _event(events, "tool_result").data["summary"]


def test_arguments_changed_while_waiting_for_approval_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _ModeTool()
    registry = ToolRegistry()
    registry.register(tool)
    permission = PermissionContext(mode="confirm", source="test")
    tool_ctx = ToolExecutionContext(permission=permission, workspace_root=tmp_path)
    checker = PermissionChecker(PermissionSettings(), tmp_path)
    tc = ToolCallEvent(id="drift", name=tool.name, arguments={"mode": "write"})
    authorization = _authorize_final_tool_request(
        tc,
        tool_registry=registry,
        permission_checker=checker,
        permission_context=permission,
        tool_ctx=tool_ctx,
    )
    assert authorization.request is not None
    assert authorization.permission_decision is not None
    _bind_final_tool_request(tc, authorization.request)

    async def approve(_call_id: str):
        tc.arguments["mode"] = "read"
        return {"action": "approve"}

    async def collect():
        state = AgentState(user_message="test", iterations=1)
        context = ContextBuilder(TokenBudget())
        context.append_assistant_tool_calls([tc])
        return [
            event
            async for event in execute_serial(
                tc,
                perm=PermissionLevel.CONFIRM,
                permission_decision=authorization.permission_decision,
                permission_checker=checker,
                permission_context=permission,
                ctx=context,
                state=state,
                tool_registry=registry,
                tool_ctx=tool_ctx,
                approval_handler=approve,
                skill_manager=None,
                iteration_id="iter:1",
            )
        ]

    events = asyncio.run(collect())
    assert tool.executed == []
    assert (
        "changed while approval was pending"
        in _event(events, "tool_result").data["summary"]
    )


def test_tool_mutation_of_execution_copy_does_not_change_canonical_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _ModeTool(mutate_execution_args=True)
    args = {"mode": "read", "payload": {"value": "canonical"}}
    events, state, _ = asyncio.run(
        _collect_batch(
            tmp_path,
            tool,
            ToolCallEvent(
                id="execution-copy", name=tool.name, arguments=deepcopy(args)
            ),
        )
    )
    assert tool.executed == [args]
    assert _event(events, "tool_call").data["args"] == args
    assert state.tool_calls[0].tool_input == args


class _MutatingPermissionChecker:
    def evaluate(self, tool_name, args, *, context=None, tool=None):
        args["mode"] = "read"
        return PermissionDecision(
            permission_level=PermissionLevel.AUTO,
            decision="allow",
            capability_allowed=True,
            capability_reason="allowed",
            approval_policy="auto",
            matched_rule_source="external_checker",
            matched_rule="test",
            risk="low",
            scope={"workspace_scope": "project"},
            expiry="call",
        )


def test_permission_evaluator_cannot_mutate_final_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _ModeTool()
    events, _, _ = asyncio.run(
        _collect_batch(
            tmp_path,
            tool,
            ToolCallEvent(
                id="checker-mutation", name=tool.name, arguments={"mode": "write"}
            ),
            checker=_MutatingPermissionChecker(),
        )
    )
    assert tool.executed == []
    assert "mutated the request" in _event(events, "tool_result").data["summary"]


def test_exit_plan_mode_user_edit_is_reauthorized_and_becomes_final_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Original plan\n", encoding="utf-8")
    permission = PermissionContext(
        mode="plan",
        source="runtime",
        pre_plan_mode="confirm",
        filesystem_constraints={"plan_files": [str(plan_path)]},
    )
    mode_updates: list[tuple[str, str]] = []
    prompt_updates: list[tuple[list[str], str]] = []

    async def set_mode(mode: str, *, source: str):
        mode_updates.append((mode, source))

    async def set_prompts(prompts: list[str], *, source: str):
        prompt_updates.append((list(prompts), source))

    async def approve(_call_id: str):
        return {
            "action": "approve",
            "updated_plan": "# Edited plan\n\n1. Implement safely.\n",
            "command_prompts": [{"tool": "run_command", "prompt": "run focused tests"}],
        }

    events, _, _ = asyncio.run(
        _collect_batch(
            tmp_path,
            ExitPlanModeTool(),
            ToolCallEvent(id="exit-plan", name="exit_plan_mode", arguments={}),
            permission=permission,
            approval_handler=approve,
            run_context=RunContext(
                permission_mode_setter=set_mode,
                command_prompt_allow_rules_setter=set_prompts,
            ),
        )
    )

    approval = _event(events, "approval_request")
    tool_call = _event(events, "tool_call")
    result = _event(events, "tool_result")
    assert approval.data["args"]["plan"] == "# Original plan\n"
    assert tool_call.data["args"]["plan"].startswith("# Edited plan")
    assert tool_call.data["request_digest"] != approval.data["request_digest"]
    assert result.data["request_digest"] == tool_call.data["request_digest"]
    assert plan_path.read_text(encoding="utf-8").startswith("# Edited plan")
    assert mode_updates == [("confirm", "exit_plan_mode")]
    assert prompt_updates == [(["run focused tests"], "exit_plan_mode.command_prompts")]


class _ApprovalRuntime(SessionApprovalRuntimeMixin):
    def __init__(self) -> None:
        self.session_id = "session-1"
        self.active_conversation_id = "conversation-1"
        self.turn_wait_state = TurnWaitState()
        self.approval_diff_cache: dict[str, Any] = {}


def test_session_approval_cache_is_bound_to_final_digest() -> None:
    runtime = _ApprovalRuntime()
    args_a = {"mode": "write", "payload": {"value": "a"}}
    args_b = {"mode": "write", "payload": {"value": "b"}}
    payload_a = {
        "conversation_id": "conversation-1",
        "request_digest": canonical_tool_request_digest("mode_tool", args_a),
    }
    payload_b = {
        "conversation_id": "conversation-1",
        "request_digest": canonical_tool_request_digest("mode_tool", args_b),
    }
    runtime._mark_session_approved("mode_tool", args_a, payload=payload_a)
    assert runtime._is_session_approved("mode_tool", args_a, payload=payload_a)
    assert not runtime._is_session_approved("mode_tool", args_b, payload=payload_b)


def test_ws_pending_approval_rejects_explicit_wrong_digest() -> None:
    async def run() -> None:
        runtime = _ApprovalRuntime()
        args = {"mode": "write"}
        digest = canonical_tool_request_digest("mode_tool", args)
        runtime.turn_wait_state.pending_approval_payloads["call-1"] = {
            "tool_name": "mode_tool",
            "args": args,
            "request_digest": digest,
        }
        future = asyncio.get_running_loop().create_future()
        runtime.turn_wait_state.pending_approvals["call-1"] = future
        assert not runtime._resolve_pending_approval(
            "call-1",
            {"action": "approve", "request_digest": "f" * 64},
        )
        assert not future.done()
        assert runtime._resolve_pending_approval("call-1", {"action": "approve"})
        assert (await future)["request_digest"] == digest

    asyncio.run(run())
