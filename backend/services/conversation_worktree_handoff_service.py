from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from backend.services.workspace_service import sanitized_git_env
from backend.workspace.worktree import isolated_worktree_root


def _git(root: Path, *args: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, env=sanitized_git_env(), capture_output=True,
            text=True, encoding="utf-8", timeout=10, check=True,
        )
        return True, (result.stdout or "").strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return False, str(getattr(exc, "stderr", "") or exc)


def _status(root: Path) -> str:
    ok, output = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    return output if ok else "<status unavailable>"


def _head(root: Path) -> str:
    ok, output = _git(root, "rev-parse", "HEAD")
    return output if ok else ""


def _branch(root: Path) -> str:
    ok, output = _git(root, "branch", "--show-current")
    return output if ok else ""


def _ignored_sample(root: Path) -> list[str]:
    ok, output = _git(root, "ls-files", "--others", "--ignored", "--exclude-standard")
    return output.splitlines()[:20] if ok and output else []


def _check(code: str, severity: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message, **({"details": details} if details else {})}


def build_handoff_preflight(
    conversation: Any,
    *,
    target: str,
    conversation_repo: Any,
    main_worktree_root: Any,
    has_running_turn: bool,
    dirty_action: str = "block",
) -> dict[str, Any]:
    conversation_id = str(getattr(conversation, "id", "") or "")
    isolated = bool(getattr(conversation, "git_isolated", False))
    direction = "worktree_to_local" if target == "local" else "local_to_worktree"
    source_path = Path(str(getattr(conversation, "worktree_path", "") or getattr(conversation, "workspace_root", "") or ".")).resolve()
    base_root = main_worktree_root(source_path)
    worktree_path = isolated_worktree_root(base_root) / conversation_id
    worktree_branch = str(getattr(conversation, "git_branch", "") or f"minicode/{conversation_id}")
    checks: list[dict[str, Any]] = []

    if target not in {"local", "worktree"}:
        checks.append(_check("target.invalid", "blocking", "Handoff target must be local or worktree."))
    if (target == "local") != isolated:
        checks.append(_check("direction.invalid", "blocking", "Conversation is already bound to the requested workspace type."))
    if has_running_turn:
        checks.append(_check("turn.running", "blocking", "Wait for the running turn to finish before moving this task."))
    if not base_root.exists():
        checks.append(_check("repository.missing", "blocking", "The main Git checkout no longer exists.", path=str(base_root)))

    source_status = _status(source_path) if source_path.exists() else "<missing>"
    main_status = _status(base_root) if base_root.exists() else "<missing>"
    main_head = _head(base_root) if base_root.exists() else ""
    main_branch = _branch(base_root) if base_root.exists() else ""
    if source_status and source_status != "<missing>" and source_status != "<status unavailable>":
        if dirty_action == "stash":
            checks.append(_check("source.dirty.stash", "warning", "Local changes will be stashed and restored in the destination workspace."))
        else:
            checks.append(_check("source.dirty", "blocking", "Commit changes, or choose 'stash' to move tracked and untracked changes safely."))
    if source_status == "<status unavailable>":
        checks.append(_check("source.status_unavailable", "blocking", "Could not verify source Git status."))

    if direction == "local_to_worktree":
        ok, branch_ref = _git(base_root, "show-ref", "--verify", f"refs/heads/{worktree_branch}") if base_root.exists() else (False, "")
        if ok and branch_ref:
            checks.append(_check("branch.collision", "blocking", "The isolated branch already exists.", branch=worktree_branch))
        if worktree_path.exists():
            checks.append(_check("path.collision", "blocking", "The isolated workspace path already exists.", path=str(worktree_path)))
    else:
        if main_status and main_status not in {"<missing>", "<status unavailable>"}:
            checks.append(_check("target.dirty", "blocking", "The local checkout must be clean before switching branches."))
        if main_status == "<status unavailable>":
            checks.append(_check("target.status_unavailable", "blocking", "Could not verify local checkout Git status."))
        shared = []
        for item in conversation_repo.list_conversations():
            if str(getattr(item, "id", "")) == conversation_id or getattr(item, "archived", False) or getattr(item, "git_isolated", False):
                continue
            candidate = str(getattr(item, "workspace_root", "") or "").strip()
            if candidate and Path(candidate).resolve() == base_root:
                shared.append(str(getattr(item, "id", "")))
        if shared:
            checks.append(_check("target.shared", "blocking", "Another active task uses the local checkout; switching its branch would affect that task.", conversation_ids=shared))

    ignored = _ignored_sample(source_path) if source_path.exists() else []
    if ignored:
        checks.append(_check("ignored.files", "warning", "Ignored dependencies and local files are not copied between workspaces.", sample=ignored))

    fingerprint_payload = {
        "conversation_id": conversation_id,
        "direction": direction,
        "source_path": str(source_path),
        "source_head": _head(source_path) if source_path.exists() else "",
        "source_branch": _branch(source_path) if source_path.exists() else "",
        "source_status": source_status,
        "target_path": str(worktree_path if direction == "local_to_worktree" else base_root),
        "target_head": main_head,
        "target_previous_branch": main_branch,
        "target_branch": worktree_branch,
        "target_status": main_status,
        "dirty_action": dirty_action,
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "conversation_id": conversation_id,
        "target": target,
        "direction": direction,
        "source": {"path": str(source_path), "branch": fingerprint_payload["source_branch"], "head": fingerprint_payload["source_head"]},
        "destination": {"path": fingerprint_payload["target_path"], "branch": worktree_branch},
        "main_checkout": {
            "path": str(base_root),
            "branch": main_branch,
            "head": main_head,
        },
        "checks": checks,
        "allowed": not any(check["severity"] == "blocking" for check in checks),
        "fingerprint": fingerprint,
        "dirty_action": dirty_action,
    }


def stash_workspace_changes(root: Path, *, label: str) -> tuple[bool, str]:
    """Move tracked and untracked work aside for a handoff, retaining recovery."""
    ok, output = _git(root, "stash", "push", "--include-untracked", "--message", label)
    if not ok:
        return False, output or "Failed to stash local changes"
    # ``git stash push`` exits successfully when the checkout is already
    # clean, but reports that no stash was created. Treat that as a no-op so
    # choosing the stash path does not block an otherwise valid handoff.
    if "No local changes" in output:
        return True, ""
    ok, ref = _git(root, "stash", "list", "-1", "--format=%gd")
    return (bool(ok and ref), ref or output)


def restore_workspace_stash(root: Path, stash_ref: str) -> tuple[bool, str]:
    return _git(root, "stash", "pop", stash_ref)


def switch_main_checkout(base_root: Path, branch: str) -> tuple[bool, str]:
    return _git(base_root, "switch", branch)


def restore_main_checkout(
    base_root: Path,
    *,
    branch: str = "",
    head: str = "",
) -> tuple[bool, str]:
    clean_branch = str(branch or "").strip()
    if clean_branch:
        return _git(base_root, "switch", clean_branch)
    clean_head = str(head or "").strip()
    if clean_head:
        return _git(base_root, "switch", "--detach", clean_head)
    return False, "The previous local checkout could not be identified"


def delete_local_branch(base_root: Path, branch: str) -> tuple[bool, str]:
    clean_branch = str(branch or "").strip()
    if not clean_branch:
        return True, ""
    return _git(base_root, "branch", "-D", clean_branch)
