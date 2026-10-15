"""Thread-owned Source MCP approvals, keyed by tool identity rather than arguments."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MCPApprovalKey = tuple[str, str | None, str | None, str | None, str]


@dataclass
class MCPApprovalStore:
    session_approved: set[MCPApprovalKey] = field(default_factory=set)


async def apply_mcp_approval(proxy, response: dict, owner) -> None:
    key = proxy.session_approval_key()
    if key is None or owner is None or owner.mcp_tool_approvals is None:
        return
    persistent = response.get("remember_for_future") is True
    if persistent:
        # Source logs a failed config edit and remembers this session instead.
        # It does not turn a user-approved tool into a fabricated execution.
        try:
            await persist_mcp_approval(proxy)
        except (OSError, ValueError, RuntimeError) as error:
            logger.error("Failed to persist MCP tool approval for %s: %s", proxy.policy_identity, error)
    if persistent or response.get("remember_for_session") is True:
        owner.mcp_tool_approvals.session_approved.add(key)


async def persist_mcp_approval(proxy) -> None:
    from backend.mcp.config_file import _MCP_CONFIG_WRITE_LOCK, read_mcp_config, write_mcp_config
    from backend.config_helpers import _SETTINGS_WRITE_LOCK, _load_settings_json, _write_settings_json

    target = proxy.persistent_approval_target()
    if target is None:
        raise ValueError("MCP tool has no configured permanent approval target")
    if target["kind"] == "plugin":
        with _SETTINGS_WRITE_LOCK:
            data = _load_settings_json()
            plugin = data.setdefault("plugins", {}).setdefault(target["plugin_id"], {})
            server = plugin.setdefault("mcp_servers", {}).setdefault(target["server"], {})
            server.setdefault("tools", {}).setdefault(proxy._tool_def.name, {})["approval_mode"] = "approve"
            _write_settings_json(data)
    else:
        path = Path(target["path"])
        with _MCP_CONFIG_WRITE_LOCK:
            data = json.loads(read_mcp_config(path)["content"])
            server = data["servers"][target["server"]]
            server.setdefault("tools", {}).setdefault(proxy._tool_def.name, {})["approval_mode"] = "approve"
            write_mcp_config(json.dumps(data, ensure_ascii=False), path)
    await proxy._manager.reload_config()
