"""Private controller/JavaScript host frames; tool authority stays in the controller."""
from __future__ import annotations

import asyncio
import json
import struct

MAX_FRAME_BYTES = 64 * 1024 * 1024
PROTOCOL_VERSION = 1


def encode_frame(message: dict) -> bytes:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError(f"Code host frame exceeds {MAX_FRAME_BYTES} bytes")
    return struct.pack("<I", len(payload)) + payload


def frame_length(header: bytes) -> int:
    length, = struct.unpack("<I", header)
    if length > MAX_FRAME_BYTES:
        raise ValueError(f"Code host frame exceeds {MAX_FRAME_BYTES} bytes")
    return length


async def read_frame(reader: asyncio.StreamReader) -> dict:
    header = await reader.readexactly(4)
    return json.loads(await reader.readexactly(frame_length(header)))
