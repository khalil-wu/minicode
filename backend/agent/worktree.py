"""Lightweight git worktree isolation for delegated subagents.

Mirrors cc's AgentTool worktree flow (cc/src/utils/worktree.ts
createAgentWorktree / hasWorktreeChanges / removeAgentWorktree): create a
throwaway worktree on a temporary branch under ``.minicode/worktrees/``,
run the subagent inside it, then remove worktree + branch when nothing
changed and keep it (reporting the path) when it did.

All functions are synchronous subprocess wrappers; callers in async code
should invoke them via ``asyncio.to_thread``. Failures never raise — a
``None`` / conservative result lets the caller degrade to non-isolated
execution with an explanation.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from backend.atomic_io import atomic_write_text, canonical_file_path_key, file_mutation_locks
from backend.runtime_env import sanitized_git_env

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_S = 60

# Worktrees created (or resumed) by this process. Stale-cleanup never touches
# these, so concurrent delegations cannot race the janitor.
_ACTIVE_WORKTREE_PATHS: set[str] = set()
# Git roots already swept this process; stale cleanup runs once per root.
_STALE_SWEEP_DONE: set[str] = set()
_STALE_SWEEP_COOLDOWN_SECONDS = 30.0


@dataclass(frozen=True)
class AgentWorktree:
    """A created subagent worktree."""

    worktree_path: Path
    branch: str
    head_commit: str
    git_root: Path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=sanitized_git_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("git %s failed in %s: %s", " ".join(args), cwd, exc)
        return None


def _git_output(cwd: Path, *args: str) -> str:
    result = _git(cwd, *args)
    if result is None or result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def find_git_root(base: Path) -> Path | None:
    """Canonical main-repo root for ``base`` (follows linked worktrees)."""
    common = _git_output(base, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    common_path = Path(common)
    # <root>/.git for a normal repo; bare/odd layouts fall back to toplevel.
    if common_path.name == ".git":
        return common_path.parent
    toplevel = _git_output(base, "rev-parse", "--show-toplevel")
    return Path(toplevel) if toplevel else None


def create_agent_worktree(slug: str, base: Path) -> tuple[AgentWorktree | None, str]:
    """Create a temporary worktree for a subagent.

    Returns ``(worktree, "")`` on success or ``(None, reason)`` when isolation
    is unavailable (not a git repo, git missing, add failed).
    """
    from backend.agent.checkpoint import validate_storage_id

    try:
        slug = validate_storage_id(slug, field_name="worktree_slug")
    except ValueError as exc:
        return None, str(exc)

    git_root = find_git_root(base)
    if git_root is None:
        return None, f"{base} is not inside a git repository (or git is unavailable)"

    head = _git_output(git_root, "rev-parse", "HEAD")
    if not head:
        return None, f"could not resolve HEAD in {git_root} (empty repository?)"

    worktrees_dir = git_root / ".minicode" / "worktrees"
    worktree_path = worktrees_dir / slug
    branch = f"minicode/{slug}"
    if worktree_path.exists():
        return None, f"worktree path already exists: {worktree_path}"
    try:
        worktrees_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"could not create {worktrees_dir}: {exc}"

    result = _git(
        git_root, "worktree", "add", "-b", branch, str(worktree_path), head
    )
    if result is None or result.returncode != 0:
        stderr = (result.stderr or "").strip() if result is not None else "git unavailable"
        return None, f"git worktree add failed: {stderr[:300]}"

    # Keep throwaway worktrees out of git status without touching .gitignore.
    _ensure_excluded(git_root)
    logger.info("Created agent worktree %s (branch %s)", worktree_path, branch)
    resolved_path = worktree_path.resolve()
    _ACTIVE_WORKTREE_PATHS.add(canonical_file_path_key(resolved_path))
    return (
        AgentWorktree(
            worktree_path=resolved_path,
            branch=branch,
            head_commit=head,
            git_root=git_root.resolve(),
        ),
        "",
    )


def _ensure_excluded(git_root: Path) -> None:
    try:
        git_dir = _git_output(git_root, "rev-parse", "--path-format=absolute", "--git-dir")
        if not git_dir:
            return
        exclude = Path(git_dir) / "info" / "exclude"
        entry = ".minicode/"
        with file_mutation_locks([exclude]):
            existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if entry not in existing.splitlines():
                exclude.parent.mkdir(parents=True, exist_ok=True)
                suffix = "" if not existing or existing.endswith(("\n", "\r")) else "\n"
                atomic_write_text(exclude, f"{existing}{suffix}{entry}\n")
    except OSError as exc:  # best-effort only
        logger.debug("Could not update git exclude for %s: %s", git_root, exc)


def has_worktree_changes(worktree: AgentWorktree) -> bool:
    """True when the worktree has uncommitted files or new commits.

    Conservative: any git failure counts as "changed" so the worktree is kept.
    """
    status = _git(worktree.worktree_path, "status", "--porcelain")
    if status is None or status.returncode != 0:
        return True
    if (status.stdout or "").strip():
        return True
    rev_list = _git(
        worktree.worktree_path,
        "rev-list",
        "--count",
        f"{worktree.head_commit}..HEAD",
    )
    if rev_list is None or rev_list.returncode != 0:
        return True
    try:
        return int((rev_list.stdout or "0").strip()) > 0
    except ValueError:
        return True


def remove_agent_worktree(worktree: AgentWorktree) -> bool:
    """Remove a clean agent worktree and delete its temporary branch."""
    result = _git(
        worktree.git_root, "worktree", "remove", "--force", str(worktree.worktree_path)
    )
    if result is None or result.returncode != 0:
        stderr = (result.stderr or "").strip() if result is not None else "git unavailable"
        logger.warning("Failed to remove agent worktree %s: %s", worktree.worktree_path, stderr)
        return False
    branch_result = _git(worktree.git_root, "branch", "-D", worktree.branch)
    if branch_result is None or branch_result.returncode != 0:
        logger.warning("Could not delete agent worktree branch %s", worktree.branch)
    logger.info("Removed agent worktree %s", worktree.worktree_path)
    return True


def cleanup_agent_worktree(worktree: AgentWorktree) -> tuple[bool, str]:
    """Remove the worktree when unchanged; keep it (with its path) otherwise.

    Returns ``(kept, worktree_path_if_kept)``.
    """
    _ACTIVE_WORKTREE_PATHS.discard(canonical_file_path_key(worktree.worktree_path))
    if has_worktree_changes(worktree):
        logger.info("Agent worktree has changes, keeping: %s", worktree.worktree_path)
        return True, str(worktree.worktree_path)
    if not remove_agent_worktree(worktree):
        return True, str(worktree.worktree_path)
    return False, ""


def resume_agent_worktree(
    worktree_path: str | Path,
    *,
    expected_repo_root: str | Path,
    expected_subagent_id: str,
) -> AgentWorktree | None:
    """Re-adopt a worktree recorded in a checkpoint for a resumed subagent.

    Returns ``None`` when the directory is gone or no longer a valid worktree,
    letting the caller degrade to non-isolated execution.
    """
    try:
        from backend.agent.checkpoint import validate_storage_id

        subagent_id = validate_storage_id(
            expected_subagent_id,
            field_name="subagent_id",
        )
        expected_base = Path(expected_repo_root).resolve()
        repo_root = (find_git_root(expected_base) or expected_base).resolve()
        path = Path(worktree_path).resolve()
        expected_path = (repo_root / ".minicode" / "worktrees" / subagent_id).resolve()
        if path != expected_path:
            return None
        if not path.is_dir():
            return None
        git_root = find_git_root(path)
        if git_root is None or git_root.resolve() != repo_root:
            return None
        # The worktree must actually be a linked worktree of this repo (its
        # branch/HEAD are re-derived rather than trusted from the checkpoint).
        toplevel = _git_output(path, "rev-parse", "--show-toplevel")
        if not toplevel or Path(toplevel).resolve() != path.resolve():
            return None
        branch = _git_output(path, "rev-parse", "--abbrev-ref", "HEAD")
        # Anchor head_commit to the main repo HEAD, not the worktree HEAD:
        # commits made before the process died then look "ahead" to
        # has_worktree_changes, so cleanup keeps the worktree instead of
        # deleting a branch that holds real work.
        head = _git_output(git_root, "rev-parse", "HEAD") or _git_output(path, "rev-parse", "HEAD")
        if not branch or not head:
            return None
        resolved = path.resolve()
        _ACTIVE_WORKTREE_PATHS.add(canonical_file_path_key(resolved))
        return AgentWorktree(
            worktree_path=resolved,
            branch=branch,
            head_commit=head,
            git_root=git_root.resolve(),
        )
    except OSError as exc:
        logger.warning("Could not resume agent worktree %s: %s", worktree_path, exc)
        return None


def cleanup_stale_worktrees(base: Path) -> None:
    """Best-effort janitor for orphaned ``.minicode/worktrees/`` entries.

    A killed process leaves worktrees behind. On the first delegation per git
    root we prune deleted-directory registrations and remove any leftover
    worktree that is not active in this process and has no changes. Worktrees
    with uncommitted files or new commits are always kept. Idempotent; never
    raises.
    """
    try:
        git_root = find_git_root(base)
        if git_root is None:
            return
        root_key = canonical_file_path_key(git_root)
        if root_key in _STALE_SWEEP_DONE:
            return
        # Drop registrations whose directories were deleted out-of-band.
        _git(git_root, "worktree", "prune")

        worktrees_dir = git_root / ".minicode" / "worktrees"
        if not worktrees_dir.is_dir():
            return
        lease_path = worktrees_dir / ".janitor.lease"
        now = time.time()
        with file_mutation_locks([lease_path]):
            try:
                previous = float(lease_path.read_text(encoding="ascii").strip())
            except (FileNotFoundError, ValueError, OSError):
                previous = 0.0
            if now - previous < _STALE_SWEEP_COOLDOWN_SECONDS:
                _STALE_SWEEP_DONE.add(root_key)
                return
            atomic_write_text(lease_path, f"{now:.6f}")
        _STALE_SWEEP_DONE.add(root_key)
        for entry in worktrees_dir.iterdir():
            try:
                if not entry.is_dir():
                    continue
                resolved = entry.resolve()
                if canonical_file_path_key(resolved) in _ACTIVE_WORKTREE_PATHS:
                    continue
                branch = _git_output(resolved, "rev-parse", "--abbrev-ref", "HEAD")
                if not branch:
                    # Not a functioning worktree (e.g. registration pruned but
                    # directory remains). Leave it alone — deleting unknown
                    # directories is riskier than a little disk residue.
                    continue
                candidate = AgentWorktree(
                    worktree_path=resolved,
                    branch=branch,
                    head_commit=_git_output(resolved, "rev-parse", "HEAD"),
                    git_root=git_root.resolve(),
                )
                # head_commit == current HEAD, so "new commits" cannot be seen
                # here; uncommitted changes (porcelain status) still keep it.
                # Committed-only orphans keep their branch: the branch ref keeps
                # the commits reachable even after worktree removal, but to stay
                # conservative we keep the whole worktree unless status is clean
                # AND the branch is not ahead of any other ref — simplified to:
                # clean status keeps nothing uncommitted, and we skip branch
                # deletion when the branch has commits beyond the repo HEAD.
                status = _git(resolved, "status", "--porcelain")
                if status is None or status.returncode != 0 or (status.stdout or "").strip():
                    logger.info("Keeping stale worktree with changes: %s", resolved)
                    continue
                repo_head = _git_output(git_root, "rev-parse", "HEAD")
                ahead = _git_output(resolved, "rev-list", "--count", f"{repo_head}..HEAD") if repo_head else ""
                if ahead and ahead != "0":
                    logger.info("Keeping stale worktree with commits: %s", resolved)
                    continue
                logger.info("Removing stale agent worktree: %s", resolved)
                remove_agent_worktree(candidate)
            except Exception as exc:  # noqa: BLE001 — janitor must never break delegation
                logger.warning("Stale worktree sweep skipped %s: %s", entry, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Stale worktree sweep failed for %s: %s", base, exc)
