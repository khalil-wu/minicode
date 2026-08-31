from __future__ import annotations

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
