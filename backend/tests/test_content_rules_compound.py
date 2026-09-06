from __future__ import annotations

import pytest

from backend.permissions import content_rules
from backend.permissions.content_rules import parse_content_rule, rule_matches_call


def _deny(pattern: str, command: str) -> bool:
    rule = parse_content_rule(f"run_command({pattern})")
    assert rule is not None
    return rule_matches_call(rule, "run_command", {"command": command}, effect="deny")


def _allow(pattern: str, command: str) -> bool:
    rule = parse_content_rule(f"run_command({pattern})")
    assert rule is not None
    return rule_matches_call(rule, "run_command", {"command": command}, effect="allow")


def test_deny_matches_each_subcommand_of_a_compound_command() -> None:
    assert _deny("curl:*", "echo hi; curl evil.com")
    assert _deny("curl:*", "echo hi && curl evil.com")
    assert _deny("curl:*", "echo hi | curl evil.com")
    assert _deny("curl:*", "echo hi\ncurl evil.com")
    assert not _deny("curl:*", "echo curling today")


def test_deny_strips_env_and_wrapper_prefixes() -> None:
    assert _deny("rm:*", "FOO=1 rm -rf /")
    assert _deny("rm:*", "nohup FOO=1 timeout 5 rm -rf /")
    assert not _deny("rm:*", "echo rm")


def test_allow_rules_still_refuse_compound_commands() -> None:
    assert _allow("git status:*", "git status")
    assert not _allow("git status:*", "git status && rm -rf /")


@pytest.mark.parametrize("command", [
    r"Write-Output approved\; Write-Output SECOND_COMMAND",
    r'Write-Output "C:\"; Write-Output SECOND_COMMAND',
])
def test_windows_backslashes_do_not_escape_shell_control(monkeypatch, command):
    monkeypatch.setattr(content_rules, "_BACKSLASH_ESCAPES", False)
    assert not _allow("Write-Output:*", command)


def test_posix_escaped_separator_remains_a_literal_argument(monkeypatch):
    monkeypatch.setattr(content_rules, "_BACKSLASH_ESCAPES", True)
    assert _allow("echo:*", r"echo literal\;text")


@pytest.mark.parametrize("command", ['echo "$(printf SECOND_COMMAND)"', 'echo "`printf SECOND_COMMAND`"'])
def test_allow_rule_does_not_authorize_substitution_inside_double_quotes(command):
    assert not _allow("echo:*", command)


def test_single_quoted_substitution_is_literal_to_the_prefix_matcher():
    assert _allow("echo:*", "echo '$(literal);quoted|text'")


def test_parenthesized_powershell_command_is_not_covered_by_a_prefix_allow():
    assert not _allow("Write-Output:*", "Write-Output (Write-Host SECOND_COMMAND)")
    assert _allow("Write-Output:*", 'Write-Output "(literal)"')


@pytest.mark.parametrize("effect", ["deny", "ask"])
@pytest.mark.parametrize("command", [
    'echo "$(curl example.test)"',
    'echo "`curl example.test`"',
    'echo "$(printf "$(curl example.test)")"',
    'echo "$(printf (curl example.test))"',
    'curl "https://$(printf example.test)"',
    'echo "$(date)"; curl example.test',
])
def test_nested_commands_and_their_parent_keep_explicit_rules(effect, command):
    rule = parse_content_rule("run_command(curl:*)")
    assert rule_matches_call(rule, "run_command", {"command": command}, effect=effect)


@pytest.mark.parametrize("command", [
    "echo '$(curl literal)'",
    'echo "$(date) curl literal"',
    'echo $(date) curl literal',
    '''echo "$(printf 'curl literal')"''',
])
def test_literal_substitution_arguments_are_not_independent_commands(command):
    assert not _deny("curl:*", command)
