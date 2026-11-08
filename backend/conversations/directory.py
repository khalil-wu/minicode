"""Indexed thread summaries and keyset pagination over committed conversations."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import os
import sqlite3

from backend.conversations.models import ConversationSummary


SORT_COLUMNS = {"created_at": "created_at", "updated_at": "updated_at", "recency_at": "recency_at",
    "section_position": "section_position", "sidebar_position": "sidebar_position"}


def _decode_summary(payload):
    from backend.conversations.read_state import initial_read_state

    item = json.loads(payload)
    item["read_state"] = item.get("read_state") or initial_read_state(item["id"], item.get("has_unread_turn", False))
    return item


class ConversationDirectory:
    def __init__(self, repository):
        self.repository = repository
        self.path = repository.storage_root / ".conversation-directory.sqlite3"
        self._indexed_source = None

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS directory_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    instance_id TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, recency_at TEXT NOT NULL, provider TEXT NOT NULL,
                    source_kind TEXT NOT NULL, archived INTEGER NOT NULL, conversation_type TEXT NOT NULL,
                    project_id TEXT, project_key TEXT NOT NULL, cwd TEXT NOT NULL, parent_id TEXT NOT NULL, pinned INTEGER NOT NULL,
                    payload TEXT NOT NULL, change_revision INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS directory_created ON threads(archived,conversation_type,created_at,id);
                CREATE INDEX IF NOT EXISTS directory_updated ON threads(archived,conversation_type,updated_at,id);
                CREATE INDEX IF NOT EXISTS directory_recency ON threads(archived,conversation_type,recency_at,id);
                CREATE INDEX IF NOT EXISTS directory_project ON threads(project_id,archived,updated_at,id);
                CREATE INDEX IF NOT EXISTS directory_sidebar_project ON threads(project_key,archived,updated_at,id);
                CREATE INDEX IF NOT EXISTS directory_parent ON threads(parent_id);
            """)
            with connection:
                yield connection
        finally:
            connection.close()

    _UPSERT = """INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET created_at=excluded.created_at, updated_at=excluded.updated_at,
            recency_at=excluded.recency_at, provider=excluded.provider, source_kind=excluded.source_kind,
            archived=excluded.archived, conversation_type=excluded.conversation_type,
            project_id=excluded.project_id, project_key=excluded.project_key, cwd=excluded.cwd, parent_id=excluded.parent_id,
            pinned=excluded.pinned, payload=excluded.payload, change_revision=excluded.change_revision
        WHERE threads.payload != excluded.payload"""

    @staticmethod
    def _values(summary, revision, project_keys):
        from backend.conversations.sidebar_order import project_key

        membership = summary.project_id if summary.project_id is not None else summary.worktree_path or summary.workspace_root
        if membership not in project_keys:
            project_keys[membership] = project_key(membership)
        # Encoding owns the resulting string; deepcopying all summary fields
        # first adds no ownership boundary to the stored JSON.
        payload = json.dumps(vars(summary), ensure_ascii=False, separators=(",", ":"))
        return (summary.id, summary.created_at, summary.updated_at, summary.content_updated_at or summary.updated_at,
            summary.model_selection.get("provider", ""), summary.thread_source, summary.archived, summary.conversation_type,
            summary.project_id, project_keys[membership], summary.execution_cwd or summary.worktree_path or summary.workspace_root,
            summary.parent_conversation_id, summary.pinned, payload, revision)

    def committed(self, summary: ConversationSummary | None, *, conversation_id: str, revision: int):
        # Until the first directory query, the existing manifests remain the
        # only index. Once built, every writer publishes its committed row.
        if not self.path.exists():
            return
        with self.connect() as connection:
            if summary is None:
                connection.execute("DELETE FROM threads WHERE id=?", (conversation_id,))
            else:
                connection.execute(self._UPSERT, self._values(summary, revision, {}))
            # A missing revision may be an interrupted publication. Leave the
            # repair boundary behind it so a normal read rebuilds that gap.
            connection.execute("UPDATE directory_state SET revision=? WHERE singleton=1 AND revision=?",
                (revision, revision - 1))

    def reordered(self, revision):
        if self.path.exists():
            with self.connect() as connection:
                connection.execute("UPDATE directory_state SET revision=? WHERE singleton=1 AND revision=?", (revision, revision - 1))

    def _repair(self):
        repository = self.repository
        while True:
            with repository._store_lock():
                instance = repository._read_or_create_store_instance_id_unlocked()
                revision = repository._read_store_revision_unlocked()
            # Cold migration reads atomic manifests outside the publication
            # lock. A concurrent writer changes revision and restarts capture.
            with repository._process_lock:
                repository._ensure_summary_index_loaded()
                summaries = dict(repository._summary_index)
                source = (instance, revision, dict(repository._summary_index_stamps))
            project_keys = {}
            prepared = (None if source == self._indexed_source else
                [self._values(summary, revision, project_keys) for summary in summaries.values()])
            with repository._store_lock(), self.connect() as connection:
                if revision != repository._read_store_revision_unlocked() or instance != repository._read_or_create_store_instance_id_unlocked():
                    continue
                state = connection.execute("SELECT instance_id,revision FROM directory_state WHERE singleton=1").fetchone()
                # Discovery still observes external file edits. An unchanged
                # source and published index need no JSON encoding or upserts.
                if prepared is None and state is not None and (state[0], state[1]) == (instance, revision):
                    return
                if prepared is None:
                    prepared = [self._values(summary, revision, project_keys) for summary in summaries.values()]
                previous = {row[0] for row in connection.execute("SELECT id FROM threads")}
                if state is not None and state[0] != instance:
                    connection.execute("DELETE FROM threads")
                    previous = set()
                connection.executemany(self._UPSERT, prepared)
                for identifier in previous - summaries.keys():
                    connection.execute("DELETE FROM threads WHERE id=?", (identifier,))
                connection.execute("INSERT OR REPLACE INTO directory_state VALUES (1,?,?)", (instance, revision))
            self._indexed_source = source
            return

    @staticmethod
    def _cursor(instance, sort_key, direction, value, identifier, *, backwards=False):
        payload = json.dumps([1, instance, sort_key, direction, value, identifier, backwards], separators=(",", ":"))
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    @staticmethod
    def _read_cursor(cursor, instance, sort_key, direction):
        try:
            values = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
            version, stored_instance, stored_key, stored_direction, value, identifier, backwards = values
            if (version != 1 or stored_instance != instance or stored_key != sort_key or stored_direction != direction
                    or not isinstance(identifier, str) or not isinstance(backwards, bool)
                    or (not isinstance(value, int) if sort_key.endswith("_position") else not isinstance(value, str))):
                raise ValueError("cursor does not match this directory or ordering")
            return value, identifier, backwards
        except (ValueError, TypeError, UnicodeDecodeError) as error:
            raise ValueError("Invalid conversation directory cursor") from error

    def page(self, query: dict | None = None):
        query = query or {}
        sort_key = query.get("sort_key") or "created_at"
        if sort_key not in SORT_COLUMNS:
            raise ValueError("Unknown conversation directory sort key")
        column = SORT_COLUMNS[sort_key]
        direction = query.get("sort_direction") or ("asc" if sort_key.endswith("_position") else "desc")
        if direction not in {"asc", "desc"}:
            raise ValueError("sort_direction must be asc or desc")
        limit = min(100, max(1, query.get("limit") if query.get("limit") is not None else 25))
        excluded = query.get("excluded_thread_ids") or []
        if len(excluded) > 100:
            raise ValueError("excluded_thread_ids accepts at most 100 entries")
        for identifier in excluded:
            self.repository._safe_id(identifier)
        if query.get("originators"):
            raise ValueError("originator filtering is not supported by the local thread directory")
        if query.get("parent_thread_id") and query.get("ancestor_thread_id"):
            raise ValueError("parent_thread_id and ancestor_thread_id are mutually exclusive")
        if query.get("attached_to_thread_id") is not None:
            raise ValueError("attached thread listing requires persistent attached-thread storage")
        if not query.get("use_state_db_only", False):
            self._repair()
        with self.repository._store_lock(), self.connect() as connection:
            state = connection.execute("SELECT * FROM directory_state WHERE singleton=1").fetchone()
            if state is None:
                raise ValueError("Conversation directory is not initialized; perform a normal directory read first")
            clauses, params = ["archived=?"], [bool(query.get("archived"))]
            if query.get("conversation_type") is not None:
                clauses.append("conversation_type=?")
                params.append(query["conversation_type"])
            sources = query.get("source_kinds")
            if sources:
                clauses.append("source_kind IN (" + ",".join("?" for _ in sources) + ")")
                params.extend(sources)
            else:
                clauses.append("conversation_type='main'")
                if query.get("conversation_type") is None and not (query.get("parent_thread_id") or query.get("ancestor_thread_id")):
                    clauses.append("source_kind IN ('user','cli','vscode','appServer','exec')")
            for key, field in (("model_providers", "provider"), ("excluded_thread_ids", "id")):
                values = query.get(key)
                if values:
                    clauses.append(field + (" NOT IN (" if key == "excluded_thread_ids" else " IN (") + ",".join("?" for _ in values) + ")")
                    params.extend(values)
            if "project_id" in query:
                if query["project_id"] == "":
                    raise ValueError("project_id must not be empty")
                if query["project_id"] is None:
                    clauses.append("(project_id IS NULL OR project_id='')")
                else:
                    clauses.append("project_id=?")
                    params.append(query["project_id"])
            cwd = query.get("cwd")
            if cwd is not None:
                paths = [cwd] if isinstance(cwd, str) else cwd
                clauses.append("cwd IN (" + ",".join("?" for _ in paths) + ")")
                params.extend(path if path.startswith("file:") else os.path.abspath(path) for path in paths)
            if query.get("search_term"):
                clauses.append("instr(lower(json_extract(payload, '$.title')),lower(?))>0")
                params.append(query["search_term"])
            if query.get("parent_thread_id"):
                clauses.append("parent_id=?")
                params.append(query["parent_thread_id"])
            if query.get("ancestor_thread_id"):
                clauses.append("id IN (WITH RECURSIVE descendants(id) AS (SELECT id FROM threads WHERE parent_id=? "
                    "UNION SELECT child.id FROM threads child JOIN descendants parent ON child.parent_id=parent.id) SELECT id FROM descendants)")
                params.append(query["ancestor_thread_id"])
                clauses.append("id!=?")
                params.append(query["ancestor_thread_id"])
            if "pinned" in query:
                clauses.append("pinned=?")
                params.append(query["pinned"])
            if "_sidebar_project" in query:
                aliases = query.get("_sidebar_legacy_roots", ())
                clauses.append("(project_key=? OR (project_id IS NULL AND project_key IN (" + ",".join("?" for _ in aliases) + ")))")
                params.extend((query["_sidebar_project"], *aliases))
            connection.execute("CREATE TEMP TABLE positions (thread_id TEXT PRIMARY KEY,section_id TEXT,position INTEGER)")
            connection.execute("CREATE TEMP TABLE sidebar_positions (thread_id TEXT PRIMARY KEY,position INTEGER)")
            connection.executemany("INSERT INTO sidebar_positions VALUES (?,?)",
                ((identifier, position) for position, identifier in enumerate(query.get("_sidebar_order_ids", ()))))
            for section in self.repository.thread_sections.list():
                connection.executemany("INSERT INTO positions VALUES (?,?,?)", [(key[7:], section["id"], index)
                    for index, key in enumerate(section["item_keys"]) if key.startswith("thread:")])
            if "section_id" in query:
                clauses.append("positions.section_id IS ?")
                params.append(query["section_id"])
            cursor = query.get("cursor")
            backwards = False
            if cursor:
                value, identifier, backwards = self._read_cursor(cursor, state["instance_id"], sort_key, direction)
                ascending = (direction == "asc") != backwards
                clauses.append(f"({column},id) {'>' if ascending else '<'} (?,?)")
                params.extend((value, identifier))
            order = "ASC" if (direction == "asc") != backwards else "DESC"
            rows = connection.execute("SELECT * FROM (SELECT threads.*,coalesce(positions.position,9223372036854775807) AS section_position,positions.section_id, "
                "coalesce(sidebar_positions.position,9223372036854775807) AS sidebar_position "
                "FROM threads LEFT JOIN positions ON positions.thread_id=threads.id "
                "LEFT JOIN sidebar_positions ON sidebar_positions.thread_id=threads.id) WHERE " + " AND ".join(clauses).replace("positions.section_id", "section_id")
                + f" ORDER BY {column} {order},id {order} LIMIT ?", [*params, limit + 1]).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            if backwards:
                rows.reverse()
            cursor_for = lambda row, back: self._cursor(state["instance_id"], sort_key, direction, row[column], row["id"], backwards=back)
            return {"data": [_decode_summary(row["payload"]) for row in rows],
                "next_cursor": cursor_for(rows[-1], False) if rows and (has_more if not backwards else True) else None,
                "backwards_cursor": cursor_for(rows[0], True) if rows else None,
                "inventory_instance_id": state["instance_id"], "inventory_revision": state["revision"]}

    def ensure_current(self):
        """Repair an interrupted index publication before desktop pagination."""
        with self.repository._store_lock(), self.connect() as connection:
            state = connection.execute("SELECT * FROM directory_state WHERE singleton=1").fetchone()
            current = (state is not None and state["instance_id"] == self.repository._read_or_create_store_instance_id_unlocked()
                and state["revision"] == self.repository._read_store_revision_unlocked())
        if not current:
            self._repair()

    def all_summaries(self, conversation_type=None):
        """Legacy full-list consumers share the same scan/publication boundary."""
        self._repair()
        with self.repository._store_lock(), self.connect() as connection:
            state = connection.execute("SELECT instance_id,revision FROM directory_state WHERE singleton=1").fetchone()
            rows = connection.execute("SELECT payload FROM threads" + (" WHERE conversation_type=?" if conversation_type is not None else "")
                + " ORDER BY created_at DESC,id DESC", (conversation_type,) if conversation_type is not None else ()).fetchall()
        return state["instance_id"], state["revision"], [ConversationSummary.from_dict(json.loads(row[0])) for row in rows]

    def inventory(self, *, known_ids=(), since_revision=0, active_id=None, project_keys=()):
        """A sidebar refresh publishes visible heads and changes to known rows."""
        self.ensure_current()
        with self.repository._store_lock(), self.connect() as connection:
            page = self.sidebar_page(limit=25)
            selected = {item["id"]: item for item in page["data"]}
            for row in connection.execute("SELECT payload FROM threads WHERE pinned=1 AND archived=0 AND conversation_type='main'"):
                item = _decode_summary(row[0])
                selected[item["id"]] = item
            sections = self.repository.thread_sections.list()
            wanted = {active_id} if active_id else set()
            wanted.update(key[7:] for section in sections for key in section["item_keys"] if key.startswith("thread:"))
            wanted.update(known_ids)
            connection.execute("CREATE TEMP TABLE known (id TEXT PRIMARY KEY,refresh INTEGER)")
            known = set(known_ids)
            connection.executemany("INSERT INTO known VALUES (?,?)", ((identifier, identifier not in known) for identifier in wanted))
            for row in connection.execute("SELECT payload FROM threads JOIN known USING(id) WHERE change_revision> ? OR id=? OR pinned=1 OR known.refresh=1",
                    (since_revision, active_id)):
                item = _decode_summary(row[0])
                selected[item["id"]] = item
            project_pages = {}
            for key in project_keys:
                project_page = self.sidebar_page(project_key=key, limit=5)
                selected.update((item["id"], item) for item in project_page["data"])
                project_pages[key] = project_page["next_cursor"]
            removed = [row[0] for row in connection.execute("SELECT known.id FROM known LEFT JOIN threads USING(id) WHERE threads.id IS NULL")]
            summaries = [ConversationSummary.from_dict(item) for item in selected.values()]
            pinned = self.repository._read_pinned_order()
            return {"conversations": list(selected.values()), "partial_inventory": True,
                "removed_conversation_ids": removed, "directory_page": {"next_cursor": page["next_cursor"], "project_cursors": project_pages},
                "inventory_instance_id": page["inventory_instance_id"], "inventory_revision": page["inventory_revision"],
                "pinned_conversation_ids": self.repository._ordered_pinned_ids(summaries, pinned["thread_ids"]),
                "pinned_sort_mode": pinned["sort_mode"], "sidebar_order": self.repository._read_sidebar_order(),
                "sidebar_sections": sections}

    def sidebar_page(self, *, project_key=None, cursor=None, limit=25):
        order = self.repository._read_sidebar_order()
        query = {"limit": limit, "cursor": cursor, "sort_key": "updated_at", "pinned": False,
            "section_id": None, "use_state_db_only": True, "conversation_type": "main"}
        if project_key is not None:
            query["_sidebar_project"] = project_key
            from backend.workspace.recent_projects import RecentProjectStore
            from backend.conversations.sidebar_order import project_key as normalized_project_key

            query["_sidebar_legacy_roots"] = tuple(normalized_project_key(root)
                for project in RecentProjectStore().list() if normalized_project_key(project.path) == project_key and project.named
                for root in project.workspace_roots)
        if order["sort_mode"] == "manual":
            query.update(sort_key="sidebar_position", _sidebar_order_ids=(order["recent_thread_ids"]
                if project_key is None else order["project_thread_orders"].get(project_key, [])))
        return self.page(query)
