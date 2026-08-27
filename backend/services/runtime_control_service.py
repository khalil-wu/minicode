from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CommandOutcome:
    command: str
    message: str
    level: str = "info"
    data: dict[str, Any] | None = None
