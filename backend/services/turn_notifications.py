"""Codex desktop completion decisions (Pnn / Inn / dlc in installed Source)."""

from __future__ import annotations

import re
from typing import Any


def heartbeat_user_message(automation_id: str, current_time_iso: str, prompt: str) -> str:
    return (f"<heartbeat>\n  <automation_id>{automation_id}</automation_id>\n"
            f"  <current_time_iso>{current_time_iso}</current_time_iso>\n"
            f"  <instructions>\n{prompt}\n  </instructions>\n</heartbeat>\n")


def heartbeat_assistant_message(text: str, *, heartbeat: bool) -> dict[str, Any] | None:
    unwrapped = re.fullmatch(r"<!\[CDATA\[ ([\s\S]*) \]\]>", text)
    if heartbeat and (unwrapped[1] if unwrapped else text).strip() == "DONT_NOTIFY":
        return {"decision": "DONT_NOTIFY", "visible_text": "", "notification_message": None}
    visible = re.sub(r"```(?:xml)?\s*<heartbeat>[\s\S]*?</heartbeat>\s*```", "", text.strip(), flags=re.I)
    visible = re.sub(r"<heartbeat>[\s\S]*?</heartbeat>", "", visible, flags=re.I).strip()
    envelopes = list(re.finditer(r"<heartbeat>[\s\S]*?</heartbeat>", text.strip(), flags=re.I))
    if visible == text.strip() or not envelopes:
        return None
    envelope = envelopes[-1][0]
    decision = re.search(r"<decision>\s*(NOTIFY|DONT_NOTIFY)\s*</decision>", envelope, flags=re.I)
    if decision is None:
        return None
    message = re.search(r"<message>\s*([\s\S]*?)\s*</message>", envelope, flags=re.I)
    notification_message = message[1].strip() if message else ""
    message_cdata = re.fullmatch(r"<!\[CDATA\[([\s\S]*)\]\]>", notification_message)
    if message_cdata:
        notification_message = message_cdata[1].strip()
    return {"decision": decision[1].upper(), "visible_text": visible,
            "notification_message": notification_message or None}


def completion_notification(
    *, status: str, last_agent_message: str | None, thread_source: str = "user",
    automation_kind: str | None = None, notification_policy: str | None = None,
    has_pending_continuation: bool = False, continuing_goal: bool = False,
) -> dict[str, Any]:
    heartbeat = heartbeat_assistant_message(last_agent_message, heartbeat=automation_kind == "heartbeat") if last_agent_message is not None else None
    decision = heartbeat["decision"] if heartbeat is not None else None
    if decision is None and automation_kind == "heartbeat" and status == "completed" and not (last_agent_message or "").strip():
        decision = "DONT_NOTIFY"
    if notification_policy == "failed_runs_only":
        decision = "DONT_NOTIFY" if status == "completed" else "NOTIFY"
    text = (heartbeat["notification_message"] or heartbeat["visible_text"]) if heartbeat is not None else last_agent_message
    unwrapped = re.fullmatch(r"<!\[CDATA\[ ([\s\S]*) \]\]>", text) if text is not None else None
    skipped = text is not None and re.fullmatch(r"::\s*SKIP_COMPLETION\s*::", (unwrapped[1] if unwrapped else text).strip()) is not None
    quiet = decision == "DONT_NOTIFY" or skipped and status == "completed"
    return {
        "thread_source": thread_source, "automation_kind": automation_kind,
        "automation_notification_decision": decision, "heartbeat_assistant_message": heartbeat,
        "last_agent_message": last_agent_message, "has_pending_continuation": has_pending_continuation,
        "eligible": thread_source not in {"aeon", "realtime_voice", "chatgpt_hidden", "dreaming"} and not has_pending_continuation,
        "body": None if quiet else text if not skipped and text and text.strip() else "MiniCode 已完成一个回合。",
        "should_mark_unread": decision == "NOTIFY" or decision != "DONT_NOTIFY" and not continuing_goal,
    }
