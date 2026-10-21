"""The public Source board wire records and bounded SSE framing."""
from __future__ import annotations

import codecs
from datetime import datetime, timezone
from typing import Generic, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from httpx_sse import EventSource
from pydantic import AwareDatetime, BaseModel, ConfigDict, NonNegativeInt, computed_field, field_validator

from backend.agent.agent_identity import AgentPath
from backend.agent.message_board import MessageBoardError

MAX_BODY = 512 * 1024
MAX_RESPONSE = 1024 * 1024


def board_identity(tree: str) -> UUID:
    """Project MiniCode's stable root thread into the service's UUID identity."""
    return uuid5(NAMESPACE_URL, f"minicode:agent-message-board:{tree}")


def member_identity(tree: str, path: str) -> UUID:
    return uuid5(board_identity(tree), path)


class WireRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    @field_validator("author", "created_by", "target_agent", check_fields=False)
    @classmethod
    def agent_path(cls, value: str) -> str:
        if not value.startswith("/root") or AgentPath.parse(value).logical_name != value:
            raise ValueError("invalid absolute agent path")
        return value

    @field_validator("created_at", "last_activity_at", check_fields=False)
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class PostMetadata(WireRecord):
    message_id: UUID
    channel_name: str
    author: str
    thread_id: UUID
    created_at: AwareDatetime


class PostPreview(PostMetadata):
    text_preview: str
    n_chars: NonNegativeInt
    truncated: bool


class PostContent(PostMetadata):
    text: str
    n_chars: NonNegativeInt
    next_offset_chars: NonNegativeInt


class ChannelSummary(WireRecord):
    channel_name: str
    created_at: AwareDatetime
    created_by: str
    message_count: NonNegativeInt
    last_message_id: UUID | None = None


class ThreadSummary(WireRecord):
    thread_id: UUID
    root_post: PostPreview
    reply_count: NonNegativeInt
    last_activity_at: AwareDatetime
    latest_reply: PostPreview | None = None


class SubscriptionState(WireRecord):
    channel_name: str
    thread_id: UUID | None = None
    target_agent: str
    enabled: bool
    last_message_id: UUID | None = None


T = TypeVar("T")


class Page(WireRecord, Generic[T]):
    results: list[T]
    next_cursor: str | None = None

    @computed_field
    @property
    def n_returned(self) -> int:
        return len(self.results)

    @computed_field
    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


class ThreadPage(Page[PostPreview]):
    root_post: PostPreview


class BoardNotification(WireRecord):
    recipient: UUID
    turn_id: str
    post: PostPreview


class Failure(WireRecord):
    code: str
    message: str


class _BoundedSSE(httpx.AsyncByteStream):
    def __init__(self, response: httpx.Response):
        self.response = response

    async def __aiter__(self):
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        frame_bytes, line_empty, previous_cr = 0, True, False
        async for chunk in self.response.aiter_bytes():
            decoder.decode(chunk)
            for byte in chunk:
                if previous_cr and byte != 10:
                    if line_empty:
                        frame_bytes = 0
                    line_empty = True
                frame_bytes += 1
                if frame_bytes > MAX_BODY:
                    raise MessageBoardError("board SSE frame exceeds the service limit")
                if byte == 10:
                    if line_empty:
                        frame_bytes = 0
                    line_empty = True
                elif byte != 13:
                    line_empty = False
                previous_cr = byte == 13
            yield chunk
        decoder.decode(b"", final=True)

    async def aclose(self):
        await self.response.aclose()


def board_events(response: httpx.Response):
    # The stream already contains decoded bytes; do not decompress them twice.
    # Source selects its SSE parser directly, independently of the MIME header.
    bounded = httpx.Response(response.status_code, headers={"content-type": "text/event-stream"},
        request=response.request, stream=_BoundedSSE(response))
    return EventSource(bounded).aiter_sse()
