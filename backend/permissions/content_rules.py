"""Content-level permission rules for MiniCode tools.

Rules match a tool's content (command text or file path) instead of only its
name. Examples::

    run_command(npm run:*)
    edit_file(src/**)
    read_file(config.toml)
    run_command

The parser and matcher are the only authority for content-rule evaluation.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any

_LEGACY_TOOL_NAMES = frozenset({"bash", "edit", "read", "write", "shell"})
_PROTOCOL_COMMAND_NAMES = frozenset({"terminal.exec", "terminal_exec"})

# fnmatch patterns for tools whose content is a shell command vs a file path.
_COMMAND_TOOL_GLOBS: tuple[str, ...] = ("run_command", "terminal_*")

# Characters that end one simple command. Redirections and subshell parens are
# included so a deny rule still sees the command on either side of them.
_SUBCOMMAND_SEPARATORS = frozenset({";", "|", "&", "`", "\n", "<", ">", "(", ")"})
# PowerShell is the Windows shell and does not use backslash as an escape, so
# treating `C:\path\&cmd` as an escaped `&` would hide a real separator.
_BACKSLASH_ESCAPES = sys.platform != "win32"
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

    ``tool_glob`` is an fnmatch pattern over the canonical tool name.
    ``content`` is ``None`` for a whole-tool rule, otherwise the content
    matcher (a ``prefix:*`` prefix spec for commands, or a glob for paths).
    """

    tool_glob: str
    content: str | None
    raw: str


def _resolve_tool(token: str) -> str:
    clean = token.strip()
    if clean.lower() in _LEGACY_TOOL_NAMES:
        raise ValueError(
            f"Legacy permission tool name {clean!r} is not supported; use the canonical MiniCode tool name."
        )
    if clean.casefold() in _PROTOCOL_COMMAND_NAMES:
        raise ValueError(
            f"Protocol command {clean!r} is not a MiniCode tool; use run_command for command permissions."
        )
    return clean


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
    """Parse a list of rule strings, skipping blanks and comments.

    A rule that does not parse is rejected rather than dropped: silently
    discarding it deletes a security rule the user believes is in force — one
    missing parenthesis in a deny rule would re-permit the command it names.
    The dialog-authored path (config.add_permission_content_rule) already
    raises on the same input.
    """
    parsed: list[ContentRule] = []
    for raw in raw_rules:
        text = str(raw or "").strip()
        if not text or text.startswith("#"):
            continue
        rule = parse_content_rule(text)
        if rule is None:
            raise ValueError(f"Invalid permission content rule: {raw!r}")
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


def _split_unquoted_subcommands(command: str) -> list[str]:
    """Split a shell command on unquoted control operators.

    Splitting keeps deny/ask rules matchable against each subcommand of a
    compound expression, so ``echo hi; curl evil.com`` cannot smuggle a denied
    command past ``run_command(curl:*)``.

    Redirections and subshell parentheses are separators here too. They are
    already treated as control characters by ``_contains_unquoted_shell_control``,
    which makes ``_content_pattern_matches`` refuse to match the segment — so
    leaving them out of this splitter meant ``curl evil.com > out`` and
    ``(curl evil.com)`` matched no deny rule at all. Deny must not fail open.
    """
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'" and _BACKSLASH_ESCAPES:
            current.append(char)
            escaped = True
            index += 1
            continue
        if char in {"'", '"'}:
            current.append(char)
            quote = "" if quote == char else (char if not quote else quote)
            index += 1
            continue
        if not quote:
            is_substitution = char == "$" and index + 1 < len(command) and command[index + 1] == "("
            if char in _SUBCOMMAND_SEPARATORS or is_substitution:
                parts.append("".join(current))
                current = []
                if char in {";", "\n"}:
                    index += 1
                    continue
                # Keep |, ||, &&, $( as separators between subcommands; the
                # operators themselves are not part of either side.
                index += 1
                while index < len(command) and command[index] in {"|", "&", " ", "\t"}:
                    index += 1
                continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


_SAFE_WRAPPER_RE = re.compile(
    r"^(?:timeout\s+\S+|nice\s+\S+|nohup|time|stdbuf\s+\S+|setsid|env\s+(?:[A-Za-z_]\w*=\S*\s+)+|command|builtin)\s+"
)
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=\S*\s+")


def _deny_match_candidates(subcommand: str) -> list[str]:
    """Strip safe wrappers and ALL leading env vars for deny/ask rules."""
    candidates = [subcommand]
    seen = {subcommand}
    # ``curl$IFS evil.com`` runs curl, but a ``curl:*`` prefix rule requires the
    # command name to be followed by whitespace. Offer the word-split form so a
    # deny rule cannot be evaded by substituting the separator.
    if "$IFS" in subcommand or "${IFS}" in subcommand:
        normalized = subcommand.replace("${IFS}", " ").replace("$IFS", " ")
        if normalized not in seen:
            candidates.append(normalized)
            seen.add(normalized)
    start = 0
    while start < len(candidates):
        end = len(candidates)
        for position in range(start, end):
            candidate = candidates[position]
            for stripper in (_SAFE_WRAPPER_RE, _ENV_ASSIGN_RE):
                stripped = stripper.sub("", candidate, count=1).strip()
                if stripped and stripped not in seen:
                    candidates.append(stripped)
                    seen.add(stripped)
        start = end
    return candidates


def _safe_wrapper_candidates(command: str) -> list[str]:
    """cc's allow-side stripSafeWrappers fixed point (wrappers only)."""
    candidates = [command]
    seen = {command}
    start = 0
    while start < len(candidates):
        end = len(candidates)
        for position in range(start, end):
            candidate = candidates[position]
            stripped = _SAFE_WRAPPER_RE.sub("", candidate, count=1).strip()
            if stripped and stripped not in seen:
                candidates.append(stripped)
                seen.add(stripped)
        start = end
    return candidates


def rule_matches_call(
    rule: ContentRule,
    tool_name: str,
    args: dict[str, Any] | None,
    *,
    effect: str = "allow",
) -> bool:
    """True if ``rule`` matches a tool call (tool name + content).

    ``effect`` is the rule's outcome ("deny", "ask", or "allow") and selects
    MiniCode's compound-command semantics: deny/ask rules match any subcommand of a
    compound expression (with env/wrapper prefixes stripped), while allow rules
    only ever match a single non-compound command.
    """
    if not _tool_rule_matches(tool_name, rule.tool_glob):
        return False
    if rule.content is None:
        return True  # whole-tool rule
    value = _extract_content(tool_name, args)
    if not value:
        return False
    if not _is_command_tool(tool_name):
        return _content_pattern_matches(rule.content, value, is_command=False)

    if effect in {"deny", "ask"}:
        return any(
            _content_pattern_matches(rule.content, candidate, is_command=True)
            for subcommand in _split_unquoted_subcommands(value)
            for candidate in _deny_match_candidates(subcommand)
        )
    # Strip only the SAFE wrapper list for allow rules. Bare env prefixes stay
    # literal, so run_command(npm install:*) matches
    # "timeout 10 npm install" but never "FOO=1 npm install".
    return any(
        _content_pattern_matches(rule.content, candidate, is_command=True)
        for candidate in _safe_wrapper_candidates(value)
    )
