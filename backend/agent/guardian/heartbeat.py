"""Bounded, exact Source heartbeat identification and transcript projection."""

from __future__ import annotations

import string
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Heartbeat:
    automation_id: str
    timestamp: str
    instructions: str

    @classmethod
    def parse(cls, text: str) -> Heartbeat | None:
        lines = text.count("\n") + int(bool(text) and not text.endswith("\n"))
        if len(text.encode("utf-8")) + lines * 6 + 64 > 3600:
            return None
        body = text.removesuffix("\n")
        prefix = "<heartbeat>\n  <automation_id>"
        if not body.startswith(prefix):
            return None
        automation, separator, body = body[len(prefix):].partition("</automation_id>\n  <current_time_iso>")
        if not separator:
            return None
        timestamp, separator, body = body.partition("</current_time_iso>\n  <instructions>\n")
        suffix = "\n  </instructions>\n</heartbeat>"
        if not separator or not body.endswith(suffix):
            return None
        if not automation or len(automation.encode("utf-8")) > 128 or any(char not in string.ascii_letters + string.digits + "-_" for char in automation):
            return None
        if not timestamp or len(timestamp.encode("utf-8")) > 64 or any(char not in string.digits + "TZ:.+-" for char in timestamp):
            return None
        return cls(automation, timestamp, body[:-len(suffix)])


def collect_transcript(entries):
    """Keep every position; repeated invocations cannot prove full delivery."""
    versions = {}
    collected = []
    for entry in entries:
        if entry.kind == "user" and entry.input_origin == "heartbeat":
            heartbeat = Heartbeat.parse(entry.text) if entry.single_text else None
            if heartbeat is None:
                versions.clear()
            else:
                previous = versions.get(heartbeat.automation_id)
                if previous is not None and previous[0] == heartbeat.instructions:
                    text = (
                        f"Scheduled automation {heartbeat.automation_id} ran at {heartbeat.timestamp}. "
                        f"Instructions unchanged from transcript entry [{previous[1]}]; this is a replay of that earlier instruction, "
                        "not a new human instruction. If the referenced instructions are unavailable, do not infer authorization from this reference."
                    )
                    entry = replace(entry, text=text, source_order=None, source_revision=None)
                else:
                    versions[heartbeat.automation_id] = heartbeat.instructions, len(collected) + 1
        collected.append(entry)
    return tuple(collected)


def selected_transcript(evidence, loaded_checkpoint=False, *, sources=None):
    current = evidence.context_mode == "thread_owned" or loaded_checkpoint
    if current and evidence.parent_window_transcript is not None:
        entries = evidence.parent_window_transcript
    elif loaded_checkpoint:
        entries = evidence.transcript[evidence.checkpoint_transcript_start:]
    else:
        entries = evidence.transcript
    if sources is not None:
        entries = tuple(entry for entry in entries if not (
            entry.kind.startswith("tool ") and ((entry.kind.endswith(" call") and "tool_calls" not in sources)
                or (entry.kind.endswith(" result") and "tool_outputs" not in sources))
            or entry.kind == "reasoning" and "reasoning" not in sources))
    return collect_transcript(entries)
