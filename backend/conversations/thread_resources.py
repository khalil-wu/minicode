"""Durable thread resource references, independent of uploaded message files."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, UTC
import json
from urllib.parse import quote, unquote, urlparse

from backend.atomic_io import canonical_file_path_key
from backend.ids import new_uuid7


def resource_reference(attachment_type, identity_key, payload):
    return {"id": new_uuid7(), "attachment_type": attachment_type, "identity_key": identity_key,
        "payload": deepcopy(payload), "created_at": int(datetime.now(UTC).timestamp())}


def validate_resource_identity(attachment_type, identity_key, payload=None):
    for name, value in (("attachment_type", attachment_type), ("identity_key", identity_key)):
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 256:
            raise ValueError(f"{name} must be a nonempty string of at most 256 bytes")
    if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > 64 * 1024:
        raise ValueError("Resource payload must not exceed 65536 bytes")


def pull_request_identity(url):
    parsed = urlparse(url)
    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A pull request URL must identify its original host, owner, repository and number")
    merge_route = next((index for index in range(2, len(parts) - 2) if parts[index:index + 2] == ["-", "merge_requests"]), None)
    if len(parts) >= 4 and parts[2] == "pull":
        owner, repository, number = parts[0], parts[1], parts[3]
        project_parts = parts[:2]
    elif merge_route is not None:
        split = merge_route
        if parsed.scheme != "https" or parsed.port not in {None, 443} or parsed.username or parsed.password or "\\" in url:
            raise ValueError("A GitLab merge request requires an HTTPS project URL")
        owner, repository, number = "/".join(parts[:split - 1]), parts[split - 1], parts[split + 2] if len(parts) > split + 2 else ""
        project_parts = parts[:split]
    else:
        raise ValueError("A pull request URL must identify its original host, owner, repository and number")
    if not owner or not repository or not number.isascii() or not number.isdigit() or int(number) < 1 or any(
        not part or part in {".", ".."} or any(char.isspace() or char in "/\\" for char in part)
        for part in project_parts
    ):
        raise ValueError("Invalid pull request project identity")
    return json.dumps([parsed.hostname.lower().rstrip("."), owner.lower(), repository.lower(), int(number)], separators=(",", ":"))


def pull_request_resource(url, *, root=None, head_branch=None):
    identity = pull_request_identity(url)
    host, owner, repository, number = json.loads(identity)
    project = "/".join(quote(part, safe="") for part in [*owner.split("/"), repository])
    route = f"-/merge_requests/{number}" if "/-/merge_requests/" in url else f"pull/{number}"
    return identity, {"url": f"https://{host}/{project}/{route}", "root": root, "headBranch": head_branch}


def worktree_identity(path):
    return canonical_file_path_key(path)


def set_worktree_reference(record, path, workspace_root, *, backend="git"):
    identity = worktree_identity(path)
    if not any(item["attachment_type"] == "worktree" and item["identity_key"] == identity for item in record.resource_references):
        payload = {"root": path, "workspaceRoot": workspace_root, "backend": backend}
        validate_resource_identity("worktree", identity, payload)
        if len(record.resource_references) >= 100:
            raise ValueError("A conversation can retain at most 100 resource references")
        record.resource_references.append(resource_reference("worktree", identity, payload))


class ThreadResourceReferences:
    def __init__(self, repository):
        self.repository = repository

    def add(self, conversation_id, attachment_type, identity_key, payload):
        validate_resource_identity(attachment_type, identity_key, payload)
        outcome = {}
        def mutate(record):
            existing = next((item for item in record.resource_references
                if item["attachment_type"] == attachment_type and item["identity_key"] == identity_key), None)
            if existing is not None:
                outcome.update(outcome="existing", attachment=deepcopy(existing))
                return False
            if len(record.resource_references) >= 100:
                raise ValueError("A conversation can retain at most 100 resource references")
            item = resource_reference(attachment_type, identity_key, payload)
            record.resource_references.append(item)
            outcome.update(outcome="created", attachment=deepcopy(item))
        record = self.repository._mutate_meta(conversation_id, mutate, touch_updated_at=False)
        if record is None:
            raise ValueError("Conversation not found")
        return outcome

    def list(self, conversation_id, *, cursor=None, limit=100):
        record = self.repository.get_conversation_summary(conversation_id)
        if record is None:
            raise ValueError("Conversation not found")
        items = sorted(record.resource_references, key=lambda item: (item["created_at"], item["id"]))
        after = None
        if cursor is not None:
            fields = cursor.split("|")
            if len(fields) != 3 or fields[0] != conversation_id or not fields[2]:
                raise ValueError("Invalid resource pagination cursor")
            after = [int(fields[1]), fields[2]]
        page = self._page(items, after, limit, key=lambda item: [item["created_at"], item["id"]])
        if page["next_cursor"] is not None:
            created_at, identifier = page["next_cursor"]
            page["next_cursor"] = f"{conversation_id}|{created_at}|{identifier}"
        return page

    def remove(self, conversation_id, attachment_type, identity_key):
        validate_resource_identity(attachment_type, identity_key)
        removed = None
        def mutate(record):
            nonlocal removed
            removed = next((item for item in record.resource_references
                if item["attachment_type"] == attachment_type and item["identity_key"] == identity_key), None)
            if removed is None:
                return False
            record.resource_references.remove(removed)
        if self.repository._mutate_meta(conversation_id, mutate, touch_updated_at=False) is None:
            raise ValueError("Conversation not found")
        return removed

    def owners(self, attachment_type, identity_key, *, archived=None, cursor=None, limit=100):
        validate_resource_identity(attachment_type, identity_key)
        scope = {"attachment_type": attachment_type, "identity_key": identity_key, "archived": archived}
        after = None
        if cursor is not None:
            anchor = json.loads(cursor)
            if not isinstance(anchor, dict) or any(anchor.get(key) != value for key, value in scope.items()) or anchor.get("archived") is not archived or not isinstance(anchor.get("thread_id"), str) or not anchor["thread_id"]:
                raise ValueError("Invalid resource owner pagination cursor")
            after = anchor["thread_id"]
        owners = []
        for summary in self.repository.list_conversations():
            if archived is not None and summary.archived != archived:
                continue
            if any(item["attachment_type"] == attachment_type and item["identity_key"] == identity_key
                for item in summary.resource_references):
                owners.append({"id": summary.id, "thread_id": summary.id, "archived": summary.archived})
        page = self._page(sorted(owners, key=lambda item: item["id"]), after, limit, key=lambda item: item["id"])
        if page["next_cursor"] is not None:
            page["next_cursor"] = json.dumps({**scope, "thread_id": page["next_cursor"]}, separators=(",", ":"))
        for owner in page["data"]:
            del owner["id"]
        return page

    @staticmethod
    def _page(items, after, limit, *, key):
        remaining = items if after is None else [item for item in items if key(item) > after]
        page = remaining[:limit]
        next_cursor = key(page[-1]) if len(remaining) > limit else None
        return {"data": deepcopy(page), "next_cursor": next_cursor}
