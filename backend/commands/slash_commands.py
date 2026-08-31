from __future__ import annotations

import logging
import re
import shlex
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from backend.commands.catalog import get_composer_command_catalog

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession


logger = logging.getLogger(__name__)

SlashHandler = Callable[["WebSocketSession", str, Any], Awaitable[tuple[bool, str]]]

_PERMISSION_MODE_TOKENS = {"plan", "confirm", "auto", "bypass"}

_EFFORT_ALIASES = {
    "none": "none",
    "minimal": "minimal",
    "min": "minimal",
    "low": "low",
    "medium": "medium",
    "med": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "x-high": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
    "max": "max",
    "maximum": "max",
    "ultra": "ultra",
}

_PERMISSION_LEVEL_ALIASES = {
    "confirm": "confirm",
    "diff": "diff",
    "diff_review": "diff",
    "diffreview": "diff",
    "deny": "deny",
    "always_deny": "deny",
    "block": "deny",
}

_MEMORY_MODE_ALIASES = {
    "enabled": "enabled",
    "enable": "enabled",
    "on": "enabled",
    "disabled": "disabled",
    "disable": "disabled",
    "off": "disabled",
}


def _split_args(arg: str) -> list[str]:
    raw = str(arg or "").strip()
    if not raw:
        return []
    try:
        # CC uses shell-quote parsing so quoted command arguments remain one
        # indexed value. shlex provides the same quoting behavior here; on an
        # incomplete quote both implementations fall back to whitespace split.
        return [token for token in shlex.split(raw, posix=True) if token]
    except ValueError:
        return [token for token in raw.split() if token]


def _normalize_permission_mode(token: str) -> str | None:
    normalized = str(token or "").strip().lower()
    return normalized if normalized in _PERMISSION_MODE_TOKENS else None


def _normalize_permission_level(token: str) -> str | None:
    return _PERMISSION_LEVEL_ALIASES.get(str(token or "").strip().lower())


def _normalize_memory_mode(token: str) -> str | None:
    return _MEMORY_MODE_ALIASES.get(str(token or "").strip().lower())


def _normalize_effort(token: str) -> str | None:
    normalized = str(token or "").strip().lower()
    if not normalized:
        return None
    # Codex accepts model-defined effort strings. The config service still
    # rejects any value the active model did not explicitly advertise.
    return _EFFORT_ALIASES.get(normalized, normalized)


async def _handle_conversation_export(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = arg, attachments
    from backend.ws.handlers.conversation import handle_conversation_export

    await handle_conversation_export(
        ws,
        {"conversation_id": ws.active_conversation_id},
    )
    return True, ""


def _active_conversation_id(ws: "WebSocketSession") -> str | None:
    if ws.active_conversation_id:
        return ws.active_conversation_id
    ws._ensure_active_conversation()
    return ws.active_conversation_id


async def _dispatch_command(
    ws: "WebSocketSession", command_type: str, payload: dict[str, Any]
) -> bool:
    return await ws.command_registry.dispatch(command_type, payload)


async def _emit_usage_warning(ws: "WebSocketSession", command: str, usage: str) -> None:
    await ws.emit_command_result(command, usage, level="warning")


async def _emit_command_unavailable(ws: "WebSocketSession", command: str) -> None:
    await ws.emit_command_result(
        command,
        f"Command '/{command}' is unavailable in this runtime.",
        level="error",
    )


async def _apply_permission_mode(
    ws: "WebSocketSession",
    *,
    command: str,
    mode: str,
    source: str,
) -> bool:
    handled = await _dispatch_command(
        ws,
        "conversation.permission_mode.set",
        {
            "mode": mode,
            "source": source,
        },
    )
    if not handled:
        await _emit_command_unavailable(ws, command)
        return False
    await ws.emit_command_result(
        command,
        f"Permission mode set to '{mode}'.",
        data={"mode": mode},
    )
    return True


async def _handle_new(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = attachments
    tokens = _split_args(arg)
    if tokens:
        await _emit_usage_warning(ws, "new", "Usage: /new")
        return True, ""

    handled = await _dispatch_command(
        ws,
        "conversation.create",
        {
            "source": "slash:/new",
        },
    )
    if not handled:
        await _emit_command_unavailable(ws, "new")
        return True, ""

    await ws.emit_command_result(
        "new",
        "Started a new conversation.",
    )
    return True, ""


async def _handle_clear(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = arg
    _ = attachments
    handled = await _dispatch_command(
        ws,
        "conversation.clear",
        {
            "conversation_id": getattr(ws, "active_conversation_id", ""),
            "source": "slash:/clear",
        },
    )
    if not handled:
        await _emit_command_unavailable(ws, "clear")
        return True, ""

    # handle_conversation_clear owns the outcome: it emits its own success and
    # returns True on five distinct failure branches too. Emitting success here
    # overwrote a refusal in the durable activity trace (both entries share the
    # id "command-result-clear", and appendAgentProgress replaces in place).
    return True, ""


async def _handle_permissions(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    tokens = _split_args(arg)
    if not tokens:
        await _dispatch_command(
            ws,
            "session.permissions.inspect",
            {"source": "slash:/permissions"},
        )
        return True, ""

    first = tokens[0].lower()
    maybe_mode = _normalize_permission_mode(first)
    if maybe_mode is not None:
        remainder = " ".join(tokens[1:]).strip()
        success = await _apply_permission_mode(
            ws,
            command="permissions",
            mode=maybe_mode,
            source="slash:/permissions",
        )
        if not success:
            return True, ""
        if remainder or attachments:
            return False, remainder
        return True, ""

    if first != "rules":
        await _emit_usage_warning(
            ws,
            "permissions",
            "Usage: /permissions [default|confirm|auto] | /permissions rules [list|add|remove]",
        )
        return True, ""

    if len(tokens) == 1 or tokens[1].lower() == "list":
        payload: dict[str, Any] = {"source": "slash:/permissions"}
        conversation_id = _active_conversation_id(ws)
        if conversation_id:
            payload["conversation_id"] = conversation_id
        await _dispatch_command(ws, "conversation.permission.rules.list", payload)
        return True, ""

    action = tokens[1].lower()
    if action == "add":
        if len(tokens) < 4:
            await _emit_usage_warning(
                ws,
                "permissions",
                "Usage: /permissions rules add deny <pattern> | /permissions rules add override <pattern> <confirm|diff|deny>",
            )
            return True, ""

        kind = tokens[2].lower()
        conversation_id = _active_conversation_id(ws)
        payload = {
            "rule_kind": kind,
            "source": "slash:/permissions",
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id

        if kind == "deny":
            pattern = " ".join(tokens[3:]).strip()
            if not pattern:
                await _emit_usage_warning(
                    ws,
                    "permissions",
                    "Usage: /permissions rules add deny <pattern>",
                )
                return True, ""
            payload["pattern"] = pattern
            await _dispatch_command(ws, "conversation.permission.rules.add", payload)
            return True, ""

        if kind == "override":
            if len(tokens) < 5:
                await _emit_usage_warning(
                    ws,
                    "permissions",
                    "Usage: /permissions rules add override <pattern> <confirm|diff|deny>",
                )
                return True, ""
            level = _normalize_permission_level(tokens[4])
            if level is None:
                await _emit_usage_warning(
                    ws,
                    "permissions",
                    "Usage: /permissions rules add override <pattern> <confirm|diff|deny>",
                )
                return True, ""
            payload["pattern"] = tokens[3]
            payload["level"] = level
            await _dispatch_command(ws, "conversation.permission.rules.add", payload)
            return True, ""

        await _emit_usage_warning(
            ws,
            "permissions",
            "Usage: /permissions rules add deny <pattern> | /permissions rules add override <pattern> <confirm|diff|deny>",
        )
        return True, ""

    if action == "remove":
        if len(tokens) < 4:
            await _emit_usage_warning(
                ws,
                "permissions",
                "Usage: /permissions rules remove <deny|override> <pattern>",
            )
            return True, ""
        kind = tokens[2].lower()
        if kind not in {"deny", "override"}:
            await _emit_usage_warning(
                ws,
                "permissions",
                "Usage: /permissions rules remove <deny|override> <pattern>",
            )
            return True, ""
        pattern = " ".join(tokens[3:]).strip()
        if not pattern:
            await _emit_usage_warning(
                ws,
                "permissions",
                "Usage: /permissions rules remove <deny|override> <pattern>",
            )
            return True, ""

        payload: dict[str, Any] = {
            "rule_kind": kind,
            "pattern": pattern,
            "source": "slash:/permissions",
        }
        conversation_id = _active_conversation_id(ws)
        if conversation_id:
            payload["conversation_id"] = conversation_id
        await _dispatch_command(ws, "conversation.permission.rules.remove", payload)
        return True, ""

    await _emit_usage_warning(
        ws,
        "permissions",
        "Usage: /permissions rules [list|add|remove]",
    )
    return True, ""


async def _handle_memory(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = attachments
    conversation_id = _active_conversation_id(ws)
    if not conversation_id:
        await _emit_command_unavailable(ws, "memory")
        return True, ""

    memory_mode = "enabled"
    tokens = _split_args(arg)
    if tokens:
        normalized = _normalize_memory_mode(tokens[0])
        if normalized is None:
            await _emit_usage_warning(ws, "memory", "Usage: /memory [enabled|disabled]")
            return True, ""
        memory_mode = normalized

    handled = await _dispatch_command(
        ws,
        "conversation.memory_mode.set",
        {
            "conversation_id": conversation_id,
            "memory_mode": memory_mode,
            "source": "slash:/memory",
        },
    )
    if not handled:
        await _emit_command_unavailable(ws, "memory")
        return True, ""

    await ws.emit_command_result(
        "memory",
        f"Conversation memory mode set to '{memory_mode}'.",
        data={"memory_mode": memory_mode},
    )
    return True, ""


async def _handle_archive(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = arg
    _ = attachments
    conversation_id = ws.active_conversation_id
    if not conversation_id:
        await _emit_command_unavailable(ws, "archive")
        return True, ""
    handled = await _dispatch_command(
        ws,
        "conversation.archive",
        {
            "conversation_id": conversation_id,
            "source": "slash:/archive",
        },
    )
    if not handled:
        await _emit_command_unavailable(ws, "archive")
        return True, ""
    await ws.emit_command_result(
        "archive",
        "Archived the current conversation.",
        data={"conversation_id": conversation_id},
    )
    return True, ""


async def _handle_unarchive(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = arg
    _ = attachments
    conversation_id = ws.active_conversation_id
    if not conversation_id:
        await _emit_command_unavailable(ws, "unarchive")
        return True, ""
    handled = await _dispatch_command(
        ws,
        "conversation.unarchive",
        {
            "conversation_id": conversation_id,
            "source": "slash:/unarchive",
        },
    )
    if not handled:
        await _emit_command_unavailable(ws, "unarchive")
        return True, ""
    await ws.emit_command_result(
        "unarchive",
        "Unarchived the current conversation.",
        data={"conversation_id": conversation_id},
    )
    return True, ""


async def _handle_tasks(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = arg
    _ = attachments
    await _dispatch_command(
        ws,
        "session.tasks.inspect",
        {"source": "slash:/tasks"},
    )
    return True, ""


async def _handle_status(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = arg
    _ = attachments
    await _dispatch_command(
        ws,
        "session.status.inspect",
        {"source": "slash:/status"},
    )
    return True, ""


async def _handle_effort(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = attachments
    tokens = _split_args(arg)
    if not tokens:
        await _emit_usage_warning(ws, "effort", "Usage: /effort [none|minimal|low|medium|high|xhigh|max|ultra]")
        return True, ""
    effort = _normalize_effort(tokens[0])
    if effort is None:
        await _emit_usage_warning(ws, "effort", "Usage: /effort [none|minimal|low|medium|high|xhigh|max|ultra]")
        return True, ""
    from backend.config import active_provider_reasoning_effort_levels

    declared_levels = active_provider_reasoning_effort_levels()
    if effort not in declared_levels:
        await _dispatch_command(
            ws,
            "llm.config.set",
            {
                "reasoning_effort": effort,
                "source": "slash:/effort",
            },
        )
        await ws.emit_command_result(
            "effort",
            "Reasoning effort was not applied because the active model "
            f"did not declare the '{effort}' level.",
            level="warning",
            data={"reasoning_effort": effort, "applied": False},
        )
        return True, ""
    handled = await _dispatch_command(
        ws,
        "llm.config.set",
        {
            "reasoning_effort": effort,
            "source": "slash:/effort",
        },
    )
    if not handled:
        await _emit_command_unavailable(ws, "effort")
        return True, ""
    await ws.emit_command_result(
        "effort",
        f"Reasoning effort set to '{effort}'.",
        data={"reasoning_effort": effort},
    )
    return True, ""


async def _handle_usage(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = arg
    _ = attachments
    await _dispatch_command(
        ws,
        "session.usage.inspect",
        {"source": "slash:/usage"},
    )
    return True, ""


async def _handle_context(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = arg
    _ = attachments
    # Focused view of the context/token budget (opens the usage inspector with
    # a focus hint the frontend can use to land on the context section).
    await _dispatch_command(
        ws,
        "session.usage.inspect",
        {"source": "slash:/context", "focus": "context"},
    )
    return True, ""


async def _handle_cost(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = arg
    _ = attachments
    await _dispatch_command(
        ws,
        "session.usage.inspect",
        {"source": "slash:/cost", "focus": "cost"},
    )
    return True, ""


async def _handle_help(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = arg
    _ = attachments

    enabled_entries = [
        entry
        for entry in get_composer_command_catalog()
        if bool(entry.get("enabled", True))
    ]
    local_commands = sorted(
        {
            str(entry.get("command", "")).strip().lower()
            for entry in enabled_entries
            if str(entry.get("type", "")).strip().lower() == "local"
            and str(entry.get("command", "")).strip()
        }
    )
    template_commands = sorted(
        {
            str(entry.get("command", "")).strip().lower()
            for entry in enabled_entries
            if str(entry.get("type", "")).strip().lower() == "template"
            and str(entry.get("command", "")).strip()
        }
    )

    message_lines = [
        "Catalog-backed slash commands:",
        "Local: " + (", ".join(f"/{name}" for name in local_commands) if local_commands else "(none)"),
        "Templates: "
        + (", ".join(f"/{name}" for name in template_commands) if template_commands else "(none)"),
        "Examples:",
        "  /review Inspect recent changes",
        "  /permissions rules add deny run_in_terminal",
        "  /permissions rules add override write_file confirm",
    ]
    await ws.emit_command_result(
        "help",
        "\n".join(message_lines),
        data={
            "local_commands": local_commands,
            "template_commands": template_commands,
        },
    )
    return True, ""


async def _handle_skills(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = attachments
    skill_name = str(arg or "").strip()
    if skill_name:
        return False, f"${skill_name}"

    await _dispatch_command(ws, "skills.list", {"source": "slash:/skills"})
    await _dispatch_command(ws, "skills.marketplace.list", {"source": "slash:/skills"})
    await ws.emit_command_result(
        "skills",
        "Opening Skills browser.",
        data={"ui_action": "open_skills_marketplace"},
    )
    return True, ""


async def _handle_skill(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = attachments
    skill_name = str(arg or "").strip()
    if skill_name:
        return False, f"${skill_name}"
    await _dispatch_command(ws, "skills.list", {"source": "slash:/skill"})
    await ws.emit_command_result("skill", "Choose a skill from the composer menu.")
    return True, ""


async def _handle_plan(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    success = await _apply_permission_mode(
        ws,
        command="plan",
        mode="plan",
        source="slash:/plan",
    )
    if not success:
        return True, ""
    remainder = str(arg or "").strip()
    if remainder or attachments:
        return False, remainder
    return True, ""


def _settings_handler(command: str, tab: str) -> SlashHandler:
    async def _handler(
        ws: "WebSocketSession", arg: str, attachments: Any
    ) -> tuple[bool, str]:
        _ = arg, attachments
        await ws.emit_command_result(
            command,
            f"Opening {command} settings.",
            data={"ui_action": f"open_settings:{tab}"},
        )
        return True, ""

    return _handler


async def _handle_compact(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = attachments
    from backend.ws.handlers.conversation import handle_context_compact

    await handle_context_compact(
        ws,
        {"focus": str(arg or "").strip(), "source": "slash:/compact"},
    )
    return True, ""


async def _handle_goal(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = attachments
    raw = str(arg or "").strip()
    conversation_id = _active_conversation_id(ws)
    if not conversation_id:
        await _emit_command_unavailable(ws, "goal")
        return True, ""

    action = "set"
    text = raw
    if not raw:
        action = "show"
        text = ""
    else:
        first = raw.split(maxsplit=1)[0].strip().lower()
        if first in {"show", "status", "inspect", "pause", "resume", "clear", "delete", "reset"}:
            action = first
            text = raw.split(maxsplit=1)[1].strip() if len(raw.split(maxsplit=1)) > 1 else ""

    handled = await _dispatch_command(
        ws,
        "conversation.goal.set",
        {
            "conversation_id": conversation_id,
            "action": action,
            "text": text,
            "source": "slash:/goal",
        },
    )
    if not handled:
        await _emit_command_unavailable(ws, "goal")
    return True, ""


async def _handle_resume(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    """Resume from the latest checkpoint (if any)."""
    _ = arg, attachments
    from backend.services.checkpoint_service import (
        CheckpointServiceError,
        prepare_run_checkpoint_resume,
    )

    try:
        resume = prepare_run_checkpoint_resume(
            session_id=str(ws.session_id or ""),
            requested_conversation_id=_active_conversation_id(ws),
            active_conversation_id=_active_conversation_id(ws),
        )
    except (CheckpointServiceError, ValueError) as exc:
        await ws.emit_command_result(
            "resume",
            str(exc),
            level="error",
        )
        return True, ""

    if resume is None:
        await ws.emit_command_result(
            "resume",
            "No incomplete checkpoint found for this conversation. The last task completed successfully or no checkpoint exists yet.",
            level="info",
        )
        return True, ""

    await ws.emit_command_result(
        "resume",
        f"Resuming from iteration {resume.iteration}. Previous stop: {resume.stopped_reason}",
        level="success",
    )

    await ws.start_agent_run(
        resume.user_message,
        conversation_id=resume.conversation_id,
        metadata={
            "resume_from_checkpoint": True,
            "resume_checkpoint_run_id": resume.run_id,
            "conversation_id": resume.conversation_id,
        },
    )
    return True, ""


_LOCAL_COMMAND_HANDLERS: dict[str, SlashHandler] = {
    "export": _handle_conversation_export,
    "plan": _handle_plan,
    "new": _handle_new,
    "clear": _handle_clear,
    "effort": _handle_effort,
    "permissions": _handle_permissions,
    "memory": _handle_memory,
    "archive": _handle_archive,
    "unarchive": _handle_unarchive,
    "tasks": _handle_tasks,
    "status": _handle_status,
    "usage": _handle_usage,
    "context": _handle_context,
    "cost": _handle_cost,
    "help": _handle_help,
    "skill": _handle_skill,
    "skills": _handle_skills,
    "model": _settings_handler("model", "provider"),
    "mcp": _settings_handler("mcp", "connectors"),
    "plugins": _settings_handler("plugins", "plugins"),
    "compact": _handle_compact,
    "goal": _handle_goal,
    "resume": _handle_resume,
}

def _build_local_handler(command_name: str) -> SlashHandler:
    async def _handler(
        ws: "WebSocketSession", arg: str, attachments: Any
    ) -> tuple[bool, str]:
        handler = _LOCAL_COMMAND_HANDLERS.get(command_name)
        if handler is None:
            await _emit_command_unavailable(ws, command_name)
            return True, ""
        return await handler(ws, arg, attachments)

    return _handler


# MiniCode's own token for the directory a SKILL.md was loaded from. This is the
# only spelling MiniCode emits or documents.
SKILL_DIR_TOKEN = "${MINICODE_SKILL_DIR}"

def _build_template_handler(
    command_name: str,
    template: str,
    argument_names: list[str] | None = None,
    *,
    base_dir: str = "",
    is_skill_file: bool = False,
) -> SlashHandler:
    base_template = str(template or "").strip()
    skill_dir = str(base_dir or "").strip()
    # Skill directories are rendered with POSIX separators even on Windows so
    # relative references in SKILL.md stay portable across the model/tool
    # boundary.
    if is_skill_file:
        skill_dir = skill_dir.replace("\\", "/")
    if is_skill_file and skill_dir:
        base_template = f"Base directory for this skill: {skill_dir}\n\n{base_template}"
    named_arguments = [str(name) for name in (argument_names or []) if str(name)]
    async def _handler(
        ws: "WebSocketSession", arg: str, attachments: Any
    ) -> tuple[bool, str]:
        _ = attachments
        if not base_template:
            await _emit_command_unavailable(ws, command_name)
            return True, ""
        extra = str(arg or "").strip()
        prompt = _substitute_command_arguments(
            base_template,
            extra,
            argument_names=named_arguments,
        )
        if skill_dir:
            prompt = _substitute_skill_dir(prompt, skill_dir)
        await ws.emit_command_result(
            command_name,
            f"Prepared template prompt for '/{command_name}'.",
        )
        return False, prompt

    return _handler


def _substitute_skill_dir(prompt: str, skill_dir: str) -> str:
    """Expand MiniCode's canonical skill-directory token."""
    return prompt.replace(SKILL_DIR_TOKEN, skill_dir)


def _substitute_command_arguments(
    template: str,
    arguments: str,
    *,
    argument_names: list[str] | None = None,
) -> str:
    """Apply CC's $ARGUMENTS, indexed, and shorthand substitutions."""
    raw = str(arguments or "").strip()
    original_template = template
    values = _split_args(raw)

    def indexed(match: Any) -> str:
        index = int(match.group(1))
        return values[index] if index < len(values) else ""

    content = template
    had_placeholder = bool(
        re.search(r"\$ARGUMENTS(?:\[\d+\])?", original_template)
        or re.search(r"(?<![A-Za-z0-9_])\$\d+\b", original_template)
        or any(
            re.search(rf"\${re.escape(name)}(?![\[\w])", original_template)
            for name in (argument_names or [])
            if name and not name.isdecimal()
        )
    )
    for index, name in enumerate(argument_names or []):
        if not name or name.isdecimal():
            continue
        value = values[index] if index < len(values) else ""
        content = re.sub(
            rf"\${re.escape(name)}(?![\[\w])",
            lambda _match, replacement=value: replacement,
            content,
        )
    content = re.sub(r"\$ARGUMENTS\[(\d+)\]", indexed, content)
    content = re.sub(r"(?<![A-Za-z0-9_])\$(\d+)\b", indexed, content)
    content = content.replace("$ARGUMENTS", raw)
    if raw and not had_placeholder:
        content = f"{content.rstrip()}\n\nARGUMENTS: {raw}"
    return content.strip()


def _build_protocol_handler(entry: dict[str, Any]) -> SlashHandler:
    command_name = str(entry.get("command", "")).strip().lower()
    protocol_command = str(
        entry.get("protocol_command")
        or entry.get("command_type")
        or entry.get("handler")
        or ""
    ).strip()
    base_payload = dict(entry.get("payload") or {}) if isinstance(entry.get("payload"), dict) else {}
    arg_key = str(entry.get("arg_key") or "").strip()
    plugin_name = str(entry.get("plugin_name") or "").strip()

    async def _handler(
        ws: "WebSocketSession", arg: str, attachments: Any
    ) -> tuple[bool, str]:
        _ = attachments
        if not protocol_command:
            await _emit_command_unavailable(ws, command_name)
            return True, ""

        payload = dict(base_payload)
        payload.setdefault("source", f"slash:/{command_name}")
        if plugin_name:
            payload.setdefault("plugin_name", plugin_name)
        payload.setdefault("command", command_name)
        raw_arg = str(arg or "").strip()
        if raw_arg:
            payload[arg_key or "arg"] = raw_arg

        handled = await _dispatch_command(ws, protocol_command, payload)
        if not handled:
            await _emit_command_unavailable(ws, command_name)
        return True, ""

    return _handler


def register_all_slash_commands(registry: Any) -> None:
    """Register slash commands from the composer command catalog."""

    for entry in get_composer_command_catalog():
        if not bool(entry.get("enabled", True)):
            continue

        command_name = str(entry.get("command", "")).strip().lower()
        if not command_name:
            continue

        command_type = str(entry.get("type", "")).strip().lower()
        slash_name = f"/{command_name}"

        if command_type == "local":
            registry.register_slash(slash_name, _build_local_handler(command_name))
            continue

        if command_type == "template":
            registry.register_slash(
                slash_name,
                _build_template_handler(
                    command_name,
                    str(entry.get("template", "")),
                    list(entry.get("argument_names") or []),
                    base_dir=str(entry.get("base_dir") or ""),
                    is_skill_file=bool(entry.get("is_skill_file")),
                ),
            )
            continue

        if command_type == "protocol":
            registry.register_slash(slash_name, _build_protocol_handler(entry))



def refresh_slash_commands(registry: Any) -> None:
    clear = getattr(registry, "clear_slash_handlers", None)
    if callable(clear):
        clear()
    register_all_slash_commands(registry)
