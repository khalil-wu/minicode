"""Source app-server realtime arguments and version-specific public wire messages."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from html import escape

V1_VOICES = ("juniper", "maple", "spruce", "ember", "vale", "breeze", "arbor", "sol", "cove")
V2_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar")
Version = Literal["v1", "v2", "v3"]
Role = Literal["user", "assistant", "developer"]


class SourceParams(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, extra="forbid", strict=True, hide_input_in_errors=True)


class AudioChunk(SourceParams):
    data: str
    sample_rate: int = Field(ge=0, le=0xFFFFFFFF)
    num_channels: int = Field(ge=0, le=0xFFFF)
    samples_per_channel: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    item_id: str | None = None


class Websocket(SourceParams):
    type: Literal["websocket"]


class Webrtc(SourceParams):
    type: Literal["webrtc"]
    sdp: str


class ExistingCall(SourceParams):
    type: Literal["existingCall"]
    call_id: str


Transport = Annotated[Websocket | Webrtc | ExistingCall, Field(discriminator="type")]


class InitialItem(SourceParams):
    role: Role
    text: str


class Start(SourceParams):
    thread_id: str
    output_modality: Literal["audio", "text"]
    transport: Transport = Websocket(type="websocket")
    version: Version | None = None
    model: str | None = None
    voice: str | None = None
    client_managed_handoffs: bool = False
    delegation_ack_filler: bool | None = None
    flush_transcript_tail_on_session_end: bool = False
    codex_responses_as_items: bool = False
    codex_response_item_prefix: str | None = None
    codex_response_handoff_mode: Literal["thinking", "commentary", "bemTags"] = "thinking"
    backend_reasoning_status: bool = False
    codex_response_handoff_channel_prefixes: dict[str, list[str]] | None = None
    include_startup_context: bool | None = None
    initial_items: list[InitialItem] | None = None
    realtime_start_instructions: str | None = None
    realtime_end_instructions: str | None = None
    prompt: str | None = None
    realtime_session_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def source_optional_defaults(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            for field in ("transport", "clientManagedHandoffs", "flushTranscriptTailOnSessionEnd",
                          "codexResponsesAsItems", "codexResponseHandoffMode"):
                if value.get(field, False) is None:
                    value.pop(field)
        return value


class Thread(SourceParams):
    thread_id: str


class AppendAudio(Thread):
    audio: AudioChunk


class AppendText(Thread):
    text: str
    role: Role = "user"


class AppendSpeech(Thread):
    text: str


COMMAND_PARAMS = {"start": Start, "appendAudio": AppendAudio, "appendText": AppendText,
    "appendSpeech": AppendSpeech, "stop": Thread}
NOTIFICATIONS = ("started", "itemAdded", "item/started", "item/transcript/delta", "item/completed",
    "transcript/delta", "transcript/done", "outputAudio/delta", "sdp", "error", "closed")


def voices() -> dict:
    return {"voices": {"v1": list(V1_VOICES), "v2": list(V2_VOICES), "defaultV1": "cove", "defaultV2": "marin"}}


def input_text(text: str) -> list[dict]:
    return [{"type": "input_text", "text": text}]


def context_chunks(text: str) -> list[str]:
    """Source's 500-byte UTF-8 context append bound, without splitting scalars."""
    pieces, current, size = [], [], 0
    for char in text:
        width = len(char.encode())
        if size + width > 500:
            pieces.append("".join(current)); current, size = [], 0
        current.append(char); size += width
    pieces.append("".join(current))
    return pieces


def text_message(version: str, text: str, role: str) -> dict:
    if version == "v3":
        return {"type": "session.context.append", "content": input_text(text)}
    return {"type": "conversation.item.create", "item": {"type": "message", "role": role,
        "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}]}}


def transcript_tail_fragment(transcript: str) -> str:
    instruction = "The user just ended their realtime session. Here is the remaining handoff/transcript tail. You probably do not have to do anything; acknowledge the handoff unless the transcript itself asks for something."
    escaped = escape(transcript, quote=False).encode("utf-8")
    if len(escaped) > 4096:
        escaped = "…".encode() + escaped[-4093:].decode("utf-8", errors="ignore").encode()
    return ("<realtime_delegation>\n  <source>transcript_tail_flush</source>\n  <input>" + instruction
        + "</input>\n  <transcript_delta>" + escaped.decode() + "</transcript_delta>\n</realtime_delegation>")
