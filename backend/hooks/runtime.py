"""Small runtime helpers for best-effort hook notifications."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def run_config_change_hook(*, source: str, file_path: str = "") -> None:
    """Notify ConfigChange hooks after a config write has succeeded."""
    try:
        from backend.hooks import get_hook_manager

        hook_mgr = get_hook_manager()
        if not hook_mgr:
            return
        await hook_mgr.run_config_change(source=source, file_path=file_path)
    except Exception:
        logger.debug("ConfigChange hook failed for %s", source, exc_info=True)


async def run_cwd_changed_hook(*, old_cwd: str, new_cwd: str) -> None:
    """Notify CwdChanged hooks after the session workspace root changes."""
    old_value = str(old_cwd or "").strip()
    new_value = str(new_cwd or "").strip()
    if old_value == new_value:
        return
    try:
        from backend.hooks import get_hook_manager
        from backend.hooks.manager import HookEvent

        hook_mgr = get_hook_manager()
        if hook_mgr and hook_mgr.has_hooks(HookEvent.CWD_CHANGED):
            await hook_mgr.run_cwd_changed(old_cwd=old_value, new_cwd=new_value)
    except Exception:
        logger.debug("CwdChanged hook failed", exc_info=True)


async def run_notification_hook_for_event(*, event_type: str, payload: dict[str, Any]) -> None:
    """Notify Notification hooks for user-visible notice/error events."""
    clean_type = str(event_type or "").strip()
    if clean_type not in {"system_notice", "error"}:
        return
    message = str(
        payload.get("content")
        or payload.get("message")
        or payload.get("error")
        or payload.get("detail")
        or ""
    ).strip()
    if not message:
        return
    title = str(payload.get("title") or ("Error" if clean_type == "error" else "Notification")).strip()
    notification_type = str(
        payload.get("level")
        or payload.get("error_type")
        or clean_type
    ).strip()
    try:
        from backend.hooks import get_hook_manager

        hook_mgr = get_hook_manager()
        if not hook_mgr:
            return
        await hook_mgr.run_notification(
            message,
            title=title,
            notification_type=notification_type,
        )
    except Exception:
        logger.debug("Notification hook failed for event %s", clean_type, exc_info=True)


async def run_session_end_hook(*, session_id: str, reason: str = "") -> None:
    """Notify SessionEnd hooks during best-effort session cleanup."""
    clean_session_id = str(session_id or "").strip()
    clean_reason = str(reason or "").strip()
    if not clean_session_id:
        return
    try:
        from backend.hooks import get_hook_manager

        hook_mgr = get_hook_manager()
        if hook_mgr:
            await hook_mgr.run_session_end(session_id=clean_session_id, reason=clean_reason)
    except Exception:
        logger.debug("session_end hook failed for %s", clean_session_id, exc_info=True)
