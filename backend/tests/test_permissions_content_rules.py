"""Tool(content) permission rules: parser, matcher, and checker integration.

These rules let a user allow/deny based on a tool's content (command text or
file path), e.g. ``run_command(npm run:*)`` or ``edit_file(src/**)``, instead of
only the tool name.
"""

from __future__ import annotations

import tempfile
import json
from pathlib import Path

import pytest

from backend.config import permission_settings_from_config
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.artifact.store import ArtifactStore
from backend.mcp.client import MCPToolDef
from backend.mcp.registry import MCPToolProxy
from backend.permissions.checker import PermissionChecker
from backend.permissions.rules import PermissionRuleMatcher
from backend.permissions.content_rules import (
    parse_content_rule,
    rule_matches_call,
)
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.exec_policy import ExecPolicyOwner
from backend.llm.base import ToolCallEvent
from backend.sandbox import SandboxPolicy
from backend.tests.native_command_harness import configure_command_context, authorize_command_call
from backend.tests.test_codex_mcp_approval_producer import authorize as authorize_mcp, proxy as source_mcp_proxy
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
        settings=permission_settings_from_config({"permissions": settings_kwargs}),
        workspace_root=Path(td),
    )


async def _authorize_command(root, args, checker, permission, sandbox, *, rule_decision=None):
    tool = RunCommandTool(ArtifactStore(storage_dir=root / "artifacts"))
    context = configure_command_context(tool, ToolExecutionContext(permission, workspace_root=root), sandbox_policy=sandbox)
    context.permission_checker = checker
    call = ToolCallEvent(id="source-command", name=tool.name, arguments=args)
    original = (await authorize_command_call(call, context))
    if rule_decision is not None:
        rules = "\n".join(f'prefix_rule(pattern={json.dumps(list(command))}, decision="{rule_decision}")'
            for command in original.request.exec_policy.parsed_commands)
        home = root / "source-policy"
        (home / "rules").mkdir(parents=True, exist_ok=True)
        (home / "rules/default.rules").write_text(rules, encoding="utf-8")
        stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(home / "settings.json")), {}),))
        context.run_context.exec_policy_owner = ExecPolicyOwner(home=home, stack=stack)
        return (await authorize_command_call(call, context))
    return original


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


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "curl https://example.invalid",
    "sudo curl https://example.invalid",
    "doas curl https://example.invalid",
    "xargs -I {} curl https://example.invalid",
    "env -u NAME curl https://example.invalid",
    "env -S 'curl https://example.invalid'",
    r"env -S 'curl\_https://example.invalid'",
    r"env -S 'curl\c ignored'",
])
async def test_native_command_admission_preserves_configured_content_deny(tmp_path, command):
    checker = _checker(str(tmp_path), content_deny_rules=["run_command(curl:*)"])
    context = checker.build_context(mode="bypass")
    result = await _authorize_command(tmp_path, {"command": command}, checker, context, SandboxPolicy.bypass())
    assert result.request.exec_policy.kind == "skip"
    assert result.permission_decision.permission_level == PermissionLevel.ALWAYS_DENY
    assert result.permission_decision.matched_rule_source == "content_rule"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,expected", [("on-request", PermissionLevel.CONFIRM), ("never", PermissionLevel.ALWAYS_DENY)])
async def test_native_command_admission_keeps_content_ask_and_approval_policy(tmp_path, policy, expected):
    checker = _checker(str(tmp_path), content_ask_rules=["run_command(curl:*)"])
    context = checker.build_context(mode="bypass", approval_policy=policy)
    result = await _authorize_command(tmp_path, {"command": "sudo curl https://example.invalid"},
        checker, context, SandboxPolicy.bypass())
    assert result.request.exec_policy.kind == "skip"
    assert result.permission_decision.permission_level == expected


@pytest.mark.parametrize("allow_rule", ["run_command", "run_command(Write-Output:*)"])
@pytest.mark.parametrize("with_tool", [False, True])
def test_remembered_allow_cannot_lower_command_substitution_boundary(tmp_path, allow_rule, with_tool):
    checker = _checker(str(tmp_path), content_allow_rules=[allow_rule])
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts")) if with_tool else None
    level = checker.check(
        "run_command",
        {"command": 'Write-Output "$(Write-Output SECOND_COMMAND)"'},
        context=PermissionContext(mode="confirm"),
        tool=tool,
    )
    assert level == PermissionLevel.CONFIRM


def test_windows_remembered_prefix_does_not_approve_second_command(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.permissions.content_rules._BACKSLASH_ESCAPES", False)
    checker = _checker(str(tmp_path), content_allow_rules=["run_command(Write-Output:*)"])
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    level = checker.check(
        "run_command",
        {"command": r"Write-Output approved\; Write-Output SECOND_COMMAND"},
        context=PermissionContext(mode="confirm"),
        tool=tool,
    )
    assert level == PermissionLevel.CONFIRM


@pytest.mark.parametrize("command", [
    'Write-Output "$(Write-Host SECOND_COMMAND)"',
    'Write-Host "$(Write-Output marker)"',
    'Write-Output (Write-Host SECOND_COMMAND)',
])
@pytest.mark.parametrize("mode", ["confirm", "bypass"])
def test_nested_command_deny_applies_in_every_mode(tmp_path, command, mode):
    checker = _checker(str(tmp_path), content_deny_rules=["run_command(Write-Host:*)"])
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    assert checker.check(
        "run_command", {"command": command}, context=PermissionContext(mode=mode), tool=tool,
    ) == PermissionLevel.ALWAYS_DENY


def test_remembered_prefix_does_not_approve_parenthesized_command(tmp_path):
    checker = _checker(str(tmp_path), content_allow_rules=["run_command(Write-Output:*)"])
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    assert checker.check(
        "run_command", {"command": "Write-Output (Write-Host SECOND_COMMAND)"},
        context=PermissionContext(mode="confirm"), tool=tool,
    ) == PermissionLevel.CONFIRM


def test_content_allow_does_not_force_auto_in_plan_mode():
    td = tempfile.mkdtemp()
    checker = _checker(td, content_allow_rules=["edit_file(src/**)"])
    ctx = PermissionContext(mode="plan")
    # An allow rule must not force AUTO in plan mode (plan stays restrictive);
    # whatever plan normally decides, it must not be auto-approved.
    level = checker.check("edit_file", {"file_path": "src/app.py"}, context=ctx)
    assert level != PermissionLevel.AUTO


@pytest.mark.asyncio
async def test_content_allow_cannot_bypass_command_escalation_gate(tmp_path):
    checker = _checker(str(tmp_path), content_allow_rules=["run_command(echo approved)"])
    authorization = (await _authorize_command(tmp_path,
        {"command": "echo approved", "sandbox_permissions": "require_escalated"}, checker,
        checker.build_context(mode="confirm"), SandboxPolicy.workspace_default(tmp_path)))
    assert not authorization.error
    assert authorization.request.exec_policy.kind == "needs_approval"
    assert authorization.permission_decision.permission_level == PermissionLevel.CONFIRM


@pytest.mark.asyncio
async def test_session_auto_override_cannot_bypass_command_escalation_gate(tmp_path):
    checker = _checker(str(tmp_path))
    authorization = (await _authorize_command(tmp_path,
        {"command": "echo approved", "sandbox_permissions": "require_escalated"}, checker,
        checker.build_context(mode="confirm",
            session_overrides={"run_command": PermissionLevel.AUTO}), SandboxPolicy.workspace_default(tmp_path)))
    assert not authorization.error
    assert authorization.request.exec_policy.kind == "needs_approval"
    assert authorization.permission_decision.permission_level == PermissionLevel.CONFIRM


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


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,expected", [("prompt", PermissionLevel.CONFIRM), ("forbidden", PermissionLevel.ALWAYS_DENY)])
async def test_local_allows_cannot_override_source_command_rules(tmp_path, decision, expected):
    checker = _checker(str(tmp_path), content_allow_rules=["run_command(git:*)"])
    permission = checker.build_context(mode="confirm",
        session_overrides={"run_command": PermissionLevel.AUTO})
    authorization = (await _authorize_command(tmp_path, {"command": "git reset --hard"}, checker, permission,
        SandboxPolicy.bypass(), rule_decision=decision))
    assert authorization.permission_decision.permission_level == expected
    assert authorization.request.exec_policy.kind == ("needs_approval" if decision == "prompt" else "forbidden")
    with pytest.raises(ValueError, match="Protocol command"):
        parse_content_rule("terminal.exec")
    from backend.permissions.checker import check_catastrophic_command
    allowed, reason = check_catastrophic_command("rm -rf /")
    assert not allowed and reason


@pytest.mark.asyncio
async def test_source_mcp_annotations_and_explicit_asks_remain_distinct(tmp_path):
    class Client:
        connected = True

    ctx = PermissionContext(mode="confirm", workspace_root=tmp_path)
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

    for proxy, expected in zip(proxies, (PermissionLevel.AUTO, PermissionLevel.CONFIRM)):
        result, _, _ = (await authorize_mcp(proxy, tmp_path, SandboxPolicy.workspace_default(tmp_path), ctx,
            permission_settings=permission_settings_from_config({}), arguments={}))
        assert result.permission_decision.permission_level == expected
        for rules, required in (({"content_ask_rules": [proxy.policy_identity]}, PermissionLevel.CONFIRM),
            ({"content_deny_rules": [proxy.policy_identity]}, PermissionLevel.ALWAYS_DENY)):
            restricted, _, _ = (await authorize_mcp(proxy, tmp_path, SandboxPolicy.workspace_default(tmp_path), ctx,
                permission_settings=permission_settings_from_config({"permissions": rules}), arguments={}))
            assert restricted.permission_decision.permission_level == required


@pytest.mark.asyncio
async def test_source_read_only_mcp_auto_and_configured_prompt(tmp_path):
    for mode, expected in (("auto", PermissionLevel.AUTO), ("prompt", PermissionLevel.CONFIRM)):
        proxy, _, _ = source_mcp_proxy({"readOnlyHint": True}, mode)
        result, _, _ = (await authorize_mcp(proxy, tmp_path, SandboxPolicy.workspace_default(tmp_path),
            PermissionContext(mode="auto", workspace_root=tmp_path),
            permission_settings=permission_settings_from_config({}), arguments={}))
        assert result.permission_decision.permission_level == expected


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


@pytest.mark.asyncio
async def test_auto_and_bypass_keep_their_explicit_product_semantics(tmp_path):
    td = str(tmp_path)
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

    allowed = (await _authorize_command(tmp_path, {"command": "echo approved"}, checker,
        checker.build_context(mode="bypass"), SandboxPolicy.bypass()))
    assert allowed.request.exec_policy.kind == "skip"
    assert allowed.permission_decision.decision == "allow"
    denied = (await _authorize_command(tmp_path, {"command": "echo approved"}, checker,
        checker.build_context(mode="bypass"), SandboxPolicy.bypass(), rule_decision="forbidden"))
    assert denied.permission_decision.decision == "deny"


@pytest.mark.asyncio
async def test_source_execpolicy_owns_exact_command_approval_without_legacy_sandbox_flags(tmp_path):
    from dataclasses import replace
    checker = _checker(str(tmp_path))
    permission = checker.build_context(mode="confirm")
    for legacy in (False, True):
        captured = (await _authorize_command(tmp_path, {"command": "git status"}, checker,
            replace(permission, sandbox_auto_allow_commands=legacy), SandboxPolicy.bypass(),
            rule_decision="allow"))
        assert captured.request.exec_policy.kind == "skip"
        assert captured.permission_decision.permission_level == PermissionLevel.AUTO
        prompted = (await _authorize_command(tmp_path, {"command": "git status"}, checker,
            replace(permission, sandbox_auto_allow_commands=legacy), SandboxPolicy.bypass(),
            rule_decision="prompt"))
        assert prompted.request.exec_policy.kind == "needs_approval"
        assert prompted.permission_decision.permission_level == PermissionLevel.CONFIRM


@pytest.mark.asyncio
async def test_legacy_allows_cannot_replace_actual_source_command_decisions(tmp_path):
    checker = _checker(str(tmp_path), content_allow_rules=["run_command(git:*)", "run_command(echo:*)"])
    permission = checker.build_context(mode="confirm",
        session_overrides={"run_command": PermissionLevel.AUTO})
    for command in ("git clean -fdx", "git reset --hard", "git push --force", "echo $(id)", "echo `id`"):
        captured = (await _authorize_command(tmp_path, {"command": command}, checker, permission,
            SandboxPolicy.bypass(), rule_decision="prompt"))
        assert captured.request.exec_policy.kind == "needs_approval", captured.request.exec_policy.evidence_json
        assert captured.permission_decision.permission_level == PermissionLevel.CONFIRM


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
