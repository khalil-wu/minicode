"""User-owned Git preferences used by the desktop Git actions."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.agent.markdown_scopes import get_minicode_config_home_dir
from backend.atomic_io import atomic_write_text, file_mutation_locks


GIT_PREFERENCES_FILE = get_minicode_config_home_dir() / "git-preferences.json"


class GitPreferences(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    branch_prefix: str = "codex/"
    always_force_push: bool = False
    create_draft_pull_requests: bool = True
    pull_request_merge_method: Literal["merge", "squash"] = "merge"
    commit_instructions: str = ""
    pull_request_instructions: str = ""
    review_delivery: Literal["inline", "detached"] = "inline"


def _read_preferences() -> GitPreferences:
    try:
        contents = GIT_PREFERENCES_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return GitPreferences()
    return GitPreferences.model_validate_json(contents)


def get_git_preferences() -> GitPreferences:
    with file_mutation_locks([GIT_PREFERENCES_FILE]):
        return _read_preferences()


def update_git_preferences(updates: GitPreferences) -> GitPreferences:
    with file_mutation_locks([GIT_PREFERENCES_FILE]):
        preferences = _read_preferences().model_copy(update=updates.model_dump(exclude_unset=True))
        atomic_write_text(GIT_PREFERENCES_FILE, json.dumps(preferences.model_dump(), ensure_ascii=False, indent=2) + "\n")
        return preferences


def generated_branch_name(name: str) -> str:
    return get_git_preferences().branch_prefix + name
