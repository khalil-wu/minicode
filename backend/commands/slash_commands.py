from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from backend.commands.catalog import get_composer_command_catalog
from backend.skills.loader import SkillLoader

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession


SlashHandler = Callable[["WebSocketSession", str, Any], Awaitable[tuple[bool, str]]]

_PERMISSION_MODE_ALIASES = {
    "auto": "auto",
    "default": "default",
    "confirm": "confirm",
    "ask": "confirm",
    "ask_permissions": "confirm",
    "off": "default",
    "full_access": "bypass",
    "full-access": "bypass",
    "fullaccess": "bypass",
    "full access": "bypass",
    "danger_full_access": "bypass",
    "danger-full-access": "bypass",
    "dangerfullaccess": "bypass",
    "bypass": "bypass",
    "bypasspermissions": "bypass",
    "bypass_permissions": "bypass",
}

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
    "none": "none",
    "summary": "summary",
    "profile": "profile",
}


def _split_args(arg: str) -> list[str]:
    return [token for token in str(arg or "").strip().split() if token]


def _normalize_permission_mode(token: str) -> str | None:
    return _PERMISSION_MODE_ALIASES.get(str(token or "").strip().lower())


def _normalize_permission_level(token: str) -> str | None:
    return _PERMISSION_LEVEL_ALIASES.get(str(token or "").strip().lower())


def _normalize_memory_mode(token: str) -> str | None:
    return _MEMORY_MODE_ALIASES.get(str(token or "").strip().lower())


def _normalize_effort(token: str) -> str | None:
    return _EFFORT_ALIASES.get(str(token or "").strip().lower())


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
    await ws._emit_command_result(command, usage, level="warning")


async def _emit_command_unavailable(ws: "WebSocketSession", command: str) -> None:
    await ws._emit_command_result(
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
    await ws._emit_command_result(
        command,
        f"Permission mode set to '{mode}'.",
        data={"mode": mode},
    )
    return True


async def _handle_new(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = attachments
    memory_mode = "none"
    tokens = _split_args(arg)
    if tokens:
        normalized = _normalize_memory_mode(tokens[0])
        if normalized is None:
            await _emit_usage_warning(ws, "new", "Usage: /new [none|summary|profile]")
            return True, ""
        memory_mode = normalized

    handled = await _dispatch_command(
        ws,
        "conversation.create",
        {
            "memory_mode": memory_mode,
            "source": "slash:/new",
        },
    )
    if not handled:
        await _emit_command_unavailable(ws, "new")
        return True, ""

    await ws._emit_command_result(
        "new",
        f"Started a new conversation (memory mode: '{memory_mode}').",
        data={"memory_mode": memory_mode},
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

    await ws._emit_command_result(
        "clear",
        "Conversation history cleared.",
    )
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

    memory_mode = "none"
    tokens = _split_args(arg)
    if tokens:
        normalized = _normalize_memory_mode(tokens[0])
        if normalized is None:
            await _emit_usage_warning(ws, "memory", "Usage: /memory [none|summary|profile]")
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

    await ws._emit_command_result(
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
    await ws._emit_command_result(
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
    await ws._emit_command_result(
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
        await _emit_usage_warning(ws, "effort", "Usage: /effort [none|minimal|low|medium|high|xhigh|max]")
        return True, ""
    effort = _normalize_effort(tokens[0])
    if effort is None:
        await _emit_usage_warning(ws, "effort", "Usage: /effort [none|minimal|low|medium|high|xhigh|max]")
        return True, ""
    from backend.config import active_provider_supports_reasoning_effort

    if not active_provider_supports_reasoning_effort():
        await _dispatch_command(
            ws,
            "llm.config.set",
            {
                "reasoning_effort": effort,
                "source": "slash:/effort",
            },
        )
        await ws._emit_command_result(
            "effort",
            "Reasoning effort is not applied by the active Chat Completions provider.",
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
    await ws._emit_command_result(
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
    await ws._emit_command_result(
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
        handled = await _dispatch_command(
            ws,
            "load_skill",
            {
                "skill_name": skill_name,
                "source": "slash:/skills",
            },
        )
        if not handled:
            await _emit_command_unavailable(ws, "skills")
            return True, ""
        await ws._emit_command_result(
            "skills",
            f"Requested skill activation: {skill_name}",
            data={"skill_name": skill_name},
        )
        return True, ""

    await _dispatch_command(ws, "skills.list", {"source": "slash:/skills"})
    await _dispatch_command(ws, "skills.marketplace.list", {"source": "slash:/skills"})
    await ws._emit_command_result(
        "skills",
        "Opening Skills marketplace.",
        data={"ui_action": "open_skills_marketplace"},
    )
    return True, ""


async def _handle_compact(
    ws: "WebSocketSession", arg: str, attachments: Any
) -> tuple[bool, str]:
    _ = attachments
    builder = getattr(ws, "context_builder", None)
    if builder is None or not hasattr(builder, "compact"):
        await _emit_command_unavailable(ws, "compact")
        return True, ""
    before_used = 0
    before_total = 0
    try:
        before_snapshot = _build_compact_budget_snapshot(ws, builder)
        before_used = int(before_snapshot.get("used") or 0)
        before_total = int(before_snapshot.get("total") or 0)
    except Exception:
        before_snapshot = None
    try:
        summary = await builder.compact(focus=arg.strip() if arg else "")
    except Exception as exc:
        await ws._emit_command_result(
            "compact",
            f"Compaction failed: {exc}",
            level="error",
        )
        return True, ""
    from backend.agent.message import AgentEvent
    short = (summary or "").strip()
    if len(short) > 240:
        short = short[:237] + "..."
    await ws._send_event(
        AgentEvent(
            type="context_compacted",
            data={
                "summary": short or "Context compacted.",
                **({"conversation_id": ws.active_conversation_id} if ws.active_conversation_id else {}),
            },
        )
    )
    after_used = before_used
    after_total = before_total
    try:
        budget_snapshot = _build_compact_budget_snapshot(ws, builder)
        after_used = int(budget_snapshot.get("used") or 0)
        after_total = int(budget_snapshot.get("total") or 0)
        if ws.active_conversation_id:
            budget_snapshot = {**budget_snapshot, "conversation_id": ws.active_conversation_id}
        await ws._send_event(AgentEvent(type="budget_update", data=budget_snapshot))
        await ws._send_event(
            AgentEvent(
                type="context_usage",
                data={
                    "used": after_used,
                    "limit": after_total,
                    **({"conversation_id": ws.active_conversation_id} if ws.active_conversation_id else {}),
                },
            )
        )
    except Exception:
        if before_snapshot:
            if ws.active_conversation_id:
                before_snapshot = {**before_snapshot, "conversation_id": ws.active_conversation_id}
            await ws._send_event(AgentEvent(type="budget_update", data=before_snapshot))
    saved = max(0, before_used - after_used)
    suffix = f" Saved about {saved} tokens." if saved > 0 else " No measurable reduction; recent context was already compact."
    await ws._emit_command_result(
        "compact",
        f"Context manually compacted.{suffix}",
        data={
            "summary": short,
            "before_used": before_used,
            "after_used": after_used,
            "saved_tokens": saved,
            "total": after_total or before_total,
        },
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
    from backend.agent.checkpoint import load_latest_checkpoint

    session_id = ws.session_id
    if not session_id:
        await ws._emit_command_result(
            "resume",
            "No active session ID. Cannot resume.",
            level="error",
        )
        return True, ""

    checkpoint = load_latest_checkpoint(session_id)
    if checkpoint is None:
        await ws._emit_command_result(
            "resume",
            "No incomplete checkpoint found. The last task completed successfully or no checkpoint exists yet.",
            level="info",
        )
        return True, ""

    # Directly trigger agent run with resume metadata
    conversation_id = _active_conversation_id(ws)
    if not conversation_id:
        await ws._emit_command_result(
            "resume",
            "No active conversation. Cannot resume.",
            level="error",
        )
        return True, ""

    await ws._emit_command_result(
        "resume",
        f"Resuming from iteration {checkpoint.iterations}. Previous stop: {checkpoint.stopped_reason}",
        level="success",
    )

    # Trigger agent run with resume_from_checkpoint metadata
    await ws._run_agent(
        user_message=checkpoint.user_message,
        conversation_id=conversation_id,
        metadata={"resume_from_checkpoint": True},
    )
    return True, ""


def _build_compact_budget_snapshot(ws: "WebSocketSession", builder: Any) -> dict[str, Any]:
    state = getattr(ws, "_last_agent_state", None)
    if state is None:
        from backend.agent.state import AgentState

        state = AgentState(user_message="")
    tool_schemas = None
    try:
        tool_schemas = ws.tool_registry.get_schemas(
            budget=getattr(ws.config.token_budget, "tool_schemas", 6000),
            permission_checker=ws.permission_checker,
            permission_context=ws.permission_context,
        )
    except Exception:
        tool_schemas = None
    return builder.get_budget_snapshot(state=state, tool_schemas=tool_schemas)


_LOCAL_COMMAND_HANDLERS: dict[str, SlashHandler] = {
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
    "skills": _handle_skills,
    "compact": _handle_compact,
    "goal": _handle_goal,
    "resume": _handle_resume,
}

_PERMISSION_MODE_ALIAS_COMMANDS = {
    "default": "default",
    "confirm": "confirm",
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


def _template_workspace_root(ws: "WebSocketSession") -> str:
    resolver = getattr(ws, "_workspace_root_for_conversation", None)
    root: Any = None
    if callable(resolver):
        try:
            root = resolver()
        except TypeError:
            root = resolver(getattr(ws, "active_conversation", None))
    if root is None:
        active = getattr(ws, "active_conversation", None)
        root = getattr(active, "workspace_root", "") if active is not None else ""
    if root is None:
        root = getattr(ws, "workspace_root", "")
    if not root:
        return ""
    try:
        return str(Path(root).expanduser().resolve())
    except (OSError, RuntimeError, TypeError, ValueError):
        return str(root)


def _template_skill_dir(skill_name: str) -> str:
    normalized = str(skill_name or "").strip()
    if not normalized:
        return ""
    meta = SkillLoader().get_meta(normalized)
    if meta is None:
        return ""
    try:
        return str(meta.source_path.parent.expanduser().resolve())
    except (OSError, RuntimeError):
        return str(meta.source_path.parent)


def _expand_template_variables(template: str, ws: "WebSocketSession", skill_name: str = "") -> str:
    if "${" not in template:
        return template
    return (
        template
        .replace("${CLAUDE_SKILL_DIR}", _template_skill_dir(skill_name))
        .replace("${WORKSPACE}", _template_workspace_root(ws))
    )


def _build_template_handler(command_name: str, template: str, skill_name: str = "") -> SlashHandler:
    base_template = str(template or "").strip()

    async def _handler(
        ws: "WebSocketSession", arg: str, attachments: Any
    ) -> tuple[bool, str]:
        _ = attachments
        if not base_template:
            await _emit_command_unavailable(ws, command_name)
            return True, ""
        prompt = _expand_template_variables(base_template, ws, skill_name)
        extra = str(arg or "").strip()
        if extra:
            prompt = f"{prompt}\n\nAdditional command context:\n{extra}"
        await ws._emit_command_result(
            command_name,
            f"Prepared template prompt for '/{command_name}'.",
        )
        return False, prompt

    return _handler


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


def _build_plugin_local_handler(entry: dict[str, Any]) -> SlashHandler:
    command_name = str(entry.get("command", "")).strip().lower()
    ui_action = str(entry.get("ui_action") or "").strip()
    component = str(entry.get("component") or "").strip()
    plugin_name = str(entry.get("plugin_name") or "").strip()
    base_payload = dict(entry.get("payload") or {}) if isinstance(entry.get("payload"), dict) else {}
    arg_key = str(entry.get("arg_key") or "").strip()

    async def _handler(
        ws: "WebSocketSession", arg: str, attachments: Any
    ) -> tuple[bool, str]:
        _ = attachments
        if not ui_action:
            await _emit_command_unavailable(ws, command_name)
            return True, ""

        raw_arg = str(arg or "").strip()
        data = dict(base_payload)
        data["ui_action"] = ui_action
        if component:
            data["component"] = component
        data.setdefault("source", f"slash:/{command_name}")
        if plugin_name:
            data.setdefault("plugin_name", plugin_name)
        data.setdefault("command", command_name)
        if raw_arg:
            data[arg_key or "arg"] = raw_arg

        await ws._emit_command_result(
            command_name,
            f"Opening plugin command: /{command_name}.",
            data=data,
        )
        return True, ""

    return _handler


def _build_permission_mode_alias_handler(
    command_name: str, mode: str
) -> SlashHandler:
    async def _handler(
        ws: "WebSocketSession", arg: str, attachments: Any
    ) -> tuple[bool, str]:
        remainder = str(arg or "").strip()
        success = await _apply_permission_mode(
            ws,
            command=command_name,
            mode=mode,
            source=f"slash:/{command_name}",
        )
        if not success:
            return True, ""
        if remainder or attachments:
            return False, remainder
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
            if str(entry.get("source", "")).strip().lower() == "plugin":
                registry.register_slash(slash_name, _build_plugin_local_handler(entry))
            else:
                registry.register_slash(slash_name, _build_local_handler(command_name))
            continue

        if command_type == "template":
            registry.register_slash(
                slash_name,
                _build_template_handler(
                    command_name,
                    str(entry.get("template", "")),
                    str(entry.get("skill_name", "")),
                ),
            )
            continue

        if command_type == "protocol":
            registry.register_slash(slash_name, _build_protocol_handler(entry))

    # Keep legacy aliases that map cleanly to catalog-backed permission modes.
    for command_name, mode in _PERMISSION_MODE_ALIAS_COMMANDS.items():
        registry.register_slash(
            f"/{command_name}",
            _build_permission_mode_alias_handler(command_name, mode),
        )


def refresh_slash_commands(registry: Any) -> None:
    clear = getattr(registry, "clear_slash_handlers", None)
    if callable(clear):
        clear()
    register_all_slash_commands(registry)
