from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.subprocesses import communicate, spawn_exec
from backend.services.github_service import GitHubCommandError, github_cli_command, github_command_env, github_repository_context

if TYPE_CHECKING:
    from backend.sandbox import SandboxPolicy


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


def readonly_git_policy(root: Path) -> SandboxPolicy:
    """Capture readonly authority, including registered MiniCode worktree metadata.

    The selected checkout's .git file is NOT a source of grants. Only the
    trusted original repository's worktree registry can admit shared metadata.
    """
    from backend.sandbox import SandboxPolicy
    from backend.config import load_config_layer_stack
    from backend.permissions.context import PermissionContext
    from backend.sandbox.policy import (
        FileSystemAccessMode, FileSystemPath, FileSystemSpecialPath, FileSystemSandboxEntry,
        FileSystemSandboxPolicy, NetworkSandboxPolicy, PermissionProfile,
        sandbox_policy_for_permission_context,
    )
    from backend.workspace.trust import is_workspace_trusted

    root = root.resolve()
    stack = load_config_layer_stack(cwd=root)
    captured = sandbox_policy_for_permission_context(
        root, PermissionContext(mode="confirm", workspace_root=root, source="control-plane-readonly"),
        config_stack=stack,
    )
    authority = captured.resolve(cwd=root)
    denied_roots = tuple(path for path, access in authority.resolved_entries if access is FileSystemAccessMode.DENY)

    def may_read(path: Path) -> bool:
        # Snapshot grants never reopen a captured ancestor DENY, even where
        # the base workspace-write profile has a more specific workspace grant.
        return authority.resolve_access(path).can_read and not any(path.is_relative_to(denied) for denied in denied_roots)

    readable = [root]
    if root.parent.name == "worktrees" and root.parent.parent.name == ".minicode":
        original = root.parents[2]
        metadata = (original / ".git").resolve()
        registry = metadata / "worktrees"
        if (is_workspace_trusted(original) and metadata.is_relative_to(original)
                and may_read(metadata) and registry.is_dir()):
            for entry in registry.iterdir():
                backlink = entry / "gitdir"
                if not backlink.resolve().is_relative_to(metadata) or not backlink.is_file():
                    continue
                if not may_read(backlink) or not may_read(backlink.resolve()):
                    continue
                registered = Path(backlink.read_text(encoding="utf-8").strip())
                if not registered.is_absolute():
                    registered = backlink.parent / registered
                if registered.resolve() == root / ".git":
                    readable.append(metadata)
                    break
    # Intersect the legacy snapshot grants with the captured canonical profile.
    # An ancestor managed DENY must not be reopened by a more precise READ.
    requested = SandboxPolicy(workspace_root=root, readable_roots=tuple(readable)).resolve(cwd=root)
    entries = []
    if authority.root_read_baseline:
        entries.append(FileSystemSandboxEntry(
            FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ,
        ))
    for path, access in requested.resolved_entries:
        if may_read(path):
            entries.append(FileSystemSandboxEntry(FileSystemPath.path(path), FileSystemAccessMode.READ))
    entries.extend(entry for entry in captured.permission_profile.file_system.entries if entry.access is FileSystemAccessMode.DENY)
    profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted(
        entries, glob_scan_max_depth=captured.permission_profile.file_system.glob_scan_max_depth,
    ), network=NetworkSandboxPolicy.RESTRICTED)
    return replace(
        captured, permission_profile=profile,
        readable_roots=tuple(path for path in readable if may_read(path)),
        allow_unsandboxed_commands=False, auto_allow_commands_if_sandboxed=False,
        fail_if_unavailable=True,
    )


def run_readonly_git(
    root: Path, *args: str, timeout: float = 5,
    sandbox_policy: SandboxPolicy | None = None,
) -> subprocess.CompletedProcess[str]:
    """Keep synchronous snapshot callers inside the canonical Git boundary.

    These services also run in existing async WS/scheduler hosts. A joined
    worker owns the coroutine through pipe and sandbox cleanup; no task is
    detached, and no session's bypass authority is borrowed for a snapshot.
    """
    from backend.sandbox import SandboxRunner
    from backend.tools.git_support import _run_git

    policy = sandbox_policy or readonly_git_policy(root)
    launch_cwd = root
    argv = ["git", *args]
    if (root != policy.workspace_root
            and SandboxRunner(policy).capability(cwd=root).backend == "windows-elevated-wfp"):
        # The native account prepares the process cwd for reading. Starting at
        # the captured owner keeps its repository metadata reachable; Git -C
        # still scopes discovery, pathspecs and output to the requested folder.
        launch_cwd = policy.workspace_root
        argv[1:1] = ["-C", str(root)]

    def execute() -> subprocess.CompletedProcess[bytes]:
        return asyncio.run(_run_git(
            argv, root=policy.workspace_root, cwd=launch_cwd,
            sandbox_policy=policy, timeout=timeout,
        ))

    with ThreadPoolExecutor(max_workers=1) as worker:
        result = worker.submit(execute).result()
    return subprocess.CompletedProcess(
        result.args, result.returncode,
        result.stdout.decode("utf-8"),
        result.stderr.decode("utf-8"),
    )


def readonly_git_host_path(root: Path, value: str, *, sandbox_policy: SandboxPolicy | None = None) -> Path:
    from backend.sandbox import SandboxRunner

    mapper = SandboxRunner(sandbox_policy or readonly_git_policy(root))
    return Path(mapper.map_path_from_sandbox(value))


def git_branch_for(path: Path) -> str:
    """Return the current branch for a workspace path."""
    root = path.resolve()
    try:
        result = run_readonly_git(root, "branch", "--show-current")
        result.check_returncode()
        return result.stdout.strip()
    except Exception:
        return ""


def main_worktree_root(path: Path) -> Path:
    root = path.resolve()
    try:
        policy = readonly_git_policy(root)
        if len(policy.readable_roots) == 2:
            # The original owner was admitted by its trusted worktree registry,
            # not inferred from an untrusted checkout's .git pointer or a
            # container alias for the metadata directory's parent.
            return root.parents[2]
        result = run_readonly_git(root, "worktree", "list", "--porcelain", sandbox_policy=policy)
        result.check_returncode()
    except Exception:
        return root

    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            candidate = readonly_git_host_path(root, line[9:].strip(), sandbox_policy=policy).resolve()
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


def worktree_has_local_changes(path: Path, *, timeout: float = 5) -> bool:
    result = run_readonly_git(
        path, "status", "--porcelain=v1", "--ignored", "--untracked-files=all",
        timeout=timeout,
    )
    result.check_returncode()
    return bool(result.stdout.strip())


def git_pr_status_payload(
    *,
    pr: dict[str, Any] | None = None,
    checks: list[dict[str, str]] | None = None,
    error: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "git.pr_status",
        "pr": pr,
        "checks": checks or [],
    }
    if error is not None:
        payload["error"] = error
        payload["error_code"] = error_code or "request_failed"
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
    try:
        repository = await github_repository_context(resolved_workspace_root)
    except (GitHubCommandError, OSError, asyncio.TimeoutError) as exc:
        return {**git_pr_status_payload(error="暂时无法读取当前仓库信息，请刷新后重试。", error_code="repository_unavailable"),
            "automation": automation, "diagnostic": str(exc)}
    if not repository["eligible"] or not repository["branch"]:
        return {**git_pr_status_payload(), "automation": automation, "github": repository}
    gh_path = github_cli_command()
    if not gh_path:
        payload = git_pr_status_payload(error="GitHub 连接组件尚未就绪。", error_code="gh_unavailable")
        payload["diagnostic"] = "gh CLI not found"
        payload["automation"] = automation
        payload["github"] = repository
        return payload

    try:
        code, out = await _run_gh_pr_view(gh_path, cwd=str(resolved_workspace_root))
        if code == 0 and out:
            pr_info, checks = parse_gh_pr_status(out)
            # Only actual open PRs may drive automation. A branch named main
            # can be a legitimate head (for example in a fork); branch names
            # are not a substitute for the PR's authoritative state.
            state = str((pr_info or {}).get("state") or "").upper()
            if state in {"MERGED", "CLOSED"}:
                pr_info = None
                checks = []
            payload = git_pr_status_payload(pr=pr_info, checks=checks)
        elif "no pull requests found" in out.lower() or "no open pull requests" in out.lower():
            payload = git_pr_status_payload()
        else:
            message = out or f"gh pr view exited with code {code}"
            detail = message.lower()
            if any(term in detail for term in ("gh auth login", "not logged", "authentication", "401", "bad credentials")):
                error_code = "auth_required"
            elif any(term in detail for term in ("403", "forbidden", "resource not accessible")):
                error_code = "permission_denied"
            else:
                error_code = "request_failed"
            payload = git_pr_status_payload(error=message, error_code=error_code)
    except (OSError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
        payload = git_pr_status_payload(error=str(exc))
    payload["automation"] = automation
    payload["github"] = repository
    if "error" in payload:
        payload["diagnostic"] = payload["error"]
        payload["error"] = {
            "gh_unavailable": "GitHub 连接组件尚未就绪。",
            "auth_required": "GitHub 尚未连接，请先连接账号。",
            "permission_denied": "当前账号无法访问此仓库，请检查 GitHub 仓库权限。",
            "request_failed": "暂时无法读取 PR 状态，请重试。",
        }[payload["error_code"]]
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
    diagnostic = ""
    if "auto_merge" in data:
        repository = await github_repository_context(Path(workspace_root))
        gh_path = github_cli_command()
        if not repository["eligible"]:
            auto_merge_error = "当前仓库未关联 GitHub，自动合并设置未更改。"
        elif not gh_path:
            auto_merge_error = "GitHub 连接组件尚未就绪，自动合并设置未更改。"
        else:
            view_code, view_out = await _run_gh_pr_view(gh_path, cwd=str(workspace_root))
            pr_info = None
            if view_code == 0 and view_out:
                try:
                    pr_info, _checks = parse_gh_pr_status(view_out)
                except json.JSONDecodeError as exc:
                    diagnostic = str(exc)
                state = str((pr_info or {}).get("state") or "").upper()
                if state != "OPEN":
                    pr_info = None
            if not pr_info:
                auto_merge_error = "尚未确认当前分支的开放 PR，自动合并设置未更改。"
                diagnostic = diagnostic or view_out
            elif (data.get("expected_pr_number") != pr_info["number"]
                    or data.get("expected_branch") != repository["branch"]):
                auto_merge_error = "当前 PR 或分支已经变化，请刷新后重新确认自动合并。"
            else:
                code, output = await _run_gh_pr_merge_auto(
                    gh_path,
                    cwd=str(workspace_root),
                    pr_number=int(pr_info["number"]),
                    enabled=bool(data["auto_merge"]),
                )
                if code != 0:
                    auto_merge_error = "GitHub 未能更新自动合并设置，请重试。"
                    diagnostic = output
    if not auto_merge_error:
        current = write_pr_automation(workspace_root, data)
    else:
        current = read_pr_automation(workspace_root)
    payload = await fetch_git_pr_status_payload(workspace_root)
    payload["automation"] = current
    if auto_merge_error:
        payload["error"] = auto_merge_error
        payload["error_code"] = "auto_merge_failed"
        payload["diagnostic"] = diagnostic
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
        env=github_command_env(),
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
        env=github_command_env(),
    )
    stdout, stderr = await communicate(proc, timeout=20)
    return proc.returncode or 0, (stdout or stderr or b"").decode(errors="replace").strip()
