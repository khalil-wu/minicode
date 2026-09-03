from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
from threading import Lock
from typing import Any

import yaml
from pathspec.gitignore import GitIgnoreSpec
from backend.managed_settings import default_minicode_managed_dir
from backend.agent.markdown_scopes import get_minicode_config_home_dir

logger = logging.getLogger(__name__)

# MiniCode's own project instruction files. These are the names MiniCode writes
# (see /init) and the names it ranks first.
PROJECT_INSTRUCTIONS_FILENAME = Path(".minicode") / "INSTRUCTIONS.md"
PROJECT_INSTRUCTIONS_LOCAL_FILENAME = Path(".minicode") / "INSTRUCTIONS.local.md"

# AGENTS.md is the cross-tool instruction convention published at agents.md and
# honoured by Codex, pi, and ~20 other harnesses, so reading it is interop with
# the wider ecosystem rather than deference to one tool. MiniCode reads it and
# never writes it; a repository that has one keeps working unchanged.
SHARED_INSTRUCTIONS_FILENAME = "AGENTS.md"


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
            "additional_directories": [
                str(path) for path in self.additional_directories
            ],
            "rendered_markdown": self.rendered_markdown,
            "blocks": [block.to_dict() for block in self.blocks],
        }


_GUIDELINE_CACHE: dict[
    tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...], int],
    GuidelineBundle,
] = {}
_GUIDELINE_CACHE_LOCK = Lock()
_INCLUDE_PARENT_PATHS: dict[str, str] = {}


def clear_guideline_cache() -> None:
    """清除 guideline 缓存（用于文件变更时重新加载）"""
    with _GUIDELINE_CACHE_LOCK:
        _GUIDELINE_CACHE.clear()
        _INCLUDE_PARENT_PATHS.clear()
    logger.info("Guideline cache cleared")


def guideline_change_metadata(path: str | Path) -> dict[str, str] | None:
    """Describe whether a changed file participates in the active guideline graph.

    Direct MiniCode instruction files and files reached through an instruction
    import both invalidate the rendered guideline bundle. Keep
    this check next to the cache/import graph so file watchers do not have to
    duplicate only part of the discovery contract.
    """

    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        resolved = Path(path).expanduser().absolute()
    normalized_parts = {part.lower() for part in resolved.parts}
    direct = (
        resolved.name
        in {
            PROJECT_INSTRUCTIONS_FILENAME.name,
            PROJECT_INSTRUCTIONS_LOCAL_FILENAME.name,
            SHARED_INSTRUCTIONS_FILENAME,
        }
        or (resolved.suffix.lower() == ".md" and ".minicode" in normalized_parts)
    )
    with _GUIDELINE_CACHE_LOCK:
        parent = _INCLUDE_PARENT_PATHS.get(os.path.normcase(str(resolved)), "")
    if not direct and not parent:
        return None
    return {
        "path": str(resolved),
        "source_kind": "import" if parent else "direct",
        **({"parent_path": parent} if parent else {}),
    }


def _normalize_directory(value: str | Path | None) -> Path:
    if value is None:
        return Path.cwd().absolute()
    return Path(value).expanduser().absolute()


def _normalize_additional_directories(
    workspace_dir: Path,
    additional_directories: list[str | Path] | tuple[str | Path, ...] | None = None,
) -> tuple[Path, ...]:
    normalized: list[Path] = []
    seen: set[str] = set()
    for raw_path in additional_directories or ():
        path = Path(raw_path).expanduser().absolute()
        try:
            canonical = path.resolve()
        except OSError:
            canonical = path
        key = os.path.normcase(str(canonical))
        try:
            workspace_key = os.path.normcase(str(workspace_dir.resolve()))
        except OSError:
            workspace_key = os.path.normcase(str(workspace_dir))
        if key in seen or key == workspace_key:
            continue
        seen.add(key)
        normalized.append(path)
    return tuple(normalized)


def _get_managed_minicode_dir() -> Path:
    """Return MiniCode's platform-managed policy directory."""

    return default_minicode_managed_dir()


# Instructions are discovered from the project root down to the working
# directory. Root-first rendering lets the closest scope override earlier text.
INSTRUCTIONS_MAX_BYTES = 32 * 1024
MAX_MEMORY_CHARACTER_COUNT = 40_000
MAX_INCLUDE_DEPTH = 5
TEXT_FILE_EXTENSIONS = frozenset(
    {
        ".md", ".txt", ".text", ".json", ".yaml", ".yml", ".toml", ".xml", ".csv",
        ".html", ".htm", ".css", ".scss", ".sass", ".less", ".js", ".ts", ".tsx",
        ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".py", ".pyi", ".pyw", ".rb",
        ".erb", ".rake", ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".c",
        ".cpp", ".cc", ".cxx", ".h", ".hpp", ".cs", ".sh", ".bash", ".zsh",
        ".fish", ".ps1", ".sql", ".graphql", ".proto", ".ini", ".conf", ".cfg",
    }
)
_INCLUDE_RE = re.compile(r"(?:^|\s)@((?:[^\s\\]|\\ )+)")
_FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", re.DOTALL)


def _parse_rule_content(raw_content: str) -> tuple[str, tuple[str, ...]]:
    """Return the Markdown body and CC-compatible ``paths`` frontmatter."""
    match = _FRONTMATTER_RE.match(raw_content)
    if match is None:
        return raw_content.strip(), ()
    try:
        payload = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return raw_content.strip(), ()
    if not isinstance(payload, dict):
        return raw_content.strip(), ()
    raw_paths = payload.get("paths")
    if isinstance(raw_paths, str):
        paths = [part.strip() for part in raw_paths.split(",")]
    elif isinstance(raw_paths, list):
        paths = [str(part).strip() for part in raw_paths]
    else:
        paths = []
    normalized = tuple(
        path[:-3] if path.endswith("/**") else path
        for path in paths
        if path and path != "**"
    )
    return raw_content[match.end() :].strip(), normalized


def _normalize_project_root_markers(
    project_root_markers: list[str] | tuple[str, ...] | None,
) -> tuple[str, ...]:
    if project_root_markers is None:
        return (".git",)
    return tuple(
        marker.strip()
        for marker in project_root_markers
        if isinstance(marker, str) and marker.strip()
    )


def _normalize_project_doc_fallback_filenames(
    filenames: list[str] | tuple[str, ...] | None,
) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_name in filenames or ():
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        if (
            not name
            or name in {"INSTRUCTIONS.local.md", "INSTRUCTIONS.md"}
            or name in normalized
        ):
            continue
        normalized.append(name)
    return tuple(normalized)


def _normalize_project_doc_max_bytes(value: int | None) -> int:
    if value is None:
        return INSTRUCTIONS_MAX_BYTES
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return INSTRUCTIONS_MAX_BYTES


def _find_project_root(
    start: Path,
    project_root_markers: list[str] | tuple[str, ...] | None = None,
) -> Path | None:
    """Return the git/project root at or above ``start``, or None if none found.

    Walks up looking for configured project-root markers. MiniCode defaults to
    ``.git``; an explicitly empty list disables parent traversal.
    """
    try:
        current = start.expanduser().absolute()
    except OSError:
        return None
    markers = _normalize_project_root_markers(project_root_markers)
    if not markers:
        return None
    for directory in (current, *current.parents):
        if any((directory / marker).exists() for marker in markers):
            return directory
    return None


def _instruction_scope_chain(
    scope_dir: Path,
    project_root_markers: list[str] | tuple[str, ...] | None = None,
) -> list[Path]:
    """Directories from project root down to ``scope_dir`` (inclusive), root first.

    When ``scope_dir`` is not inside a project, only ``scope_dir`` is returned.
    """
    markers = _normalize_project_root_markers(project_root_markers)
    if not markers:
        return [scope_dir]
    root = _find_project_root(scope_dir, markers)
    if root is None:
        return [scope_dir]
    try:
        scope_resolved = scope_dir.expanduser().absolute()
        root_resolved = root.expanduser().absolute()
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


def _rule_files(rules_dir: Path) -> list[Path]:
    """Return recursive Markdown rules in stable path order."""
    if not rules_dir.exists() or not rules_dir.is_dir():
        return []
    try:
        return sorted(
            (path for path in rules_dir.rglob("*.md") if path.is_file()),
            key=lambda path: str(path).casefold(),
        )
    except OSError:
        return []


def _instruction_candidates(
    scope_dir: Path,
    *,
    project_root_markers: tuple[str, ...],
    project_doc_fallback_filenames: tuple[str, ...],
) -> list[tuple[Path, str, str, int]]:
    """Resolve MiniCode's project instruction hierarchy for a scope."""
    chain = _instruction_scope_chain(scope_dir, project_root_markers)
    candidates: list[tuple[Path, str, str, int]] = []
    for depth, directory in enumerate(chain):
        chosen: Path | None = None
        for candidate in (
            directory / PROJECT_INSTRUCTIONS_LOCAL_FILENAME,
            directory / PROJECT_INSTRUCTIONS_FILENAME,
            directory / SHARED_INSTRUCTIONS_FILENAME,
            *(
                directory / ".minicode" / filename
                for filename in project_doc_fallback_filenames
            ),
        ):
            if candidate.exists() and candidate.is_file():
                chosen = candidate
                break
        if chosen is not None:
            candidates.append(
                (chosen, "project_instruction", "Project Instructions", 40 + depth)
            )
    return candidates


def _iter_guideline_specs(
    workspace_dir: Path,
    additional_directories: tuple[Path, ...],
    *,
    project_root_markers: tuple[str, ...],
    project_doc_fallback_filenames: tuple[str, ...],
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
        logical = path.expanduser().absolute()
        try:
            resolved = logical.resolve()
        except OSError:
            return
        key = os.path.normcase(str(resolved))
        if key in seen_paths or not logical.exists() or not logical.is_file():
            return
        seen_paths.add(key)
        specs.append((logical, source_kind, label, priority, scope))

    # MiniCode's single precedence chain is managed, user, then project scope.
    managed_dir = _get_managed_minicode_dir()
    add_candidate(
        managed_dir / "INSTRUCTIONS.md",
        "managed_instruction",
        "Managed Instructions",
        0,
        str(managed_dir),
    )
    for index, rule_file in enumerate(_rule_files(managed_dir / "rules")):
        add_candidate(
            rule_file,
            "managed_rule",
            "Managed Rule",
            1 + index,
            str(managed_dir),
        )

    user_dir = get_minicode_config_home_dir()
    add_candidate(
        user_dir / "INSTRUCTIONS.md",
        "user_instruction",
        "User Instructions",
        10,
        str(user_dir),
    )
    for index, rule_file in enumerate(_rule_files(user_dir / "rules")):
        add_candidate(
            rule_file,
            "user_rule",
            "User Rule",
            11 + index,
            str(user_dir),
        )

    def register_scope(scope_dir: Path) -> None:
        # Render order is INSERTION order of this list (the `priority` field is
        # informational only — sorting by it would interleave scopes and break
        # per-scope grouping when additional_directories are present). So the
        # order here IS the contract: managed -> user -> project-root -> cwd.
        candidates: list[tuple[Path, str, str, int]] = []
        candidates += list(
            _instruction_candidates(
                scope_dir,
                project_root_markers=project_root_markers,
                project_doc_fallback_filenames=project_doc_fallback_filenames,
            )
        )
        for depth, directory in enumerate(
            _instruction_scope_chain(scope_dir, project_root_markers)
        ):
            base_priority = 100 + depth * 10
            rules_dir = directory / ".minicode" / "rules"
            for rule_file in _rule_files(rules_dir):
                candidates.append(
                    (rule_file, "project_rule", "Project Rule", base_priority + 2)
                )

        for path, source_kind, label, priority in candidates:
            add_candidate(path, source_kind, label, priority, str(scope_dir))

    register_scope(workspace_dir)
    for directory in additional_directories:
        register_scope(directory)
    return specs


def _build_signature(
    specs: list[tuple[Path, str, str, int, str]],
) -> tuple[tuple[str, int, int], ...]:
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
        raw = match.group(1).split("#", 1)[0].replace(r"\ ", " ").strip()
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
    """Expand MiniCode instruction imports with bounded depth and deduplication."""
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
        try:
            content = resolved.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        if source_kind.endswith("_rule"):
            _, conditional_paths = _parse_rule_content(content)
            if conditional_paths:
                return
        allowed_root = Path(scope).resolve()
        for included in _extract_include_paths(content, resolved):
            suffix = included.suffix.lower()
            if suffix and suffix not in TEXT_FILE_EXTENSIONS:
                logger.warning("Skipping non-text instruction import: %s", included)
                continue
            try:
                included.relative_to(allowed_root)
            except ValueError:
                logger.warning("Skipping instruction import outside its scope: %s", included)
                continue
            with _GUIDELINE_CACHE_LOCK:
                _INCLUDE_PARENT_PATHS[os.path.normcase(str(included))] = str(resolved)
            visit(
                (included, source_kind, f"{label} import", priority, scope), depth + 1
            )

    for item in specs:
        visit(item, 0)
    return expanded


def _read_blocks(
    specs: list[tuple[Path, str, str, int, str]],
    *,
    load_reason: str = "session_start",
    project_doc_max_bytes: int = INSTRUCTIONS_MAX_BYTES,
    hook_manager: Any | None = None,
) -> tuple[GuidelineBlock, ...]:
    blocks: list[GuidelineBlock] = []
    project_instruction_bytes_used = 0
    for path, source_kind, label, priority, scope in specs:
        if source_kind == "project_instruction":
            remaining = project_doc_max_bytes - project_instruction_bytes_used
            if remaining <= 0:
                logger.debug("Project instruction budget exhausted; skipping %s", path)
                continue
            try:
                raw_content = path.read_bytes()
            except Exception as exc:
                logger.debug("Failed to read %s: %s", path, exc)
                continue
            truncated = raw_content[:remaining]
            content = truncated.decode("utf-8", errors="replace")
            if not content.strip():
                continue
            if len(raw_content) > remaining:
                logger.warning(
                    "Project instructions exceed remaining byte budget; truncating %s to %s bytes",
                    path,
                    remaining,
                )
            project_instruction_bytes_used += len(truncated)
        else:
            try:
                content = path.read_text(encoding="utf-8", errors="ignore").strip()
            except Exception as exc:
                logger.debug("Failed to read %s: %s", path, exc)
                continue
        if source_kind.endswith("_rule"):
            content, conditional_paths = _parse_rule_content(content)
            # Conditional rules enter context only after a touched path matches.
            if conditional_paths:
                continue
        if not content:
            continue
        if (
            source_kind != "project_instruction"
            and len(content) > MAX_MEMORY_CHARACTER_COUNT
        ):
            logger.warning(
                "Large instruction file (%s chars > %s): %s",
                len(content),
                MAX_MEMORY_CHARACTER_COUNT,
                path,
            )
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
        with _GUIDELINE_CACHE_LOCK:
            include_parent = _INCLUDE_PARENT_PATHS.get(
                os.path.normcase(str(path.resolve())), ""
            )
        _schedule_instructions_loaded_hook(
            path,
            source_kind,
            load_reason="" if include_parent else load_reason,
            parent_file_path=include_parent,
            hook_manager=hook_manager,
        )
    return tuple(blocks)


def _schedule_instructions_loaded_hook(
    path: Path,
    source_kind: str,
    *,
    load_reason: str = "",
    trigger_file_path: str = "",
    parent_file_path: str = "",
    hook_manager: Any | None = None,
) -> None:
    try:
        hook_mgr = hook_manager
        if not hook_mgr:
            return
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    memory_type = {
        "project_instruction": "Project",
        "project_rule": "Project",
        "user_instruction": "User",
        "user_rule": "User",
        "managed_instruction": "Managed",
        "managed_rule": "Managed",
    }.get(source_kind, source_kind)
    if not parent_file_path:
        with _GUIDELINE_CACHE_LOCK:
            parent_file_path = _INCLUDE_PARENT_PATHS.get(
                os.path.normcase(str(path.resolve())), ""
            )
    resolved_reason = load_reason or ("include" if parent_file_path else "session_start")

    async def _run() -> None:
        try:
            await hook_mgr.run_instructions_loaded(
                file_path=str(path),
                memory_type=memory_type,
                load_reason=resolved_reason,
                trigger_file_path=trigger_file_path,
                parent_file_path=parent_file_path,
            )
        except Exception:
            logger.debug("instructions_loaded hook failed for %s", path)

    loop.create_task(_run())


def load_project_guideline_bundle(
    workspace_dir: str | Path | None = None,
    additional_directories: list[str | Path] | tuple[str | Path, ...] | None = None,
    *,
    load_reason: str = "session_start",
    project_root_markers: list[str] | tuple[str, ...] | None = None,
    project_doc_fallback_filenames: list[str] | tuple[str, ...] | None = None,
    project_doc_max_bytes: int | None = None,
    hook_manager: Any | None = None,
) -> GuidelineBundle:
    workspace_path = _normalize_directory(workspace_dir)
    extra_paths = _normalize_additional_directories(
        workspace_path, additional_directories
    )
    root_markers = _normalize_project_root_markers(project_root_markers)
    fallback_filenames = _normalize_project_doc_fallback_filenames(
        project_doc_fallback_filenames
    )
    max_bytes = _normalize_project_doc_max_bytes(project_doc_max_bytes)
    cache_key = (
        str(workspace_path),
        tuple(str(path) for path in extra_paths),
        root_markers,
        fallback_filenames,
        max_bytes,
    )
    specs = _expand_guideline_imports(
        _iter_guideline_specs(
            workspace_path,
            extra_paths,
            project_root_markers=root_markers,
            project_doc_fallback_filenames=fallback_filenames,
        )
    )
    signature = _build_signature(specs)

    with _GUIDELINE_CACHE_LOCK:
        cached = _GUIDELINE_CACHE.get(cache_key)
        if cached is not None and cached.cache_signature == signature:
            return cached

    blocks = _read_blocks(
        specs,
        load_reason=load_reason,
        project_doc_max_bytes=max_bytes,
        hook_manager=hook_manager,
    )
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
    *,
    load_reason: str = "session_start",
    project_root_markers: list[str] | tuple[str, ...] | None = None,
    project_doc_fallback_filenames: list[str] | tuple[str, ...] | None = None,
    project_doc_max_bytes: int | None = None,
    hook_manager: Any | None = None,
) -> str:
    return load_project_guideline_bundle(
        workspace_dir=workspace_dir,
        additional_directories=additional_directories,
        load_reason=load_reason,
        project_root_markers=project_root_markers,
        project_doc_fallback_filenames=project_doc_fallback_filenames,
        project_doc_max_bytes=project_doc_max_bytes,
        hook_manager=hook_manager,
    ).rendered_markdown


def load_matching_project_rules(
    workspace_dir: str | Path | None,
    target_paths: list[str | Path] | tuple[str | Path, ...],
    additional_directories: list[str | Path] | tuple[str | Path, ...] | None = None,
    *,
    project_root_markers: list[str] | tuple[str, ...] | None = None,
    project_doc_fallback_filenames: list[str] | tuple[str, ...] | None = None,
    hook_manager: Any | None = None,
) -> str:
    """Load conditional ` .minicode/rules` blocks matching files touched this turn."""
    workspace_path = _normalize_directory(workspace_dir)
    extra_paths = _normalize_additional_directories(
        workspace_path, additional_directories
    )
    resolved_targets: list[Path] = []
    for target in target_paths:
        try:
            candidate = Path(target)
            resolved_targets.append(
                candidate.resolve()
                if candidate.is_absolute()
                else (workspace_path / candidate).resolve()
            )
        except OSError:
            continue

    blocks: list[GuidelineBlock] = []
    seen: set[str] = set()
    for path, source_kind, label, priority, scope in _iter_guideline_specs(
        workspace_path,
        extra_paths,
        project_root_markers=_normalize_project_root_markers(project_root_markers),
        project_doc_fallback_filenames=_normalize_project_doc_fallback_filenames(
            project_doc_fallback_filenames
        ),
    ):
        if not source_kind.endswith("_rule"):
            continue
        try:
            raw_content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        content, patterns = _parse_rule_content(raw_content)
        if not content or not patterns:
            continue
        base_dir = workspace_path
        if source_kind == "project_rule":
            for parent in path.parents:
                if parent.name == ".minicode":
                    base_dir = parent.parent
                    break
        matcher = GitIgnoreSpec.from_lines(patterns)
        matched = False
        matched_target = ""
        for target in resolved_targets:
            try:
                relative = target.relative_to(base_dir).as_posix()
            except ValueError:
                continue
            if relative and matcher.match_file(relative):
                matched = True
                matched_target = str(target)
                break
        key = os.path.normcase(str(path.resolve()))
        if not matched or key in seen:
            continue
        seen.add(key)
        blocks.append(
            GuidelineBlock(
                path=path.resolve(),
                scope=scope,
                source_kind=source_kind,
                label=f"{label} (path match)",
                priority=priority,
                content=content,
            )
        )
        _schedule_instructions_loaded_hook(
            path,
            source_kind,
            load_reason="path_glob_match",
            trigger_file_path=matched_target,
            hook_manager=hook_manager,
        )
    if not blocks:
        return ""
    return "\n\n".join(block.to_markdown() for block in blocks)
