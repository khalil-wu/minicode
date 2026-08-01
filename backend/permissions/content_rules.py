"""Content-level permission rules: the ``Tool(content)`` syntax.

Mirrors Claude Code's permission rule grammar so a user can write rules that
match a tool's *content* (the command text, the file path) rather than just the
tool name. Examples::

    Bash(npm run:*)      # allow/deny any ``npm run <anything>`` command
    run_command(git *)    # same idea, MiniCode tool name
    Edit(src/**)          # any edit under src/
    Read(~/.zshrc)        # a specific file
    Bash                  # whole-tool rule (content omitted)

This module is deliberately small: a parser, a matcher, and tool-name aliases.
The checker integrates the parsed rules into its allow/deny pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any

# Friendly aliases → MiniCode tool-name globs. Lets rules use the short cc-style
# names (Bash/Edit/Read/Write) which are more memorable than run_command/etc.
_TOOL_ALIASES: dict[str, str] = {
    "bash": "run_command",
    "shell": "run_command",
    "terminal.exec": "run_command",
    "terminal_exec": "run_command",
    "terminal": "terminal_*",
    "edit": "edit_file",
    "write": "write_file",
    "read": "read_file",
}

# fnmatch patterns for tools whose content is a shell command vs a file path.
_COMMAND_TOOL_GLOBS: tuple[str, ...] = ("run_command", "terminal_*")
_FILE_TOOL_GLOBS: tuple[str, ...] = ("read_file", "write_file", "edit_file", "save_*")

_RULE_RE = re.compile(r"^([A-Za-z_][\w*?.\-]*)\s*(?:\((.*)\))?$", re.DOTALL)

_UNSAFE_COMMAND_WRAPPERS = frozenset(
    {
        "bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh",
        "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
        "env", "sudo", "doas", "pkexec", "xargs", "timeout", "nice", "nohup",
        "time", "stdbuf", "setsid", "command", "builtin",
    }
)


@dataclass(frozen=True)
class ContentRule:
    """A parsed ``Tool(content)`` rule.

    ``tool_glob`` is an fnmatch pattern over the (alias-resolved) tool name.
    ``content`` is ``None`` for a whole-tool rule, otherwise the content
    matcher (a ``prefix:*`` prefix spec for commands, or a glob for paths).
    """

    tool_glob: str
    content: str | None
    raw: str


def _resolve_tool(token: str) -> str:
    return _TOOL_ALIASES.get(token.strip().lower(), token.strip())


def parse_content_rule(raw: str) -> ContentRule | None:
    """Parse a ``Tool(content)`` rule string. Returns None if blank/invalid."""
    s = raw.strip()
    if not s or s.startswith("#"):
        return None
    match = _RULE_RE.match(s)
    if not match:
        return None
    tool_token, content = match.group(1), match.group(2)
    tool_glob = _resolve_tool(tool_token)
    if content is None:
        return ContentRule(tool_glob=tool_glob, content=None, raw=raw)
    content = content.strip()
    if content == "":
        return ContentRule(tool_glob=tool_glob, content=None, raw=raw)
    return ContentRule(tool_glob=tool_glob, content=content, raw=raw)


def parse_content_rules(raw_rules: list[str]) -> list[ContentRule]:
    """Parse a list of rule strings, skipping blanks/invalid ones."""
    parsed: list[ContentRule] = []
    for raw in raw_rules:
        rule = parse_content_rule(raw)
        if rule is not None:
            parsed.append(rule)
    return parsed


def _is_command_tool(tool_name: str) -> bool:
    return any(fnmatch(tool_name, pattern) for pattern in _COMMAND_TOOL_GLOBS)


def _is_file_tool(tool_name: str) -> bool:
    return any(fnmatch(tool_name, pattern) for pattern in _FILE_TOOL_GLOBS)


def _tool_rule_matches(tool_name: str, pattern: str) -> bool:
    normalized_pattern = str(pattern or "").strip()
    if fnmatch(tool_name, normalized_pattern):
        return True
    return bool(
        normalized_pattern.startswith("mcp__")
        and "*" not in normalized_pattern
        and tool_name.startswith(f"{normalized_pattern}__")
    )


def _extract_content(tool_name: str, args: dict[str, Any] | None) -> str:
    if not args:
        return ""
    if _is_command_tool(tool_name):
        return str(args.get("command") or args.get("cmd") or "")
    if _is_file_tool(tool_name):
        return str(
            args.get("file_path") or args.get("path") or args.get("directory") or ""
        )
    return ""


def command_prefix_uses_unsafe_wrapper(prefix: str) -> bool:
    """Reject wrapper prefixes even when the binary is path-qualified."""
    first = str(prefix or "").strip().split(maxsplit=1)[0].strip("'\"")
    if not first:
        return False
    binary = re.split(r"[/\\]", first)[-1].lower()
    return binary in _UNSAFE_COMMAND_WRAPPERS


def _content_pattern_matches(pattern: str, value: str, *, is_command: bool) -> bool:
    """Match a content pattern against the extracted value.

    For commands, ``prefix:*`` is the cc prefix syntax (startswith). Everything
    else (commands and paths) uses fnmatch globbing — fnmatch's ``*`` spans path
    separators, which is appropriately permissive for permission rules.
    """
    if not value:
        return False
    if is_command and _contains_unquoted_shell_control(value.strip()):
        return False
    if is_command and pattern.endswith(":*"):
        prefix = pattern[:-2].strip()
        command = value.strip()
        # A persistent command allow must describe one simple command.  Raw
        # startswith let `git status:*` approve `git status && rm -rf ...` and
        # even `git statusevil`; neither is part of the approved command.
        return (
            bool(prefix)
            and (command == prefix or (command.startswith(prefix) and command[len(prefix):len(prefix) + 1].isspace()))
        )
    # Collapse ``**`` so ``src/**`` behaves like ``src/*`` under fnmatch.
    glob = pattern.replace("/**", "/*").replace("**", "*")
    return fnmatch(value, glob)


def _contains_unquoted_shell_control(command: str) -> bool:
    """Whether a shell chain/redirection escapes a remembered simple command."""
    quote = ""
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if char in {"'", '"'}:
            quote = "" if quote == char else (char if not quote else quote)
            continue
        if quote:
            continue
        if char in {";", "|", "&", "`", "\n", "<", ">"}:
            return True
        if char == "$" and index + 1 < len(command) and command[index + 1] == "(":
            return True
    return False


def rule_matches_call(rule: ContentRule, tool_name: str, args: dict[str, Any] | None) -> bool:
    """True if ``rule`` matches a tool call (tool name + content)."""
    if not _tool_rule_matches(tool_name, rule.tool_glob):
        return False
    if rule.content is None:
        return True  # whole-tool rule
    value = _extract_content(tool_name, args)
    if not value:
        return False
    return _content_pattern_matches(rule.content, value, is_command=_is_command_tool(tool_name))
