"""Source-compatible inference identifiers shared by internal request producers."""
from __future__ import annotations

import secrets
import time
from uuid import UUID


def new_uuid7() -> str:
    """Mint an RFC 9562 UUID v7 using the existing inference-ID algorithm."""
    milliseconds = time.time_ns() // 1_000_000
    random = secrets.randbits(74)
    value = (milliseconds << 80) | (7 << 76) | ((random >> 62) << 64) | (2 << 62) | (random & ((1 << 62) - 1))
    return str(UUID(int=value))
