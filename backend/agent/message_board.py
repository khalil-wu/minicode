"""Tree-scoped Source message boards and current-turn-only notification delivery."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any
from uuid import UUID

from backend.agent.agent_identity import AgentPath, coordination_agent_id
from backend.ids import new_uuid7

logger = logging.getLogger(__name__)
SUPPORTED_BOARD_BACKENDS = frozenset({"in_memory", "local", "remote"})
SCHEMA = """
CREATE TABLE IF NOT EXISTS deleted_boards (board TEXT PRIMARY KEY NOT NULL);
CREATE TABLE IF NOT EXISTS channels (
 board TEXT NOT NULL, name TEXT NOT NULL, name_search TEXT NOT NULL,
 created_at TEXT NOT NULL, timestamp INTEGER NOT NULL, author TEXT NOT NULL,
 PRIMARY KEY(board,name));
CREATE TABLE IF NOT EXISTS posts (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT NOT NULL, id TEXT NOT NULL,
 channel TEXT NOT NULL, root TEXT NOT NULL, author TEXT NOT NULL, timestamp INTEGER NOT NULL,
 body_search TEXT NOT NULL, payload TEXT NOT NULL, request_id TEXT NOT NULL, request TEXT NOT NULL,
 UNIQUE(board,id), UNIQUE(board,request_id));
CREATE INDEX IF NOT EXISTS posts_board_channel_timestamp ON posts(board,channel,timestamp,seq);
CREATE INDEX IF NOT EXISTS posts_board_root_timestamp ON posts(board,root,timestamp,seq);
CREATE INDEX IF NOT EXISTS posts_board_timestamp ON posts(board,timestamp,seq);
CREATE TABLE IF NOT EXISTS subscriptions (
 board TEXT NOT NULL, target TEXT NOT NULL, agent TEXT NOT NULL, PRIMARY KEY(board,target,agent));
CREATE TABLE IF NOT EXISTS subscription_opt_outs (
 board TEXT NOT NULL, target TEXT NOT NULL, agent TEXT NOT NULL, PRIMARY KEY(board,target,agent));
"""


class MessageBoardError(ValueError):
    pass


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise MessageBoardError("invalid message or thread UUID") from exc


def _offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
    except ValueError as exc:
        raise MessageBoardError("invalid cursor") from exc
    if len(raw) != 4 or "=" in cursor or base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
        raise MessageBoardError("invalid cursor")
    return int.from_bytes(raw, "big")


def _page(rows: list, offset: int, limit: int, project=lambda row: row) -> dict:
    more = len(rows) > limit
    results = [project(row) for row in rows[:limit]]
    next_offset = offset + len(results)
    if more and next_offset > 0xFFFFFFFF:
        raise MessageBoardError("cursor offset exceeds the board limit")
    cursor = base64.urlsafe_b64encode(next_offset.to_bytes(4, "big")).decode().rstrip("=") if more else None
    return {"results": results, "n_returned": len(results), "has_more": more, "next_cursor": cursor}


def _preview(payload: str, chars: int) -> dict:
    post = json.loads(payload)
    text = post["text"]
    return {**post["metadata"], "text_preview": text[:chars], "n_chars": len(text), "truncated": len(text) > chars}


def notification(post: dict) -> str:
    suffix = "\n[Use read_post for the rest.]" if post["truncated"] else ""
    return (f"Message Type: CHANNEL_POST\nSender: {post['author']}\nChannel: {post['channel_name']}\n"
        f"Message ID: {post['message_id']}\nThread ID: {post['thread_id']}\nPayload:\n{post['text_preview']}{suffix}")


class BoardDatabase:
    def __init__(self, path: Path | None):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path) if path is not None else ":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.lock = RLock()

    @contextmanager
    def transaction(self, *, write: bool):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield self.connection
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def close(self):
        with self.lock:
            self.connection.close()


@dataclass(frozen=True)
class ActiveBoardTurn:
    actor_id: str
    turn_id: str
    queue: Any


class MessageBoards:
    """One host owns handles; opening a disabled factory creates no database."""
    def __init__(self, state_root: Path):
        self.path = state_root / "agent_message_board_1.sqlite"
        self.local: BoardDatabase | None = None
        self.memory: dict[str, BoardDatabase] = {}
        self.active: dict[tuple[str, str], ActiveBoardTurn] = {}
        self.remote_receivers: dict[tuple[str, str], tuple[str, Any, Any]] = {}

    def open(self, tree: str, backend: str) -> BoardDatabase:
        if backend == "local":
            if self.local is None:
                self.local = BoardDatabase(self.path)
            return self.local
        if backend == "in_memory":
            if tree not in self.memory:
                self.memory[tree] = BoardDatabase(None)
            return self.memory[tree]
        raise MessageBoardError(f"Message-board backend {backend!r} is unavailable")

    def bind_turn(self, record, metadata: dict, queue) -> None:
        if record.conversation_id:
            path = AgentPath.parse(record.agent_path).logical_name
            self.active[(record.conversation_id, path)] = ActiveBoardTurn(
                coordination_agent_id(metadata), record.run_id, queue)

    def unbind_turn(self, record) -> None:
        key = (record.conversation_id, AgentPath.parse(record.agent_path).logical_name)
        current = self.active.get(key)
        if current is not None and current.turn_id == record.run_id:
            del self.active[key]
        receiver = self.remote_receivers.get(key)
        if receiver is not None and receiver[0] == record.run_id:
            del self.remote_receivers[key]
            receiver[1].cancel()
            receiver[2].retain_lifecycle_task(receiver[1], label="remote-message-board-notifications")

    def notify(self, tree: str, path: str, actor_id: str, post: dict, *, expected_turn_id: str | None = None) -> bool:
        active = self.active.get((tree, path))
        if active is None or active.actor_id != actor_id or (expected_turn_id is not None and active.turn_id != expected_turn_id):
            return False
        return active.queue.deliver_message_board_notification(notification(post),
            expected_turn_id=expected_turn_id if expected_turn_id is not None else active.turn_id)

    def delete_tree(self, tree: str) -> None:
        database = self.memory.pop(tree, None)
        if database is not None:
            database.close()
        if self.local is not None or self.path.exists():
            database = self.open(tree, "local")
            with database.transaction(write=True) as db:
                db.execute("INSERT OR IGNORE INTO deleted_boards(board) VALUES(?)", (tree,))
                for table in ("channels", "posts", "subscriptions", "subscription_opt_outs"):
                    db.execute(f"DELETE FROM {table} WHERE board=?", (tree,))

    def close(self) -> None:
        for _, task, owner in self.remote_receivers.values():
            task.cancel()
            owner.retain_lifecycle_task(task, label="remote-message-board-notifications")
        self.remote_receivers.clear()
        for database in self.memory.values():
            database.close()
        self.memory.clear()
        if self.local is not None:
            self.local.close()
            self.local = None
        self.active.clear()


class BoardActor:
    """Resolve a live caller through real parent edges and registered Source paths."""
    def __init__(self, context):
        from backend.tools.agent_control_plane import AgentControlPlane

        self.control = AgentControlPlane(context, source_v2=True)
        self.runtime = self.control.runtime
        self.tree = self.control.conversation_id
        self.actor_id = self.control.actor_id
        if not self.tree or not self.control.has_actor_identity:
            raise MessageBoardError("message board requires a registered agent tree")
        if self.control.actor_is_subagent and self.runtime.get_subagent(self.actor_id) is None:
            raise MessageBoardError("agent has no registered tree path")
        self.path = self.registered_path(self.actor_id)

    def registered_path(self, actor_id: str) -> str:
        current = actor_id
        seen = set()
        path = None
        while current not in seen:
            seen.add(current)
            child = self.runtime.get_subagent(current)
            if child is None:
                root = self.runtime.get_run(current)
                if root is None or root.parent_run_id or root.role != "main" or root.conversation_id != self.tree:
                    raise MessageBoardError("agent belongs to another message board")
                return path or "/root"
            child_path = self.control.normalize_public_tree_path(AgentPath.parse(child.agent_path).logical_name, strict=True)
            if path is None:
                path = child_path
            parent = self.runtime.get_subagent(child.parent_run_id)
            parent_path = AgentPath.parse(parent.agent_path).logical_name if parent is not None else "/root"
            if child_path.rsplit("/", 1)[0] != parent_path:
                raise MessageBoardError("agent does not own its tree path")
            current = child.parent_run_id
        raise MessageBoardError("agent parent graph contains a cycle")

    def resolve(self, reference: str) -> tuple[str, str]:
        path = self.control.normalize_public_tree_path(reference, strict=True)
        if path == "/root":
            root = self.control.root_tree_record()
            if root is None:
                raise MessageBoardError("root agent is unavailable")
            target_id = root["run_id"]
        else:
            target = self.control.resolve_target(path, scope="tree")
            if target is None:
                raise MessageBoardError(f"agent path not found in this board: {path}")
            target_id = target.subagent_id
        if self.registered_path(target_id) != path:
            raise MessageBoardError("agent does not own its tree path")
        return path, target_id


class AgentMessageBoard:
    def __init__(self, database: BoardDatabase, actor: BoardActor):
        self.database, self.actor = database, actor
        self.tree = actor.tree

    def _channel(self, db, name: str) -> dict:
        channel = db.execute("SELECT * FROM channels WHERE board=? AND name=?", (self.tree, name)).fetchone()
        if channel is None:
            raise MessageBoardError("channel not found in this board")
        count = db.execute("SELECT COUNT(*) FROM posts WHERE board=? AND channel=?", (self.tree, name)).fetchone()[0]
        last = db.execute("SELECT id FROM posts WHERE board=? AND channel=? ORDER BY timestamp DESC,seq DESC LIMIT 1", (self.tree, name)).fetchone()
        return {"channel_name": name, "created_at": channel["created_at"], "created_by": channel["author"],
            "message_count": count, "last_message_id": last[0] if last else None}

    def _load_post(self, db, message_id: str, *, root: bool = False):
        row = db.execute("SELECT * FROM posts WHERE board=? AND id=?", (self.tree, _uuid(message_id))).fetchone()
        if row is None:
            raise MessageBoardError("post not found in this board")
        if root and row["id"] != row["root"]:
            raise MessageBoardError("thread_id must identify a top-level post")
        return row

    def _insert_channel(self, db, name: str, now: datetime) -> None:
        if not name or len(name.encode()) > 128 or name.strip() != name or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name):
            raise MessageBoardError("channel names must contain 1–128 bytes without edge whitespace or control characters")
        if db.execute("SELECT 1 FROM channels WHERE board=? AND name=?", (self.tree, name)).fetchone():
            raise MessageBoardError("channel already exists")
        db.execute("INSERT INTO channels VALUES(?,?,?,?,?,?)", (self.tree, name, name.casefold(), now.isoformat().replace("+00:00", "Z"),
            int(now.timestamp() * 1_000_000), self.actor.path))

    def _auto_subscribe(self, db, target: str, path: str) -> None:
        db.execute("INSERT OR IGNORE INTO subscriptions SELECT ?1,?2,?3 WHERE NOT EXISTS(SELECT 1 FROM subscription_opt_outs WHERE board=?1 AND target=?2 AND agent=?3)",
            (self.tree, target, path))

    async def run(self, name: str, args: dict, *, request_id: str = "", limit: int = 20, chars: int = 1000) -> tuple[dict, list[str], dict | None]:
        write = name in {"create_channel", "subscribe", "unsubscribe", "post"}
        with self.database.transaction(write=write) as db:
            if db.execute("SELECT 1 FROM deleted_boards WHERE board=?", (self.tree,)).fetchone():
                raise MessageBoardError("message board has been permanently deleted")
            if name == "create_channel":
                channel = args["channel_name"]
                self._insert_channel(db, channel, datetime.now(timezone.utc))
                if args.get("subscribe", True):
                    self._auto_subscribe(db, encode(["channel", channel]), self.actor.path)
                return self._channel(db, channel), [], None
            if name == "post":
                return self._post(db, args, request_id)
            if name in {"subscribe", "unsubscribe"}:
                return self._subscription(db, args, name == "subscribe"), [], None
            return self._read(db, name, args, limit, chars), [], None

    def _post(self, db, args: dict, request_id: str):
        destinations = [field for field in ("channel_name", "new_channel_name", "thread_id") if args.get(field) is not None]
        if len(destinations) != 1:
            raise MessageBoardError("Supply exactly one of channel_name, new_channel_name or thread_id")
        targets = args.get("agents_to_notify") or []
        if not args["text"] or len(args["text"].encode()) > 64 * 1024 or not request_id or len(request_id.encode()) > 512 or len(targets) > 256:
            raise MessageBoardError("post text, request ID or recipient count exceeds the board limits")
        destination = destinations[0]
        destination_value = _uuid(args[destination]) if destination == "thread_id" else args[destination]
        paths = [self.actor.control.normalize_public_tree_path(target, strict=True) for target in targets]
        request = encode({"destination": [destination, destination_value], "text": args["text"], "agents_to_notify": paths})
        invocation = f"{self.actor.path}:{request_id}"
        existing = db.execute("SELECT request,payload FROM posts WHERE board=? AND request_id=?", (self.tree, invocation)).fetchone()
        if existing is not None:
            if existing["request"] != request:
                raise MessageBoardError("request ID was already used for a different post")
            return json.loads(existing["payload"])["metadata"], [], None
        for path in paths:
            self.actor.resolve(path)
        now = datetime.now(timezone.utc)
        message_id = str(new_uuid7())
        if destination == "thread_id":
            root = self._load_post(db, destination_value, root=True)
            channel, thread_id = root["channel"], root["id"]
            target = encode(["thread", thread_id])
        else:
            channel, thread_id = destination_value, message_id
            target = encode(["channel", channel])
            if destination == "new_channel_name":
                self._insert_channel(db, channel, now)
                self._auto_subscribe(db, target, self.actor.path)
            else:
                self._channel(db, channel)
        recipients = {row[0] for row in db.execute("SELECT agent FROM subscriptions WHERE board=? AND target=?", (self.tree, target))}
        recipients.update(paths)
        recipients.discard(self.actor.path)
        metadata = {"message_id": message_id, "channel_name": channel, "author": self.actor.path,
            "thread_id": thread_id, "created_at": now.isoformat().replace("+00:00", "Z")}
        payload = encode({"metadata": metadata, "text": args["text"]})
        db.execute("INSERT INTO posts(board,id,channel,root,author,timestamp,body_search,payload,request_id,request) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.tree, message_id, channel, thread_id, self.actor.path, int(now.timestamp() * 1_000_000), args["text"].casefold(), payload, invocation, request))
        self._auto_subscribe(db, encode(["thread", thread_id]), self.actor.path)
        return metadata, sorted(recipients), _preview(payload, 150)

    def _subscription(self, db, args: dict, enabled: bool) -> dict:
        fields = [field for field in ("channel_name", "thread_id") if args.get(field) is not None]
        if len(fields) != 1:
            raise MessageBoardError("Supply exactly one of channel_name or thread_id")
        path = self.actor.resolve(args["target_agent"])[0] if args.get("target_agent") is not None else self.actor.path
        if fields[0] == "channel_name":
            channel = self._channel(db, args["channel_name"])
            name, thread_id, last = channel["channel_name"], None, channel["last_message_id"]
            target = encode(["channel", name])
        else:
            root = self._load_post(db, args["thread_id"], root=True)
            last = db.execute("SELECT id FROM posts WHERE board=? AND root=? ORDER BY timestamp DESC,seq DESC LIMIT 1", (self.tree, root["id"])).fetchone()[0]
            name, thread_id = root["channel"], root["id"]
            target = encode(["thread", thread_id])
        selected, removed = ("subscriptions", "subscription_opt_outs") if enabled else ("subscription_opt_outs", "subscriptions")
        db.execute(f"DELETE FROM {removed} WHERE board=? AND target=? AND agent=?", (self.tree, target, path))
        db.execute(f"INSERT OR IGNORE INTO {selected} VALUES(?,?,?)", (self.tree, target, path))
        return {"channel_name": name, "thread_id": thread_id, "target_agent": path, "enabled": enabled, "last_message_id": last}

    def _read(self, db, name: str, args: dict, limit: int, chars: int) -> dict:
        offset = _offset(args.get("cursor"))
        order = "DESC" if args.get("recent_first", True) else "ASC"
        if name == "get_channels":
            rows = list(db.execute(f"SELECT c.name FROM channels c WHERE c.board=? AND instr(c.name_search,?)>0 ORDER BY COALESCE((SELECT MAX(p.timestamp) FROM posts p WHERE p.board=c.board AND p.channel=c.name),c.timestamp) {order},c.name {order} LIMIT ? OFFSET ?",
                (self.tree, (args.get("query") or "").casefold(), limit + 1, offset)))
            return _page(rows, offset, limit, lambda row: self._channel(db, row[0]))
        if name == "read_post":
            row = self._load_post(db, args["message_id"])
            post = json.loads(row["payload"])
            text = post["text"]
            start = min(args.get("offset_chars") or 0, len(text))
            body = text[start:start + chars]
            return {**post["metadata"], "text": body, "n_chars": len(text), "next_offset_chars": start + len(body)}
        if name == "list_threads":
            self._channel(db, args["channel_name"])
            activity = "MAX(p.timestamp,COALESCE((SELECT MAX(q.timestamp) FROM posts q WHERE q.board=p.board AND q.root=p.id),p.timestamp))"
            sorting = activity if args.get("sort", "created") == "activity" else "p.timestamp"
            rows = list(db.execute(f"SELECT p.* FROM posts p WHERE p.board=? AND p.channel=? AND p.id=p.root ORDER BY {sorting} {order},p.seq {order} LIMIT ? OFFSET ?",
                (self.tree, args["channel_name"], limit + 1, offset)))
            bound = min(chars, 20_000 // (2 * max(1, min(limit, len(rows)))))
            def summary(row):
                reply = db.execute("SELECT * FROM posts WHERE board=? AND root=? AND id<>root ORDER BY timestamp DESC,seq DESC LIMIT 1", (self.tree, row["id"])).fetchone()
                count = db.execute("SELECT COUNT(*) FROM posts WHERE board=? AND root=? AND id<>root", (self.tree, row["id"])).fetchone()[0]
                root = _preview(row["payload"], bound)
                latest = _preview(reply["payload"], bound) if reply is not None else None
                return {"thread_id": row["id"], "root_post": root, "reply_count": count,
                    "last_activity_at": latest["created_at"] if reply is not None and reply["timestamp"] > row["timestamp"] else root["created_at"], "latest_reply": latest}
            return _page(rows, offset, limit, summary)
        if name == "read_thread":
            root = self._load_post(db, args["thread_id"], root=True)
            rows = list(db.execute("SELECT * FROM posts WHERE board=? AND root=? AND id<>root ORDER BY timestamp DESC,seq DESC LIMIT ? OFFSET ?",
                (self.tree, root["id"], limit + 1, offset)))
            bound = min(chars, 20_000 // (min(limit, len(rows)) + 1))
            return {"root_post": _preview(root["payload"], bound), **_page(rows, offset, limit, lambda row: _preview(row["payload"], bound))}
        if name == "search_posts":
            conditions, params = ["board=?"], [self.tree]
            for field, column in (("channel_name", "channel"), ("author", "author")):
                if args.get(field) is not None:
                    value = self.actor.control.normalize_public_tree_path(args[field], strict=True) if field == "author" else args[field]
                    conditions.append(f"{column}=?")
                    params.append(value)
            if args.get("query") is not None:
                conditions.append("instr(body_search,?)>0")
                params.append(args["query"].casefold())
            if args.get("after_message_id") is not None:
                after = self._load_post(db, args["after_message_id"])
                conditions.append("(timestamp,seq)>(?,?)")
                params.extend([after["timestamp"], after["seq"]])
            rows = list(db.execute("SELECT * FROM posts WHERE " + " AND ".join(conditions) + " ORDER BY timestamp DESC,seq DESC LIMIT ? OFFSET ?", (*params, limit + 1, offset)))
            bound = min(chars, 20_000 // max(1, min(limit, len(rows))))
            return _page(rows, offset, limit, lambda row: _preview(row["payload"], bound))
        raise MessageBoardError(f"unknown message-board operation: {name}")
