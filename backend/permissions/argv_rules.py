"""Dangerous-command rules over resolved argv.

These rules run on the literal commands recovered by
:mod:`backend.permissions.shell_ast`. Each rule receives a single command's
argv with quoting already resolved and shell composition already stripped, so
it can reason about ``argv[0]`` and its flags instead of scanning text. The
string-level patterns in :mod:`backend.permissions.checker` remain the floor
when a script cannot be parsed.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable

from backend.permissions.shell_ast import DYNAMIC_WORD

_DELETE_COMMANDS = frozenset({"rm", "del", "erase", "rmdir", "rd", "unlink", "shred"})
_SYSTEM_ROOT_DIRS = frozenset({"etc", "usr", "var", "bin", "sbin", "System", "Library"})
_ACCOUNT_ROOT_DIRS = frozenset({"Users", "home"})
_DD_SYSTEM_TARGETS = frozenset({"dev", "etc", "boot", "proc", "sys", "System", "Library", "Users", "home"})

# Executables that merely run another command. The prefix is stripped so the
# wrapped command is judged by its own name.
_TRANSPARENT_PREFIXES = frozenset({"nohup", "time", "command", "exec", "builtin", "caffeinate"})
_PREFIXES_WITH_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "xargs": frozenset({"-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a", "--max-args", "--max-procs", "--delimiter", "--arg-file"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "doas": frozenset({"-u", "-C"}),
}


def executable_name(raw: str) -> str:
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    if len(name) > 2 and name[1] == ":" and name[0].isalpha():
        name = name[2:]
    lowered = name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def strip_transparent_prefixes(argv: list[str]) -> list[str]:
    """Drop ``nohup``/``timeout 5``/``xargs -0`` style prefixes."""
    current = list(argv)
    for _ in range(8):
        if not current:
            return current
        name = executable_name(current[0])
        if name in _TRANSPARENT_PREFIXES:
            current = current[1:]
            continue
        value_options = _PREFIXES_WITH_VALUE_OPTIONS.get(name)
        if value_options is None:
            return current
        index = 1
        if name == "timeout":
            # ``timeout [options] DURATION COMMAND``: the first non-option
            # word is the duration.
            duration_seen = False
        while index < len(current):
            argument = current[index]
            if argument == "--":
                index += 1
                break
            if argument.startswith("-"):
                option = argument.split("=", 1)[0]
                if option in value_options and "=" not in argument:
                    index += 2
                else:
                    index += 1
                continue
            if name == "timeout" and not duration_seen:
                duration_seen = True
                index += 1
                continue
            break
        current = current[index:]
    return current


def _short_flag_chars(args: Iterable[str]) -> str:
    chars: list[str] = []
    for argument in args:
        if argument == "--":
            break
        if argument.startswith("-") and not argument.startswith("--") and len(argument) > 1:
            chars.append(argument[1:])
    return "".join(chars)


def _long_flags(args: Iterable[str]) -> set[str]:
    flags: set[str] = set()
    for argument in args:
        if argument == "--":
            break
        if argument.startswith("--"):
            flags.add(argument.split("=", 1)[0])
    return flags


def _operands(args: list[str]) -> list[str]:
    operands: list[str] = []
    passthrough = False
    for argument in args:
        if passthrough:
            operands.append(argument)
        elif argument == "--":
            passthrough = True
        elif not argument.startswith("-"):
            operands.append(argument)
    return operands


def _normalize_posix_target(target: str) -> str:
    collapsed = re.sub(r"/+", "/", target)
    if not collapsed.startswith("/"):
        return collapsed
    return posixpath.normpath(collapsed)


def _rm_target_reason(target: str) -> str:
    if target in {"~", "~/"} or target.startswith("~/") and target.count("/") == 1 and not target[2:]:
        return "recursive delete of home directory"
    if target.startswith(("%USERPROFILE%", "$HOME", "${HOME}")):
        rest = target.split("}", 1)[-1] if target.startswith("${HOME}") else target[len("$HOME") if target.startswith("$HOME") else len("%USERPROFILE%"):]
        if rest in {"", "/", "\\"}:
            return "recursive delete of home directory"
    if not target.startswith("/"):
        return ""
    if target.endswith("/*") and _normalize_posix_target(target[:-2] or "/") == "/":
        return "recursive delete of root filesystem"
    normalized = _normalize_posix_target(target)
    if normalized == "/":
        return "recursive delete of root filesystem"
    parts = normalized.strip("/").split("/")
    if parts[0] in _SYSTEM_ROOT_DIRS:
        return "recursive delete of system directory"
    if parts[0] in _ACCOUNT_ROOT_DIRS and len(parts) <= 2:
        return "recursive delete of system directory"
    return ""


_WINDOWS_DRIVE_ROOT = re.compile(r"^[A-Za-z]:[\\/]*$")
_WINDOWS_USER_HOME = re.compile(r"^[A-Za-z]:[\\/]+Users[\\/]+[^\\/]+[\\/]*$", re.IGNORECASE)
_WINDOWS_SYSTEM_DIR = re.compile(r"^[A-Za-z]:[\\/]+(?:Windows|Program Files(?: \(x86\))?)(?:[\\/]|$)", re.IGNORECASE)
_POWERSHELL_DELETE_ALIASES = frozenset({"remove-item", "ri", "rd", "rmdir", "rm", "del", "erase"})
_PROCESS_KILL_CMDLETS = frozenset({"stop-process", "spps", "kill"})


def _windows_target_reason(target: str) -> str:
    if target.lower() in {"$home", "$env:userprofile", "%userprofile%", "~"}:
        return "recursive delete of home directory"
    if _WINDOWS_DRIVE_ROOT.match(target):
        return "recursive delete of drive root"
    if _WINDOWS_USER_HOME.match(target):
        return "recursive delete of system directory"
    if _WINDOWS_SYSTEM_DIR.match(target):
        return "delete Windows system directory"
    return ""


def _powershell_flag_set(args: list[str]) -> set[str]:
    return {argument.split(":", 1)[0].lower() for argument in args if argument.startswith("-")}


def _powershell_reason(name: str, args: list[str]) -> str:
    """Rules for cmdlets and their aliases as PowerShell would resolve them."""
    if name == "remove-item" or name == "ri" or (name in {"rm", "rd", "rmdir", "del", "erase"} and any(a.startswith("-") and a.lower() not in {"-rf", "-r", "-f", "-fr"} for a in args)):
        flags = _powershell_flag_set(args)
        recursive = bool(flags & {"-recurse", "-r"}) or any(a.lower() == "-recurse:$true" for a in args)
        for target in _operands(args):
            reason = _windows_target_reason(target)
            if reason and (recursive or "root" in reason):
                return reason
        return ""
    if name in {"del", "erase"}:
        flags = {a.lower() for a in args}
        if "/s" in flags:
            for target in _operands([a for a in args if not a.startswith("/")]):
                reason = _windows_target_reason(target)
                if reason:
                    return reason
        return ""
    if name in {"rd", "rmdir"}:
        flags = {a.lower() for a in args}
        if "/s" in flags:
            for target in [a for a in args if not a.startswith("/")]:
                reason = _windows_target_reason(target)
                if reason:
                    return reason
        return ""
    if name == "format":
        return "drive format"
    if name in _PROCESS_KILL_CMDLETS:
        flags = _powershell_flag_set(args)
        if flags & {"-name", "-n", "-processname"}:
            return "process-name termination is not scoped to an owned background command"
        return ""
    if name == "taskkill":
        if any(a.lower() == "/im" for a in args):
            return "taskkill image-name termination is not scoped to an owned background command"
        return ""
    if name == "invoke-expression" or name == "iex":
        return "dynamic script evaluation"
    return ""


def catastrophic_reason(argv: list[str]) -> str:
    """Return why *argv* must never run unapproved, or ``""``."""
    argv = strip_transparent_prefixes(argv)
    if not argv:
        return ""
    name = executable_name(argv[0])
    args = argv[1:]

    windows_reason = _powershell_reason(name, args)
    if windows_reason:
        return windows_reason
    if name == "rm":
        for target in _operands(args):
            reason = _rm_target_reason(target)
            if reason:
                return reason
        return ""
    if name.startswith("mkfs"):
        return "filesystem format"
    if name == "dd":
        for argument in args:
            if argument.startswith("of="):
                target = _normalize_posix_target(argument[3:])
                if target.startswith("/") and target.strip("/").split("/")[0] in _DD_SYSTEM_TARGETS:
                    return "raw system-file write"
        return ""
    if name == "find":
        return _find_reason(args)
    if name == "git":
        return _git_reason(args)
    if name == "kubectl" and args[:1] == ["delete"]:
        return "destructive Kubernetes delete"
    if name == "terraform" and args[:1] == ["destroy"]:
        return "destructive Terraform destroy"
    if name in {"drop", "truncate"} and args[:1] and args[0].lower() in {"table", "database", "schema"}:
        return "destructive database operation"
    if name == "pkill":
        return "pkill is not scoped to an owned background command"
    if name == "killall":
        return "killall is not scoped to an owned background command"
    return ""


def _find_has_dynamic_action(args: list[str]) -> bool:
    """A dynamic word in predicate position may expand to ``-delete``.

    Values of predicates that take one (``-name "$pat"``) are ordinary data.
    """
    # Starting points come before the first predicate and are plain paths.
    # ``find "$dir" -type f`` is the common spelling, so leading dynamic
    # words are paths; a dynamic word after a literal path may be a predicate.
    index = 0
    while index < len(args) and args[index] == DYNAMIC_WORD:
        index += 1
    while index < len(args) and not args[index].startswith("-") and args[index] not in {"(", "!", DYNAMIC_WORD}:
        index += 1
    while index < len(args):
        argument = args[index]
        if argument in _FIND_VALUE_PREDICATES:
            index += 2
            continue
        if argument in {"-exec", "-execdir", "-ok", "-okdir"}:
            index += 1
            while index < len(args) and args[index] not in {";", "+"}:
                index += 1
            index += 1
            continue
        if argument == DYNAMIC_WORD:
            return True
        index += 1
    return False


_FIND_VALUE_PREDICATES = frozenset({
    "-name", "-iname", "-path", "-ipath", "-regex", "-iregex", "-wholename", "-iwholename",
    "-type", "-xtype", "-size", "-mtime", "-atime", "-ctime", "-mmin", "-amin", "-cmin",
    "-newer", "-newermt", "-user", "-group", "-uid", "-gid", "-perm", "-maxdepth", "-mindepth",
    "-links", "-inum", "-samefile", "-fstype", "-printf", "-fprint", "-fprintf", "-fls",
})


def _find_reason(args: list[str]) -> str:
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == "-delete":
            return "find delete operation"
        if argument in {"-exec", "-execdir", "-ok", "-okdir"}:
            payload: list[str] = []
            index += 1
            while index < len(args) and args[index] not in {";", "+"}:
                payload.append(args[index])
                index += 1
            stripped = strip_transparent_prefixes(payload)
            if stripped and executable_name(stripped[0]) in _DELETE_COMMANDS:
                return "find recursive delete operation"
            if any(executable_name(word) in _DELETE_COMMANDS for word in payload):
                return "find recursive delete operation"
            if any(re.search(r"\b(?:rm|del|erase|rmdir|rd|unlink|shred)\b", word) for word in payload[1:]):
                return "find recursive delete operation"
        index += 1
    return ""


def _verb(name: str, args: list[str]) -> str:
    index = 0
    if name == "git":
        while index < len(args) and args[index].startswith("-"):
            index += 2 if args[index] in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"} else 1
    return args[index] if index < len(args) else ""


def _git_reason(args: list[str]) -> str:
    # Skip global options such as ``-C path`` or ``--git-dir=…`` before the verb.
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}:
            index += 2
        else:
            index += 1
    verb_args = args[index:]
    if not verb_args:
        return ""
    verb, rest = verb_args[0], verb_args[1:]
    short = _short_flag_chars(rest)
    long_flags = _long_flags(rest)
    if verb == "reset" and "--hard" in long_flags:
        return "destructive git reset"
    if verb == "clean":
        if "n" in short or "--dry-run" in long_flags:
            return ""
        if "f" in short or "--force" in long_flags:
            return "destructive git clean"
        return ""
    if verb in {"checkout", "restore"} and _operands(rest) == ["."]:
        return "destructive git working-tree restore"
    if verb == "stash" and rest[:1] and rest[0] in {"drop", "clear"}:
        return "destructive git stash removal"
    if verb == "branch" and ("D" in short or {"--delete", "--force"} <= long_flags):
        return "destructive git branch deletion"
    if verb == "push" and ("f" in short or "--force" in long_flags or "--force-with-lease" in long_flags):
        return "destructive forced git push"
    if verb == "commit" and "--amend" in long_flags:
        return "destructive git amend"
    return ""


def destructive_reason(argv: list[str]) -> str:
    """Return why *argv* discards data, or ``""``.

    Wider than :func:`catastrophic_reason`: a project-local ``rm -rf build`` is
    destructive (always confirmed) without being catastrophic (never
    auto-approved by a remembered rule).
    """
    catastrophic = catastrophic_reason(argv)
    if catastrophic:
        return catastrophic
    argv = strip_transparent_prefixes(argv)
    if not argv:
        return ""
    name = executable_name(argv[0])
    args = argv[1:]
    # A word that only exists after expansion can be any flag or path. For
    # commands that delete, the rules above could not see it, so the
    # invocation is treated as if it were the worst it could be.
    if DYNAMIC_WORD in args:
        if name in _DELETE_COMMANDS or name == "remove-item":
            return "delete command with arguments resolved at run time"
        if name == "find" and _find_has_dynamic_action(args):
            return "find action resolved at run time"
        if name in {"git", "kubectl", "terraform"} and _verb(name, args) == DYNAMIC_WORD:
            return f"{name} subcommand resolved at run time"
    if name == "rm":
        short = _short_flag_chars(args)
        if "r" in short.lower() or "f" in short or _long_flags(args) & {"--recursive", "--force"}:
            return "recursive or forced delete"
        return ""
    if name in {"remove-item", "ri"}:
        flags = _powershell_flag_set(args)
        if flags & {"-recurse", "-force", "-r"}:
            return "recursive or forced delete"
        return ""
    if name in {"del", "erase", "rmdir", "rd"}:
        return "delete command"
    if name == "format":
        return "drive format"
    return ""


def compound_destructive_reason(argv: list[str]) -> str:
    """Rules applied to every command of a multi-command script."""
    argv = strip_transparent_prefixes(argv)
    if not argv:
        return ""
    name = executable_name(argv[0])
    args = argv[1:]
    if name == "rm" and ("r" in _short_flag_chars(args).lower() or "--recursive" in _long_flags(args)):
        return "recursive delete hidden inside a compound command"
    if name in {"remove-item", "ri", "del", "erase", "rd", "rmdir"}:
        lowered = {argument.split(":", 1)[0].lower() for argument in args}
        if lowered & {"-recurse", "-r", "/s"}:
            return "recursive delete hidden inside a compound command"
        return ""
    if name == "git":
        reason = _git_reason(args)
        if reason:
            return "destructive git operation hidden inside a compound command"
        return ""
    if name == "find" and _find_reason(args):
        return "destructive find operation hidden inside a compound command"
    if name == "kubectl" and args[:1] == ["delete"] or name == "terraform" and args[:1] == ["destroy"]:
        return "destructive external operation hidden inside a compound command"
    if name in {"drop", "truncate"} and args[:1] and args[0].lower() in {"table", "database", "schema"}:
        return "destructive database operation hidden inside a compound command"
    return ""


_EXTERNAL_GIT_VERBS = frozenset({"push", "fetch", "pull", "clone"})
_EXTERNAL_GH_VERBS = frozenset({"api", "pr", "issue", "release", "repo", "workflow", "run"})
_EXTERNAL_NETWORK_TOOLS = frozenset({"curl", "wget", "invoke-webrequest", "invoke-restmethod", "ssh", "scp", "sftp", "rsync"})
_EXTERNAL_INFRA_TOOLS = frozenset({"docker", "podman", "kubectl", "helm", "terraform"})
_PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_PYTHON_PACKAGE_MANAGERS = frozenset({"pip", "conda", "mamba", "micromamba"})


def is_external(argv: list[str]) -> bool:
    """Whether *argv* reaches outside the workspace (network, registry, cluster)."""
    argv = strip_transparent_prefixes(argv)
    if not argv:
        return False
    name = executable_name(argv[0])
    args = argv[1:]
    if name == "git":
        index = 0
        while index < len(args) and args[index].startswith("-"):
            index += 2 if args[index] in {"-C", "-c", "--git-dir", "--work-tree"} else 1
        verb = args[index : index + 1]
        if verb and verb[0] in _EXTERNAL_GIT_VERBS:
            return True
        return verb == ["remote"] and args[index + 1 : index + 2] and args[index + 1] in {"add", "remove", "set-url"}
    if name == "gh":
        return bool(args[:1]) and args[0] in _EXTERNAL_GH_VERBS
    if name in _EXTERNAL_NETWORK_TOOLS or name in _EXTERNAL_INFRA_TOOLS:
        return True
    if name in _PACKAGE_MANAGERS:
        return bool(args[:1]) and args[0] in {"install", "add", "remove", "publish"}
    if name in _PYTHON_PACKAGE_MANAGERS or re.fullmatch(r"pip\d+", name):
        return bool(args[:1]) and args[0] in {"install", "uninstall", "remove", "create"}
    if name == "uv" and args[:1] == ["pip"]:
        return bool(args[1:2]) and args[1] in {"install", "uninstall"}
    return False
