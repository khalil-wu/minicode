from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from backend.agent.run_context import RunContext

# The paths MiniCode refuses to auto-edit: version control, editor state, and
# MiniCode's own behaviour-defining files. There is deliberately no
# credential-file list here — .env / .npmrc / SSH keys are governed by the
# permission and approval flow, not by a hard refusal.
DANGEROUS_FILES = {
    ".gitconfig",
    ".gitmodules",
    ".bashrc",
    ".bash_profile",
    ".zshrc",
    ".zprofile",
    ".profile",
    ".ripgreprc",
    ".mcp.json",
}
DANGEROUS_DIRECTORIES = {
    ".git",
    ".vscode",
    ".idea",
    # MiniCode's own instructions, rules, todos and worktree state live here; an
    # agent must not silently rewrite the directory that defines its behaviour.
    ".minicode",
}


def _is_dangerous_path(path: Path) -> bool:
    name = path.name.lower()
    if name in DANGEROUS_FILES:
        return True
    return any(part.lower() in DANGEROUS_DIRECTORIES for part in path.parts)


# ``is_protected_write_path`` is the single dangerous-path implementation.
# ``is_sensitive_file`` remains a compatibility alias for older integrations;
# it deliberately delegates instead of carrying a second rule set.
def application_state_roots(run_context: RunContext | None = None) -> tuple[Path, ...]:
    """Actual host-owned storage, independent of workspace directory names."""
    from backend.config import DATA_ROOT

    roots = [DATA_ROOT]
    if run_context is not None:
        if run_context.agent_runtime is not None:
            roots.append(run_context.agent_runtime.state_root)
        if run_context.conversation_repository is not None:
            roots.append(run_context.conversation_repository.storage_root)
    return tuple(dict.fromkeys(path.resolve() for path in roots))


def is_protected_write_path(
    path: Path,
    *,
    state_roots: Iterable[Path] = (),
    application_roots: Iterable[Path] | None = None,
    resolved_path: Path | None = None,
) -> bool:
    if _is_dangerous_path(path):
        return True
    if not path.is_absolute():
        return False
    # Batch readers can reuse their request's canonical roots and target. The
    # ordinary file-tool callers still resolve both through this same rule.
    target = path.resolve() if resolved_path is None else resolved_path
    roots = application_state_roots() if application_roots is None else application_roots
    return any(target.is_relative_to(root) for root in (*roots, *state_roots))


def is_sensitive_file(path: Path) -> bool:
    return is_protected_write_path(path)
