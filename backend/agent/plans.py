"""MiniCode Markdown plan-file ownership.

A conversation owns a lazily-created word slug, the plan lives outside the
workspace by default, resume reuses that slug, and forks receive a new slug and
an independent copy. Plan mode grants access to one exact owner path only.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence

from backend.atomic_io import atomic_write_bytes, atomic_write_text, file_mutation_locks
from backend.config import STATE_ROOT, load_config_layer_stack


MAX_SLUG_RETRIES = 10
PLAN_SLUG_KEY = "plan_slug"
PLAN_FILE_CONSTRAINT = "plan_files"
PLAN_FILE_REFERENCE_KEY = "plan_file_reference"
PLAN_RECOVERY_STATUS_KEY = "plan_recovery_status"

_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+){2}$")
_AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_ADJECTIVES = (
    "bright", "calm", "clever", "curious", "gentle", "golden",
    "hidden", "lively", "luminous", "misty", "quiet", "radiant",
    "serene", "steady", "swift", "tranquil", "vivid", "warm",
)
_VERBS = (
    "building", "crafting", "exploring", "mapping", "planning",
    "polishing", "reviewing", "shaping", "tracing", "weaving",
)
_NOUNS = (
    "beacon", "bridge", "comet", "forest", "harbor", "lantern",
    "meadow", "orchid", "phoenix", "river", "summit", "willow",
)


class PlanFileError(ValueError):
    """Raised when a plan owner/path cannot be represented safely."""


def _resolved_without_following_leaf(path: Path) -> Path:
    """Resolve the parent while retaining an absent/non-symlink leaf."""

    return path.parent.expanduser().resolve() / path.name


def _settings_for_workspace(workspace_root: Path | str | None) -> Mapping[str, Any]:
    try:
        settings = load_config_layer_stack(
            cwd=Path(workspace_root).expanduser().resolve() if workspace_root else None,
        ).effective_config()
    except Exception:
        return {}
    return settings if isinstance(settings, Mapping) else {}


def get_plans_directory(
    workspace_root: Path | str | None = None,
    *,
    settings: Mapping[str, Any] | None = None,
    create: bool = True,
) -> Path:
    """Return the configured plans directory with project-root safety rules."""

    effective = settings if isinstance(settings, Mapping) else _settings_for_workspace(workspace_root)
    configured = str(
        effective.get("plansDirectory")
        or effective.get("plans_directory")
        or ""
    ).strip()
    default = (STATE_ROOT / "plans").expanduser().resolve()
    plans_dir = default
    if configured and workspace_root:
        root = Path(workspace_root).expanduser().resolve()
        candidate = (root / configured).resolve()
        if candidate.is_relative_to(root):
            plans_dir = candidate
    if create:
        plans_dir.mkdir(parents=True, exist_ok=True)
    return plans_dir


def validate_plan_slug(slug: str) -> str:
    normalized = str(slug or "").strip().lower()
    if not _SLUG_PATTERN.fullmatch(normalized):
        raise PlanFileError("Invalid plan slug")
    return normalized


def generate_plan_slug(
    workspace_root: Path | str | None = None,
    *,
    plans_directory: Path | None = None,
) -> str:
    plans_dir = Path(plans_directory or get_plans_directory(workspace_root)).resolve()
    for _ in range(MAX_SLUG_RETRIES):
        slug = "-".join((
            secrets.choice(_ADJECTIVES),
            secrets.choice(_VERBS),
            secrets.choice(_NOUNS),
        ))
        if not (plans_dir / f"{slug}.md").exists():
            return slug
    # Keep the human-readable shape while making the final candidate
    # collision-resistant after the bounded allocation attempts.
    return f"bright-planning-{secrets.token_hex(6)}"


def plan_slug_from_snapshot(snapshot: Mapping[str, Any] | None) -> str | None:
    raw = str((snapshot or {}).get(PLAN_SLUG_KEY) or "").strip()
    if not raw:
        return None
    try:
        return validate_plan_slug(raw)
    except PlanFileError:
        return None


def ensure_plan_slug(
    snapshot: MutableMapping[str, Any],
    workspace_root: Path | str | None = None,
) -> str:
    current = plan_slug_from_snapshot(snapshot)
    if current:
        return current
    slug = generate_plan_slug(workspace_root)
    snapshot[PLAN_SLUG_KEY] = slug
    return slug


def bind_plan_owner(
    repository: Any,
    conversation_id: str,
    workspace_root: Path | str | None = None,
) -> tuple[str, Path]:
    """Persist a lazy conversation slug and return its exact Plan path."""

    owner = str(conversation_id or "").strip()
    if not owner:
        raise PlanFileError("Conversation owner is required")
    record = repository.get_conversation(owner)
    if record is None:
        raise PlanFileError("Conversation owner was not found")
    snapshot = dict(getattr(record, "context_snapshot", {}) or {})
    slug = ensure_plan_slug(snapshot, workspace_root or getattr(record, "workspace_root", ""))
    if snapshot != getattr(record, "context_snapshot", {}):
        updated = repository.patch_context_snapshot(
            owner,
            {PLAN_SLUG_KEY: slug},
        )
        if updated is None:
            raise PlanFileError("Conversation owner disappeared while binding its plan")
    return slug, get_plan_file_path(
        slug,
        workspace_root or getattr(record, "workspace_root", "") or None,
    )


def get_plan_file_path(
    slug: str,
    workspace_root: Path | str | None = None,
    *,
    agent_id: str | None = None,
    settings: Mapping[str, Any] | None = None,
) -> Path:
    normalized_slug = validate_plan_slug(slug)
    suffix = ""
    if agent_id:
        normalized_agent = str(agent_id).strip()
        if not _AGENT_ID_PATTERN.fullmatch(normalized_agent):
            raise PlanFileError("Invalid plan agent owner")
        suffix = f"-agent-{normalized_agent}"
    plans_dir = get_plans_directory(workspace_root, settings=settings)
    candidate = _resolved_without_following_leaf(
        plans_dir / f"{normalized_slug}{suffix}.md"
    )
    if not candidate.is_relative_to(plans_dir.resolve()):
        raise PlanFileError("Plan path escapes plans directory")
    if candidate.exists() and candidate.is_symlink():
        raise PlanFileError("Plan files may not be symbolic links")
    return candidate


def plan_path_for_snapshot(
    snapshot: Mapping[str, Any] | None,
    workspace_root: Path | str | None = None,
    *,
    agent_id: str | None = None,
) -> Path | None:
    slug = plan_slug_from_snapshot(snapshot)
    return get_plan_file_path(slug, workspace_root, agent_id=agent_id) if slug else None


def plan_file_reference(path: Path | str, content: str | None = None) -> dict[str, str]:
    reference = {
        "type": "plan_file_reference",
        "path": str(Path(path).expanduser().resolve()),
    }
    if isinstance(content, str) and content:
        reference["plan_content"] = content
    return reference


def ensure_plan_file_for_resume(
    snapshot: MutableMapping[str, Any],
    transcript: Sequence[Mapping[str, Any]],
    workspace_root: Path | str | None = None,
) -> tuple[Path | None, str]:
    """Reuse a resumed session's slug and recover its missing Plan file.

    Existing files win; a missing file is recovered from transcript
    snapshots/normalized plan-tool input. Failure is explicit in the snapshot
    and never allocates an unrelated slug.
    """

    path = plan_path_for_snapshot(snapshot, workspace_root)
    if path is None:
        snapshot.pop(PLAN_FILE_REFERENCE_KEY, None)
        snapshot.pop(PLAN_RECOVERY_STATUS_KEY, None)
        return None, "no_plan"
    current = read_plan(path)
    if current is not None:
        snapshot[PLAN_FILE_REFERENCE_KEY] = plan_file_reference(path, current)
        snapshot[PLAN_RECOVERY_STATUS_KEY] = "available"
        return path, "available"
    recovered = recover_plan_from_transcript(transcript)
    if recovered:
        write_plan(path, recovered)
        snapshot[PLAN_FILE_REFERENCE_KEY] = plan_file_reference(path, recovered)
        snapshot[PLAN_RECOVERY_STATUS_KEY] = "recovered"
        return path, "recovered"
    snapshot[PLAN_FILE_REFERENCE_KEY] = plan_file_reference(path)
    snapshot[PLAN_RECOVERY_STATUS_KEY] = "missing"
    return path, "missing"


def read_plan(path: Path | str) -> str | None:
    target = Path(path)
    with file_mutation_locks([target]):
        if target.is_symlink():
            raise PlanFileError("Plan files may not be symbolic links")
        try:
            return target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None


def write_plan(path: Path | str, content: str) -> None:
    target = Path(path)
    with file_mutation_locks([target]):
        if target.exists() and target.is_symlink():
            raise PlanFileError("Plan files may not be symbolic links")
        atomic_write_text(target, str(content))


def copy_plan_for_fork(
    source_snapshot: Mapping[str, Any] | None,
    target_snapshot: MutableMapping[str, Any],
    workspace_root: Path | str | None,
) -> tuple[str | None, Path | None]:
    """Give a fork a new slug and copy the source plan when one exists."""

    original_slug = plan_slug_from_snapshot(source_snapshot)
    if not original_slug:
        target_snapshot.pop(PLAN_SLUG_KEY, None)
        return None, None
    plans_dir = get_plans_directory(workspace_root)
    new_slug = generate_plan_slug(workspace_root, plans_directory=plans_dir)
    source_path = get_plan_file_path(original_slug, workspace_root)
    target_path = get_plan_file_path(new_slug, workspace_root)
    with file_mutation_locks([source_path, target_path]):
        if source_path.exists():
            if source_path.is_symlink():
                raise PlanFileError("Plan files may not be symbolic links")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                atomic_write_bytes(target_path, source_path.read_bytes(), overwrite=False)
            except FileExistsError as exc:
                raise PlanFileError("Generated fork plan path already exists") from exc
    target_snapshot[PLAN_SLUG_KEY] = new_slug
    return new_slug, target_path if target_path.exists() else None


def recover_plan_from_transcript(messages: Sequence[Mapping[str, Any]]) -> str | None:
    """Apply MiniCode's transcript recovery precedence for a missing plan file."""

    # File snapshots are authoritative when present.
    for message in reversed(messages):
        if not isinstance(message, Mapping):
            continue
        snapshots = message.get("snapshotFiles") or message.get("snapshot_files")
        if isinstance(snapshots, list):
            for item in snapshots:
                if isinstance(item, Mapping) and item.get("key") == "plan":
                    content = item.get("content")
                    if isinstance(content, str) and content:
                        return content

    for message in reversed(messages):
        if not isinstance(message, Mapping):
            continue
        # Normalized ExitPlanMode input persisted in assistant tool blocks.
        blocks = message.get("content") or message.get("blocks")
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, Mapping):
                    continue
                name = str(block.get("name") or block.get("tool_name") or "")
                payload = block.get("input") or block.get("arguments")
                if name in {"ExitPlanMode", "exit_plan_mode"} and isinstance(payload, Mapping):
                    plan = payload.get("plan")
                    if isinstance(plan, str) and plan:
                        return plan
        plan_content = message.get("plan_content")
        if isinstance(plan_content, str) and plan_content:
            return plan_content
        attachment = message.get("attachment")
        if isinstance(attachment, Mapping) and attachment.get("type") == "plan_file_reference":
            plan = attachment.get("plan_content")
            if isinstance(plan, str) and plan:
                return plan
    return None


def plan_constraints(path: Path | str) -> dict[str, list[str]]:
    return {PLAN_FILE_CONSTRAINT: [str(Path(path).expanduser().resolve())]}


def current_plan_paths(permission_or_context: Any) -> tuple[Path, ...]:
    permission = getattr(permission_or_context, "permission", permission_or_context)
    constraints = getattr(permission, "filesystem_constraints", {}) or {}
    paths: list[Path] = []
    for raw in constraints.get(PLAN_FILE_CONSTRAINT, []):
        try:
            paths.append(_resolved_without_following_leaf(Path(str(raw))))
        except (OSError, ValueError):
            continue
    return tuple(paths)


def is_current_plan_file(path: Path | str, permission_or_context: Any) -> bool:
    try:
        candidate = _resolved_without_following_leaf(Path(path))
    except (OSError, ValueError):
        return False
    return any(candidate == allowed for allowed in current_plan_paths(permission_or_context))


def merge_plan_constraints(
    constraints: Mapping[str, Sequence[str]] | None,
    path: Path | str | None,
) -> dict[str, list[str]]:
    merged = {key: [str(item) for item in values] for key, values in (constraints or {}).items()}
    if path is None:
        merged.pop(PLAN_FILE_CONSTRAINT, None)
    else:
        merged[PLAN_FILE_CONSTRAINT] = [str(Path(path).expanduser().resolve())]
    return merged


def cleanup_plan_file(path: Path | str | None) -> None:
    if path is None:
        return
    target = Path(path)
    with file_mutation_locks([target]):
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "PLAN_FILE_CONSTRAINT", "PLAN_SLUG_KEY", "PlanFileError",
    "bind_plan_owner", "cleanup_plan_file", "copy_plan_for_fork", "current_plan_paths",
    "ensure_plan_file_for_resume", "ensure_plan_slug", "generate_plan_slug", "get_plan_file_path",
    "get_plans_directory", "is_current_plan_file", "merge_plan_constraints",
    "plan_constraints", "plan_file_reference", "plan_path_for_snapshot", "plan_slug_from_snapshot",
    "read_plan", "recover_plan_from_transcript", "validate_plan_slug",
    "write_plan",
]
