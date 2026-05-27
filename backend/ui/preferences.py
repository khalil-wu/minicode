"""UI preferences storage and retrieval."""
from dataclasses import dataclass, field, asdict
from typing import Literal
import json
from pathlib import Path


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
        return self.data_dir / f"ui_prefs_{session_id}.json"

    def get(self, session_id: str) -> UIPreferences:
        """Get preferences for session."""
        if session_id in self._cache:
            return self._cache[session_id]

        path = self._get_path(session_id)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                prefs = UIPreferences.from_dict(data)
                self._cache[session_id] = prefs
                return prefs
            except Exception:
                pass

        # Return defaults
        prefs = UIPreferences()
        self._cache[session_id] = prefs
        return prefs

    def save(self, session_id: str, preferences: UIPreferences) -> None:
        """Save preferences for session."""
        self._cache[session_id] = preferences
        path = self._get_path(session_id)
        path.write_text(json.dumps(preferences.to_dict(), indent=2))

    def update(self, session_id: str, updates: dict) -> UIPreferences:
        """Update preferences for session."""
        prefs = self.get(session_id)
        for key, value in updates.items():
            if hasattr(prefs, key):
                setattr(prefs, key, value)
        self.save(session_id, prefs)
        return prefs
