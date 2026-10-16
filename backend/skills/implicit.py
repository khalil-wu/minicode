"""Source implicit Skill invocation recognition before admitted execution."""
from __future__ import annotations

from dataclasses import dataclass
import ntpath
from pathlib import Path

from backend.agent.command_actions import parse_command_actions
from backend.agent.guardian.skills import verified_skill_path
from backend.workspace.executor_paths import ExecutorPath


@dataclass(frozen=True)
class ImplicitSkillInvocation:
    name: str
    resource: str
    environment_id: str
    source_kind: str
    scope: str | None = None


def _accesses(command, cwd: ExecutorPath, *, powershell: bool):
    from backend.permissions.powershell_ast import parse_literal_commands as parse_powershell
    from backend.permissions.shell_ast import parse_literal_commands as parse_shell

    parsed = (parse_powershell if powershell else parse_shell)(command)
    script = None
    if parsed is not None and parsed.commands:
        tokens = parsed.commands[0]
        runner = ntpath.basename(tokens[0]).casefold().removesuffix(".exe") if tokens else ""
        if runner in {"python", "python3", "bash", "zsh", "sh", "node", "deno", "ruby", "perl", "pwsh"}:
            candidate = next((token for token in tokens[1:] if token != "--" and not token.startswith("-")), "")
            if candidate.casefold().endswith((".py", ".sh", ".js", ".ts", ".rb", ".pl", ".ps1")):
                script = candidate
    if script is not None:
        yield "script", cwd.joinpath(script)
    for action in parse_command_actions(command, cwd.native, powershell=powershell):
        if action["type"] == "read":
            yield "document", cwd.joinpath(action["path"])


def detect_implicit_skill_invocation(skill_manager, command: str, cwd_uri: str, *, environment_id: str,
    platform_os: str | None) -> ImplicitSkillInvocation | None:
    cwd = ExecutorPath.parse(cwd_uri, platform_os=platform_os)
    accesses = tuple(_accesses(command, cwd, powershell=platform_os == "windows"))
    if environment_id == "local":
        metas = skill_manager.list_metas()
        for kind, access in accesses:
            candidate = Path(access.native).resolve()
            for meta in metas:
                main = meta.source_path.resolve()
                matches = candidate == main if kind == "document" else candidate.is_relative_to((main.parent / "scripts").resolve())
                if matches:
                    scope = {"workspace": "repo", "builtin": "system", "managed": "admin"}.get(meta.source_level, "user")
                    return ImplicitSkillInvocation(meta.name, str(main), environment_id, "host", scope)
    catalog = skill_manager.executor_catalog
    if catalog is not None:
        for kind, access in accesses:
            for entry in catalog.entries:
                if not entry.enabled or entry.environment_id != environment_id:
                    continue
                main = ExecutorPath.parse(entry.main_path, platform_os=platform_os)
                if access == main if kind == "document" else access.is_relative_to(main.parent.joinpath("scripts")):
                    return ImplicitSkillInvocation(entry.name, entry.main_resource, environment_id, "executor")
    return None


def admit_implicit_skill_invocation(*, tool_name: str, args: dict, tool_context, context_builder) -> ImplicitSkillInvocation | None:
    if tool_name != "run_command" or tool_context.run_context is None or tool_context.run_context.skill_manager is None:
        return None
    environment = tool_context.turn_environment
    if environment is not None:
        environment_id = environment.selection.environment_id
        platform_os = environment.peer_info.get("platformOs")
        cwd = ExecutorPath.resolve(args.get("cwd") or environment.selection.cwd,
            cwd_uri=environment.selection.cwd, platform_os=platform_os)
    else:
        import os

        environment_id = tool_context.environment_id
        if environment_id != "local":
            return None
        platform_os = "windows" if os.name == "nt" else "linux"
        root = tool_context.workspace_root or Path.cwd()
        cwd = ExecutorPath.resolve(args.get("cwd") or str(root), cwd_uri=Path(root).resolve().as_uri(), platform_os=platform_os)
    manager = tool_context.run_context.skill_manager
    invocation = detect_implicit_skill_invocation(manager, args["command"], cwd.uri,
        environment_id=environment_id, platform_os=platform_os)
    if invocation is None or not tool_context.turn_id:
        return invocation
    if manager.record_implicit_invocation(tool_context.turn_id, invocation):
        if invocation.source_kind == "host":
            verified = verified_skill_path(invocation.resource)
            if verified is not None:
                context_builder.guardian_evidence.record_trusted_skill(tool_context.turn_id, verified)
    return invocation
