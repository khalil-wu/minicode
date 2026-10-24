"""Session-owned, paged search of public conversation bodies."""
from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

from backend.conversations.search import search_conversation_summary
from backend.owner_scope import canonical_workspace_root
from backend.services.chat_api_service import ChatApiServiceError


def conversation_search_page(
    session: Any, *, session_id: str, query: str, archived: bool = False,
    project: str | None = None, sort: str = "updated", cursor: str = "", limit: int = 50,
) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise ChatApiServiceError(422, "Search requires a non-empty query.")
    if not session.config.feature_flags.enabled("global_search"):
        raise ChatApiServiceError(403, "Conversation search is disabled.")
    repository = session.conversation_repo
    store_id, _, summaries = repository.list_conversations_with_revision(conversation_type="main")
    project_key = canonical_workspace_root(project) if project is not None else None
    owner = {"session_id": session_id, "store": store_id, "query": query,
             "archived": archived, "project": project_key, "sort": sort}
    after = None
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        except (binascii.Error, ValueError, UnicodeError) as exc:
            raise ChatApiServiceError(400, "Invalid conversation search cursor.") from exc
        if not isinstance(decoded, dict) or decoded.get("owner") != owner:
            raise ChatApiServiceError(409, "The search cursor belongs to a different session or query.")
        after = decoded.get("after")
        if not isinstance(after, list) or len(after) != 2 or not all(isinstance(value, str) for value in after):
            raise ChatApiServiceError(400, "Invalid conversation search cursor.")

    def sort_key(summary):
        return (summary.title.casefold(), summary.id) if sort == "alphabetical" else (
            summary.created_at if sort == "created" else summary.updated_at, summary.id)

    candidates = [summary for summary in summaries if summary.archived == archived
                  and (project_key is None or canonical_workspace_root(
                      summary.project_id or summary.workspace_root or summary.worktree_path) == project_key)]
    candidates.sort(key=sort_key, reverse=sort != "alphabetical")
    if after is not None:
        candidates = [summary for summary in candidates if
                      (sort_key(summary) > tuple(after) if sort == "alphabetical" else sort_key(summary) < tuple(after))]
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    results = []
    scanned = 0
    # Non-matching conversations also advance the cursor. An empty result page
    # can therefore be followed without scanning the entire store in one HTTP request.
    scan_limit = min(200, max(50, limit * 4))
    for summary in candidates[:scan_limit]:
        scanned += 1
        match = repository.find_transcript_match(summary.id, pattern)
        if match is None:
            continue
        results.append({"conversation": search_conversation_summary(summary), **match})
        if len(results) == limit:
            break
    if scanned == len(candidates):
        return {"data": results, "next_cursor": None}
    next_cursor = base64.urlsafe_b64encode(json.dumps({"owner": owner, "after": sort_key(candidates[scanned - 1])},
                                                   ensure_ascii=False).encode()).decode().rstrip("=")
    return {"data": results, "next_cursor": next_cursor}
