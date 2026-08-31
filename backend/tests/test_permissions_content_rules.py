"""Tool(content) permission rules: parser, matcher, and checker integration.

These rules let a user allow/deny based on a tool's content (command text or
file path), e.g. ``run_command(npm run:*)`` or ``edit_file(src/**)``, instead of
only the tool name.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from backend.config import PermissionSettings
from backend.artifact.store import ArtifactStore
from backend.mcp.client import MCPToolDef
from backend.mcp.registry import MCPToolProxy
from backend.permissions.checker import PermissionChecker
from backend.permissions.rules import PermissionRuleMatcher
from backend.permissions.content_rules import (
    parse_content_rule,
    rule_matches_call,
)
from backend.permissions.context import PermissionContext
from backend.tools.base import PermissionLevel
from backend.tools.apply_patch import ApplyPatchTool
from backend.tools.command_tool import RunCommandTool
from backend.tools.edit_file import EditFileTool
from backend.tools.plan_tool import ExitPlanModeTool
from backend.tools.write_file import WriteFileTool


# ── parser ──────────────────────────────────────────────────────────────────

def test_parser_command_prefix():
    rule = parse_content_rule("run_command(npm run:*)")
    assert rule is not None
    assert rule.tool_glob == "run_command"
    assert rule.content == "npm run:*"


def test_parser_file_glob():
    rule = parse_content_rule("edit_file(src/**)")
    assert rule is not None
    assert rule.tool_glob == "edit_file"
    assert rule.content == "src/**"


def test_parser_whole_tool_rule():
    rule = parse_content_rule("run_command")
    assert rule is not None
    assert rule.tool_glob == "run_command"
    assert rule.content is None


def test_parser_rejects_blank_and_invalid():
    assert parse_content_rule("") is None
    assert parse_content_rule("# comment") is None
    assert parse_content_rule("   ") is None


def test_parser_rejects_upstream_tool_aliases():
    for legacy in (
        "Bash(npm run:*)",
        "Edit(src/**)",
        "Read(config.toml)",
        "Write(src/app.py)",
    ):
        with pytest.raises(ValueError, match="canonical MiniCode tool name"):
            parse_content_rule(legacy)


# ── matcher ─────────────────────────────────────────────────────────────────

def test_match_command_prefix():
    rule = parse_content_rule("run_command(npm run:*)")
    assert rule_matches_call(rule, "run_command", {"command": "npm run build"})
    assert rule_matches_call(rule, "run_command", {"command": "npm run test"})
    assert not rule_matches_call(rule, "run_command", {"command": "git status"})


def test_command_prefix_requires_word_boundary_and_rejects_compounds():
    rule = parse_content_rule("run_command(ls:*)")
    assert rule_matches_call(rule, "run_command", {"command": "ls -la"})
    assert not rule_matches_call(rule, "run_command", {"command": "lsof -i"})

    git_rule = parse_content_rule("run_command(git status:*)")
    assert not rule_matches_call(
        git_rule,
        "run_command",
        {"command": "git status && rm -rf build"},
    )
    assert rule_matches_call(
        git_rule,
        "run_command",
        {"command": 'git status --pathspec-from-file="a&&b"'},
    )


def test_match_file_glob():
    rule = parse_content_rule("edit_file(src/**)")
    assert rule_matches_call(rule, "edit_file", {"file_path": "src/app.py"})
    assert rule_matches_call(rule, "edit_file", {"file_path": "src/sub/x.py"})
    assert not rule_matches_call(rule, "edit_file", {"file_path": "tests/x.py"})


def test_match_whole_tool():
    rule = parse_content_rule("run_command")
    assert rule_matches_call(rule, "run_command", {"command": "anything"})
    assert not rule_matches_call(rule, "edit_file", {"file_path": "a.py"})


def test_outside_workspace_error_identifies_target_and_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt"

    allowed, reason = PermissionRuleMatcher(workspace).check_file_access(target, "write")

    assert not allowed
    assert str(target.resolve()) in reason
    assert str(workspace.resolve()) in reason


# ── checker integration ─────────────────────────────────────────────────────

def _checker(td: str, **settings_kwargs) -> PermissionChecker:
    return PermissionChecker(
        settings=PermissionSettings(**settings_kwargs),
        workspace_root=Path(td),
    )


def test_content_allow_rule_forces_auto_in_confirm_mode():
    td = tempfile.mkdtemp()
    checker = _checker(td, content_allow_rules=["run_command(git status)"])
    ctx = PermissionContext(mode="confirm")
    # run_command normally CONFIRM in confirm mode, but the content rule allows
    # exactly "git status" → AUTO.
    level = checker.check("run_command", {"command": "git status"}, context=ctx)
    assert level == PermissionLevel.AUTO
    # A different command is unaffected.
    other = checker.check("run_command", {"command": "rm -rf /tmp"}, context=ctx)
    assert other != PermissionLevel.AUTO


def test_content_deny_rule_overrides_bypass():
    td = tempfile.mkdtemp()
    checker = _checker(td, content_deny_rules=["run_command(rm -rf:*)"])
    ctx = PermissionContext(mode="bypass")
    level = checker.check("run_command", {"command": "rm -rf /"}, context=ctx)
    assert level == PermissionLevel.ALWAYS_DENY


def test_content_allow_does_not_force_auto_in_plan_mode():
    td = tempfile.mkdtemp()
    checker = _checker(td, content_allow_rules=["edit_file(src/**)"])
    ctx = PermissionContext(mode="plan")
    # An allow rule must not force AUTO in plan mode (plan stays restrictive);
    # whatever plan normally decides, it must not be auto-approved.
    level = checker.check("edit_file", {"file_path": "src/app.py"}, context=ctx)
    assert level != PermissionLevel.AUTO


def test_content_allow_cannot_bypass_command_escalation_gate():
    td = tempfile.mkdtemp()
    checker = _checker(td, content_allow_rules=["run_command(pip install x)"])
    tool = RunCommandTool(ArtifactStore(storage_dir=Path(td) / "artifacts"))
    ctx = PermissionContext(mode="confirm")

    level = checker.check(
        "run_command",
        {"command": "pip install x", "with_escalated_permissions": True},
        context=ctx,
        tool=tool,
    )

    assert level == PermissionLevel.CONFIRM


def test_session_auto_override_cannot_bypass_command_escalation_gate():
    td = tempfile.mkdtemp()
    checker = _checker(td)
    tool = RunCommandTool(ArtifactStore(storage_dir=Path(td) / "artifacts"))
    ctx = PermissionContext(
        mode="confirm",
        session_overrides={"run_command": PermissionLevel.AUTO},
    )

    level = checker.check(
        "run_command",
        {"command": "echo ok", "with_escalated_permissions": True},
        context=ctx,
        tool=tool,
    )

    assert level == PermissionLevel.CONFIRM


def test_content_allow_cannot_bypass_diff_review_capability_floor():
    td = tempfile.mkdtemp()
    ctx = PermissionContext(mode="confirm")

    edit_checker = _checker(td, content_allow_rules=["edit_file(src/**)"])
    edit_decision = edit_checker.evaluate(
        "edit_file",
        {"file_path": "src/app.py", "old_string": "a", "new_string": "b"},
        context=ctx,
        tool=EditFileTool(),
    )

    patch_checker = _checker(td, content_allow_rules=["apply_patch"])
    patch_decision = patch_checker.evaluate(
        "apply_patch",
        {"patch": "*** Begin Patch\n*** End Patch"},
        context=ctx,
        tool=ApplyPatchTool(),
    )

    assert edit_decision.permission_level == PermissionLevel.DIFF_REVIEW
    assert edit_decision.matched_rule_source == "tool_capability"
    assert patch_decision.permission_level == PermissionLevel.DIFF_REVIEW
    assert patch_decision.matched_rule_source == "tool_capability"


def test_session_auto_override_cannot_bypass_diff_review_capability_floor():
    td = tempfile.mkdtemp()
    checker = _checker(td)
    ctx = PermissionContext(
        mode="confirm",
        session_overrides={
            "edit_file": PermissionLevel.AUTO,
            "apply_patch": PermissionLevel.AUTO,
        },
    )

    assert checker.check(
        "edit_file",
        {"file_path": "src/app.py", "old_string": "a", "new_string": "b"},
        context=ctx,
        tool=EditFileTool(),
    ) == PermissionLevel.DIFF_REVIEW
    assert checker.check(
        "apply_patch",
        {"patch": "*** Begin Patch\n*** End Patch"},
        context=ctx,
        tool=ApplyPatchTool(),
    ) == PermissionLevel.DIFF_REVIEW


def test_rules_cannot_auto_approve_a_destructive_command_invocation():
    td = tempfile.mkdtemp()
    tool = RunCommandTool(ArtifactStore(storage_dir=Path(td) / "artifacts"))
    args = {"command": "rm -rf build"}

    content_checker = _checker(td, content_allow_rules=["run_command(rm -rf build)"])
    session_checker = _checker(td)

    assert content_checker.check(
        "run_command",
        args,
        context=PermissionContext(mode="confirm"),
        tool=tool,
    ) == PermissionLevel.CONFIRM
    assert session_checker.check(
        "run_command",
        args,
        context=PermissionContext(
            mode="confirm",
            session_overrides={"run_command": PermissionLevel.AUTO},
        ),
        tool=tool,
    ) == PermissionLevel.CONFIRM

    with pytest.raises(ValueError, match="Protocol command"):
        parse_content_rule("terminal.exec")
    # Catastrophic commands are gated by the tool's own permission hook and by
    # the exact-request-digest check inside execute(); validate_input
    # deliberately stays out of it so approval evidence can be produced first.
    from backend.permissions.checker import check_catastrophic_command

    allowed, reason = check_catastrophic_command("rm -rf /")
    assert not allowed and reason
    assert tool.validate_input({"command": "rm -rf /"}) == ""
    assert tool.check_permission(
        {"command": "rm -rf /"},
        PermissionContext(mode="bypass"),
    ) == PermissionLevel.CONFIRM


def test_rules_cannot_auto_approve_open_world_or_destructive_mcp_tools():
    class Client:
        connected = True

    td = tempfile.mkdtemp()
    ctx = PermissionContext(mode="confirm")
    proxies = [
        MCPToolProxy(
            "figma",
            MCPToolDef(
                name="selected_context",
                description="Read the current desktop selection",
                annotations={"readOnlyHint": True, "openWorldHint": True},
            ),
            Client(),  # type: ignore[arg-type]
        ),
        MCPToolProxy(
            "github",
            MCPToolDef(
                name="delete_issue",
                description="Delete an issue",
                annotations={"readOnlyHint": True, "destructiveHint": True},
            ),
            Client(),  # type: ignore[arg-type]
        ),
    ]

    for proxy in proxies:
        content_checker = _checker(td, content_allow_rules=[proxy.name])
        session_checker = _checker(td)
        assert content_checker.check(proxy.name, {}, context=ctx, tool=proxy) == PermissionLevel.CONFIRM
        assert session_checker.check(
            proxy.name,
            {},
            context=PermissionContext(
                mode="confirm",
                session_overrides={proxy.name: PermissionLevel.AUTO},
            ),
            tool=proxy,
        ) == PermissionLevel.CONFIRM


def test_read_only_mcp_still_requires_confirmation():
    class Client:
        connected = True

    td = tempfile.mkdtemp()
    proxy = MCPToolProxy(
        "docs",
        MCPToolDef(
            name="search",
            description="Search documentation",
            annotations={"readOnlyHint": True},
        ),
        Client(),  # type: ignore[arg-type]
    )
    assert _checker(td).check(
        proxy.name,
        {},
        context=PermissionContext(mode="auto"),
        tool=proxy,
    ) == PermissionLevel.CONFIRM
    assert _checker(td, content_allow_rules=["mcp__docs"]).check(
        proxy.name,
        {},
        context=PermissionContext(mode="auto"),
        tool=proxy,
    ) == PermissionLevel.AUTO


def test_plan_mode_denies_open_world_mcp_reader():
    class Client:
        connected = True
        _command = "node"
        _args = ["extension.js"]

    definition = MCPToolDef(
        name="fetch_page",
        description="Fetch a public page",
        annotations={"readOnlyHint": True, "openWorldHint": True},
    )
    proxy = MCPToolProxy("websearch", definition, Client())  # type: ignore[arg-type]
    checker = _checker(tempfile.mkdtemp())
    context = PermissionContext(mode="plan")

    assert checker.check(proxy.name, {}, context=context, tool=proxy) == PermissionLevel.ALWAYS_DENY


def test_plan_mode_denies_command_side_effects_even_when_tool_requests_confirm():
    td = tempfile.mkdtemp()
    checker = _checker(td)
    tool = RunCommandTool(ArtifactStore(storage_dir=Path(td) / "artifacts"))
    context = PermissionContext(mode="plan")

    for command in ("rm -rf /", "find . -delete", "git status"):
        assert checker.check(
            "run_command", {"command": command}, context=context, tool=tool
        ) == PermissionLevel.ALWAYS_DENY


def test_plan_mode_preserves_exit_plan_confirmation_and_plan_file_exception(tmp_path: Path):
    plan_path = tmp_path / "plan.md"
    context = PermissionContext(
        mode="plan",
        filesystem_constraints={"plan_files": [str(plan_path)]},
    )
    checker = _checker(str(tmp_path))

    assert checker.check(
        "exit_plan_mode", {}, context=context, tool=ExitPlanModeTool(tmp_path)
    ) == PermissionLevel.CONFIRM
    assert checker.check(
        "write_file",
        {"file_path": str(plan_path), "content": "# Plan"},
        context=context,
        tool=WriteFileTool(),
    ) == PermissionLevel.AUTO
    assert checker.check(
        "edit_file",
        {
            "file_path": str(plan_path),
            "old_string": "# Plan",
            "new_string": "# Revised plan",
        },
        context=context,
        tool=EditFileTool(),
    ) == PermissionLevel.AUTO
    assert checker.check(
        "write_file",
        {"file_path": str(tmp_path / "not-the-plan.md"), "content": "blocked"},
        context=context,
        tool=WriteFileTool(),
    ) == PermissionLevel.ALWAYS_DENY


def test_auto_and_bypass_keep_their_explicit_product_semantics():
    td = tempfile.mkdtemp()
    checker = _checker(td, content_allow_rules=["edit_file(src/**)", "apply_patch"])

    assert checker.check(
        "edit_file",
        {"file_path": "src/app.py", "old_string": "a", "new_string": "b"},
        context=PermissionContext(mode="auto"),
        tool=EditFileTool(),
    ) == PermissionLevel.AUTO
    assert checker.check(
        "apply_patch",
        {"patch": "*** Begin Patch\n*** End Patch"},
        context=PermissionContext(mode="auto"),
        tool=ApplyPatchTool(),
    ) == PermissionLevel.AUTO

    command_tool = RunCommandTool(ArtifactStore(storage_dir=Path(td) / "artifacts"))
    assert checker.check(
        "run_command",
        {"command": "rm -rf build"},
        context=PermissionContext(mode="bypass"),
        tool=command_tool,
    ) == PermissionLevel.CONFIRM


def test_explicit_allows_clear_the_run_command_capability_floor():
    """An explicit allow decides this exact capability; a broad mode does not.

    ``run_command`` declares ``mutates_external_state`` and never reports an
    invocation as read-only, so the metadata floor previously returned CONFIRM on
    every call and silently overrode every allow mechanism the UI advertises.
    """
    import dataclasses

    td = tempfile.mkdtemp()
    tool = RunCommandTool(ArtifactStore(storage_dir=Path(td) / "artifacts"))
    args = {"command": "git status"}
    base = PermissionSettings()

    content = PermissionChecker(
        settings=dataclasses.replace(base, content_allow_rules=["run_command(git status:*)"]),
        workspace_root=Path(td),
    )
    assert content.check(
        "run_command", args, context=PermissionContext(mode="confirm"), tool=tool
    ) == PermissionLevel.AUTO

    session = _checker(td)
    assert session.check(
        "run_command",
        args,
        context=PermissionContext(
            mode="confirm", session_overrides={"run_command": PermissionLevel.AUTO}
        ),
        tool=tool,
    ) == PermissionLevel.AUTO

    # A coarse static auto_allow tool-name entry is deliberately NOT an explicit
    # capability allow: embedding hosts (backend/sdk.py's MCP bridge) synthesize
    # `auto_allow=[tool_name]` themselves, so honouring it here would let a
    # non-read-only SDK tool cross that API with no approval channel at all.
    static = PermissionChecker(
        settings=dataclasses.replace(
            base,
            auto_allow=[*base.auto_allow, "run_command"],
            require_confirm=[p for p in base.require_confirm if p != "run_command"],
        ),
        workspace_root=Path(td),
    )
    assert static.check(
        "run_command", args, context=PermissionContext(mode="confirm"), tool=tool
    ) == PermissionLevel.CONFIRM

    # sandbox.autoAllowCommandsIfSandboxed reaches the checker through the tool's own
    # check_permission, which must beat the static require_confirm default.
    assert session.check(
        "run_command",
        args,
        context=PermissionContext(mode="confirm", sandbox_auto_allow_commands=True),
        tool=tool,
    ) == PermissionLevel.AUTO

    # A broad session mode is deliberately not an explicit allow.
    for mode in ("confirm", "auto"):
        assert session.check(
            "run_command", args, context=PermissionContext(mode=mode), tool=tool
        ) == PermissionLevel.CONFIRM, mode
    assert session.check(
        "run_command", args, context=PermissionContext(mode="bypass"), tool=tool
    ) == PermissionLevel.AUTO


def test_explicit_allows_do_not_clear_destructive_or_injection_floors():
    import dataclasses

    td = tempfile.mkdtemp()
    tool = RunCommandTool(ArtifactStore(storage_dir=Path(td) / "artifacts"))
    base = PermissionSettings()
    allowed = PermissionChecker(
        settings=dataclasses.replace(base, content_allow_rules=["run_command(git:*)", "run_command(echo:*)"]),
        workspace_root=Path(td),
    )
    sandboxed = PermissionContext(mode="confirm", sandbox_auto_allow_commands=True)

    for command in ("git clean -fdx", "git reset --hard", "git push --force"):
        assert allowed.check(
            "run_command", {"command": command}, context=PermissionContext(mode="confirm"), tool=tool
        ) == PermissionLevel.CONFIRM, command
        assert _checker(td).check(
            "run_command", {"command": command}, context=sandboxed, tool=tool
        ) == PermissionLevel.CONFIRM, command

    assert allowed.check(
        "run_command", {"command": "echo $(id)"}, context=PermissionContext(mode="confirm"), tool=tool
    ) == PermissionLevel.CONFIRM
    assert _checker(td).check(
        "run_command", {"command": "echo `id`"}, context=sandboxed, tool=tool
    ) == PermissionLevel.CONFIRM


def test_find_exec_shell_wrapper_is_classified_catastrophic():
    from backend.permissions.checker import check_catastrophic_command

    for command in (
        r'find . -exec sh -c "rm -rf /" \;',
        r"find . -exec bash -c 'rm -rf {}' \;",
        r"find . -exec rm {} \;",
        "find . -delete",
    ):
        allowed, reason = check_catastrophic_command(command)
        assert not allowed and reason, command
    for command in (r"find . -name '*.py' -exec grep -l x {} \;", "find . -type f -exec cat {} +"):
        allowed, _reason = check_catastrophic_command(command)
        assert allowed, command
