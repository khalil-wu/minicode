"""Session-owned, volatile JSON values shared by later code cells."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class CodeCellReceipt:
    id: str
    status: str
    error: str
    output: list[dict]
    hook_context: list[str]
    discarded_calls: int


class CodeExecutionStore:
    def __init__(self):
        self.values: dict[str, Any] = {}
        self.receipts: dict[str, CodeCellReceipt] = {}
        self._sizes: dict[str, int] = {}
        self._bytes = 0

    def put(self, key: str, value: Any) -> None:
        size = len(json.dumps({key: value}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        total = self._bytes - self._sizes.get(key, 0) + size
        if total > 8 * 1024 * 1024:
            raise ValueError("Code-mode store exceeded 8 MiB; store artifact references instead of complete datasets.")
        self.values[key] = value
        self._sizes[key] = size
        self._bytes = total
