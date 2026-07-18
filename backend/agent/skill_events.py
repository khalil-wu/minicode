from __future__ import annotations

import re
from typing import Any

from backend.agent.message import AgentEvent


def skill_process_event(
    skill_name: str,
    *,
    lifecycle: str,
    trigger_mode: str,
    status: str = "completed",
    reason: str = "",
    skill_manager: Any | None = None,
    loop_id: str = "",
    iteration_id: str = "",
) -> AgentEvent:
    """Build a visible, non-CoT process item for skill lifecycle changes."""
    name = skill_name.strip()
    meta = _skill_meta(skill_manager, name)
    source_level = _meta_value(meta, "source_level")
    token_estimate = _skill_token_estimate(skill_manager, name)
    description = _meta_value(meta, "description")
    title = _skill_title(name, lifecycle, trigger_mode)
    summary = reason or description or title
    content = _skill_content(name, lifecycle, trigger_mode, reason, source_level, token_estimate)
    event_id = ":".join(
        part for part in (
            "skill",
            _safe_event_id(name),
            _safe_event_id(trigger_mode),
            _safe_event_id(lifecycle),
        )
        if part
    )
    return AgentEvent.agent_item(
        id=event_id,
        kind="skill",
        content=content,
        loop_id=loop_id,
        iteration_id=iteration_id,
        role="runtime",
        source="runtime",
        status=status,
        title=title,
        summary=summary,
        visibility="timeline",
        display_scope="activity",
        panel_hint="inspector",
        skill_name=name,
        trigger_mode=trigger_mode,
        source_level=source_level,
        reason=reason or description,
        token_estimate=token_estimate,
    )


def _skill_title(skill_name: str, lifecycle: str, trigger_mode: str) -> str:
    if lifecycle == "selected":
        return f"自动匹配 Skill: {skill_name}" if trigger_mode == "implicit" else f"准备使用 Skill: {skill_name}"
    if lifecycle == "loaded":
        return f"已加载 Skill: {skill_name}"
    if lifecycle == "skipped":
        return f"跳过 Skill: {skill_name}"
    if lifecycle == "unloaded":
        return f"已停用 Skill: {skill_name}"
    if lifecycle == "failed":
        return f"Skill 加载失败: {skill_name}"
    return f"Skill: {skill_name}"


def _skill_content(
    skill_name: str,
    lifecycle: str,
    trigger_mode: str,
    reason: str,
    source_level: str,
    token_estimate: int | None,
) -> str:
    parts: list[str] = []
    if lifecycle == "selected":
        action = _trigger_action_label(trigger_mode)
        parts.append(f"{action} Skill: {skill_name}。")
    elif lifecycle == "loaded":
        parts.append(f"已加载 Skill: {skill_name}。")
    elif lifecycle == "skipped":
        parts.append(f"跳过 Skill: {skill_name}。")
    elif lifecycle == "unloaded":
        parts.append(f"已停用 Skill: {skill_name}。")
    elif lifecycle == "failed":
        parts.append(f"Skill 加载失败: {skill_name}。")
    else:
        parts.append(f"Skill 更新: {skill_name}。")

    if reason:
        parts.append(reason)
    details = []
    if source_level:
        details.append(f"来源 {source_level}")
    if token_estimate is not None:
        details.append(f"约 {token_estimate} tokens")
    if details:
        parts.append("（" + "，".join(details) + "）")
    return " ".join(part for part in parts if part).strip()


def _trigger_action_label(trigger_mode: str) -> str:
    if trigger_mode == "implicit":
        return "自动匹配"
    if trigger_mode == "model":
        return "模型调用"
    if trigger_mode == "explicit":
        return "显式调用"
    return trigger_mode or "调用"


def _skill_meta(skill_manager: Any | None, skill_name: str) -> Any | None:
    if skill_manager is None or not skill_name:
        return None
    get_meta = getattr(skill_manager, "get_meta", None)
    if callable(get_meta):
        try:
            return get_meta(skill_name)
        except Exception:
            return None
    loader = getattr(skill_manager, "_loader", None)
    if loader is not None:
        get_meta = getattr(loader, "get_meta", None)
        if callable(get_meta):
            try:
                return get_meta(skill_name)
            except Exception:
                return None
    return None


def _skill_token_estimate(skill_manager: Any | None, skill_name: str) -> int | None:
    if skill_manager is None or not skill_name:
        return None
    get_active_full = getattr(skill_manager, "get_active_full", None)
    if callable(get_active_full):
        try:
            full = get_active_full(skill_name)
            value = getattr(full, "token_estimate", None)
            return int(value) if value is not None else None
        except Exception:
            return None
    active = getattr(skill_manager, "_active", None)
    if isinstance(active, dict):
        full = active.get(skill_name)
        value = getattr(full, "token_estimate", None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _meta_value(meta: Any | None, key: str) -> str:
    value = getattr(meta, key, "") if meta is not None else ""
    return str(value or "")


def _safe_event_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.:-]+", "-", value.strip()).strip("-")
