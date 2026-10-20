"""Local Claude Code / Cursor formats from Codex external-agent-migration.

Detection reads metadata and files. It never evaluates imported instructions,
connects MCP servers, runs hooks, or opens provider accounts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any
import yaml


PROVIDERS = {"claude": {"name": "Claude Code", "directory": ".claude"},
             "cursor": {"name": "Cursor", "directory": ".cursor"}}
ITEM_TYPES = ("sessions", "projects", "instructions", "settings", "skills", "plugins", "mcp", "agents", "hooks", "commands", "memory")
CURSOR_HOOK_EVENTS = {"preToolUse": "pre_tool_use", "postToolUse": "post_tool_use", "preCompact": "pre_compact",
                      "postCompact": "post_compact", "sessionStart": "session_start", "subagentStart": "subagent_start",
                      "subagentStop": "subagent_stop", "beforeSubmitPrompt": "user_prompt_submit", "stop": "stop"}


@dataclass
class ImportItem:
    provider_id: str
    item_type: str
    title: str
    source: Path
    target: Path
    workspace_root: str = ""
    key: str = ""
    value: Any = None
    supported: bool = True
    reason: str = ""
    id: str = field(init=False)

    def __post_init__(self):
        self.id = hashlib.sha256(f"{self.provider_id}\0{self.item_type}\0{self.source}\0{self.key}".encode()).hexdigest()[:24]

    def public(self) -> dict:
        return {"id": self.id, "provider_id": self.provider_id, "item_type": self.item_type, "title": self.title,
                "source_path": str(self.source), "target_path": str(self.target), "workspace_root": self.workspace_root,
                "supported": self.supported, "reason": self.reason}


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: JSON root must be an object")
    return value


def merge_objects(target: dict, source: dict) -> dict:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_objects(target[key], value)
        else:
            target[key] = value
    return target


def source_settings(provider: str, directory: Path, home_scope: bool) -> tuple[Path, dict]:
    filename = "settings.json" if provider == "claude" else "cli-config.json" if home_scope else "cli.json"
    path = directory / filename
    settings = read_json(path)
    if provider == "claude":
        local = directory / "settings.local.json"
        if local.is_file():
            merge_objects(settings, read_json(local))
            if not path.is_file():
                path = local
    return path, settings


def migrated_settings(provider: str, settings: dict) -> dict:
    result: dict = {}
    environment = {key: str(value).lower() if isinstance(value, bool) else str(value)
                   for key, value in (settings.get("env", {}) if isinstance(settings.get("env", {}), dict) else {}).items()
                   if isinstance(value, (str, int, float, bool))}
    if environment:
        result["shell_environment_policy"] = {"inherit": "core", "set": environment}
    if provider == "claude" and isinstance(settings.get("sandbox"), dict) and settings["sandbox"].get("enabled") is True:
        result["sandbox_mode"] = "workspace-write"
    return result


def migrated_agent(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8-sig")
    document = re.match(r"^---\s*\n(.*?)\n---\s*\n?", raw, re.S)
    if document is None:
        raise ValueError("Agent import requires YAML name and description metadata.")
    try:
        fields = yaml.safe_load(document[1])
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid Agent metadata: {error.problem}") from error
    if not isinstance(fields, dict) or any(not isinstance(fields.get(key), str) or not fields[key].strip() for key in ("name", "description")):
        raise ValueError("Agent import requires non-empty name and description metadata.")
    effort = fields.get("effort", "")
    effort = "xhigh" if effort == "max" else effort if effort in {"none", "minimal", "low", "medium", "high", "xhigh"} else ""
    return {"name": fields["name"], "description": fields["description"], "prompt": raw[document.end():].strip() or "No subagent instructions were found.",
            "effort": "off" if effort == "none" else effort,
            "permission_mode": {"acceptEdits": "auto", "readOnly": "plan"}.get(fields.get("permissionMode"), "")}


def migrated_mcp(provider: str, directory: Path, scope_root: Path, home: Path, settings: dict) -> list[tuple[Path, str, dict]]:
    candidates = [(directory / "mcp.json", read_json(directory / "mcp.json"))] if provider == "cursor" else [
        (scope_root / ".mcp.json", read_json(scope_root / ".mcp.json")),
        (scope_root / ".claude.json", read_json(scope_root / ".claude.json")),
    ]
    if provider == "claude" and scope_root != home:
        document = read_json(home / ".claude.json")
        candidates.append((home / ".claude.json", {"projects": document.get("projects", {})}))
    result: dict[str, tuple[Path, str, dict]] = {}
    for path, document in candidates:
        servers = dict(document.get("mcpServers", {}))
        for root, project in document.get("projects", {}).items():
            if Path(root).expanduser().resolve() == scope_root:
                servers.update(project.get("mcpServers", {}))
        for name, raw in servers.items():
            if not isinstance(raw, dict):
                raise ValueError(f"{path}: MCP server {name!r} must be an object")
            enabled_names = settings.get("enabledMcpjsonServers", [])
            if (raw.get("enabled") is False or raw.get("disabled") is True
                    or name in settings.get("disabledMcpjsonServers", [])
                    or enabled_names and name not in enabled_names):
                continue
            value = mcp_server_value(raw)
            result[name] = (path, name, value)
    return list(result.values())


def mcp_server_value(raw: dict) -> dict:
    transport = raw.get("type", "stdio" if raw.get("command") else "http")
    if transport in {"http", "streamable_http", "streamableHttp"}:
        transport = "streamable-http"
    value = {key: raw[key] for key in ("command", "args", "env", "url", "headers", "cwd", "enabled", "auto_start") if key in raw}
    if raw.get("disabled") is True:
        value["enabled"] = False
    value["transport"] = transport
    return value


def migrated_hooks(provider: str, directory: Path, settings: dict) -> tuple[Path, dict, list[str]]:
    from backend.hooks.models import HookEvent
    path = directory / ("settings.json" if provider == "claude" else "hooks.json")
    payload = settings if provider == "claude" else read_json(path)
    if payload.get("disableAllHooks") is True:
        return path, {}, []
    converted: dict[str, list] = {}
    unsupported = []
    valid_events = {event.value for event in HookEvent}
    for source_event, groups in payload.get("hooks", {}).items():
        event = re.sub(r"(?<!^)(?=[A-Z])", "_", source_event).lower() if provider == "claude" else CURSOR_HOOK_EVENTS.get(source_event)
        if event not in valid_events:
            unsupported.append(source_event)
            continue
        if not isinstance(groups, list):
            unsupported.append(source_event)
            continue
        if provider == "cursor":
            groups = [{"matcher": handler.get("matcher"), "hooks": [handler]} for handler in groups if isinstance(handler, dict)]
        for group in groups:
            if not isinstance(group, dict):
                unsupported.append(source_event)
                continue
            handlers = []
            for raw in group.get("hooks", []):
                if not isinstance(raw, dict) or raw.get("type", "command") != "command" or not isinstance(raw.get("command"), str):
                    unsupported.append(source_event)
                    continue
                handler = {"type": "command", "command": raw["command"].replace("CLAUDE_PROJECT_DIR", "MINICODE_PROJECT_DIR").replace("CURSOR_PROJECT_DIR", "MINICODE_PROJECT_DIR")}
                if raw.get("timeout", raw.get("timeoutSec")) is not None:
                    handler["timeout"] = raw.get("timeout", raw.get("timeoutSec"))
                if raw.get("statusMessage"):
                    handler["statusMessage"] = raw["statusMessage"]
                handlers.append(handler)
            if handlers:
                converted.setdefault(event, []).append({"matcher": group.get("matcher"), "hooks": handlers})
    return path, converted, sorted(set(unsupported))


def _content_text(content: Any) -> tuple[str, bool]:
    if isinstance(content, str):
        return content, False
    if content is not None and not isinstance(content, list):
        raise ValueError("Unsupported transcript message content shape.")
    parts = []
    only_results = bool(content)
    for block in content or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "thinking":
            continue
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "tool_use":
            parts.append(f"[external_agent_tool_call: {block.get('name', 'unknown')}]\ninput: {json.dumps(block.get('input', {}), ensure_ascii=False)[:2000]}\n[/external_agent_tool_call]")
        elif kind == "tool_result":
            value, _ = _content_text(block.get("content"))
            label = ": error" if block.get("is_error") else ""
            parts.append(f"[external_agent_tool_result{label}]\n{value[:4000]}\n[/external_agent_tool_result]")
        else:
            parts.append(f"[external unsupported block: {kind}]")
        if kind != "tool_result":
            only_results = False
    return "\n\n".join(part for part in parts if part.strip()), only_results


def read_session(path: Path, provider: str, fallback_cwd: str = "", *, summary_only: bool = False) -> dict:
    messages = []
    count = 0
    fallback_title = ""
    cwd = ""
    custom_title = ""
    ai_title = ""
    latest = 0.0
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            if not cwd and isinstance(record.get("cwd"), str):
                cwd = record["cwd"]
            if record.get("type") == "custom-title":
                custom_title = record.get("customTitle") or custom_title
            if record.get("type") == "ai-title":
                ai_title = record.get("aiTitle") or ai_title
            role = record.get("type") if provider == "claude" else record.get("role")
            if role not in {"user", "assistant"} or record.get("isMeta") or record.get("isSidechain"):
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            text, only_results = _content_text(message.get("content"))
            if not text.strip():
                continue
            if only_results:
                role = "assistant"
            if role == "user":
                wrapped = re.fullmatch(r"\s*(?:(?:<timestamp>.*?</timestamp>|<cursor_commands>.*?</cursor_commands>)\s*)*<user_query>(.*?)</user_query>\s*", text, re.S)
                if wrapped:
                    text = wrapped[1].strip()
            stamp = record.get("timestamp")
            seconds = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() if isinstance(stamp, str) else float(record.get("timestamp_ms", path.stat().st_mtime * 1000 if provider == "cursor" else 0)) / 1000
            latest = max(latest, seconds)
            count += 1
            if role == "user" and not fallback_title:
                fallback_title = text.strip().splitlines()[0][:100]
            if not summary_only:
                messages.append({"id": f"imported_{count - 1}", "role": role, "content": text, "timestamp": seconds * 1000})
    if not count:
        raise ValueError("No supported user or assistant messages were found in this transcript.")
    if latest <= 0:
        raise ValueError("The source transcript does not contain dated messages.")
    title = custom_title or ai_title
    if not title:
        title = fallback_title or "Imported chat"
    return {"title": title, "workspace_root": cwd or fallback_cwd, "messages": messages, "latest_timestamp": latest}
