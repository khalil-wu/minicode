"""Shared mutable application state.

Global variables that must be accessible across multiple route modules
during the lifetime of the FastAPI application.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.bootstrap.app import AppBootstrap
from backend.ws.handler import WebSocketManager

logger = logging.getLogger(__name__)

# ── WebSocket manager ──
ws_manager = WebSocketManager()

# ── Application bootstrap (set during lifespan, cleared on shutdown) ──
bootstrap: AppBootstrap | None = None

# ── Status cache ──
status_cache_payload: dict[str, Any] | None = None
status_cache_expires_at: float = 0.0
STATUS_CACHE_TTL_SECONDS: float = 5.0

# ── Capability cache ──
capability_cache_payload: dict[str, Any] | None = None
capability_cache_expires_at: float = 0.0
CAPABILITY_CACHE_TTL_SECONDS: int = 60


def invalidate_status_cache() -> None:
    """Reset both the status and capability caches."""
    global status_cache_payload, status_cache_expires_at
    global capability_cache_payload, capability_cache_expires_at
    status_cache_payload = None
    status_cache_expires_at = 0.0
    capability_cache_payload = None
    capability_cache_expires_at = 0.0
    if bootstrap is not None:
        if hasattr(bootstrap, "_status_cache_payload"):
            bootstrap._status_cache_payload = None
        if hasattr(bootstrap, "_status_cache_expires_at"):
            bootstrap._status_cache_expires_at = 0.0
