"""Turn-scoped skill selection and lifecycle projection."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.feature_flags import FeatureFlags
from backend.skills.manager import SkillManager
from backend.skills.loader import SkillMeta
from backend.permissions.context import PermissionContext
from backend.sandbox.policy import ResolvedSandboxPolicy, SandboxEnforcement

def implicit_skill_for_tool(
    skill_manager: SkillManager,
    tool_name: str,
    args: dict[str, Any],
    workspace_root: Path | None,
) -> SkillMeta | None:
    root = Path(workspace_root or Path.cwd()).resolve()
    paths: list[tuple[str, bool]] = []
    if tool_name == "read_file":
        paths.append((str(args.get("file_path") or ""), False))
    elif tool_name == "run_command":
        from backend.permissions.powershell_ast import parse_literal_commands as parse_powershell
        from backend.permissions.shell_ast import parse_literal_commands as parse_shell

        command = str(args.get("command") or "")
        parsed = (parse_powershell if os.name == "nt" else parse_shell)(command)
        if parsed is None:
            return None
        raw_cwd = str(args.get("cwd") or "").strip()
        if raw_cwd:
            cwd = Path(raw_cwd)
            root = (cwd if cwd.is_absolute() else root / cwd).resolve()
        readers = {"cat", "type", "get-content", "gc"}
        runners = {"python", "python3", "bash", "sh", "zsh", "node", "deno", "ruby", "perl", "pwsh"}
        for argv in parsed.commands:
            if not argv:
                continue
            command_name = Path(argv[0]).name.casefold().removesuffix(".exe")
            if command_name in readers:
                paths.extend((word, False) for word in argv[1:] if word.casefold().endswith("skill.md"))
            elif command_name in runners:
                script = next((word for word in argv[1:] if not word.startswith("-")), "")
                if script.casefold().endswith((".py", ".sh", ".js", ".ts", ".rb", ".pl", ".ps1")):
                    paths.append((script, True))
    for raw_path, is_script in paths:
        if not raw_path:
            continue
        candidate = Path(raw_path)
        path = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if not is_script:
            meta = skill_manager.get_meta_by_path(path)
            if meta is not None:
                return meta
            continue
        for meta in skill_manager.list_metas():
            if path.is_relative_to(meta.source_path.parent.resolve() / "scripts"):
                return meta
    return None


async def activate_turn_skills(
    skill_manager: SkillManager,
    user_message: str,
    state: AgentState,
    mcp_manager: Any | None = None,
    approval_handler: Any | None = None,
    publish_event: Any | None = None,
    *,
    permission_context: PermissionContext,
    permission_profile: ResolvedSandboxPolicy | None,
    permission_scope: Any | None = None,
    prompted_dependencies: set[str] | None = None,
    feature_flags: FeatureFlags | None = None,
    originator: str = "minicode",
) -> AsyncIterator[AgentEvent]:
    """Select textual skill workflows without granting executable hook authority."""
    prompted = prompted_dependencies if prompted_dependencies is not None else set()
    selected_skills = state.prompt_context.get("selected_skills", []) if isinstance(state.prompt_context, dict) else []
    detections = skill_manager.detect(
        user_message,
        selected_skills=selected_skills,
    )
    for detection in detections:
        name = detection.name
        trigger_mode = detection.trigger_mode
        source_path = detection.source_path
        if detection.unavailable_reason:
            yield AgentEvent(
                type="system_notice",
                data={"content": f"Skill '{name}' is unavailable at {source_path}: {detection.unavailable_reason}"},
            )
            continue
        payload = skill_manager.load_skill_payload(name, source_path=source_path or None)
        if payload is not None:
            for warning in payload.get("warnings", []):
                yield AgentEvent(type="system_notice", data={"content": warning})
            missing_dependencies = [
                server for server in payload.get("mcp_dependencies", [])
                if mcp_manager is None or mcp_manager.get_client(server) is None
            ]
            first_party = originator in {"minicode-desktop", "codex_cli_rs", "codex-tui", "codex_vscode"} or originator.startswith("Codex ")
            if missing_dependencies and mcp_manager is not None and first_party and (feature_flags or FeatureFlags()).enabled("skill_mcp_dependency_install"):
                from backend.services.mcp_service import MCPServiceError, _manual_config_from_payload, install_skill_mcp_servers

                specs = {
                    str(spec.get("value") or "").strip(): spec
                    for spec in payload.get("mcp_dependency_specs", [])
                }
                candidates: list[dict[str, Any]] = []
                descriptions: list[str] = []
                candidate_keys: list[str] = []
                for server in missing_dependencies:
                    if mcp_manager.get_server_config(server) is not None:
                        continue
                    spec = specs.get(server)
                    if spec is None:
                        continue
                    transport = str(spec.get("transport") or "streamable_http").strip().lower()
                    if transport == "streamable_http" and str(spec.get("url") or "").strip():
                        address = str(spec["url"]).strip()
                        candidate = {"name": server, "transport": "http", "url": address, "auto_start": True}
                        callback_port = spec.get("oauth_callback_port")
                        if callback_port is not None:
                            candidate["oauth"] = {"callback_port": callback_port}
                        identifier = address
                    elif transport == "stdio" and str(spec.get("command") or "").strip():
                        command = str(spec["command"]).strip()
                        candidate = {"name": server, "transport": "stdio", "command": command, "args": [], "auto_start": True}
                        identifier = command
                    else:
                        continue
                    dependency_key = f"mcp__{transport}__{identifier}"
                    if dependency_key in prompted:
                        continue
                    if not mcp_manager.allows_user_server_install(_manual_config_from_payload(candidate)):
                        continue
                    candidates.append(candidate)
                    descriptions.append(server)
                    candidate_keys.append(dependency_key)
                if candidates:
                    # codex-mcp::mcp_permission_prompt_is_auto_approved with
                    # the default (no per-tool approval override) context.
                    full_access = (permission_scope.has_full_disk_write_access() if permission_scope is not None
                        else permission_profile is not None and (
                            permission_profile.enforcement in {SandboxEnforcement.DISABLED, SandboxEnforcement.EXTERNAL}
                            or permission_profile.full_disk_write))
                    install = permission_context.approval_policy == "never" and full_access
                    if not install and permission_context.approval_policy != "never" and approval_handler is not None and publish_event is not None:
                        request_id = f"skill-mcp-{uuid4().hex}"
                        await publish_event(AgentEvent(
                            type="request_user_input",
                            data={
                                "tool_call_id": request_id,
                                "isBlocking": True,
                                "questions": [{
                                    "id": "skill_mcp_dependency_install",
                                    "header": "Install MCP servers?",
                                    "question": "The following MCP servers are required by the selected skills but are not installed yet: "
                                        + ", ".join(sorted(descriptions)) + ". Install them now?",
                                    "isOther": False,
                                    "isSecret": False,
                                    "options": [
                                        {"label": "Install", "description": "Install and enable the missing MCP servers in your global config."},
                                        {"label": "Continue anyway", "description": "Skip installation for now and do not show again for these MCP servers in this session."},
                                    ],
                                }],
                            },
                        ))
                        prompted.update(candidate_keys)
                        answer = await approval_handler(request_id)
                        install = "Install" in answer.get("answers", {}).get("skill_mcp_dependency_install", {}).get("answers", [])
                    if install:
                        try:
                            await install_skill_mcp_servers(mcp_manager, candidates)
                        except MCPServiceError as exc:
                            yield AgentEvent(
                                type="system_notice",
                                data={"content": f"Could not install MCP dependencies for Skill '{name}': {exc}"},
                            )
                        missing_dependencies = [
                            server for server in payload.get("mcp_dependencies", [])
                            if mcp_manager.get_client(server) is None
                        ]
            if missing_dependencies:
                names = ", ".join(missing_dependencies)
                payload["content"] += (
                    f"\n\nAt skill activation, MCP dependency unavailable: {names}. "
                    "These servers are not connected; do not assume their tools or resources are available."
                )
                yield AgentEvent(
                    type="system_notice",
                    data={"content": f"Skill '{name}' requires MCP server(s) that are not connected: {names}."},
                )
            pending = state.prompt_context.setdefault("skill_injections", [])
            if isinstance(pending, list) and not any(
                isinstance(item, dict) and item.get("path") == payload.get("path")
                for item in pending
            ):
                pending.append(payload)
            if name not in state.active_skills:
                state.active_skills.append(name)
        elif trigger_mode == "explicit":
            yield AgentEvent(
                type="system_notice",
                data={"content": f"Failed to load Skill '{name}' from {source_path or 'the Skill catalog'}."},
            )
