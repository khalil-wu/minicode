"""Lifecycle hook bridges used by configuration, workspace and UI runtimes."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

logger = logging.getLogger(__name__)


class ConfigChangeHookBlocked(RuntimeError):
    """Raised when a non-managed ConfigChange hook vetoes a runtime apply.

    MiniCode runs config_change before fanning a changed file into the live
    session. Service-level writers use this exception as the common
    transaction boundary: the durable mutation is only committed after the
    hook has allowed it (or the caller restores its prior snapshot).
    """

    def __init__(self, message: str, *, result: Any | None = None) -> None:
        super().__init__(message or "Configuration change blocked by hook")
        self.result = result


def _config_change_source(source: str, file_path: str = "") -> str:
    """Return the MiniCode config_change source for a caller value."""

    # Keep the canonicalizer private to hooks.manager to avoid importing the
    # manager (and its runtime bindings) from every settings service.
    from backend.hooks.manager import _canonical_config_change_source

    return _canonical_config_change_source(source, file_path)


def config_change_is_blocked(
    result: Any | None,
    *,
    source: str,
    file_path: str = "",
) -> bool:
    """Whether a ConfigChange result vetoes applying a live configuration.

    ``policy_settings`` is enterprise-managed. MiniCode still executes hooks
    for audit, but forcibly clears their blocking decision; callers must never
    let a user/plugin hook suppress managed policy.
    """

    if result is None or _config_change_source(source, file_path) == "policy_settings":
        return False
    return bool(
        getattr(result, "blocked", False)
        or str(getattr(result, "permission_decision", "") or "").strip().lower()
        == "deny"
    )


def config_change_block_message(result: Any | None) -> str:
    """Extract a stable user-facing veto message from a HookResult-like value."""

    if result is None:
        return "Configuration change blocked by hook"
    return str(
        getattr(result, "message", "")
        or getattr(result, "feedback", "")
        or "\n".join(
            str(item)
            for item in (getattr(result, "errors", ()) or ())
            if str(item).strip()
        )
        or "Configuration change blocked by hook"
    ).strip()


def raise_if_config_change_blocked(
    result: Any | None,
    *,
    source: str,
    file_path: str = "",
) -> None:
    """Raise the shared veto exception for an already-run hook result."""

    if config_change_is_blocked(result, source=source, file_path=file_path):
        raise ConfigChangeHookBlocked(
            config_change_block_message(result),
            result=result,
        )


async def run_config_change_hook(*, source: str, file_path: str = "") -> Any | None:
    """Run ConfigChange hooks and return their decision to the caller.

    ConfigChange is an audit boundary as well as a policy boundary.  The old
    helper discarded ``HookResult`` and made a blocking hook indistinguishable
    from an allow result.  Preserve the result while keeping the historical
    ``None`` return when no manager is bound, so existing service callbacks
    remain source-compatible.
    """
    try:
        from backend.hooks import get_hook_manager

        hook_mgr = get_hook_manager()
        if not hook_mgr:
            return None
        result = await hook_mgr.run_config_change(source=source, file_path=file_path)
        # The manager reducer knows the hook entry source, not the changed
        # file's provenance. Apply the policy-settings invariant at this
        # outer boundary where the canonical source is available.
        if (
            result is not None
            and _config_change_source(source, file_path) == "policy_settings"
            and (
                getattr(result, "blocked", False)
                or str(getattr(result, "permission_decision", "") or "").strip().lower()
                == "deny"
            )
        ):
            try:
                # HookResult is mutable today, but return a copy so a caller
                # retaining the manager's diagnostic object does not observe a
                # surprising policy rewrite.
                return replace(
                    result,
                    blocked=False,
                    permission_decision="",
                    permission_decision_reason="",
                )
            except (TypeError, ValueError):
                # Compatibility with test/integration hook result objects that
                # are not dataclasses: mutate only when they expose writable
                # attributes, otherwise leave the audit object untouched.
                try:
                    setattr(result, "blocked", False)
                    setattr(result, "permission_decision", "")
                    setattr(result, "permission_decision_reason", "")
                except Exception:
                    logger.warning(
                        "Could not normalize ConfigChange hook result compatibility object",
                        exc_info=True,
                    )
        return result
    except Exception:
        logger.warning(
            "ConfigChange hook failed for %s (%s)",
            source,
            file_path or "no file path",
            exc_info=True,
        )
        # A hook runtime failure is not a policy veto.  Callers can still
        # surface diagnostics through their normal config mutation response.
        return None


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


async def run_notification_hook_for_event(*, event_type: str, payload: dict[str, Any]) -> Any | None:
    """Notify Notification hooks for canonical user-visible event types."""
    clean_type = str(event_type or "").strip()
    # Map internal transport events to MiniCode notification matcher values.
    # Keep already-canonical values unchanged so MCP/auth integrations can call
    # this bridge without inventing a second event vocabulary.
    notification_type_map = {
        "system_notice": "system_notice",
        "error": "error",
        "approval_request": "permission_prompt",
        "approval.file_diff": "permission_prompt",
        "ask_user": "elicitation_dialog",
        "approval.cancelled": "permission_prompt",
        "idle_prompt": "idle_prompt",
        "auth_success": "auth_success",
        "elicitation_dialog": "elicitation_dialog",
        "elicitation_complete": "elicitation_complete",
        "elicitation_response": "elicitation_response",
    }
    canonical_type = notification_type_map.get(clean_type, clean_type)
    if not canonical_type:
        return None
    message = str(
        payload.get("content")
        or payload.get("message")
        or payload.get("error")
        or payload.get("detail")
        or ""
    ).strip()
    if not message:
        message = str(
            payload.get("reason")
            or payload.get("detail")
            or payload.get("prompt")
            or payload.get("question")
            or ""
        ).strip()
    if not message:
        return None
    title = str(
        payload.get("title")
        or ("Error" if clean_type == "error" else "Notification")
    ).strip()
    notification_type = str(
        payload.get("notification_type")
        or (payload.get("level") if clean_type in {"system_notice", "error"} else "")
        or (payload.get("error_type") if clean_type == "error" else "")
        or canonical_type
    ).strip()
    try:
        from backend.hooks import get_hook_manager

        hook_mgr = get_hook_manager()
        if not hook_mgr:
            return None
        return await hook_mgr.run_notification(
            message,
            title=title,
            notification_type=notification_type,
        )
    except Exception:
        logger.debug("Notification hook failed for event %s", clean_type, exc_info=True)
        return None


async def run_session_end_hook(*, session_id: str, reason: str = "") -> None:
    """Notify SessionEnd hooks during best-effort session cleanup."""
    clean_session_id = str(session_id or "").strip()
    clean_reason = str(reason or "").strip()
    if not clean_session_id:
        return
    try:
        from backend.hooks.manager import pop_hook_managers_for_owner

        managers = pop_hook_managers_for_owner(clean_session_id)
        for scope_id, hook_mgr in managers:
            try:
                await hook_mgr.run_session_end(
                    session_id=scope_id or clean_session_id,
                    reason=clean_reason,
                )
            except Exception:
                logger.debug(
                    "session_end hook failed for scope %s",
                    scope_id or clean_session_id,
                    exc_info=True,
                )
            finally:
                # MiniCode finalizes or kills every pending async hook at
                # session teardown.  Do not leave command processes or stale
                # rewake callbacks alive after their websocket owner is gone.
                await hook_mgr.finalize_async_hooks()
    except Exception:
        logger.debug("session_end hook failed for %s", clean_session_id, exc_info=True)
