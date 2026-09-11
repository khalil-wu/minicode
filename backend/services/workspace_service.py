from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.agent.message import AgentEvent
from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.runtime_env import sanitized_git_env
from backend.subprocesses import communicate, spawn_exec


@dataclass(frozen=True)
class WorkspaceActivationRequest:
    path_str: str
    project_path: Path | None
    error_event: AgentEvent | None = None


@dataclass(frozen=True)
class UserMessageWorkspaceRequest:
    path_str: str
    project_path: Path | None
    error_event: AgentEvent | None = None


def parse_workspace_activation_request(path_str: str) -> WorkspaceActivationRequest:
    from backend.workspace.path_utils import normalize_project_import_path
    from backend.workspace.trust import is_workspace_trusted

    clean_path = str(path_str or "").strip()
    project_path = normalize_project_import_path(clean_path)
    if not project_path.exists() or not project_path.is_dir():
        return WorkspaceActivationRequest(
            clean_path,
            project_path,
            AgentEvent.error(
                f"Session workspace does not exist: {clean_path}",
                recoverable=True,
                error_type="workspace",
                error_code="workspace_missing",
            ),
        )
    if not is_workspace_trusted(project_path):
        return WorkspaceActivationRequest(
            clean_path,
            project_path,
            AgentEvent.error(
                f"Workspace is not trusted: {clean_path}",
                recoverable=True,
                error_type="workspace",
                error_code="workspace_untrusted",
            ),
        )
    return WorkspaceActivationRequest(clean_path, project_path)


def parse_user_message_workspace_request(
    requested_workspace_root: str,
    *,
    conversation_id: str = "",
) -> UserMessageWorkspaceRequest:
    from backend.workspace.path_utils import normalize_project_import_path
    from backend.workspace.trust import is_workspace_trusted

    clean_path = str(requested_workspace_root or "").strip()
    try:
        requested_workspace_path = normalize_project_import_path(clean_path)
    except Exception as exc:
        error_event = AgentEvent.error(f"Invalid workspace path: {exc}", recoverable=True)
        if conversation_id:
            error_event.data["conversation_id"] = conversation_id
        return UserMessageWorkspaceRequest(clean_path, None, error_event)

    if not requested_workspace_path.exists() or not requested_workspace_path.is_dir():
        error_event = AgentEvent.error(
            f"Workspace does not exist: {clean_path}",
            recoverable=True,
        )
        if conversation_id:
            error_event.data["conversation_id"] = conversation_id
        return UserMessageWorkspaceRequest(clean_path, requested_workspace_path, error_event)

    if not is_workspace_trusted(requested_workspace_path):
        error_event = AgentEvent.error(
            f"Workspace is not trusted: {clean_path}",
            recoverable=True,
            error_type="workspace",
            error_code="workspace_untrusted",
        )
        if conversation_id:
            error_event.data["conversation_id"] = conversation_id
        return UserMessageWorkspaceRequest(
            clean_path,
            requested_workspace_path,
            error_event,
        )

    return UserMessageWorkspaceRequest(clean_path, requested_workspace_path)


def workspace_path_needs_activation(requested_workspace_path: Path, current_workspace_root: Path) -> bool:
    return requested_workspace_path.resolve() != current_workspace_root.resolve()


def conversation_workspace_path(conversation: Any) -> str:
    return str(
        getattr(conversation, "worktree_path", "")
        or getattr(conversation, "workspace_root", "")
        or ""
    ).strip()


def workspace_matches_context(workspace_path: str, workspace_context: Any | None) -> bool:
    if workspace_context is None:
        return False
    current_root = str(getattr(workspace_context, "root_path", "") or "").strip()
    if not current_root:
        return False
    from backend.workspace.path_utils import normalize_project_import_path

    try:
        target_root = str(normalize_project_import_path(workspace_path)).strip()
    except Exception:
        return False
    return os.path.normcase(os.path.normpath(current_root)) == os.path.normcase(
        os.path.normpath(target_root)
    )


def workspace_context_root(workspace_context: Any | None) -> str:
    if workspace_context is None:
        return ""
    return str(getattr(workspace_context, "root_path", "") or "").strip()


def create_workspace_context(project_path: Path) -> Any:
    from backend.workspace.context import WorkspaceContext

    return WorkspaceContext(project_path)


def record_recent_workspace_project(project_path: Path, metadata: Any) -> None:
    from backend.workspace.recent_projects import RecentProjectStore

    RecentProjectStore().add(
        path=str(project_path),
        name=metadata.name,
        project_type=metadata.project_type,
    )


def workspace_imported_payload(
    workspace_context: Any,
    metadata: Any,
    *,
    conversation_id: str,
    workspace_root: str | Path,
    request_id: str = "",
) -> dict[str, Any]:
    owner = str(conversation_id or "").strip()
    if not owner:
        raise ValueError("workspace.imported requires a conversation owner")
    canonical_root = str(Path(workspace_root).resolve())
    file_count = int(metadata.file_count)
    project = dict(workspace_context.to_dict())
    # One canonical value prevents the renderer from comparing a resolved
    # workspace owner with a differently formatted metadata path.
    project["root_path"] = canonical_root
    project["file_count"] = file_count
    return {
        "type": "workspace.imported",
        "conversation_id": owner,
        "workspace_root": canonical_root,
        **({"request_id": str(request_id).strip()} if str(request_id).strip() else {}),
        "project": project,
        "summary": workspace_context.get_project_summary(),
        "file_count": file_count,
    }


def workspace_recent_payload(projects: list[Any]) -> dict[str, Any]:
    return {
        "type": "workspace.recent.list",
        "projects": [project.to_dict() for project in projects],
    }


def list_workspace_recent_payload(*, limit: int | None = None) -> dict[str, Any]:
    from backend.workspace.recent_projects import RecentProjectStore

    store = RecentProjectStore()
    return workspace_recent_payload(store.list(limit=limit))


def remove_workspace_recent(path: str, *, limit: int | None = None) -> tuple[bool, dict[str, Any]]:
    """Remove one MRU entry without touching the project directory."""

    from backend.workspace.recent_projects import RecentProjectStore

    store = RecentProjectStore()
    removed = store.remove(path)
    return removed, workspace_recent_payload(store.list(limit=limit))


def clear_workspace_recent(*, limit: int | None = None) -> tuple[int, dict[str, Any]]:
    """Clear MRU metadata without deleting any workspace from disk."""

    from backend.workspace.recent_projects import RecentProjectStore

    store = RecentProjectStore()
    removed = store.clear()
    return removed, workspace_recent_payload(store.list(limit=limit))


def git_branch_for(path: Path) -> str:
    """Return the current branch for a workspace path."""
    root = path.resolve()
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root,
            env=sanitized_git_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def main_worktree_root(path: Path) -> Path:
    root = path.resolve()
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=root,
            env=sanitized_git_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=True,
        )
    except Exception:
        return root

    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            candidate = Path(line[9:].strip()).resolve()
            if (candidate / ".git").is_dir():
                return candidate
            return candidate
    return root


def is_path_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def resolve_workspace_cwd(workspace_root: Path | None, cwd: str | None = None) -> Path:
    if workspace_root is None:
        raise ValueError("Open a workspace before selecting a working directory")
    root = workspace_root.resolve()
    candidate = Path(cwd).expanduser().resolve() if cwd else root
    if not is_path_within(candidate, root):
        raise ValueError(f"CWD must stay inside workspace: {root}")
    if not candidate.exists() or not candidate.is_dir():
        raise ValueError(f"CWD does not exist or is not a directory: {candidate}")
    return candidate


def resolve_requested_workspace(
    workspace_root: Path | None,
    requested_workspace: str | None = None,
) -> Path:
    if workspace_root is None:
        raise ValueError("Open a workspace before running this command")
    root = workspace_root.resolve()
    if not requested_workspace:
        return root
    requested = Path(requested_workspace).expanduser().resolve()
    if not is_path_within(requested, root):
        raise ValueError(f"Workspace must stay inside current session workspace: {root}")
    if not requested.exists() or not requested.is_dir():
        raise ValueError(f"Workspace does not exist or is not a directory: {requested}")
    return requested


def validate_git_relative_path(path: str) -> str:
    value = str(path or "").replace("\\", "/").strip()
    candidate = Path(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Git path must be a relative path inside the workspace")
    return value


def worktree_has_local_changes(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=path,
            env=sanitized_git_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=True,
        )
        return bool(result.stdout.strip())
    except Exception:
        return True


def git_pr_status_payload(
    *,
    pr: dict[str, Any] | None = None,
    checks: list[dict[str, str]] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "git.pr_status",
        "pr": pr,
        "checks": checks or [],
    }
    if error is not None:
        payload["error"] = error
    return payload


def parse_gh_pr_status(output: str) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    if not output:
        return None, []
    raw = json.loads(output)
    pr_info = {
        "number": raw.get("number"),
        "title": raw.get("title", ""),
        "state": raw.get("state", ""),
        "url": raw.get("url", ""),
        "branch": raw.get("headRefName", ""),
    }
    checks: list[dict[str, str]] = []
    for check in raw.get("statusCheckRollup", []) or []:
        checks.append({
            "name": check.get("name") or check.get("context", ""),
            "status": (check.get("conclusion") or check.get("status") or "pending").lower(),
            "url": check.get("detailsUrl") or check.get("targetUrl", ""),
        })
    return pr_info, checks


async def fetch_git_pr_status_payload(workspace_root: Any) -> dict[str, Any]:
    raw_workspace_root = str(workspace_root or "").strip()
    if not raw_workspace_root:
        raise ValueError("Open a workspace before checking pull request status")
    resolved_workspace_root = Path(raw_workspace_root).expanduser().resolve()
    if not resolved_workspace_root.is_dir():
        raise ValueError(f"Workspace does not exist or is not a directory: {resolved_workspace_root}")
    automation = read_pr_automation(resolved_workspace_root)
    gh_path = shutil.which("gh")
    if not gh_path:
        payload = git_pr_status_payload(error="gh CLI not found")
        payload["automation"] = automation
        return payload

    try:
        code, out = await _run_gh_pr_view(gh_path, cwd=str(resolved_workspace_root))
        if code == 0 and out:
            pr_info, checks = parse_gh_pr_status(out)
            # cc ghPrStatus guards: skip PRs from the default branch (gh pr
            # view there returns the most recently MERGED PR) and merged or
            # closed PRs — downstream auto_fix/auto_merge must never act on
            # them.
            state = str((pr_info or {}).get("state") or "").upper()
            branch = str((pr_info or {}).get("branch") or "")
            if state in {"MERGED", "CLOSED"} or branch in {"main", "master"}:
                pr_info = None
                checks = []
            payload = git_pr_status_payload(pr=pr_info, checks=checks)
        else:
            payload = git_pr_status_payload()
    except Exception as exc:
        payload = git_pr_status_payload(error=str(exc))
    payload["automation"] = automation
    return payload


def _pr_automation_path(workspace_root: Any) -> Path | None:
    raw_workspace_root = str(workspace_root or "").strip()
    if not raw_workspace_root:
        return None
    try:
        root = Path(raw_workspace_root).expanduser().resolve()
    except (OSError, ValueError):
        return None
    return root / ".minicode" / "pr_automation.json" if root.is_dir() else None


def read_pr_automation(workspace_root: Any) -> dict[str, Any]:
    path = _pr_automation_path(workspace_root)
    defaults = {"auto_fix": False, "auto_merge": False}
    if path is None or not path.exists():
        return defaults
    with file_mutation_locks([path]):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return defaults
    return {
        "auto_fix": bool(raw.get("auto_fix", False)),
        "auto_merge": bool(raw.get("auto_merge", False)),
    }


def write_pr_automation(workspace_root: Any, data: dict[str, Any]) -> dict[str, Any]:
    path = _pr_automation_path(workspace_root)
    if path is None:
        raise ValueError("Open a workspace before configuring PR automation")
    with file_mutation_locks([path]):
        current = read_pr_automation(workspace_root)
        for key in ("auto_fix", "auto_merge"):
            if key in data:
                current[key] = bool(data[key])
        atomic_write_text(
            path,
            json.dumps(current, indent=2, ensure_ascii=False) + "\n",
        )
        return current


async def set_git_pr_automation_payload(workspace_root: Any, data: dict[str, Any]) -> dict[str, Any]:
    current = read_pr_automation(workspace_root)
    auto_merge_error = ""
    if "auto_merge" in data:
        gh_path = shutil.which("gh")
        if not gh_path:
            auto_merge_error = "gh CLI not found; Auto-merge was not changed."
        else:
            view_code, view_out = await _run_gh_pr_view(gh_path, cwd=str(workspace_root))
            pr_info = None
            if view_code == 0 and view_out:
                pr_info, _checks = parse_gh_pr_status(view_out)
                state = str((pr_info or {}).get("state") or "").upper()
                branch = str((pr_info or {}).get("branch") or "")
                if state != "OPEN" or branch in {"main", "master"}:
                    pr_info = None
            if not pr_info:
                auto_merge_error = (
                    "Auto-merge was not changed: no open PR is attached to this branch."
                )
            else:
                code, output = await _run_gh_pr_merge_auto(
                    gh_path,
                    cwd=str(workspace_root),
                    pr_number=int(pr_info["number"]),
                    enabled=bool(data["auto_merge"]),
                )
                if code != 0:
                    auto_merge_error = output or "The remote auto-merge update failed."
    if not auto_merge_error:
        current = write_pr_automation(workspace_root, data)
    payload = await fetch_git_pr_status_payload(workspace_root)
    payload["automation"] = current
    if auto_merge_error:
        payload["error"] = auto_merge_error
    return payload


async def _run_gh_pr_view(gh_path: str, *, cwd: str) -> tuple[int, str]:
    proc = await spawn_exec(
        gh_path,
        "pr",
        "view",
        "--json",
        "number,title,state,url,headRefName,statusCheckRollup",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await communicate(proc, timeout=15)
    return proc.returncode or 0, (stdout or stderr or b"").decode(errors="replace").strip()


async def _run_gh_pr_merge_auto(gh_path: str, *, cwd: str, pr_number: int, enabled: bool) -> tuple[int, str]:
    proc = await spawn_exec(
        gh_path,
        "pr",
        "merge",
        str(pr_number),
        *( ["--auto", "--merge"] if enabled else ["--disable-auto"] ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await communicate(proc, timeout=20)
    return proc.returncode or 0, (stdout or stderr or b"").decode(errors="replace").strip()
