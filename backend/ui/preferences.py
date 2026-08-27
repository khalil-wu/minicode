"""UI preferences storage and retrieval."""
import json
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Literal

from backend.atomic_io import atomic_write_text, file_mutation_locks

logger = logging.getLogger(__name__)

# Session ids are embedded directly into filenames, so reject anything that is
# not a safe path segment to prevent traversal outside data_dir.
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


FontSize = Literal["xs", "sm", "base", "md", "lg"]


@dataclass
class UIPreferences:
    """User UI preferences."""
    sidebar_width: int = 280
    runtime_rail_visible: bool = True
    message_font_size: FontSize = "base"
    code_font_size: FontSize = "sm"
    compact_mode: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "UIPreferences":
        """Create from dictionary."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class UIPreferencesStore:
    """Store for UI preferences."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, UIPreferences] = {}

    def _get_path(self, session_id: str) -> Path:
        """Get path for session preferences."""
        clean = str(session_id or "").strip()
        if not _SAFE_SESSION_ID.match(clean):
            raise ValueError("invalid session_id for ui preferences")
        return self.data_dir / f"ui_prefs_{clean}.json"

    def get(self, session_id: str) -> UIPreferences:
        """Get preferences for session."""
        try:
            path = self._get_path(session_id)
        except ValueError:
            logger.warning("rejected ui preferences lookup for unsafe session_id")
            prefs = UIPreferences()
            self._cache[session_id] = prefs
            return prefs
        with file_mutation_locks([path]):
            return self._read_unlocked(session_id, path)

    def save(self, session_id: str, preferences: UIPreferences) -> None:
        """Save preferences for session."""
        try:
            path = self._get_path(session_id)
        except ValueError:
            logger.warning("rejected ui preferences save for unsafe session_id")
            return
        with file_mutation_locks([path]):
            self._write_unlocked(session_id, path, preferences)

    def update(self, session_id: str, updates: dict) -> UIPreferences:
        """Update preferences for session."""
        try:
            path = self._get_path(session_id)
        except ValueError:
            logger.warning("rejected ui preferences update for unsafe session_id")
            return UIPreferences()
        # Read-modify-write must be one critical section.  The API creates a
        # store per request, so an instance-local cache or separately locked
        # get/save pair can otherwise lose concurrent panel preference edits.
        with file_mutation_locks([path]):
            prefs = self._read_unlocked(session_id, path)
            for key, value in updates.items():
                if hasattr(prefs, key):
                    setattr(prefs, key, value)
            self._write_unlocked(session_id, path, prefs)
            return prefs

    def _read_unlocked(self, session_id: str, path: Path) -> UIPreferences:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    prefs = UIPreferences.from_dict(data)
                    self._cache[session_id] = prefs
                    return prefs
            except (OSError, ValueError, TypeError):
                logger.warning("invalid ui preferences file ignored: %s", path)
        prefs = UIPreferences()
        self._cache[session_id] = prefs
        return prefs

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
        self._cache[session_id] = preferences
