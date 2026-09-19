"""Dangerous-command rules see resolved argv, not surface text."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from backend.artifact.store import ArtifactStore
from backend.config import PermissionSettings
from backend.permissions import argv_rules, shell_ast
from backend.permissions.checker import PermissionChecker, check_catastrophic_command
from backend.permissions.context import PermissionContext
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


def test_bypass_mode_confirms_composed_destructive_commands() -> None:
    td = tempfile.mkdtemp()
    tool = RunCommandTool(ArtifactStore(storage_dir=Path(td) / "artifacts"))
    checker = PermissionChecker(settings=PermissionSettings(), workspace_root=Path(td))
    bypass = PermissionContext(mode="bypass")
    remembered = PermissionContext(
        mode="confirm", session_overrides={"run_command": PermissionLevel.AUTO}
    )
    for command in ("{ rm -rf /; }", "git $(echo reset) --hard", "echo / | xargs rm -rf"):
        for context in (bypass, remembered):
            assert (
                checker.check("run_command", {"command": command}, context=context, tool=tool)
                == PermissionLevel.CONFIRM
            ), (command, context.mode)
    assert checker.check("run_command", {"command": "ls"}, context=bypass, tool=tool) == PermissionLevel.AUTO
