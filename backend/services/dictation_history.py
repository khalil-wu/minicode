"""The local dictation archive. Audio and transcript actions share one ID."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from backend.atomic_io import atomic_write_bytes, atomic_write_text, file_mutation_locks
from backend.config import DATA_ROOT


class DictationHistory:
    def __init__(self, root: Path = DATA_ROOT / "dictation") -> None:
        self.root = root
        self.index = root / "recordings.json"

    def _read(self) -> list[dict]:
        if not self.index.exists():
            return []
        return json.loads(self.index.read_text(encoding="utf-8"))

    def _write(self, records: list[dict]) -> None:
        atomic_write_text(self.index, json.dumps(records, ensure_ascii=False, indent=2))

    def list(self) -> list[dict]:
        with file_mutation_locks([self.index]):
            return self._read()

    def create(self, audio: bytes, **metadata) -> dict:
        record = {**metadata, "id": str(uuid.uuid4()), "created_at": time.time(), "text": "", "error": ""}
        with file_mutation_locks([self.index]):
            records = [record, *self._read()]
            atomic_write_bytes(self.root / (record["id"] + ".audio"), audio)
            self._write(records[:20])
            for expired in records[20:]:
                (self.root / (expired["id"] + ".audio")).unlink()
        return record

    def read_audio(self, recording_id: str) -> tuple[dict, bytes]:
        with file_mutation_locks([self.index]):
            record = next((item for item in self._read() if item["id"] == recording_id), None)
            if record is None:
                raise KeyError(recording_id)
            return record, (self.root / (record["id"] + ".audio")).read_bytes()

    def update(self, recording_id: str, **changes) -> dict | None:
        with file_mutation_locks([self.index]):
            records = self._read()
            record = next((item for item in records if item["id"] == recording_id), None)
            # Deleting or expiring a recording during transcription must not
            # recreate it when that older request completes.
            if record is None:
                return None
            record.update(changes)
            self._write(records)
            return record

    def delete(self, recording_id: str) -> None:
        with file_mutation_locks([self.index]):
            records = self._read()
            record = next((item for item in records if item["id"] == recording_id), None)
            if record is None:
                raise KeyError(recording_id)
            self._write([item for item in records if item is not record])
            (self.root / (record["id"] + ".audio")).unlink()


dictation_history = DictationHistory()
