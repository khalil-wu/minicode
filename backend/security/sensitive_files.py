from __future__ import annotations

from pathlib import Path

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


# ``is_protected_write_path`` is the write guard. ``is_sensitive_file`` is kept
# as a deprecated alias so existing call sites and transcripts resolve; both now
# mean the single cc-dangerous-path concept (no separate secret-file list).
def is_protected_write_path(path: Path) -> bool:
    return _is_dangerous_path(path)


def is_sensitive_file(path: Path) -> bool:
    return _is_dangerous_path(path)
