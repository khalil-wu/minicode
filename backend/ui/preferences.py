"""UI preferences storage and retrieval."""
import json
import re
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

from backend.atomic_io import atomic_write_text, file_mutation_locks

# Session ids are embedded directly into filenames, so reject anything that is
# not a safe path segment to prevent traversal outside data_dir.
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


FontSize = Literal["xs", "sm", "base", "md", "lg"]


class UIPreferences(BaseModel):
    """User UI preferences."""
    model_config = ConfigDict(strict=True, extra="forbid")
    sidebar_width: int = Field(default=280, gt=0)
    runtime_rail_visible: bool = True
    message_font_size: FontSize = "base"
    code_font_size: FontSize = "sm"
    compact_mode: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "UIPreferences":
        """Create from dictionary."""
        return cls.model_validate(data)


class UIPreferencesStore:
    """Store for UI preferences."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _get_path(self, session_id: str) -> Path:
        """Get path for session preferences."""
        clean = str(session_id or "").strip()
        if not _SAFE_SESSION_ID.match(clean):
            raise ValueError("invalid session_id for ui preferences")
        return self.data_dir / f"ui_prefs_{clean}.json"

    def get(self, session_id: str) -> UIPreferences:
        """Get preferences for session."""
        path = self._get_path(session_id)
        with file_mutation_locks([path]):
            return self._read_unlocked(session_id, path)

    def save(self, session_id: str, preferences: UIPreferences) -> None:
        """Save preferences for session."""
        path = self._get_path(session_id)
        with file_mutation_locks([path]):
            self._write_unlocked(session_id, path, preferences)

    def update(self, session_id: str, updates: dict) -> UIPreferences:
        """Update preferences for session."""
        path = self._get_path(session_id)
        validated = UIPreferences.from_dict(updates).model_dump(exclude_unset=True)
        # Read-modify-write must be one critical section.  The API creates a
        # store per request, so an instance-local cache or separately locked
        # get/save pair can otherwise lose concurrent panel preference edits.
        with file_mutation_locks([path]):
            prefs = self._read_unlocked(session_id, path)
            updated = prefs.model_copy(update=validated)
            self._write_unlocked(session_id, path, updated)
            return updated

    def _read_unlocked(self, session_id: str, path: Path) -> UIPreferences:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return UIPreferences()
        return UIPreferences.from_dict(data)

    def _write_unlocked(
        self,
        session_id: str,
        path: Path,
        preferences: UIPreferences,
    ) -> None:
        atomic_write_text(
            path,
            json.dumps(preferences.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )
