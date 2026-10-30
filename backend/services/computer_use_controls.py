"""Computer Use rows backed by actual local plugin inventory and catalog."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.config import load_config_layer_stack
from backend.config_layers import ConfigLayerSource
from backend.plugins.manager import (
    MarketplaceRegistry, PluginManager, _assert_marketplace_plugin_installable,
)
from backend.plugins.materializer import MaterializationError
from backend.plugins.policy import _plugin_policy_from_stack


ANY_APP_PLUGINS = ("computer-use@openai-bundled", "computer-use@minicode-bundled")


def computer_use_plugin_controls(workspace_root, selected_plugin_id=None):
    # Desktop Hr/qr uses this complete identity, not any similarly named plugin.
    stack = load_config_layer_stack(cwd=Path(workspace_root) if workspace_root else None)
    inventory = PluginManager(config_stack=stack).snapshot().to_dict()
    if not inventory["plugins_enabled"]:
        return [], None, None, stack
    policy = _plugin_policy_from_stack(stack)
    marketplaces = MarketplaceRegistry(config_stack=stack, workspace_root=Path(workspace_root) if workspace_root else None,
        include_bundled=True).list(policy=policy)
    candidates = [(identity,
        next((item for item in inventory["plugins"] if item["id"] == identity), None),
        next((record for record in marketplaces if record["name"] == identity.split("@", 1)[1]
            and record.get("status") == "ready"), None)) for identity in ANY_APP_PLUGINS]
    selected = next((item for item in candidates if item[0] == selected_plugin_id and item[1] is not None), None) if selected_plugin_id else next((item for item in candidates if item[1] is not None), None)
    if selected is None and selected_plugin_id is None:
        selected = next((item for item in candidates if item[2] is not None
            and any(plugin["id"] == item[0] for plugin in item[2]["plugins"])), None)
    if selected is None:
        return [], None, None, stack
    plugin_id, installed, marketplace = selected
    catalog = next((item for item in marketplace["plugins"] if item["id"] == plugin_id), None) if marketplace else None
    if installed is None and catalog is None:
        return [], None, None, stack
    errors = list(installed["load_errors"]) if installed else []
    installable = False
    if catalog is not None:
        if catalog.get("load_error"):
            errors.append(catalog["load_error"])
        else:
            try:
                _assert_marketplace_plugin_installable(catalog, product="MINICODE")
                installable = True
            except MaterializationError as error:
                errors.append(str(error))
    metadata = installed if installed is not None else catalog.get("interface", {})
    mutable = inventory["feature_enabled"] and policy.managed_state(plugin_id) is None
    enabled_origin = stack.origins().get(f"plugins.{plugin_id}.enabled")
    writable_enabled = enabled_origin is None or enabled_origin.source.precedence <= ConfigLayerSource("user").precedence
    control = {
        "kind": "plugin", "id": "any-app", "plugin_id": plugin_id,
        "plugin_name": "computer-use", "marketplace": plugin_id.split("@", 1)[1],
        "display_name": metadata.get("displayName") or "computer-use",
        "installed": installed is not None, "enabled": bool(installed and installed["enabled"]),
        "can_install": installed is None and installable and mutable,
        "can_toggle": bool(installed and installed["trust"]["allowed"] and mutable and writable_enabled),
        "icon_url": metadata.get("iconUrl") or None, "load_errors": errors,
        "plugin_path": installed["path"] if installed and installed["iconVariant"] else None,
        "icon_variant": installed["iconVariant"] if installed and installed["iconVariant"] else None,
        "icon_variants": installed.get("iconVariants", []) if installed else [],
    }
    record = [control, installed, catalog, marketplace["source"] if marketplace is not None else None]
    revision = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    return [control], revision, installed, stack
