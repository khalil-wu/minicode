from __future__ import annotations

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


def _iter_guideline_specs(
    workspace_dir: Path,
    additional_directories: tuple[Path, ...],
) -> list[tuple[Path, str, str, int, str]]:
    specs: list[tuple[Path, str, str, int, str]] = []
    seen_paths: set[str] = set()

    def register_scope(scope_dir: Path) -> None:
        agents_override = scope_dir / "AGENTS.override.md"
        agents_default = scope_dir / "AGENTS.md"
        agents_candidate = agents_override if agents_override.exists() else agents_default
        candidates = [
            (agents_candidate, "agent_instruction", "Agent Instructions", 50),
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
    for path, source_kind, label, priority, scope in specs:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception as exc:
            logger.debug("Failed to read %s: %s", path, exc)
            continue
        if not content:
            continue
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
    return tuple(blocks)


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
