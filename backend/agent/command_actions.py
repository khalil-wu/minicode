"""Command presentation from Codex's shell-command parser, independent of status.

Reference: codex-rs/shell-command/src/{bash,parse_command}.rs at 822e58cc.
This is presentation metadata, never permission or execution authorization.
PowerShell intentionally recognizes only the source's simple file-read form.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
import shlex
from pathlib import Path, PureWindowsPath
from typing import Any

import tree_sitter
import tree_sitter_bash

from backend.secret_redaction import redact_secrets


def _short_path(path: str) -> str:
    return path.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] or path


def _operands(args: list[str], value_flags: tuple[str, ...] = ()) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(args):
        word = args[index]
        if word == "--":
            result.extend(args[index + 1:])
            break
        if word in value_flags:
            index += 2
            continue
        if not word.startswith("-"):
            result.append(word)
        index += 1
    return result


def _join_path(cwd: str, path: str) -> str:
    if PureWindowsPath(cwd).drive or PureWindowsPath(path).drive:
        return ntpath.normpath(ntpath.join(cwd, path)).replace("\\", "/")
    return posixpath.normpath(posixpath.join(cwd, path))


def _read(command: str, path: str, cwd: str) -> dict[str, str]:
    return {"type": "read", "command": command, "name": _short_path(path),
            "path": _join_path(cwd, path)}


def _unknown(command: str) -> list[dict[str, str]]:
    return [{"type": "unknown", "command": redact_secrets(command)}]


def _plain_commands(script: str) -> list[list[str]] | None:
    # The same word-only AST boundary as Codex bash.rs. In particular, nested
    # control flow, redirection, substitutions and glob expansions are Unknown.
    source = script.encode("utf-8")
    tree = tree_sitter.Parser(tree_sitter.Language(tree_sitter_bash.language())).parse(source)
    if tree.root_node.has_error:
        return None
    allowed = {"program", "list", "pipeline", "command", "command_name", "word",
               "string", "string_content", "raw_string", "number", "concatenation"}
    punctuation = {"&&", "||", ";", "|", '"', "'"}
    nodes = [tree.root_node]
    commands = []
    while nodes:
        node = nodes.pop()
        if node.is_named:
            if node.type not in allowed:
                return None
            if node.type in {"word", "number"}:
                word = source[node.start_byte:node.end_byte].decode("utf-8")
                if node.named_child_count or word.startswith("=") or any(char in word for char in "{}*?[]\\~^#$`"):
                    return None
            if node.type == "string":
                raw = source[node.start_byte:node.end_byte].decode("utf-8")
                if re.search(r'\\[$`"\\\n]', raw[1:-1]):
                    return None
            if node.type == "command":
                commands.append(node)
        elif node.type not in punctuation and node.type.strip():
            return None
        nodes.extend(node.children)
    result = []
    for node in sorted(commands, key=lambda item: item.start_byte):
        words = []
        for word in node.named_children:
            raw = source[word.start_byte:word.end_byte].decode("utf-8")
            # AST has already rejected substitutions. Codex also rejects double
            # quoted escape removal: it must not turn source text into argv.
            if word.type == "string" and re.search(r'\\[$`"\\\n]', raw[1:-1]):
                return None
            if word.type == "command_name" and word.named_children[0].type != "word":
                return None
            pieces = shlex.split(raw)
            if len(pieces) != 1:
                return None
            words.append(pieces[0])
        result.append(words)
    return result


def _sed_in_place(args: list[str]) -> bool:
    index = 0
    while index < len(args):
        word = args[index]
        if word == "--":
            break
        if word in {"-e", "-f", "--expression", "--file"}:
            index += 2
            continue
        if word == "--in-place" or word.startswith("--in-place="):
            return True
        if word.startswith("-") and not word.startswith("--"):
            for offset, option in enumerate(word[1:]):
                if option == "i":
                    return True
                if option in {"e", "f"}:
                    if offset == len(word) - 2:
                        index += 1
                    break
        index += 1
    return False


def _sed_path(args: list[str]) -> str | None:
    # sed -n numeric print ranges are reads; -i mutations remain Unknown.
    if _sed_in_place(args):
        return None
    if "-n" not in args:
        return None
    expressions = [args[index + 1] for index, arg in enumerate(args[:-1]) if arg in {"-e", "--expression"}]
    operands = _operands(args, ("-e", "--expression", "-f", "--file"))
    if not expressions and operands:
        expressions = [operands.pop(0)]
    return operands[0] if operands and any(re.fullmatch(r"[0-9]+(?:,[0-9]+)?p", value) for value in expressions) else None


def _awk_path(args: list[str]) -> str | None:
    operands = _operands(args, ("-F", "-v", "-f", "--field-separator", "--assign", "--file"))
    if any(flag in args for flag in ("-f", "--file")):
        return operands[0] if operands else None
    return operands[1] if len(operands) > 1 else None


def _formatting(tokens: list[str]) -> bool:
    head, *args = tokens
    if head in {"wc", "tr", "cut", "sort", "uniq", "tee", "column", "yes", "printf"}:
        return True
    if head == "awk":
        return _awk_path(args) is None
    if head == "sed":
        return _sed_path(args) is None and not _sed_in_place(args)
    if head in {"head", "tail"}:
        return not args or (len(args) == 1 and args[0].startswith("-")) or (len(args) == 2 and args[0] in {"-n", "-c"} and args[1].lstrip("+").isdigit())
    if head == "xargs":
        index = 0
        while index < len(args):
            word = args[index]
            if word == "--":
                index += 1
                break
            if not word.startswith("-"):
                break
            index += 2 if word in {"-E", "-e", "-I", "-L", "-n", "-P", "-s"} else 1
        subcommand = args[index:]
        if not subcommand:
            return True
        subhead, *subargs = subcommand
        mutating = _sed_in_place(subargs) if subhead == "sed" else any(arg.startswith(("-i", "-pi", "--in-place")) for arg in subargs) if subhead in {"perl", "ruby"} else "--replace" in subargs if subhead == "rg" else False
        return not mutating
    return False


def _grep_action(tokens: list[str], args: list[str], command: str) -> dict[str, str]:
    pattern = None
    operands = []
    index = 0
    while index < len(args):
        word = args[index]
        if word == "--":
            operands.extend(args[index + 1:])
            break
        if word in {"-e", "--regexp", "-f", "--file"}:
            if index + 1 < len(args) and pattern is None:
                pattern = args[index + 1]
            index += 2
            continue
        if word in {"-m", "--max-count", "-C", "--context", "-A", "--after-context", "-B", "--before-context"}:
            index += 2
            continue
        if not word.startswith("-"):
            operands.append(word)
        index += 1
    path_index = 0 if pattern is not None else 1
    query = pattern if pattern is not None else (operands[0] if operands else None)
    return {"type": "search", "command": command,
            **({"query": query} if query is not None else {}),
            **({"path": _short_path(operands[path_index])} if len(operands) > path_index else {})}


def _summarize(tokens: list[str], cwd: str) -> dict[str, str]:
    head, *args = tokens
    command = shlex.join(tokens)
    operands: list[str]
    if head.casefold() == "get-content":
        if all(not arg.startswith("-") or arg.casefold() in {"-raw", "-path", "-literalpath"} for arg in args):
            operands = _operands(args)
            if len(operands) == 1 and operands[0] and all(char.isalnum() or char in " /\\.-_:" for char in operands[0]):
                return _read(command, operands[0], cwd)
    elif head in {"cat", "bat", "batcat", "less", "more"}:
        value_flags = {"bat": ("--theme", "--language", "--style", "--terminal-width", "--tabs", "--line-range", "--map-syntax"),
                       "batcat": ("--theme", "--language", "--style", "--terminal-width", "--tabs", "--line-range", "--map-syntax"),
                       "less": ("-p", "-P", "-x", "-y", "-z", "-j", "--pattern", "--prompt", "--tabs", "--shift", "--jump-target")}
        operands = _operands(args, value_flags.get(head, ()))
        if len(operands) == 1:
            return _read(command, operands[0], cwd)
    elif head in {"head", "tail"}:
        if args and args[0] == "-n" and len(args) > 2 and (args[1].lstrip("+") if head == "tail" else args[1]).isdigit():
            return _read(command, args[2], cwd)
        if args and re.fullmatch(r"-n\+?[0-9]+" if head == "tail" else r"-n[0-9]+", args[0]) and len(args) > 1:
            return _read(command, args[1], cwd)
        if len(args) == 1 and not args[0].startswith("-"):
            return _read(command, args[0], cwd)
    elif head in {"nl", "awk", "sed"}:
        path = _sed_path(args) if head == "sed" else _awk_path(args) if head == "awk" else next(iter(_operands(args, ("-s", "-w", "-v", "-i", "-b"))), None)
        if path is not None:
            return _read(command, path, cwd)
    elif head in {"ls", "eza", "exa", "tree", "du"}:
        flags = {"ls": ("-I", "-w", "--block-size", "--format", "--time-style", "--color", "--quoting-style"),
                 "eza": ("-I", "--ignore-glob", "--color", "--sort", "--time-style", "--time"),
                 "exa": ("-I", "--ignore-glob", "--color", "--sort", "--time-style", "--time"),
                 "tree": ("-L", "-P", "-I", "--charset", "--filelimit", "--sort"),
                 "du": ("-d", "--max-depth", "-B", "--block-size", "--exclude", "--time-style")}
        operands = _operands(args, flags[head])
        return {"type": "listFiles", "command": command, **({"path": _short_path(operands[0])} if operands else {})}
    elif head in {"rg", "rga", "ripgrep-all", "ag", "ack", "pt"}:
        flags = ("-g", "--glob", "--iglob", "-t", "--type", "--type-add", "--type-not", "-m", "--max-count", "-A", "-B", "-C", "--context", "--max-depth") if head in {"rg", "rga", "ripgrep-all"} else ("-G", "-g", "--file-search-regex", "--ignore-dir", "--ignore-file", "--path-to-ignore")
        operands = _operands(args, flags)
        if "--files" in args and head in {"rg", "rga", "ripgrep-all"}:
            return {"type": "listFiles", "command": command, **({"path": _short_path(operands[0])} if operands else {})}
        return {"type": "search", "command": command, **({"query": operands[0]} if operands else {}), **({"path": _short_path(operands[1])} if len(operands) > 1 else {})}
    elif head in {"grep", "egrep", "fgrep"} or (head == "git" and args[:1] == ["grep"]):
        return _grep_action(tokens, args[1:] if head == "git" else args, command)
    elif head == "git" and args[:1] == ["ls-files"]:
        operands = _operands(args[1:], ("--exclude", "--exclude-from", "--pathspec-from-file"))
        return {"type": "listFiles", "command": command, **({"path": _short_path(operands[0])} if operands else {})}
    elif head in {"fd", "find"}:
        operands = _operands(args, ("-t", "--type", "-e", "--extension", "-E", "--exclude", "--search-path"))
        if head == "fd":
            query = operands[0] if operands and (len(operands) > 1 or not (operands[0] in {".", ".."} or "/" in operands[0] or "\\" in operands[0])) else None
            path = operands[1] if len(operands) > 1 else operands[0] if operands and query is None else None
        else:
            path = next((arg for arg in args if not arg.startswith("-") and arg not in {"!", "(", ")"}), None)
            query = next((args[index + 1] for index, arg in enumerate(args[:-1]) if arg in {"-name", "-iname", "-path", "-regex"}), None)
        return {"type": "search" if query is not None else "listFiles", "command": command, **({"query": query} if query is not None else {}), **({"path": _short_path(path)} if path is not None else {})}
    elif re.fullmatch(r"python(?:[23](?:\.[0-9]+)?)?", head) and "-c" in args:
        index = args.index("-c")
        if index + 1 < len(args) and any(marker in args[index + 1] for marker in ("os.walk", "os.listdir", "os.scandir", "glob.glob", "glob.iglob", "pathlib.Path", ".rglob(")):
            return {"type": "listFiles", "command": command}
    return {"type": "unknown", "command": command}


def parse_command_actions(command: str, cwd: str, *, powershell: bool) -> list[dict[str, str]]:
    if powershell:
        normalized = command.replace("\\", "/")
        try:
            tokens = shlex.split(normalized)
        except ValueError:
            tokens = normalized.split()
        if tokens and tokens[0].casefold() in {"get-content", "gc", "type"}:
            args = tokens[1:]
            if all(not arg.startswith("-") or arg.casefold() in {"-raw", "-path", "-literalpath"} for arg in args):
                operands = _operands(args)
                if len(operands) == 1 and operands[0] and all(char.isalnum() or char in " /\\.-_:" for char in operands[0]) and all(token in normalized for token in tokens):
                    return [_read(redact_secrets(command), operands[0], cwd)]
        return _unknown(command)
    commands = _plain_commands(command)
    if not commands:
        return _unknown(command)
    actions = []
    effective_cwd = cwd
    for tokens in commands:
        if _formatting(tokens):
            continue
        if tokens[0] == "cd":
            paths = _operands(tokens[1:])
            if paths:
                effective_cwd = _join_path(effective_cwd, paths[-1])
            continue
        actions.append(_summarize(tokens, effective_cwd))
    while len(actions) > 1 and actions[0]["type"] == "unknown" and actions[0]["command"].startswith("echo "):
        actions.pop(0)
    if len(actions) > 1:
        actions = [
            action for action in actions
            if not (
                action["type"] == "unknown"
                and (
                    action["command"] == "true"
                    or (
                        action["command"].startswith("nl ")
                        and all(token.startswith("-") for token in shlex.split(action["command"])[1:])
                    )
                )
            )
        ]
    if not actions or any(action["type"] == "unknown" for action in actions):
        return _unknown(command)
    if len(actions) == 1 and actions[0]["type"] == "read" and "|" in command and any(tokens[:2] == ["sed", "-n"] for tokens in commands):
        actions[0]["command"] = command
    deduped = []
    for action in actions:
        public_action = {key: redact_secrets(value) for key, value in action.items()}
        if not deduped or deduped[-1] != public_action:
            deduped.append(public_action)
    return deduped


def command_actions_for_tool(tool_name: str, args: dict[str, Any], workspace_root: Any = None) -> list[dict[str, str]]:
    if tool_name != "run_command" or not isinstance(args.get("command"), str):
        return []
    workspace = Path(workspace_root or Path.cwd()).expanduser().resolve()
    requested = Path(str(args["cwd"]).strip()).expanduser() if args.get("cwd") else workspace
    cwd = requested.resolve() if requested.is_absolute() else (workspace / requested).resolve()
    from backend.tools.command_support import command_shell_argv

    shell_argv = command_shell_argv("", shell=args.get("shell"), login=False)
    shell_name = Path(shell_argv[0]).stem.lower()
    if shell_name in {"powershell", "pwsh"}:
        return parse_command_actions(args["command"], str(cwd), powershell=True)
    if shell_name in {"bash", "zsh", "sh"}:
        return parse_command_actions(args["command"], str(cwd), powershell=False)
    return _unknown(args["command"])
