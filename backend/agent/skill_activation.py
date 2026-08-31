"""Turn-scoped skill selection and lifecycle projection."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.state import AgentState

logger = logging.getLogger(__name__)


async def activate_turn_skills(
    skill_manager: Any,
    user_message: str,
    state: AgentState,
) -> AsyncIterator[AgentEvent]:
    """Select textual skill workflows without granting executable hook authority."""
    try:
        selected_skills = state.prompt_context.get("selected_skills", []) if isinstance(state.prompt_context, dict) else []
        detections = skill_manager.detect(
            user_message,
            selected_skills=selected_skills,
        )
        for detection in detections:
            name = detection.name
            trigger_mode = getattr(detection, "trigger_mode", "implicit")
            source_path = getattr(detection, "source_path", "")
            payload = skill_manager.load_skill_payload(name, source_path=source_path or None)
            if payload is not None:
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
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("Skills auto-detect failed: %s", exc)
