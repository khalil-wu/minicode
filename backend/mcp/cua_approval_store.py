"""Persistent app approvals owned by MiniCode's native CUA provider."""
from __future__ import annotations

import json
from pathlib import Path

from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.runtime_paths import agent_runtime_root


def native_approval_home() -> Path:
    return agent_runtime_root() / "computer-use-native"


class NativeAppApprovalStore:
    def __init__(self, home: Path):
        self.path = home / "app-approvals.json"

    def _read(self):
        if not self.path.exists():
            return {"schema_version": 1, "apps": {}}
        document = json.loads(self.path.read_text(encoding="utf-8"))
        if document["schema_version"] != 1:
            raise ValueError("Unsupported MiniCode native app approval store version")
        return document

    def get(self, app_id: str):
        return self._read()["apps"].get(app_id)

    def list_apps(self):
        return sorted(self._read()["apps"].values(), key=lambda app: (app["display_name"].casefold(), app["app_id"]))

    def grant(self, identity):
        record = {"app_id": identity["app_id"], "display_name": identity["name"],
            "icon_data_url": identity.get("icon_data_url"), "executable": identity["executable"], "aumid": identity["aumid"]}
        with file_mutation_locks([self.path]):
            document = self._read()
            document["apps"][record["app_id"]] = record
            atomic_write_text(self.path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        return record

    def remove(self, app_id: str):
        with file_mutation_locks([self.path]):
            document = self._read()
            document["apps"].pop(app_id, None)
            atomic_write_text(self.path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        return self.list_apps()
