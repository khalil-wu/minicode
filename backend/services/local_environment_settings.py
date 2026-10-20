"""Source local-environment documents, owned by MiniCode's project state."""
from __future__ import annotations

import hashlib
import json
import sys
import tomllib
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.atomic_io import atomic_write_text, canonical_file_path_key, file_mutation_locks
from backend.config import DATA_ROOT

SELECTIONS_FILE = DATA_ROOT / "local-environment-selections.json"
SHELL_ENVIRONMENTS_FILE = DATA_ROOT / "local-environment-shell.json"
Platform = Literal["darwin", "linux", "win32"]


class PlatformScript(BaseModel):
    model_config = ConfigDict(extra="allow")
    script: str = ""


class LifecycleScript(PlatformScript):
    darwin: PlatformScript | None = None
    linux: PlatformScript | None = None
    win32: PlatformScript | None = None

    def for_platform(self, platform: str) -> str:
        override = getattr(self, platform)
        return override.script if override is not None and override.script else self.script


class LocalAction(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    command: str
    icon: Literal["tool", "run", "debug", "test"] | None = None
    platform: Platform | None = None

    @field_validator("icon", mode="before")
    @classmethod
    def source_icon(cls, value):
        # Source's icon schema is nullable().catch(null); unknown icons use
        # the same standard Tool presentation as an omitted icon.
        return value if value in ("tool", "run", "debug", "test") else None


class LocalEnvironment(BaseModel):
    model_config = ConfigDict(extra="allow")
    version: int = Field(default=1, ge=1)
    name: str
    setup: LifecycleScript
    cleanup: LifecycleScript = Field(default_factory=LifecycleScript)
    actions: list[LocalAction] = Field(default_factory=list)


def environment_directory(root: Path) -> Path:
    return root / ".minicode" / "environments"


def source_checkout_root(root: Path) -> Path:
    return next((path for path in (root, *root.parents) if (path / ".git").exists()), root)


def config_path_for(root: Path, value: str = "") -> Path:
    directory = environment_directory(root).resolve()
    if not directory.is_relative_to(root.resolve()):
        raise ValueError("The local environment directory must stay within its project.")
    path = Path(value).resolve() if value else directory / "environment.toml"
    if path.parent != directory or path.suffix != ".toml":
        raise ValueError("Local environment files must be TOML documents in the project's .minicode/environments directory.")
    return path


def _selections() -> dict:
    if not SELECTIONS_FILE.exists():
        return {"workspaces": {}, "worktrees": {}}
    return json.loads(SELECTIONS_FILE.read_text(encoding="utf-8"))


def selected_config(root: Path, worktree: Path | None = None) -> Path | None:
    with file_mutation_locks([SELECTIONS_FILE]):
        selections = _selections()
    if worktree is not None:
        selected = selections["worktrees"].get(canonical_file_path_key(worktree))
        if selected is not None:
            return config_path_for(Path(selected["source_root"]), selected["config_path"]) if selected["config_path"] else None
    inherited = selections["worktrees"].get(canonical_file_path_key(root))
    if inherited is not None:
        return config_path_for(Path(inherited["source_root"]), inherited["config_path"]) if inherited["config_path"] else None
    selected = selections["workspaces"].get(canonical_file_path_key(root))
    if selected is not None:
        return config_path_for(root, selected) if selected else None
    default = config_path_for(root)
    return default if default.is_file() else None


def select_environment(root: Path, config_path: str, worktree: Path | None = None, *, source_workspace_root: Path | None = None) -> None:
    selected = str(config_path_for(root, config_path)) if config_path else ""
    if selected:
        read_environment(root, selected)
    with file_mutation_locks([SELECTIONS_FILE]):
        selections = _selections()
        if worktree is None:
            selections["workspaces"][canonical_file_path_key(root)] = selected
        else:
            selections["worktrees"][canonical_file_path_key(worktree)] = {"source_root": str(root), "config_path": selected,
                "source_workspace_root": str(source_workspace_root or root)}
        SELECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(SELECTIONS_FILE, json.dumps(selections, ensure_ascii=False, indent=2))


def bind_worktree_environment(root: Path, worktree: Path) -> None:
    selected = selected_config(root)
    if selected is None:
        select_environment(root, "", worktree)
        return
    target = worktree / selected.relative_to(source_checkout_root(selected.parents[2]))
    target_root = target.parents[2]
    config_path_for(target_root, str(target))
    if target.exists():
        if not target.is_file() or target.is_symlink():
            raise ValueError("The selected worktree environment is not a regular file.")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, selected.read_text(encoding="utf-8"))
    select_environment(target_root, str(target), worktree, source_workspace_root=root)


def worktree_source_workspace(root: Path, worktree: Path) -> Path:
    with file_mutation_locks([SELECTIONS_FILE]):
        entry = _selections()["worktrees"].get(canonical_file_path_key(worktree))
    return Path(entry["source_workspace_root"]) if entry is not None else root


def release_worktree_environment(worktree: Path) -> None:
    with file_mutation_locks([SELECTIONS_FILE]):
        selections = _selections()
        if selections["worktrees"].pop(canonical_file_path_key(worktree), None) is not None:
            atomic_write_text(SELECTIONS_FILE, json.dumps(selections, ensure_ascii=False, indent=2))


def read_environment(root: Path, config_path: str = "") -> dict:
    path = config_path_for(root, config_path)
    exists = path.exists()
    raw = path.read_bytes() if exists else b""
    environment = LocalEnvironment.model_validate(tomllib.loads(raw.decode("utf-8"))) if exists else None
    return {"config_path": str(path), "revision": hashlib.sha256(raw).hexdigest(),
            "environment": environment.model_dump(exclude_none=True) if environment is not None else None}


def _toml_value(value) -> str:
    if isinstance(value, str): return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool): return "true" if value else "false"
    if isinstance(value, (date, datetime, time)): return value.isoformat()
    if isinstance(value, list): return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict): return "{ " + ", ".join(f"{json.dumps(key)} = {_toml_value(item)}" for key, item in value.items()) + " }"
    return str(value)


def environment_toml(environment: LocalEnvironment) -> str:
    document = environment.model_dump(exclude_none=True)
    lines = ["# THIS IS AUTOGENERATED BY MINICODE", *[f"{json.dumps(key)} = {_toml_value(value)}"
        for key, value in document.items() if key not in {"setup", "cleanup", "actions"}]]
    for kind in ("setup", "cleanup"):
        lifecycle = document[kind]
        lines.extend(["", f"[{kind}]", *[f"{json.dumps(key)} = {_toml_value(value)}"
            for key, value in lifecycle.items() if key not in {"darwin", "linux", "win32"}]])
        for platform in ("darwin", "linux", "win32"):
            if platform in lifecycle:
                lines.extend(["", f"[{kind}.{platform}]", *[f"{json.dumps(key)} = {_toml_value(value)}" for key, value in lifecycle[platform].items()]])
    for action in document["actions"]:
        lines.extend(["", "[[actions]]", *[f"{json.dumps(key)} = {_toml_value(value)}" for key, value in action.items()]])
    return "\n".join(lines) + "\n"


class EnvironmentConflict(ValueError):
    pass


def save_environment(root: Path, config_path: str, revision: str, environment: LocalEnvironment) -> dict:
    environment.name = environment.name.strip()
    if not environment.name: raise ValueError("Environment name is required.")
    environment.actions = [action for action in environment.actions if action.name.strip() or action.command.strip()]
    for action in environment.actions:
        action.name, action.command = action.name.strip(), action.command.strip()
        if not action.name or not action.command: raise ValueError("Every action needs both a name and a script.")
    path = config_path_for(root, config_path)
    with file_mutation_locks([path]):
        current = read_environment(root, str(path))
        if current["revision"] != revision:
            raise EnvironmentConflict("The environment file changed since it was opened. Your draft has been kept.")
        # Unknown TOML fields travel through the editable document, just as the
        # original configuration does; saving never rewrites a different file.
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, environment_toml(environment))
        return read_environment(root, str(path))


def project_environments(root: Path) -> dict:
    directory = environment_directory(root)
    configs = [read_environment(root, str(path)) for path in sorted(directory.glob("*.toml"))] if directory.exists() else []
    selected = selected_config(root)
    return {"workspace_root": str(root), "configs": configs, "selected_config_path": str(selected) if selected else "",
            "default_config_path": str(config_path_for(root)), "platform": sys.platform}


def effective_environment(root: Path, worktree: Path | None = None, *, platform: str | None = None) -> dict:
    selected = selected_config(root, worktree)
    if selected is None:
        return {"config_path": "", "environment": None, "actions": []}
    document = read_environment(selected.parents[2], str(selected))
    environment = document["environment"]
    return {**document, "actions": [{**action, "index": index} for index, action in enumerate(environment["actions"])
        if action.get("platform") in (None, platform or sys.platform)]} if environment is not None else {**document, "actions": []}


def workspace_environment(path: Path) -> dict:
    from backend.services.workspace_service import main_worktree_root
    path = path.resolve()
    checkout = source_checkout_root(path)
    main = main_worktree_root(path)
    root = main / path.relative_to(checkout)
    worktree = checkout if checkout != main else None
    return {**effective_environment(root, worktree), "source_root": str(root),
            "worktree_path": str(worktree) if worktree is not None else ""}


class LocalEnvironmentActionRequest(BaseModel):
    config_path: str
    revision: str
    index: int = Field(ge=0)


def environment_action(path: Path, request: LocalEnvironmentActionRequest) -> str:
    selected = workspace_environment(path)
    if selected["config_path"] != request.config_path or selected.get("revision") != request.revision:
        raise EnvironmentConflict("The selected local environment changed. Open the action again to use its current script.")
    action = next((action for action in selected["actions"] if action["index"] == request.index), None)
    if action is None: raise ValueError("The local environment action is not available on this platform.")
    return action["command"]


def store_worktree_shell_environment(worktree: Path, values: dict) -> None:
    with file_mutation_locks([SHELL_ENVIRONMENTS_FILE]):
        saved = json.loads(SHELL_ENVIRONMENTS_FILE.read_text(encoding="utf-8")) if SHELL_ENVIRONMENTS_FILE.exists() else {}
        saved[canonical_file_path_key(worktree)] = values
        SHELL_ENVIRONMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(SHELL_ENVIRONMENTS_FILE, json.dumps(saved, ensure_ascii=False, indent=2))


def worktree_shell_policy(policy, cwd: Path):
    from dataclasses import replace
    if not SHELL_ENVIRONMENTS_FILE.exists(): return policy
    with file_mutation_locks([SHELL_ENVIRONMENTS_FILE]):
        saved = json.loads(SHELL_ENVIRONMENTS_FILE.read_text(encoding="utf-8"))
    for path in (cwd.resolve(), *cwd.resolve().parents):
        delta = saved.get(canonical_file_path_key(path))
        if delta is not None:
            # Explicit configured values and per-command overrides retain their
            # usual precedence over exports from a successful setup script.
            return replace(policy, set_values={**delta["set"], **policy.set_values},
                exclude=tuple(dict.fromkeys((*policy.exclude, *delta["exclude"]))))
    return policy
