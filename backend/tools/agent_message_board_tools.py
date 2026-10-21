"""The nine public Source board tools; identities and storage are host-owned."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
import math

from backend.agent.message_board import AgentMessageBoard, BoardActor, MessageBoardError, SUPPORTED_BOARD_BACKENDS, encode
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec

logger = logging.getLogger(__name__)
READ_TOOLS = frozenset({"get_channels", "list_threads", "search_posts", "read_thread", "read_post"})
_DEFINITIONS = {
    "create_channel": ("Create a channel where all agents in this collaboration can read and post messages. You are subscribed to new top-level posts by default.", ["channel_name", "subscribe"], ["channel_name"]),
    "get_channels": ("List channels, most recently active first, or search by a case-insensitive part of the name. Creating a channel or posting in it counts as activity.", ["query", "recent_first", "limit", "cursor"], []),
    "list_threads": ("List a channel's threads with previews of the first post and latest reply. New threads come first by default; sorting by activity brings threads with recent replies to the top.", ["channel_name", "sort", "recent_first", "limit", "cursor", "max_chars_per_post"], ["channel_name"]),
    "search_posts": ("Search top-level posts and replies, newest first. Text matches are case-insensitive substrings; omit the query to see recent activity. You can narrow by channel, author, or posts after a message ID. Results are previews; read_post can retrieve the full text.", ["channel_name", "query", "after_message_id", "author", "limit", "cursor", "max_chars_per_post"], []),
    "read_thread": ("Read a thread using its first post's message ID. Every page includes previews of the first post and the newest replies; the cursor advances through replies.", ["thread_id", "limit", "cursor", "max_chars_per_post"], ["thread_id"]),
    "read_post": ("Read a post or reply by message ID, without needing its channel. Offsets count Unicode characters; continue at next_offset_chars while it is less than n_chars.", ["message_id", "offset_chars", "limit_chars"], ["message_id"]),
    "subscribe": ("Subscribe yourself or another agent to a channel for new top-level posts, or to a thread for replies. Provide exactly one of channel_name or thread_id. Notifications only reach agents with a running turn; missed notifications are not saved.", ["channel_name", "thread_id", "target_agent"], []),
    "unsubscribe": ("Unsubscribe yourself or another agent from a channel or thread. Provide exactly one of channel_name or thread_id. Posting again does not undo a thread unsubscribe. Agents explicitly named on a post can still receive that notification.", ["channel_name", "thread_id", "target_agent"], []),
    "post": ("Start a thread in an existing or new channel, or reply using the first post's message ID as thread_id. Exactly one destination is required. Posting subscribes you to the thread unless you previously unsubscribed. agents_to_notify sends a one-time notification without subscribing recipients or starting idle agents. Returns metadata, not the post text.", ["text", "channel_name", "new_channel_name", "thread_id", "agents_to_notify"], ["text"]),
}
_FIELDS = {
    "new_channel_name": {"type": "string", "description": "Create and subscribe to this channel."},
    "subscribe": {"type": "boolean", "description": "Subscribe to new top-level posts. Default true."},
    "author": {"type": "string", "description": "An absolute agent path or a reference relative to you."},
    "target_agent": {"type": "string", "description": "An absolute agent path or a reference relative to you."},
    "recent_first": {"type": "boolean", "description": "Newest first by default."},
    "limit": {"type": "integer", "minimum": 1, "description": "Maximum results, default 20; output budgets may return fewer. Continue with next_cursor."},
    "offset_chars": {"type": "integer", "minimum": 0, "description": "Default 0."},
    "limit_chars": {"type": "integer", "minimum": 1, "description": "Maximum characters; further capped by output budget. Defaults: limit_chars 20000, max_chars_per_post 1000."},
    "max_chars_per_post": {"type": "integer", "minimum": 1, "description": "Maximum characters; further capped by output budget. Defaults: limit_chars 20000, max_chars_per_post 1000."},
    "sort": {"type": "string", "enum": ["created", "activity"]},
    "agents_to_notify": {"type": "array", "items": {"type": "string"}, "description": "Absolute agent paths or references relative to you; maximum 256."},
    "cursor": {"type": "string", "description": "Opaque next_cursor from the same query. Keep filters and sorting unchanged. Concurrent posts may shift pages; omit the cursor to refresh the listing."},
}


def _response_budget(context: ToolExecutionContext) -> int:
    if context.source_for_call(context.tool_call_id).kind == "code_mode":
        return 8000
    snapshot = context.model_execution
    policy = snapshot.model_info.truncation_policy if snapshot is not None and snapshot.model_info is not None else None
    if policy is None:
        return 8000
    return min(8000, math.ceil(policy["limit"] * 1.2) * (4 if policy["mode"] == "tokens" else 1))


class MessageBoardTool(BaseTool):
    def __init__(self, name: str):
        self.name = name
        self.description = _DEFINITIONS[name][0]
        self.read_only = name in READ_TOOLS

    def get_schema(self) -> ToolSchema:
        description, fields, required = _DEFINITIONS[self.name]
        return ToolSchema(self.name, description, {"type": "object", "properties": {
            field: deepcopy(_FIELDS.get(field, {"type": "string"})) for field in fields},
            "required": list(required), "additionalProperties": False})

    def get_spec(self) -> ToolSpec:
        return ToolSpec(self.name, toolset="agent", capability="agent.board", exposure="core")

    def validate_input(self, args=None) -> str:
        for field in ("limit", "limit_chars", "max_chars_per_post", "offset_chars"):
            value = (args or {}).get(field)
            if value is not None and value > 0xFFFFFFFF:
                return f"{field} exceeds u32"
        return ""

    async def execute(self, args: dict, context: ToolExecutionContext | None = None) -> ToolResult:
        if len(encode(args).encode()) > 128 * 1024:
            return self._error_result("message-board arguments exceed 128 KiB")
        try:
            actor = BoardActor(context)
            policy = context.metadata["_toolset_policy"]
            options = policy.coordination_options
            if not options.get("message_board_enabled"):
                raise MessageBoardError("message board is disabled for this receiving turn")
            backend = options["message_board_backend"]
            if backend not in SUPPORTED_BOARD_BACKENDS:
                raise MessageBoardError(f"Message-board backend {backend!r} is unavailable")
            budget = _response_budget(context)
            if self.name not in READ_TOOLS and budget < 2048 + len(actor.path.encode()) + len((args.get("target_agent") or "").encode()):
                raise MessageBoardError("Output budget is too small to acknowledge a message-board mutation; no change was made.")
            if backend == "remote":
                from backend.agent.message_board_remote import RemoteAgentMessageBoard
                board = RemoteAgentMessageBoard(actor, context)
            else:
                board = AgentMessageBoard(actor.runtime.message_boards.open(actor.tree, backend), actor)
            source = context.source_for_call(context.tool_call_id)
            invocation = f"{source.cell_id}:{source.runtime_call_id}" if source.kind == "code_mode" else context.tool_call_id
            request_id = f"{context.turn_id}:{invocation}"

            async def perform():
                limit = min(args.get("limit") or 20, 50)
                chars = min(args.get("limit_chars") or 20_000, 20_000) if self.name == "read_post" else min(args.get("max_chars_per_post") or 1000, 20_000)
                largest = limit if self.name == "get_channels" else chars if self.name == "read_post" else max(limit, chars)
                scale = 1
                while True:
                    result, recipients, notice = await board.run(self.name, args, request_id=request_id,
                        limit=max(1, limit // scale), chars=max(1, chars // scale))
                    body = encode(result)
                    if self.name not in READ_TOOLS:
                        for path in recipients:
                            try:
                                target_path, target_id = actor.resolve(path)
                                actor.runtime.message_boards.notify(actor.tree, target_path, target_id, notice)
                            except ValueError:
                                logger.warning("Failed to deliver message-board notification to %s", path, exc_info=True)
                    if len(body.encode()) <= budget:
                        return ToolResult(content=body, native_tool_output=body)
                    if self.name not in READ_TOOLS or largest // scale <= 1:
                        raise MessageBoardError("The output budget is too small for this result's metadata.")
                    scale *= 2

            task = asyncio.create_task(perform())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                context.run_context.retain_lifecycle_task(task, label=f"message-board:{self.name}")
                raise
        except ValueError as exc:
            return self._error_result(str(exc)[:512])


def message_board_tools() -> list[BaseTool]:
    return [MessageBoardTool(name) for name in _DEFINITIONS]
