from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import sys
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


def _get_claude_user_dir() -> Path:
    """Return Claude Code's user-memory directory (``~/.claude``)."""
    return Path.home() / ".claude"


def _get_managed_claude_dir() -> Path:
    """Return Claude Code's platform-managed policy directory."""
    if os.name == "nt":
        return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ClaudeCode"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    return Path("/etc/claude-code")


# Codex AGENTS.md behavior: discover AGENTS.md from the project (git) root down
# to the working directory, concatenated root-first so the most specific file
# (closest to cwd) wins by appearing last. Capped at AGENTS_MD_MAX_BYTES total to
# bound prompt size (Codex default is 32 KiB).
AGENTS_MD_MAX_BYTES = 32 * 1024
# CC warns for oversized memory files but does not silently truncate them.
MAX_MEMORY_CHARACTER_COUNT = 40_000
MAX_INCLUDE_DEPTH = 5
_INCLUDE_RE = re.compile(r"(?:^|\s)@((?:[^\s\\]|\\ )+)")


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


def _claude_memory_chain(scope_dir: Path) -> list[Path]:
    """Directories from the filesystem root toward ``scope_dir``.

    Claude Code deliberately does not stop at a Git root.  It walks upward to
    (but does not inspect) the filesystem root, then loads memory root-first so
    files closer to the working directory have higher priority.
    """
    try:
        current = scope_dir.resolve()
    except OSError:
        return [scope_dir]
    chain: list[Path] = []
    while current != current.parent:
        chain.append(current)
        current = current.parent
    chain.reverse()
    return chain


def _rule_files(rules_dir: Path) -> list[Path]:
    """Return recursive Markdown rules in stable path order (CC behavior)."""
    if not rules_dir.exists() or not rules_dir.is_dir():
        return []
    try:
        return sorted(
            (path for path in rules_dir.rglob("*.md") if path.is_file()),
            key=lambda path: str(path).casefold(),
        )
    except OSError:
        return []


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

    def add_candidate(
        path: Path,
        source_kind: str,
        label: str,
        priority: int,
        scope: str,
    ) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        key = os.path.normcase(str(resolved))
        if key in seen_paths or not resolved.exists() or not resolved.is_file():
            return
        seen_paths.add(key)
        specs.append((resolved, source_kind, label, priority, scope))

    # Claude Code order: managed policy, user memory, project memory, local
    # memory. Codex's user AGENTS.md is also global and precedes project docs.
    managed_dir = _get_managed_claude_dir()
    add_candidate(
        managed_dir / "CLAUDE.md",
        "managed_memory",
        "Managed Memory",
        0,
        str(managed_dir),
    )
    for index, rule_file in enumerate(_rule_files(managed_dir / ".claude" / "rules")):
        add_candidate(
            rule_file,
            "managed_rule",
            "Managed Rule",
            1 + index,
            str(managed_dir),
        )

    add_candidate(
        Path.home() / ".codex" / "AGENTS.md",
        "user_memory",
        "User Agent Instructions (Codex)",
        10,
        str(Path.home()),
    )
    claude_user_dir = _get_claude_user_dir()
    add_candidate(
        claude_user_dir / "CLAUDE.md",
        "user_memory",
        "User Memory (Claude Code)",
        20,
        str(claude_user_dir),
    )
    for index, rule_file in enumerate(_rule_files(claude_user_dir / "rules")):
        add_candidate(
            rule_file,
            "user_rule",
            "User Rule (Claude Code)",
            21 + index,
            str(claude_user_dir),
        )

    def register_scope(scope_dir: Path) -> None:
        # Render order is INSERTION order of this list (the `priority` field is
        # informational only — sorting by it would interleave scopes and break
        # per-scope grouping when additional_directories are present). So the
        # order here IS the contract: global -> project-root->cwd -> memory.
        candidates: list[tuple[Path, str, str, int]] = []
        # AGENTS.md hierarchy: project root -> cwd (Codex behavior). Each gets
        # its own spec so the chain renders root-first, most-specific last.
        candidates += list(_agents_md_candidates(scope_dir))

        # CC discovers Project and Local memory from filesystem root to cwd,
        # independently of Codex's Git-root-bounded AGENTS.md discovery.
        # Keep every directory's CLAUDE.md, .claude/CLAUDE.md, rules, and local
        # memory together so the closest directory remains the last authority.
        for depth, directory in enumerate(_claude_memory_chain(scope_dir)):
            base_priority = 100 + depth * 10
            candidates.append((directory / "CLAUDE.md", "project_memory", "Project Memory", base_priority))
            candidates.append((directory / ".claude" / "CLAUDE.md", "project_memory", "Project Memory", base_priority + 1))
            rules_dir = directory / ".claude" / "rules"
            for rule_file in _rule_files(rules_dir):
                candidates.append((rule_file, "project_rule", "Project Rule", base_priority + 2))
            candidates.append((directory / "CLAUDE.local.md", "local_memory", "Local Memory", base_priority + 3))

        for path, source_kind, label, priority in candidates:
            add_candidate(path, source_kind, label, priority, str(scope_dir))

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


def _extract_include_paths(content: str, source_path: Path) -> list[Path]:
    """Extract CC-style @path imports while ignoring code and comments."""
    without_comments = re.sub(r"<!--[\s\S]*?-->", "", content)
    visible_lines: list[str] = []
    in_fence = False
    fence_marker = ""
    for line in without_comments.splitlines():
        stripped = line.lstrip()
        marker_match = re.match(r"(```+|~~~+)", stripped)
        if marker_match:
            marker = marker_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker[0]
            elif marker[0] == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue
        visible_lines.append(re.sub(r"`[^`]*`", "", line))

    paths: list[Path] = []
    seen: set[str] = set()
    for match in _INCLUDE_RE.finditer("\n".join(visible_lines)):
        raw = match.group(1).split("#", 1)[0].replace(r"\ ", " " ).strip()
        if not raw or raw.startswith("@") or re.match(r"^[#%^&*()]", raw):
            continue
        if raw.startswith("~/") or raw.startswith("~\\"):
            candidate = Path.home() / raw[2:]
        else:
            parsed = Path(raw)
            candidate = parsed if parsed.is_absolute() else source_path.parent / parsed
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            paths.append(resolved)
    return paths


def _expand_guideline_imports(
    specs: list[tuple[Path, str, str, int, str]],
) -> list[tuple[Path, str, str, int, str]]:
    """Expand CLAUDE.md @imports parent-first with CC's depth/dedupe rules."""
    expanded: list[tuple[Path, str, str, int, str]] = []
    processed: set[str] = set()

    def visit(
        spec: tuple[Path, str, str, int, str],
        depth: int,
    ) -> None:
        path, source_kind, label, priority, scope = spec
        try:
            resolved = path.resolve()
        except OSError:
            return
        key = os.path.normcase(str(resolved))
        if key in processed or depth >= MAX_INCLUDE_DEPTH:
            return
        processed.add(key)
        if not resolved.is_file():
            return
        expanded.append((resolved, source_kind, label, priority, scope))
        if source_kind == "agent_instruction":
            return
        try:
            content = resolved.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        allowed_root = Path(scope).resolve()
        allow_external = source_kind in {"user_memory", "user_rule"}
        for included in _extract_include_paths(content, resolved):
            if not allow_external:
                try:
                    included.relative_to(allowed_root)
                except ValueError:
                    logger.warning(
                        "Skipping external CLAUDE.md import without approval: %s",
                        included,
                    )
                    continue
            visit((included, source_kind, f"{label} import", priority, scope), depth + 1)

    for item in specs:
        visit(item, 0)
    return expanded


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
        if source_kind != "agent_instruction" and len(content) > MAX_MEMORY_CHARACTER_COUNT:
            logger.warning(
                "Large CLAUDE.md memory file (%s chars > %s): %s",
                len(content),
                MAX_MEMORY_CHARACTER_COUNT,
                path,
            )
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
        "user_rule": "User",
        "managed_memory": "Managed",
        "managed_rule": "Managed",
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
    specs = _expand_guideline_imports(
        _iter_guideline_specs(workspace_path, extra_paths)
    )
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
