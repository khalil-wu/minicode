from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from backend.runtime_env import sanitized_git_env
from backend.services.git_settings_service import generated_branch_name
from backend.services.workspace_service import readonly_git_policy, run_readonly_git
from backend.workspace.worktree import isolated_worktree_root
from backend.sandbox.runner import SandboxUnavailableError

if TYPE_CHECKING:
    from backend.sandbox import SandboxPolicy


def _git(root: Path, *args: str) -> tuple[bool, str]:
    """Explicit worktree control-plane writes, not readonly UI snapshots."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, env=sanitized_git_env(), capture_output=True,
            text=True, encoding="utf-8", timeout=10, check=True,
        )
        return True, (result.stdout or "").strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return False, str(getattr(exc, "stderr", "") or exc)


def _git_snapshot(root: Path, *args: str, sandbox_policy: SandboxPolicy | None = None) -> tuple[bool, str]:
    try:
        result = run_readonly_git(root, *args, timeout=10, sandbox_policy=sandbox_policy)
        result.check_returncode()
        return True, result.stdout.rstrip("\r\n")
    except (OSError, subprocess.SubprocessError, SandboxUnavailableError) as exc:
        return False, str(getattr(exc, "stderr", "") or exc)


@dataclass(frozen=True)
class HandoffGitSnapshot:
    status: str
    head: str
    branch: str
    ignored: list[str]


def _workspace_snapshot(
    root: Path,
    *,
    sandbox_policy: SandboxPolicy | None = None,
    relative_prefix: str | None = "",
    collect_ignored: bool = True,
) -> HandoffGitSnapshot:
    # Porcelain paths are repository-relative. ls-files' old ignored-file
    # sample was cwd-relative, so a source inside another linked checkout
    # needs its actual prefix; the main checkout's prefix is already known.
    if relative_prefix is None:
        prefix_ok, relative_prefix = _git_snapshot(root, "rev-parse", "--show-prefix", sandbox_policy=sandbox_policy)
        if not prefix_ok:
            return HandoffGitSnapshot("<status unavailable>", "", "", [])
    args = ["status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all"]
    if collect_ignored:
        args.append("--ignored=traditional")
    ok, output = _git_snapshot(root, *args, sandbox_policy=sandbox_policy)
    if not ok:
        return HandoffGitSnapshot("<status unavailable>", "", "", [])
    head = ""
    branch = ""
    changes: list[str] = []
    ignored: list[str] = []
    records = iter(output.split("\0"))
    for record in records:
        if record.startswith("# branch.oid "):
            value = record.removeprefix("# branch.oid ")
            head = "" if value == "(initial)" else value
        elif record.startswith("# branch.head "):
            value = record.removeprefix("# branch.head ")
            branch = "" if value == "(detached)" else value
        elif record.startswith("! "):
            path = record[2:]
            if len(ignored) < 20 and path.startswith(relative_prefix):
                ignored.append(path.removeprefix(relative_prefix))
        elif record and not record.startswith("# "):
            changes.append(record)
            if record.startswith("2 "):
                changes.append(next(records))
    return HandoffGitSnapshot("\0".join(changes), head, branch, ignored)


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
    local_branch: str = "",
) -> dict[str, Any]:
    conversation_id = str(getattr(conversation, "id", "") or "")
    isolated = bool(getattr(conversation, "git_isolated", False))
    direction = "worktree_to_local" if target == "local" else "local_to_worktree"
    source_path = Path(str(getattr(conversation, "worktree_path", "") or getattr(conversation, "workspace_root", "") or ".")).resolve()
    base_root = main_worktree_root(source_path)
    source_policy = readonly_git_policy(source_path)
    main_policy = readonly_git_policy(base_root)
    worktree_path = isolated_worktree_root(base_root) / conversation_id
    checks: list[dict[str, Any]] = []

    if target not in {"local", "worktree"}:
        checks.append(_check("target.invalid", "blocking", "Handoff target must be local or worktree."))
    if (target == "local") != isolated:
        checks.append(_check("direction.invalid", "blocking", "Conversation is already bound to the requested workspace type."))
    if has_running_turn:
        checks.append(_check("turn.running", "blocking", "Wait for the running turn to finish before moving this task."))
    if not base_root.exists():
        checks.append(_check("repository.missing", "blocking", "The main Git checkout no longer exists.", path=str(base_root)))

    source_prefix = "" if getattr(conversation, "worktree_path", "") or source_path == base_root else (
        source_path.relative_to(base_root).as_posix() + "/" if source_path.is_relative_to(base_root) else None
    )
    source_snapshot = _workspace_snapshot(source_path, sandbox_policy=source_policy, relative_prefix=source_prefix) if source_path.exists() else HandoffGitSnapshot("<missing>", "", "", [])
    source_status, source_head, source_branch = source_snapshot.status, source_snapshot.head, source_snapshot.branch
    worktree_branch = generated_branch_name(conversation_id) if target == "worktree" else source_branch
    if source_path == base_root:
        main_status, main_head, main_branch = source_status, source_head, source_branch
    else:
        main_snapshot = _workspace_snapshot(base_root, sandbox_policy=main_policy, collect_ignored=False) if base_root.exists() else HandoffGitSnapshot("<missing>", "", "", [])
        main_status, main_head, main_branch = main_snapshot.status, main_snapshot.head, main_snapshot.branch
    if source_status and source_status != "<missing>" and source_status != "<status unavailable>":
        if dirty_action == "stash":
            checks.append(_check("source.dirty.stash", "warning", "Local changes will be stashed and restored in the destination workspace."))
        else:
            checks.append(_check("source.dirty", "blocking", "Commit changes, or choose 'stash' to move tracked and untracked changes safely."))
    if source_status == "<status unavailable>":
        checks.append(_check("source.status_unavailable", "blocking", "Could not verify source Git status."))

    reuse_branch = False
    local_branches: list[str] = []
    local_checkout_branch = ""
    local_checkout_head = ""
    if direction == "local_to_worktree":
        ok, branch_ref = _git_snapshot(base_root, "show-ref", "--verify", f"refs/heads/{worktree_branch}", sandbox_policy=main_policy) if base_root.exists() else (False, "")
        if ok and branch_ref:
            reuse_branch = source_branch == worktree_branch == str(getattr(conversation, "git_branch", "") or "")
            if reuse_branch:
                refs_ok, refs = _git_snapshot(base_root, "for-each-ref", "--sort=-committerdate", "--format=%(refname)%00%(worktreepath)%00%(objectname)%00%(symref)", "refs/heads", "refs/remotes", sandbox_policy=main_policy)
                available: dict[str, str] = {}
                default_branch = ""
                if refs_ok:
                    for row in refs.splitlines():
                        name, checked_out, head, symref = row.split("\0")
                        if name.startswith("refs/heads/") and not checked_out:
                            available[name.removeprefix("refs/heads/")] = head
                        elif name == "refs/remotes/origin/HEAD":
                            default_branch = symref.removeprefix("refs/remotes/origin/")
                    local_branches = list(available)
                    preferred = default_branch or next((name for name in ("main", "master") if name in available), "")
                    if preferred in available:
                        local_branches = [preferred, *[name for name in local_branches if name != preferred]]
                    local_checkout_branch = local_branch or next(iter(local_branches), "")
                    local_checkout_head = available.get(local_checkout_branch, "")
                    if not local_checkout_head:
                        checks.append(_check("local.branch_unavailable", "blocking", "Select another available local branch before moving this task.", branch=local_checkout_branch))
                else:
                    checks.append(_check("local.branches_unavailable", "blocking", "Could not load the available local checkout branches."))
            else:
                checks.append(_check("branch.collision", "blocking", "The isolated branch already exists.", branch=worktree_branch))
        if worktree_path.exists():
            checks.append(_check("path.collision", "blocking", "The isolated workspace path already exists.", path=str(worktree_path)))
    else:
        if main_status and main_status not in {"<missing>", "<status unavailable>"}:
            checks.append(_check("target.dirty", "blocking", "The local checkout must be clean before switching branches."))
        if main_status == "<status unavailable>":
            checks.append(_check("target.status_unavailable", "blocking", "Could not verify local checkout Git status."))

    if direction == "worktree_to_local" or reuse_branch:
        shared = []
        for item in conversation_repo.list_conversations():
            if str(getattr(item, "id", "")) == conversation_id or getattr(item, "archived", False) or getattr(item, "git_isolated", False):
                continue
            candidate = str(getattr(item, "workspace_root", "") or "").strip()
            if candidate and Path(candidate).resolve() == base_root:
                shared.append(str(getattr(item, "id", "")))
        if shared:
            checks.append(_check("target.shared", "blocking", "Another active task uses the local checkout; switching its branch would affect that task.", conversation_ids=shared))

    ignored = source_snapshot.ignored
    if ignored:
        checks.append(_check("ignored.files", "warning", "Ignored dependencies and local files are not copied between workspaces.", sample=ignored))

    fingerprint_payload = {
        "conversation_id": conversation_id,
        "direction": direction,
        "source_path": str(source_path),
        "source_head": source_head,
        "source_branch": source_branch,
        "source_status": source_status,
        "target_path": str(worktree_path if direction == "local_to_worktree" else base_root),
        "target_head": main_head,
        "target_previous_branch": main_branch,
        "target_branch": worktree_branch,
        "target_status": main_status,
        "dirty_action": dirty_action,
        "reuse_branch": reuse_branch,
        "local_checkout_branch": local_checkout_branch,
        "local_checkout_head": local_checkout_head,
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
        "reuse_branch": reuse_branch,
        "local_branches": local_branches,
        "local_checkout_branch": local_checkout_branch,
    }


def stash_workspace_changes(root: Path, *, label: str) -> tuple[bool, str]:
    """Move tracked and untracked work aside for a handoff, retaining recovery."""
    marker = f"{label}-{uuid4().hex}"
    ok, output = _git(root, "stash", "push", "--include-untracked", "--message", marker)
    if not ok:
        return False, output or "Failed to stash local changes"
    # ``git stash push`` exits successfully when the checkout is already
    # clean, but reports that no stash was created. Treat that as a no-op so
    # choosing the stash path does not block an otherwise valid handoff.
    if "No local changes" in output:
        return True, ""
    ok, entries = _git(root, "stash", "list", "--format=%H%x00%gs")
    if not ok:
        return False, entries
    for entry in entries.splitlines():
        revision, _, subject = entry.partition("\x00")
        if subject.endswith(f": {marker}"):
            return True, revision
    return False, "The handoff stash could not be identified."


def restore_workspace_stash(root: Path, stash_ref: str) -> tuple[bool, str]:
    # An immutable object id survives unrelated stashes in every worktree.
    # Retain the recovery entry: deleting a moving stash index can delete
    # another Git client's work between reflog lookup and removal.
    return _git(root, "stash", "apply", "--index", stash_ref)


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
