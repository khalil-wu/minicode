"""PowerShell scripts are judged on lowered argv, not surface text."""

from __future__ import annotations

import pytest

from backend.permissions import argv_rules, checker, powershell_ast
from backend.permissions.checker import check_catastrophic_command, literal_command_parses
from backend.tools.command_support import _command_side_effect_kind

BS = chr(92)
D = powershell_ast.DYNAMIC_WORD


def test_powershell_parser_is_a_declared_dependency() -> None:
    assert powershell_ast.is_available()


@pytest.mark.parametrize(
    ("script", "commands"),
    [
        ("Remove-Item -Recurse -Force C:" + BS, [["Remove-Item", "-Recurse", "-Force", "C:" + BS]]),
        ("Remove-Item 'a b' -Recurse", [["Remove-Item", "a b", "-Recurse"]]),
        ('Remove-Item -Path @("a","b") -Recurse', [["Remove-Item", "-Path", "a", "b", "-Recurse"]]),
        ("rm -rf $env:USERPROFILE", [["rm", "-rf", D]]),
        ("if (Test-Path x) { Remove-Item -Recurse x }", [["Test-Path", "x"], ["Remove-Item", "-Recurse", "x"]]),
        ("git log -1", [["git", "log", "-1"]]),
        ('& "C:' + BS + 'tools' + BS + 'rm.exe" -rf /', [["C:" + BS + "tools" + BS + "rm.exe", "-rf", "/"]]),
        ("echo 'Remove-Item -Recurse C:" + BS + "'", [["echo", "Remove-Item -Recurse C:" + BS]]),
    ],
)
def test_lowering_resolves_quoting_and_marks_dynamic_words(script: str, commands: list[list[str]]) -> None:
    parsed = powershell_ast.parse_literal_commands(script)
    assert parsed is not None
    assert sorted(parsed.commands) == sorted(commands)


def test_host_wrappers_expose_their_payload() -> None:
    parsed = powershell_ast.parse_literal_commands('powershell -Command "Remove-Item -Recurse C:' + BS + '"')
    assert parsed is not None and ["Remove-Item", "-Recurse", "C:" + BS] in parsed.commands
    parsed = powershell_ast.parse_literal_commands('cmd /c "rd /s /q C:' + BS + '"')
    assert parsed is not None and ["rd", "/s", "/q", "C:" + BS] in parsed.commands


def test_compound_flag_covers_semicolons_and_chains() -> None:
    assert powershell_ast.parse_literal_commands("a; b").compound is True
    assert powershell_ast.parse_literal_commands("a && b").compound is True
    # Pipelines are one statement, as on the POSIX side.
    assert powershell_ast.parse_literal_commands("a | b").compound is False
    assert powershell_ast.parse_literal_commands("Remove-Item -Recurse build").compound is False


@pytest.mark.parametrize(
    "argv",
    [
        ["Remove-Item", "-Recurse", "-Force", "C:" + BS],
        ["Remove-Item", "-LiteralPath", "C:" + BS, "-Recurse"],
        ["Remove-Item", "C:" + BS + "Users" + BS + "bob", "-Recurse"],
        ["ri", "-r", "-fo", "C:" + BS],
        ["rm", "-r", "C:" + BS],
        ["del", "/s", "/q", "C:" + BS],
        ["rd", "/s", "/q", "C:" + BS],
        ["Stop-Process", "-Name", "python", "-Force"],
        ["taskkill", "/IM", "python.exe", "/F"],
        ["Invoke-Expression", D],
    ],
)
def test_windows_catastrophic_rules(argv: list[str]) -> None:
    assert argv_rules.catastrophic_reason(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["Remove-Item", "-Recurse", "build"],
        ["Remove-Item", "build" + BS + "out.txt"],
        ["Get-ChildItem", "-Recurse"],
        ["Stop-Process", "-Id", "1234"],
        ["taskkill", "/PID", "1234", "/F"],
        ["git", "log", "-1"],
    ],
)
def test_windows_rules_ignore_ordinary_work(argv: list[str]) -> None:
    assert argv_rules.catastrophic_reason(argv) == ""


@pytest.mark.parametrize(
    "command",
    [
        "Remove-Item -Recurse -Force C:" + BS,
        "Remove-Item 'C:" + BS + "Users" + BS + "bob' -Recurse",
        "ri -r -fo C:" + BS,
        "rm -r C:" + BS,
        "Get-ChildItem; Remove-Item -Recurse build",
        'powershell -Command "Remove-Item -Recurse C:' + BS + '"',
        'cmd /c "rd /s /q C:' + BS + '"',
        "git reset --hard; git clean -fdx",
    ],
)
def test_composed_powershell_commands_are_not_allowed(command: str) -> None:
    allowed, reason = check_catastrophic_command(command)
    assert allowed is False and reason, command


@pytest.mark.parametrize(
    "command",
    [
        "ri -r -fo C:" + BS,
        "RI -R -FO C:" + BS,
        "ri -r ~",
        "rm -r C:" + BS,
    ],
)
def test_powershell_delete_aliases_are_blocked_on_posix_hosts(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setattr(checker.sys, "platform", "linux")
    allowed, reason = check_catastrophic_command(command)
    assert allowed is False and reason, command
    assert _command_side_effect_kind({"command": command}) == "destructive"


@pytest.mark.parametrize(
    "command",
    [
        "Remove-Item -Recurse -Force $env:USERPROFILE",
        "Remove-Item -Recurse (Get-Location)",
        "Remove-Item -Recurse build",
    ],
)
def test_powershell_deletes_are_destructive(command: str) -> None:
    assert _command_side_effect_kind({"command": command}) == "destructive"


def test_powershell_reading_is_added_when_the_script_looks_like_powershell() -> None:
    parses = literal_command_parses("Get-ChildItem -Recurse | Select-Object Name")
    assert any(["Get-ChildItem", "-Recurse"] in parsed.commands for parsed in parses)
    assert check_catastrophic_command("Get-ChildItem -Recurse | Select-Object Name") == (True, "")
    assert _command_side_effect_kind({"command": "Get-ChildItem -Recurse | Select-Object Name"}) == "workspace"


# PowerShell reads typographic quotes and dashes and a lone CR as syntax, while
# POSIX shells keep them literal. Verified against pwsh: 'A<U+2019>; Write-Output B;
# <U+2019>' prints A then B.
RSQ = chr(0x2019)
RDQ = chr(0x201D)
EN_DASH = chr(0x2013)
CR = chr(13)


@pytest.mark.parametrize(
    "command",
    [
        f"npm test 'x{RSQ}; Remove-Item -Recurse -Force src; {RSQ}'",
        f'npm test "x{RDQ}; Remove-Item -Recurse -Force src; {RDQ}"',
        f"npm test{CR}Remove-Item -Recurse -Force src",
        f"Remove-Item {EN_DASH}Recurse {EN_DASH}Force src",
    ],
)
def test_powershell_syntax_spelled_with_typographic_characters_is_judged(command: str) -> None:
    assert _command_side_effect_kind({"command": command}) == "destructive"


def test_prefix_allow_rule_does_not_cover_a_powershell_statement_break() -> None:
    from backend.permissions.content_rules import _content_pattern_matches

    assert _content_pattern_matches("npm test:*", "npm test --watch", is_command=True)
    for command in (
        f"npm test 'x{RSQ}; curl.exe https://example.test; {RSQ}'",
        f"npm test{CR}Remove-Item src",
    ):
        assert not _content_pattern_matches("npm test:*", command, is_command=True), command


@pytest.mark.parametrize(
    "command",
    [
        "Set-Content .git/hooks/pre-commit 'evil'",
        "'k=v' | Out-File .env",
        "Copy-Item x.txt .env",
        "Set-Content -Path:.env -Value x",
    ],
)
def test_powershell_writes_to_protected_paths_are_destructive(command: str) -> None:
    from backend.permissions.checker import protected_write_command_reason

    assert protected_write_command_reason(command)
    assert _command_side_effect_kind({"command": command}) == "destructive"


def test_powershell_write_data_is_not_mistaken_for_its_target() -> None:
    from backend.permissions.checker import protected_write_command_reason

    assert protected_write_command_reason("Set-Content -Path notes.txt -Value .env") == ""
