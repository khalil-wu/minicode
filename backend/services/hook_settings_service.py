"""Desktop hook controls backed by the same registry used for turn admission."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.config import SETTINGS_FILE, _update_settings_json
from backend.hooks.manager import load_hook_manager_for_workspace
from backend.hooks.runtime import raise_if_config_change_blocked, run_config_change_hook


def hook_settings(workspace_root: str = "") -> dict[str, Any]:
    from backend.workspace.recent_projects import RecentProjectStore
    roots = [workspace_root] if workspace_root else ["", *(str(project.path) for project in RecentProjectStore().list())]
    entries: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for root in roots:
        manager = load_hook_manager_for_workspace(Path(root) if root else None)
        payload = manager.list_hooks()
        definitions = {entry.key: entry for entry in manager.snapshot.entries}
        for entry in payload["hooks"]:
            entries.setdefault(entry["key"], {**entry, "prompt": definitions[entry["key"]].prompt or None})
        warnings.extend(payload["warnings"])
    return {"hooks": list(entries.values()), "warnings": list(dict.fromkeys(warnings))}


async def update_hook_settings(updates: list[dict[str, Any]], workspace_root: str = "") -> dict[str, Any]:
    before = hook_settings(workspace_root)
    by_key = {entry["key"]: entry for entry in before["hooks"]}
    patches: dict[str, dict[str, Any]] = {}
    for update in updates:
        key = update["key"]
        if key not in by_key:
            raise ValueError("Hook configuration changed; reload hooks before updating it.")
        entry = by_key[key]
        if entry["is_managed"]:
            raise ValueError("Managed hooks are always enabled.")
        patch: dict[str, Any] = {}
        if update.get("enabled") is not None:
            patch["enabled"] = update["enabled"]
        if update.get("trusted_hash") is not None:
            if update["trusted_hash"] != entry["current_hash"]:
                raise ValueError("Hook changed since the trust request; reload hooks before trusting it.")
            patch["trusted_hash"] = update["trusted_hash"]
        patches[key] = patch

    result = await run_config_change_hook(source="user_settings", file_path=str(SETTINGS_FILE))
    raise_if_config_change_blocked(result, source="user_settings", file_path=str(SETTINGS_FILE))

    def apply(settings: dict[str, Any]) -> None:
        hooks = settings.setdefault("hooks", {})
        states = hooks.setdefault("state", {})
        for key, patch in patches.items():
            states[key] = {**states.get(key, {}), **patch}

    _update_settings_json(apply)
    after = hook_settings(workspace_root)
    for entry in after["hooks"]:
        if entry["key"] in patches and any(entry[key] != value for key, value in patches[entry["key"]].items()):
            raise ValueError("Another config layer overrides these hook settings.")
    return after
