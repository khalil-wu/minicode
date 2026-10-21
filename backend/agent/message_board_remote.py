"""Source remote-board calls and receivers owned by the exact active turn."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import logging
import os
from uuid import UUID

import httpx
from httpx_sse import SSEError
from pydantic import BaseModel, ConfigDict, SecretStr, TypeAdapter, ValidationError

from backend.agent.message_board import BoardActor, MessageBoardError, _uuid, encode, notification
from backend.agent.message_board_remote_protocol import (
    MAX_BODY, MAX_RESPONSE, BoardNotification, ChannelSummary, Failure, Page,
    PostContent, PostMetadata, PostPreview, SubscriptionState, ThreadPage,
    ThreadSummary, WireRecord, board_events, board_identity, member_identity,
)
from backend.agents.source_roles import source_agent_options
from backend.llm.proxy_policy import provider_httpx_proxy_kwargs

logger = logging.getLogger(__name__)
_RESULTS = {"create_channel": ChannelSummary, "list_channels": Page[ChannelSummary],
    "post": PostMetadata, "list_threads": Page[ThreadSummary], "search_posts": Page[PostPreview],
    "read_thread": ThreadPage, "read_post": PostContent, "set_subscription": SubscriptionState}


class RemoteBoardConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    url: str
    bearer_token: SecretStr | None = None
    bearer_token_env_var: str | None = None


def _credential(value: str) -> str:
    if not 32 <= len(value.encode()) <= 4096 or not all(33 <= ord(char) <= 126 for char in value):
        raise MessageBoardError("message-board credentials must contain 32–4096 ASCII bytes without whitespace")
    return value


async def _wall_clock(_caller: UUID) -> datetime:
    return datetime.now(timezone.utc)


class RemoteAgentMessageBoard:
    def __init__(self, actor: BoardActor, context, *, clock=_wall_clock):
        config = context.model_execution.config
        raw = config.config_layer_stack.effective_config()["features"]["multi_agent_v2"]["message_board_remote"]
        settings = RemoteBoardConfig.model_validate(raw)
        endpoint = httpx.URL(settings.url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.host:
            raise MessageBoardError("message-board endpoint must be an HTTP(S) URL")
        if "?" in settings.url or "#" in settings.url:
            raise MessageBoardError("message-board endpoint must not contain a query or fragment")
        if settings.bearer_token_env_var is not None:
            try:
                token = os.environ[settings.bearer_token_env_var]
            except KeyError as exc:
                raise MessageBoardError("message-board credential environment variable is missing or invalid") from exc
        elif settings.bearer_token is not None:
            token = settings.bearer_token.get_secret_value()
        else:
            raise MessageBoardError("remote message board requires a credential")
        self._token = _credential(token)
        self.actor, self.clock = actor, clock
        self.identity = board_identity(actor.tree)
        self.caller = member_identity(actor.tree, actor.path)
        self.url = endpoint.copy_with(username="", password="", path=f"{endpoint.path.rstrip('/')}/v1/boards/{self.identity}")
        self.http_options = provider_httpx_proxy_kwargs(settings.url, proxy_mode=config.llm.proxy_mode)

    def _client(self):
        return httpx.AsyncClient(**self.http_options, timeout=None, follow_redirects=False,
            headers={"authorization": f"Bearer {self._token}"})

    async def _decode(self, response: httpx.Response, model):
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=4096):
            if len(body) + len(chunk) > MAX_RESPONSE:
                raise OSError("board response exceeds the service limit")
            body.extend(chunk)
        if not response.is_success:
            try:
                failure = Failure.model_validate_json(bytes(body))
            except ValidationError as error:
                raise OSError(f"invalid board error response (HTTP {response.status_code})") from error
            error = OSError if response.status_code >= 500 else MessageBoardError
            raise error(f"{failure.code}: {failure.message}")
        if isinstance(model, TypeAdapter):
            return model.validate_json(bytes(body), strict=True)
        return model.model_validate_json(bytes(body))

    async def _request(self, method: str, suffix: str, *, payload=None, model=None):
        content = encode(payload).encode() if payload is not None else None
        if suffix == "/call" and len(content) > MAX_BODY:
            raise MessageBoardError("board request exceeds the service limit")
        async with asyncio.timeout(30):
            async with self._client() as client:
                async with client.stream(method, str(self.url) + suffix, content=content,
                    headers={"content-type": "application/json"} if content is not None else {}) as response:
                    if model is None and response.is_success:
                        return None
                    return await self._decode(response, model)

    async def create_board(self) -> UUID:
        """Research-host provisioning; never called automatically by an agent tool."""
        return await self._request("PUT", "", model=TypeAdapter(UUID))

    async def register_members(self, members: dict[UUID, str]) -> SecretStr:
        token = await self._request("POST", "/members",
            payload={"members": {str(caller): path for caller, path in members.items()}}, model=TypeAdapter(str))
        return SecretStr(_credential(token))

    async def delete_board(self) -> None:
        await self._request("DELETE", "")

    def _parameters(self, name: str, args: dict, request_id: str, limit: int, chars: int):
        page = {"cursor": args.get("cursor"), "limit": limit}
        direction = "NewestFirst" if args.get("recent_first", True) else "OldestFirst"
        if name == "create_channel":
            return name, {"channel_name": args["channel_name"], "subscription": "Subscribe" if args.get("subscribe", True) else "Unsubscribe"}
        if name == "get_channels":
            return "list_channels", {"query": args.get("query"), "direction": direction, "page": page}
        if name == "list_threads":
            return name, {"channel_name": args["channel_name"], "sort": args.get("sort", "created"),
                "direction": direction, "page": page, "max_chars_per_post": chars}
        if name == "search_posts":
            author = args.get("author")
            after = args.get("after_message_id")
            return name, {"channel_name": args.get("channel_name"), "query": args.get("query"),
                "after_message_id": _uuid(after) if after is not None else None,
                "author": self.actor.resolve(author)[0] if author is not None else None,
                "page": page, "max_chars_per_post": chars}
        if name == "read_thread":
            return name, {"thread_id": _uuid(args["thread_id"]), "page": page, "max_chars_per_post": chars}
        if name == "read_post":
            return name, {"message_id": _uuid(args["message_id"]), "offset_chars": args.get("offset_chars", 0), "limit_chars": chars}
        if name in {"subscribe", "unsubscribe"}:
            destinations = [field for field in ("channel_name", "thread_id") if args.get(field) is not None]
            if len(destinations) != 1:
                raise MessageBoardError("Supply exactly one of channel_name or thread_id")
            field = destinations[0]
            value = _uuid(args[field]) if field == "thread_id" else args[field]
            target = args.get("target_agent")
            return "set_subscription", {"target": {"Thread" if field == "thread_id" else "Channel": value},
                "target_agent": self.actor.resolve(target)[0] if target is not None else None,
                "change": "Subscribe" if name == "subscribe" else "Unsubscribe"}
        destinations = [field for field in ("channel_name", "new_channel_name", "thread_id") if args.get(field) is not None]
        if len(destinations) != 1:
            raise MessageBoardError("Supply exactly one of channel_name, new_channel_name or thread_id")
        field = destinations[0]
        value = _uuid(args[field]) if field == "thread_id" else args[field]
        return "post", {"request_id": request_id, "destination": {
            {"channel_name": "Channel", "new_channel_name": "NewChannel", "thread_id": "Thread"}[field]: value},
            "text": args["text"], "agents_to_notify": [self.actor.resolve(target)[0] for target in args.get("agents_to_notify", [])]}

    async def run(self, name: str, args: dict, *, request_id: str = "", limit: int = 20, chars: int = 1000):
        method, params = self._parameters(name, args, request_id, limit, chars)
        timestamp = None
        if method in {"create_channel", "post"}:
            try:
                timestamp = (await self.clock(self.caller)).astimezone(timezone.utc).isoformat()
            except OSError:
                if method != "post":
                    raise
                # Source permits a committed post to be replayed after its clock fails.
        result = await self._request("POST", "/call", model=_RESULTS[method],
            payload={"caller": str(self.caller), "timestamp": timestamp, "method": method, "params": params})
        return result.model_dump(mode="json"), [], None

    async def notifications(self, turn_id: str, on_ready):
        async with AsyncExitStack() as stack:
            async with asyncio.timeout(30):
                client = await stack.enter_async_context(self._client())
                response = await stack.enter_async_context(client.stream("POST", str(self.url) + "/notifications",
                    json={"caller": str(self.caller), "turn_id": turn_id}))
                if not response.is_success:
                    await self._decode(response, WireRecord)
                events = board_events(response)
                first = await anext(events, None)
                if first is None or first.event != "ready":
                    raise MessageBoardError("notification stream did not acknowledge readiness")
            on_ready()
            async for event in events:
                try:
                    if event.event != "notification":
                        raise MessageBoardError("unexpected notification event")
                    notice = BoardNotification.model_validate_json(event.data)
                    if notice.recipient != self.caller or notice.turn_id != turn_id or len(notice.post.text_preview) > 150:
                        raise MessageBoardError("wrong recipient/turn or oversized notification preview")
                except ValueError as error:
                    logger.warning("Skipping invalid remote board notification: %s", type(error).__name__)
                    continue
                yield notice.post.model_dump(mode="json")


async def start_remote_board_notifications(context) -> None:
    snapshot = context.model_execution
    if snapshot is None:
        return
    options = source_agent_options(snapshot.config, model=snapshot.model, definition=snapshot.model_info)
    if not options["message_board_enabled"] or options["message_board_backend"] != "remote":
        return
    actor = BoardActor(context)
    owner = actor.runtime.message_boards
    turn_id = context.run_context.turn_input_queue.turn_id
    key = (actor.tree, actor.path)
    existing = owner.remote_receivers.get(key)
    if existing is not None and existing[0] == turn_id:
        return
    board = RemoteAgentMessageBoard(actor, context)
    first_attempt = asyncio.get_running_loop().create_future()

    def ready():
        if not first_attempt.done():
            first_attempt.set_result(None)

    async def receive():
        while True:
            try:
                async for post in board.notifications(turn_id, ready):
                    notice_bytes = len(notification(post).encode()) + len(post["author"].encode()) + len(actor.path.encode())
                    if notice_bytes > 1024:
                        logger.warning("Skipping remote board notice exceeding the context budget")
                        continue
                    owner.notify(actor.tree, actor.path, actor.actor_id, post, expected_turn_id=turn_id)
            except (httpx.HTTPError, OSError, ValueError, SSEError) as error:
                logger.warning("Remote board notification connection ended: %s", type(error).__name__)
            except BaseException as error:
                if not first_attempt.done():
                    first_attempt.set_exception(error)
                raise
            ready()
            # Source reconnects only this active turn, without replaying missed previews.
            await asyncio.sleep(1)

    task = asyncio.create_task(receive(), name="remote-message-board-notifications")
    owner.remote_receivers[key] = (turn_id, task, context.run_context)
    await first_attempt
