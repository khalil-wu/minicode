"""Partition one frozen live snapshot using the existing wire record limits."""
from __future__ import annotations

import math
from typing import Any
from uuid import uuid4

from backend.agent.message import AgentEvent
from backend.ws.payload_contracts import MAX_STREAM_RESUME_BLOCKS, MAX_STREAM_RESUME_TOOLS


def stream_resume_pages(
    conversation_id: str, snapshot: dict[str, Any],
) -> list[AgentEvent]:
    tool_states = snapshot["tool_states"]
    pending = snapshot["tool_calls_pending"]
    blocks = snapshot["content_blocks"]
    page_count = max(1, math.ceil(len(tool_states) / MAX_STREAM_RESUME_TOOLS),
                     math.ceil(len(pending) / MAX_STREAM_RESUME_TOOLS),
                     math.ceil(len(blocks) / MAX_STREAM_RESUME_BLOCKS))
    snapshot_id = uuid4().hex if page_count > 1 else ""
    return [AgentEvent.stream_resume(
        conversation_id, snapshot["message_id"],
        pending[part * MAX_STREAM_RESUME_TOOLS:(part + 1) * MAX_STREAM_RESUME_TOOLS],
        blocks[part * MAX_STREAM_RESUME_BLOCKS:(part + 1) * MAX_STREAM_RESUME_BLOCKS],
        tool_states=tool_states[part * MAX_STREAM_RESUME_TOOLS:(part + 1) * MAX_STREAM_RESUME_TOOLS],
        turn_id=snapshot["turn_id"], phase=snapshot["phase"],
        stream_status=snapshot["stream_status"], event_seq=snapshot["event_seq"],
        last_event_type=snapshot["last_event_type"],
        **({"snapshot_id": snapshot_id, "snapshot_part": part,
            "snapshot_complete": part == page_count - 1} if snapshot_id else {}),
    ) for part in range(page_count)]
