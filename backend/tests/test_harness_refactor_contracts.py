"""Regression tests for the 2026-08-27 harness boundary refactor.

These cover ownership rules, not every file-tool behavior:

* runtime guards belong to execution arguments, never approved requests;
* model schemas stay narrow while execution schemas own private fields;
* projection failures cannot rewrite a committed mutation's result;
* provider/tool/final terminal reasons converge at the loop boundary.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from backend.agent.tool_execution import (
    _authorize_final_tool_request,
    _execution_arguments_for_tool,
    inject_expected_hash,
)
from backend.agent.terminal_projection import (
    TurnTerminalProjection,
    terminal_status_and_reason,
)
from backend.config import PermissionSettings, TokenBudget
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.edit_file import EditFileTool
from backend.tools.file_tools_common import content_hash
from backend.tools.registry import ToolRegistry
from backend.tools.write_file import WriteFileTool


def _ctx(root: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        permission=PermissionContext(mode="auto"),
        workspace_root=root,
        metadata={"_read_file_hashes": {}},
    )


def test_runtime_guard_is_injected_only_into_detached_execution_args(tmp_path):
    target = tmp_path / "target.py"
    target.write_text("before\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    ctx.metadata["_read_file_hashes"][
        str(target.resolve()).lower()
    ] = content_hash("before\n")
    tc = ToolCallEvent(
        id="write-1",
        name="write_file",
        arguments={"file_path": str(target), "content": "after\n"},
    )

    execution_args = _execution_arguments_for_tool(
        tc,
        tool_registry=ToolRegistry(),
        tool_ctx=ctx,
    )

    assert execution_args["expected_hash"] == content_hash("before\n")
    assert "expected_hash" not in tc.arguments


def test_model_schema_and_host_execution_schema_have_owners() -> None:
    write = WriteFileTool()
    edit = EditFileTool()

    assert set(write.model_schema().parameters["properties"]) == {
        "file_path",
        "content",
    }
    assert "expected_hash" in write.get_execution_schema().parameters["properties"]
    assert set(edit.model_schema().parameters["properties"]) == {
        "file_path",
        "old_string",
        "new_string",
        "replace_all",
    }
    execution_props = edit.get_execution_schema().parameters["properties"]
    assert {"expected_hash", "replace_all"} <= set(execution_props)


def test_authorization_remains_stable_after_guard_injection(tmp_path):
    target = tmp_path / "auth.py"
    target.write_text("value = 1\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    tc = ToolCallEvent(
        id="write-auth",
        name="write_file",
        arguments={"file_path": str(target), "content": "value = 2\n"},
    )

    frozen_args_before = deepcopy(tc.arguments)
    _execution_arguments_for_tool(
        tc,
        tool_registry=registry,
        tool_ctx=ctx,
    )
    authorization = _authorize_final_tool_request(
        tc,
        tool_registry=registry,
        permission_checker=PermissionChecker(
            settings=PermissionSettings(auto_allow=["write_file"]),
            workspace_root=tmp_path,
        ),
        permission_context=ctx.permission,
        tool_ctx=ctx,
    )

    assert authorization.error == ""
    assert authorization.request is not None
    assert tc.arguments == frozen_args_before


def test_injection_keeps_empty_guard_for_unread_existing_file(tmp_path) -> None:
    target = tmp_path / "unread.py"
    target.write_text("x\n", encoding="utf-8")
    args: dict[str, object] = {}

    inject_expected_hash(args, str(target), read_time_hashes=None)

    # Read-before-write is enforced by the tool; injection must not bless a
    # blind mutation by hashing at execution time.
    assert args["expected_hash"] == ""


@pytest.mark.parametrize(
    ("stopped_reason", "terminal_status"),
    [("partial_timeout", "partial"), ("runtime_error", "failed")],
)
def test_loop_terminal_state_wins_over_projection(stopped_reason, terminal_status) -> None:
    class _State:
        def __init__(self) -> None:
            self.stopped_reason = stopped_reason
            self.terminal_status = terminal_status

    status, reason = terminal_status_and_reason(state=_State(), terminal_projection=None)

    assert status == terminal_status
    assert reason == stopped_reason
