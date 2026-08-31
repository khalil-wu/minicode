from __future__ import annotations

from backend.commands.catalog import get_composer_command_catalog


def test_builtin_command_arguments_are_unambiguous() -> None:
    permissions = next(
        command
        for command in get_composer_command_catalog()
        if command.get("command") == "permissions"
    )
    arguments = permissions.get("args") or []
    values = [str(argument["value"]).casefold() for argument in arguments]
    assert len(values) == len(set(values))
    assert {"confirm", "diff", "auto", "rules list"}.issubset(values)
