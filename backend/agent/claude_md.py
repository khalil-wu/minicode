from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuidelineBlock:
    path: Path
    scope: str
    source_kind: str
    label: str
    priority: int
    content: str

    def to_markdown(self) -> str:
        return f"## Project Guideline ({self.label}): {self.path.name}\n{self.content}"

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "scope": self.scope,
            "source_kind": self.source_kind,
            "label": self.label,
            "priority": self.priority,
            "content": self.content,
        }


@dataclass(frozen=True)
class GuidelineBundle:
    workspace_dir: Path
    additional_directories: tuple[Path, ...]
    blocks: tuple[GuidelineBlock, ...]
    rendered_markdown: str
    cache_signature: tuple[tuple[str, int, int], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_dir": str(self.workspace_dir),
            "additional_directories": [str(path) for path in self.additional_directories],
            "rendered_markdown": self.rendered_markdown,
            "blocks": [block.to_dict() for block in self.blocks],
        }


_GUIDELINE_CACHE: dict[tuple[str, tuple[str, ...]], GuidelineBundle] = {}
_GUIDELINE_CACHE_LOCK = Lock()


def clear_guideline_cache() -> None:
    """清除 guideline 缓存（用于文件变更时重新加载）"""
    with _GUIDELINE_CACHE_LOCK:
        _GUIDELINE_CACHE.clear()
    logger.info("Guideline cache cleared")


def _normalize_directory(value: str | Path | None) -> Path:
    if value is None:
        return Path.cwd().resolve()
    return Path(value).resolve()


def _normalize_additional_directories(
    workspace_dir: Path,
    additional_directories: list[str | Path] | tuple[str | Path, ...] | None = None,
) -> tuple[Path, ...]:
    normalized: list[Path] = []
    seen: set[str] = set()
    for raw_path in additional_directories or ():
        path = Path(raw_path).resolve()
        key = str(path)
        if key in seen or key == str(workspace_dir):
            continue
        seen.add(key)
        normalized.append(path)
    return tuple(normalized)


def _get_user_config_dir() -> Path:
    """Return ~/.minicode/ (user-level config directory)."""
    if os.name == "nt":
        base = Path(os.environ.get("USERPROFILE", "~"))
    else:
        base = Path(os.environ.get("HOME", "~"))
    return base.expanduser() / ".minicode"


# Codex AGENTS.md behavior: discover AGENTS.md from the project (git) root down
# to the working directory, concatenated root-first so the most specific file
# (closest to cwd) wins by appearing last. Capped at AGENTS_MD_MAX_BYTES total to
# bound prompt size (Codex default is 32 KiB).
AGENTS_MD_MAX_BYTES = 32 * 1024


def _find_project_root(start: Path) -> Path | None:
    """Return the git/project root at or above ``start``, or None if none found.

    Walks up looking for a ``.git`` marker (dir or file — worktrees use a file).
    Mirrors Codex's discovery, which stops at the Git root and does not climb
    past it.
    """
    try:
        current = start.resolve()
    except OSError:
        return None
    for directory in (current, *current.parents):
        if (directory / ".git").exists():
            return directory
    return None


def _agents_md_chain(scope_dir: Path) -> list[Path]:
    """Directories from project root down to ``scope_dir`` (inclusive), root first.

    When ``scope_dir`` is not inside a git project, only ``scope_dir`` itself is
    returned (Codex's cwd-only fallback).
    """
    root = _find_project_root(scope_dir)
    if root is None:
        return [scope_dir]
    try:
        scope_resolved = scope_dir.resolve()
        root_resolved = root.resolve()
    except OSError:
        return [scope_dir]
    # Build root -> scope_dir inclusive. parents are cwd-first, so reverse.
    chain: list[Path] = [scope_resolved]
    for parent in scope_resolved.parents:
        chain.append(parent)
        if parent == root_resolved:
            break
    else:
        # scope_dir was not under root (shouldn't happen) — fall back to cwd only.
        return [scope_dir]
    chain.reverse()  # root first, scope_dir last
    return chain


def _agents_md_candidates(scope_dir: Path) -> list[tuple[Path, str, str, int]]:
    """Resolve the AGENTS.md hierarchy for a scope into guideline specs.

    Each directory in the root->cwd chain contributes its AGENTS.override.md if
    present, else its AGENTS.md. Priority decreases toward cwd (lower number =
    rendered earlier) so root guidance appears first and cwd guidance last.
    """
    chain = _agents_md_chain(scope_dir)
    candidates: list[tuple[Path, str, str, int]] = []
    # Spread priorities just below the workspace CLAUDE.md band (100) so AGENTS.md
    # still precedes project memory; root gets the smallest number.
    for depth, directory in enumerate(chain):
        override = directory / "AGENTS.override.md"
        default = directory / "AGENTS.md"
        chosen = override if override.exists() else default
        candidates.append((chosen, "agent_instruction", "Agent Instructions", 40 + depth))
    return candidates



def _iter_guideline_specs(
    workspace_dir: Path,
    additional_directories: tuple[Path, ...],
) -> list[tuple[Path, str, str, int, str]]:
    specs: list[tuple[Path, str, str, int, str]] = []
    seen_paths: set[str] = set()

    def register_scope(scope_dir: Path) -> None:
        # Render order is INSERTION order of this list (the `priority` field is
        # informational only — sorting by it would interleave scopes and break
        # per-scope grouping when additional_directories are present). So the
        # order here IS the contract: global -> project-root->cwd -> memory.
        candidates: list[tuple[Path, str, str, int]] = []
        # Global user-level AGENTS.md (Codex hierarchy: ~/.codex/AGENTS.md is the
        # baseline for all projects, then ~/.minicode/AGENTS.md). Rendered first.
        candidates.append((Path.home() / ".codex" / "AGENTS.md", "user_memory", "User Agent Instructions (Codex)", 10))
        candidates.append((_get_user_config_dir() / "AGENTS.md", "user_memory", "User Agent Instructions", 20))
        # AGENTS.md hierarchy: project root -> cwd (Codex behavior). Each gets
        # its own spec so the chain renders root-first, most-specific last.
        candidates += list(_agents_md_candidates(scope_dir))
        candidates += [
            (scope_dir / "CLAUDE.md", "project_memory", "Project Memory", 100),
        ]

        # User-level CLAUDE.md (~/.minicode/CLAUDE.md) — priority 150
        user_config = _get_user_config_dir() / "CLAUDE.md"
        candidates.append((user_config, "user_memory", "User Memory", 150))

        candidates.append((scope_dir / ".claude" / "CLAUDE.md", "project_memory", "Project Memory", 200))

        rules_dir = scope_dir / ".claude" / "rules"
        if rules_dir.exists() and rules_dir.is_dir():
            for rule_file in sorted(rules_dir.glob("*.md")):
                candidates.append((rule_file, "project_rule", "Project Rule", 300))
        candidates.append((scope_dir / "CLAUDE.local.md", "local_memory", "Local Memory", 400))

        for path, source_kind, label, priority in candidates:
            resolved = path.resolve()
            key = str(resolved)
            if key in seen_paths or not resolved.exists() or not resolved.is_file():
                continue
            seen_paths.add(key)
            specs.append((resolved, source_kind, label, priority, str(scope_dir)))

    register_scope(workspace_dir)
    for directory in additional_directories:
        register_scope(directory)
    return specs


def _build_signature(specs: list[tuple[Path, str, str, int, str]]) -> tuple[tuple[str, int, int], ...]:
    signature: list[tuple[str, int, int]] = []
    for path, _, _, _, _ in specs:
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((str(path), int(stat.st_mtime_ns), int(stat.st_size)))
    return tuple(signature)


def _read_blocks(specs: list[tuple[Path, str, str, int, str]]) -> tuple[GuidelineBlock, ...]:
    blocks: list[GuidelineBlock] = []
    agents_bytes_used = 0  # cumulative AGENTS.md budget (Codex project_doc cap)
    for path, source_kind, label, priority, scope in specs:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception as exc:
            logger.debug("Failed to read %s: %s", path, exc)
            continue
        if not content:
            continue
        if source_kind == "agent_instruction":
            remaining = AGENTS_MD_MAX_BYTES - agents_bytes_used
            if remaining <= 0:
                logger.debug("AGENTS.md budget exhausted; skipping %s", path)
                continue
            encoded = content.encode("utf-8")
            if len(encoded) > remaining:
                # Truncate on a UTF-8 char boundary, append a marker.
                content = encoded[:remaining].decode("utf-8", errors="ignore").rstrip()
                content += "\n\n[... AGENTS.md truncated to fit context budget ...]"
            agents_bytes_used += len(content.encode("utf-8"))
        blocks.append(
            GuidelineBlock(
                path=path,
                scope=scope,
                source_kind=source_kind,
                label=label,
                priority=priority,
                content=content,
            )
        )
        _schedule_instructions_loaded_hook(path, source_kind)
    return tuple(blocks)


def _schedule_instructions_loaded_hook(path: Path, source_kind: str) -> None:
    try:
        from backend.hooks import get_hook_manager

        hook_mgr = get_hook_manager()
        if not hook_mgr:
            return
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    except Exception:
        return

    memory_type = {
        "agent_instruction": "Project",
        "project_memory": "Project",
        "project_rule": "Project",
        "local_memory": "Local",
        "user_memory": "User",
    }.get(source_kind, source_kind)

    async def _run() -> None:
        try:
            await hook_mgr.run_instructions_loaded(
                file_path=str(path),
                memory_type=memory_type,
                load_reason="session_start",
            )
        except Exception:
            logger.debug("instructions_loaded hook failed for %s", path)

    loop.create_task(_run())


def load_project_guideline_bundle(
    workspace_dir: str | Path | None = None,
    additional_directories: list[str | Path] | tuple[str | Path, ...] | None = None,
) -> GuidelineBundle:
    workspace_path = _normalize_directory(workspace_dir)
    extra_paths = _normalize_additional_directories(workspace_path, additional_directories)
    cache_key = (str(workspace_path), tuple(str(path) for path in extra_paths))
    specs = _iter_guideline_specs(workspace_path, extra_paths)
    signature = _build_signature(specs)

    with _GUIDELINE_CACHE_LOCK:
        cached = _GUIDELINE_CACHE.get(cache_key)
        if cached is not None and cached.cache_signature == signature:
            return cached

    blocks = _read_blocks(specs)
    rendered_markdown = ""
    if blocks:
        rendered_markdown = "\n\n# Project Guidelines & Memory\n" + "\n\n".join(
            block.to_markdown() for block in blocks
        )

    bundle = GuidelineBundle(
        workspace_dir=workspace_path,
        additional_directories=extra_paths,
        blocks=blocks,
        rendered_markdown=rendered_markdown,
        cache_signature=signature,
    )

    with _GUIDELINE_CACHE_LOCK:
        _GUIDELINE_CACHE[cache_key] = bundle
    return bundle


def load_project_guidelines(
    workspace_dir: str | Path | None = None,
    additional_directories: list[str | Path] | tuple[str | Path, ...] | None = None,
) -> str:
    return load_project_guideline_bundle(
        workspace_dir=workspace_dir,
        additional_directories=additional_directories,
    ).rendered_markdown
