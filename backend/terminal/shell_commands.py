from __future__ import annotations

import sys


_POWERSHELL_COMMAND_SEPARATORS = frozenset({";", "|", "&", "\n", "\r"})


def _replace_windows_bare_curl_commands(command: str) -> str:
    """Rewrite command-position ``curl`` tokens without touching quoted text.

    A generated PowerShell command may contain several statements or pipeline
    stages.  Replacing only the first token leaves later stages exposed to
    Windows PowerShell's ``curl``/``Invoke-WebRequest`` alias.  A small scanner
    is safer than a broad regular expression because separators inside quoted
    strings must remain literal text.
    """

    output: list[str] = []
    index = 0
    quote: str | None = None
    command_position = True
    length = len(command)

    while index < length:
        character = command[index]

        if quote is not None:
            output.append(character)
            if character == "`" and index + 1 < length:
                index += 1
                output.append(command[index])
            elif character == quote:
                quote = None
            index += 1
            continue

        if character in {"'", '"'}:
            quote = character
            command_position = False
            output.append(character)
            index += 1
            continue

        if character in _POWERSHELL_COMMAND_SEPARATORS:
            command_position = True
            output.append(character)
            index += 1
            continue

        if command_position and character.isspace():
            output.append(character)
            index += 1
            continue

        if command_position and command[index:index + 4].casefold() == "curl":
            token_end = index + 4
            if token_end == length or command[token_end].isspace():
                output.append("curl.exe")
                index = token_end
                command_position = False
                continue

        command_position = False
        output.append(character)
        index += 1

    return "".join(output)


def normalize_windows_shell_command(command: str, *, platform: str | None = None) -> str:
    """Prefer native curl on Windows instead of PowerShell's curl alias.

    In Windows PowerShell, ``curl`` resolves to ``Invoke-WebRequest``. That makes
    common curl flags such as ``-m`` fail with ambiguous PowerShell parameters,
    so generated shell commands should call ``curl.exe`` explicitly.
    """

    target_platform = sys.platform if platform is None else platform
    if target_platform != "win32":
        return command
    return _replace_windows_bare_curl_commands(command)


def windows_powershell_native_tool_alias_prelude() -> str:
    """PowerShell startup snippet that keeps familiar native CLI names usable."""

    return (
        "if (Get-Command curl.exe -ErrorAction SilentlyContinue) { "
        "Set-Alias -Name curl -Value curl.exe -Scope Local -Force; "
        "} "
    )
