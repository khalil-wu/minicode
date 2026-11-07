"""Source plugin discovery and verified, client-completed installation requests."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from backend.plugins.manager import MarketplaceRegistry, PluginManager
from backend.plugins.policy import _plugin_policy_from_stack


# Source core-plugins/discoverable.rs. Account-dependent app-only entries are
# filtered below; MiniCode's packaged computer-use plugin has its own identity.
DEFAULT_PLUGIN_IDS = frozenset(f"{name}@openai-curated" for name in (
    "github", "notion", "slack", "gmail", "google-calendar", "google-drive", "openai-developers", "canva",
    "teams", "sharepoint", "outlook-email", "outlook-calendar", "linear", "figma")) | {
    "chrome@openai-bundled", "computer-use@openai-bundled", "computer-use@minicode-bundled"}


@dataclass(frozen=True)
class PluginInstallCandidate:
    id: str
    name: str
    plugin_name: str
    marketplace: str
    description: str
    has_skills: bool
    mcp_server_names: tuple[str, ...]
    source: dict

    def entry(self):
        return {"id": self.id, "name": self.name, "description": self.description[:240], "tool_type": "plugin",
            "has_skills": self.has_skills, "mcp_server_names": list(self.mcp_server_names), "app_connector_ids": []}


def discover_install_candidates(config_stack, workspace_root: Path | None) -> tuple[PluginInstallCandidate, ...]:
    from backend.feature_flags import load_feature_flags
    settings = config_stack.effective_config()
    features = load_feature_flags(settings, managed_requirements=config_stack.requirements.feature_requirements)
    if not all(features.enabled(name) for name in ("apps", "plugins", "tool_suggest")):
        return ()
    suggest = settings.get("tool_suggest", {})
    configured = {entry["id"] for entry in suggest.get("discoverables", ()) if entry["type"] == "plugin"}
    disabled = {entry["id"] for entry in suggest.get("disabled_tools", ()) if entry["type"] == "plugin"}
    policy = _plugin_policy_from_stack(config_stack)
    installed = {entry["id"] for entry in PluginManager(config_stack=config_stack, workspace_root=workspace_root).snapshot().plugins}
    candidates = []
    for market in MarketplaceRegistry(config_stack=config_stack, workspace_root=workspace_root, include_bundled=True).list(policy=policy):
        if market.get("status") != "ready":
            continue
        for entry in market["plugins"]:
            identity = entry["id"]
            if (identity not in configured and identity not in DEFAULT_PLUGIN_IDS or identity in installed or identity in disabled
                    or entry.get("load_error") or entry.get("installation", {}).get("available") is False
                    or entry.get("policy", {}).get("installation") == "NOT_AVAILABLE" or entry.get("app_connector_ids")
                    or entry.get("apps")):
                continue
            if policy.managed_state(identity, str(entry.get("version") or "")) is False:
                continue
            candidates.append(PluginInstallCandidate(identity, entry.get("interface", {}).get("displayName") or entry["name"],
                entry["name"], market["name"], entry.get("description", ""), bool(entry.get("skill_summaries") or entry.get("skills")),
                tuple(entry.get("mcp_server_names", ())), deepcopy(entry.get("source", {}))))
    return tuple(sorted(candidates, key=lambda entry: (entry.name, entry.id)))


def installed_plugin(candidate: PluginInstallCandidate, workspace_root):
    from backend.config import load_config_layer_stack

    stack = load_config_layer_stack(cwd=workspace_root)
    plugin = PluginManager(config_stack=stack, workspace_root=workspace_root).resolve(candidate.id, require_enabled=False)
    return stack, bool(plugin is not None and plugin.get("store_valid") is not False and not plugin.get("load_errors"))


def disable_install_suggestion(identity):
    from backend.config import _update_settings_json

    def update(settings):
        disabled = settings.setdefault("tool_suggest", {}).setdefault("disabled_tools", [])
        entry = {"type": "plugin", "id": identity}
        if entry not in disabled:
            disabled.append(entry)
    _update_settings_json(update)
