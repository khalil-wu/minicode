from dataclasses import replace

import pytest

from backend.artifact.store import ArtifactStore
from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.base import PermissionLevel
from backend.tools.plan_tool import ExitPlanModeTool
from backend.tools.read_file import ReadFileTool
from backend.tools.write_file import WriteFileTool


@pytest.mark.parametrize("source", ["content_rule", "session_override", "static_policy"])
@pytest.mark.parametrize("approval_policy,expected", [("on-request", "ask"), ("never", "deny")])
def test_plan_read_preserves_explicit_approval_rules(tmp_path, source, approval_policy, expected):
    settings = PermissionSettings()
    overrides = {}
    if source == "content_rule":
        settings = replace(settings, content_ask_rules=["read_file(note.txt)"])
    elif source == "session_override":
        overrides["read_file"] = PermissionLevel.CONFIRM
    else:
        settings = replace(settings, require_confirm=["read_file"])
    checker = PermissionChecker(settings, tmp_path)
    tool = ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    decision = checker.evaluate(tool.name, {"file_path": "note.txt"}, tool=tool,
        context=PermissionContext(mode="plan", workspace_root=tmp_path,
            session_overrides=overrides, approval_policy=approval_policy))
    assert decision.decision == expected
    assert decision.matched_rule_source == (source if expected == "ask" else "managed_requirements")


def test_plan_keeps_read_defaults_and_exit_confirmation(tmp_path):
    checker = PermissionChecker(PermissionSettings(), tmp_path)
    context = PermissionContext(mode="plan", workspace_root=tmp_path)
    reader = ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    assert checker.evaluate(reader.name, {"file_path": "note.txt"}, tool=reader, context=context).decision == "allow"
    exit_tool = ExitPlanModeTool(tmp_path)
    assert checker.evaluate(exit_tool.name, {}, tool=exit_tool, context=context).decision == "ask"


def test_plan_file_exception_keeps_content_ask_without_enabling_other_writes(tmp_path):
    plan_path = tmp_path / "plan.md"
    context = PermissionContext(mode="plan", workspace_root=tmp_path,
        filesystem_constraints={"plan_files": [str(plan_path)]})
    tool = WriteFileTool()
    args = {"file_path": str(plan_path), "content": "# Plan"}
    default = PermissionChecker(PermissionSettings(), tmp_path)
    assert default.evaluate(tool.name, args, tool=tool, context=context).decision == "allow"
    restricted = PermissionChecker(PermissionSettings(content_ask_rules=["write_file"]), tmp_path)
    assert restricted.evaluate(tool.name, args, tool=tool, context=context).decision == "ask"
    assert restricted.evaluate(tool.name, {**args, "file_path": str(tmp_path / "other.md")},
        tool=tool, context=context).decision == "deny"
