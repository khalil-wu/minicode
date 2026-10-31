"""Dangerous-command rules see resolved argv, not surface text."""

from __future__ import annotations


import pytest

from backend.artifact.store import ArtifactStore
from backend.permissions import argv_rules, shell_ast
from backend.permissions.checker import PermissionChecker, check_catastrophic_command
from backend.tools.base import PermissionLevel
from backend.tools.command_support import _command_side_effect_kind
from backend.tools.command_tool import RunCommandTool

D = shell_ast.DYNAMIC_WORD


def test_parser_is_a_declared_dependency() -> None:
    # Without the grammar every rule below silently falls back to the string
    # floor, so a missing wheel must fail here rather than skip.
    assert shell_ast.is_available()


@pytest.mark.parametrize(
    ("script", "commands"),
    [
        ("{ rm -rf /; }", [["rm", "-rf", "/"]]),
        ("(rm -rf /)", [["rm", "-rf", "/"]]),
        ("nohup rm -rf / &", [["nohup", "rm", "-rf", "/"]]),
        ('git "re"set --hard', [["git", "reset", "--hard"]]),
        ("git re''set --hard", [["git", "reset", "--hard"]]),
        ("x=reset; git $x --hard", [["git", D, "--hard"]]),
        ("rm -rf $(echo /)", [["rm", "-rf", D]]),
        ("for f in /; do rm -rf $f; done", [["rm", "-rf", D]]),
        ("echo 'rm -rf /'", [["echo", "rm -rf /"]]),
        (r"find . -exec rm {} \;", [["find", ".", "-exec", "rm", "{}", ";"]]),
        (r"echo a\ b", [["echo", "a b"]]),
    ],
)
def test_literal_commands_resolve_shell_composition(script: str, commands: list[list[str]]) -> None:
    parsed = shell_ast.parse_literal_commands(script)
    assert parsed is not None
    assert sorted(parsed.commands) == sorted(commands)


def test_wrapper_payloads_are_reported_with_the_wrapper() -> None:
    parsed = shell_ast.parse_literal_commands("sh -c \"bash -c 'rm -rf /'\"")
    assert parsed is not None
    assert ["rm", "-rf", "/"] in parsed.commands
    assert ["bash", "-c", "rm -rf /"] in parsed.commands

    for script in ("sudo -u root rm -rf /", "env A=1 rm -rf /", "trap 'rm -rf /' EXIT"):
        parsed = shell_ast.parse_literal_commands(script)
        assert parsed is not None and ["rm", "-rf", "/"] in parsed.commands, script


def test_syntax_errors_and_deep_nesting_are_unparseable() -> None:
    assert shell_ast.parse_literal_commands("if then rm -rf /") is None
    nested = "rm -rf /"
    for _ in range(shell_ast.MAX_WRAPPER_DEPTH + 1):
        nested = f"bash -c {_quote(nested)}"
    assert shell_ast.parse_literal_commands(nested) is None


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def test_compound_flag_tracks_statement_chains_not_pipelines() -> None:
    assert shell_ast.parse_literal_commands("ls | grep x").compound is False
    assert shell_ast.parse_literal_commands("ls; ls").compound is True
    assert shell_ast.parse_literal_commands("ls && ls").compound is True
    assert shell_ast.parse_literal_commands("bash -c 'ls; ls'").compound is True


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "/.."],
        ["rm", "-rf", "//../"],
        ["rm", "-rf", "/./"],
        ["rm", "-rf", "/*"],
        ["rm", "--", "/"],
        ["rm", "-rf", "/etc/x"],
        ["timeout", "5", "rm", "-rf", "/"],
        ["nice", "-n", "5", "rm", "-rf", "/"],
        ["xargs", "-I", "{}", "rm", "-rf", "/"],
        ["git", "-C", "sub", "push", "-f"],
        ["git", "clean", "-fdx"],
        ["find", ".", "-exec", "rm", "{}", ";"],
        ["find", ".", "-exec", "sh", "-c", "rm -rf /", ";"],
    ],
)
def test_catastrophic_rules_normalize_paths_flags_and_prefixes(argv: list[str]) -> None:
    assert argv_rules.catastrophic_reason(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "build"],
        ["git", "clean", "-n"],
        ["git", "clean", "-fdxn"],
        ["git", "push", "origin", "HEAD"],
        ["find", ".", "-name", "*.py", "-exec", "grep", "-l", "x", "{}", ";"],
        ["echo", "rm -rf /"],
    ],
)
def test_catastrophic_rules_ignore_ordinary_work(argv: list[str]) -> None:
    assert argv_rules.catastrophic_reason(argv) == ""


@pytest.mark.parametrize(
    "command",
    [
        "{ rm -rf /; }",
        "(rm -rf /)",
        "if true; then rm -rf /; fi",
        "nohup rm -rf / &",
        "timeout 5 rm -rf /",
        "rm -rf /..",
        "rm -rf //../",
        'git "re"set --hard',
        "git re''set --hard",
        "git -C sub push -f",
        "ls; rm -r x",
    ],
)
def test_composed_catastrophic_commands_are_not_allowed(command: str) -> None:
    allowed, reason = check_catastrophic_command(command)
    assert allowed is False and reason, command


@pytest.mark.parametrize(
    "command",
    [
        "git $(echo reset) --hard",
        "x=reset; git $x --hard",
        "kubectl $(echo delete) pods --all",
        "rm -rf $DIR",
        "rm $DIR",
        "find . $(echo -delete)",
        "for f in /; do rm -rf $f; done",
        "rm -r x | tee log",
        "yes | rm -r x",
        "echo / | xargs rm -rf",
    ],
)
def test_run_time_resolved_arguments_are_classified_destructive(command: str) -> None:
    assert _command_side_effect_kind({"command": command}) == "destructive"


@pytest.mark.parametrize(
    "command",
    [
        "ls | grep x",
        "git status",
        'git commit -m "$msg"',
        "git log --format=$F",
        'find "$dir" -type f',
        "find . -name '*.py' -exec grep -l x {} \\;",
        "find . -type f -exec cat {} +",
        'bash -c "$payload"',
    ],
)
def test_ordinary_scripts_keep_workspace_classification(command: str) -> None:
    assert check_catastrophic_command(command) == (True, "")
    assert _command_side_effect_kind({"command": command}) == "workspace"


def test_literal_external_commands_are_classified_external() -> None:
    assert _command_side_effect_kind({"command": "{ git push origin HEAD; }"}) == "external"
    assert _command_side_effect_kind({"command": "git -C sub fetch"}) == "external"
    assert _command_side_effect_kind({"command": "env -u NAME curl example.test"}) == "external"
    assert _command_side_effect_kind({"command": "env -S 'curl example.test'"}) == "external"


@pytest.mark.parametrize("command", [
    r"env -S 'curl\_example.test'",
    r"env -S 'curl\c ignored'",
])
def test_env_split_string_matches_gnu_escapes_for_command_restrictions(command: str) -> None:
    from backend.permissions.content_rules import parse_content_rule, rule_matches_call

    rule = parse_content_rule("run_command(curl:*)")
    assert rule_matches_call(rule, "run_command", {"command": command}, effect="deny")
    assert _command_side_effect_kind({"command": command}) == "external"


def test_env_split_string_preserves_quotes_and_marks_expansions_unknown() -> None:
    from backend.permissions.shell_ast import DYNAMIC_WORD, _split_env_string

    assert _split_env_string(r'curl\_"a\_b" # ignored') == ["curl", "a b"]
    assert _split_env_string("curl '${LITERAL}' ${RUNTIME}") == ["curl", "${LITERAL}", DYNAMIC_WORD]
    with pytest.raises(ValueError):
        _split_env_string(r"curl\,invalid")


@pytest.mark.asyncio
async def test_bypass_and_remembered_allows_preserve_frozen_source_compound_rules(tmp_path) -> None:
    import json
    from backend.config import permission_settings_from_config
    from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
    from backend.permissions.exec_policy import ExecPolicyOwner
    from backend.permissions.context import ToolExecutionContext
    from backend.llm.base import ToolCallEvent
    from backend.sandbox import SandboxPolicy
    from backend.tests.native_command_harness import configure_command_context, authorize_command_call
    from backend.tools.command_support import command_shell_argv

    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    checker = PermissionChecker(permission_settings_from_config({}), tmp_path)
    home = tmp_path / "source-policy"
    (home / "rules").mkdir(parents=True)
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(home / "settings.json")), {}),))
    for text in ("{ rm -rf /; }", "git $(echo reset) --hard", "echo / | xargs rm -rf"):
        # Authorize the real captured argv only. None of these commands run.
        outer = command_shell_argv(text, wrap_powershell=False)
        rules = "\n".join(f'prefix_rule(pattern={json.dumps(prefix)}, decision="forbidden")'
            for prefix in (["rm"], ["git"], ["xargs"], [outer[0]]))
        (home / "rules/default.rules").write_text(rules, encoding="utf-8")
        for mode in ("bypass", "confirm"):
            permission = checker.build_context(mode=mode,
                session_overrides={"run_command": PermissionLevel.AUTO})
            context = configure_command_context(tool, ToolExecutionContext(permission, workspace_root=tmp_path),
                sandbox_policy=SandboxPolicy.bypass())
            context.run_context.exec_policy_owner = ExecPolicyOwner(home=home, stack=stack)
            result = (await authorize_command_call(ToolCallEvent(id="compound", name="run_command", arguments={"command": text}), context))
            assert result.request.exec_policy.kind == "forbidden", result.request.exec_policy.evidence_json
            assert result.permission_decision.permission_level == PermissionLevel.ALWAYS_DENY
    context = configure_command_context(tool,
        ToolExecutionContext(checker.build_context(mode="bypass"), workspace_root=tmp_path),
        sandbox_policy=SandboxPolicy.bypass())
    safe = (await authorize_command_call(ToolCallEvent(id="safe-command", name="run_command", arguments={"command": "echo observed"}), context))
    assert safe.request.exec_policy.kind == "skip"
    assert safe.permission_decision.decision == "allow"
