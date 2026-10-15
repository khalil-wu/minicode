"""Immutable action JSON and exact request identity supplied by the issuing host."""
from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.permissions.grants import permission_profile_payload, current_granted_permissions
from backend.tools.command_support import command_shell_argv, command_login_allowed
from backend.tools.apply_patch_parser import patch_target_paths
from backend.agent.final_tool_request import _canonical_json_value


@dataclass(frozen=True)
class GuardianAction:
    call_id: str
    turn_id: str
    request_digest: str
    policy_identity: str
    environment_id: str | None
    json_payload: str
    tool_descriptions: str | None = None
    approval_id: str | None = None

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.json_payload)

    @property
    def kind(self) -> str:
        return self.payload["tool"]

    def assessment_action(self) -> dict[str, Any]:
        payload = self.payload
        kind = self.kind
        if kind == "exec_command":
            return {"type": "command", "source": "unified_exec", "command": shlex.join(payload["command"]), "cwd": payload["cwd"]}
        if kind == "write_stdin":
            return {"type": "write_stdin", "approval_id": self.approval_id, "process_id": str(payload["session_id"]), "stdin": payload["chars"], "cwd": payload["cwd"]}
        if kind == "apply_patch":
            return {"type": kind, "cwd": payload["cwd"], "files": payload["files"]}
        if kind == "request_permissions":
            return {"type": kind, "reason": payload.get("reason"), "permissions": payload["permissions"]}
        if kind == "network_access":
            return {"type": kind, **{key: payload[key] for key in ("target", "host", "protocol", "port")}}
        if kind == "mcp_tool_call":
            return {"type": kind, "server": payload["server"], "tool_name": payload["tool_name"], **{key: payload.get(key) for key in ("connector_id", "connector_name", "tool_title")}}
        raise ValueError(f"Unknown Source Guardian assessment action: {kind}")

    @classmethod
    def freeze(cls, payload: dict[str, Any], *, call_id: str, turn_id: str, request_digest: str,
        policy_identity: str, environment_id: str | None, tool_descriptions: str | None = None, approval_id: str | None = None) -> GuardianAction:
        payload = dict(payload)
        if environment_id is not None:
            payload["environment_id"] = environment_id
        if payload["tool"] == "mcp_tool_call":
            payload.pop("tool_description", None)
            payload.pop("connector_description", None)
        return cls(call_id, turn_id, request_digest, policy_identity, environment_id,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), tool_descriptions, approval_id)

    @classmethod
    def from_tool_request(cls, request, context, *, command_argv: tuple[str, ...] | None = None, mcp_metadata: dict[str, Any] | None = None):
        args = _canonical_json_value(request.arguments)
        workspace = Path(context.workspace_root)
        raw_cwd = args.get("cwd") or context.metadata.get("cwd") or workspace
        cwd = Path(raw_cwd).expanduser()
        cwd = cwd if cwd.is_absolute() else workspace / cwd
        cwd = Path(os.path.abspath(cwd))
        name = request.tool_name
        if name == "run_command":
            argv = command_argv or tuple(command_shell_argv(args["command"], shell=args.get("shell"), login=args.get("login"), allow_login_shell=command_login_allowed(workspace)))
            payload: dict[str, Any] = {"tool": "exec_command", "command": list(argv), "cwd": str(cwd),
                "sandbox_permissions": "require_escalated" if args.get("with_escalated_permissions") is True else "use_default", "tty": bool(args.get("tty", False))}
            grants = current_granted_permissions(context)
            if not grants.is_empty:
                payload["additional_permissions"] = permission_profile_payload(grants)
            if args.get("justification") is not None:
                payload["justification"] = args["justification"]
        elif name == "apply_patch":
            payload = {"tool": "apply_patch", "cwd": str(cwd), "files": [str(Path(path) if Path(path).is_absolute() else cwd / path) for path in patch_target_paths(args["patch"])], "patch": args["patch"]}
        elif name == "request_permissions":
            payload = {"tool": "request_permissions", "turn_id": context.turn_id, "permissions": args["permissions"]}
            if args.get("reason") is not None:
                payload["reason"] = args["reason"]
        elif name.startswith("mcp__") and mcp_metadata is not None:
            payload = {"tool": "mcp_tool_call", **mcp_metadata, "arguments": dict(args)}
        else:
            raise ValueError(f"No Source Guardian action contract for {name}")
        return cls.freeze(payload, call_id=request.tool_call_id, turn_id=context.turn_id,
            request_digest=request.digest, policy_identity=request.policy_identity,
            environment_id=None if name.startswith("mcp__") else context.environment_id)

    @classmethod
    def from_terminal_input(cls, terminal_action, context):
        payload = {"tool": "write_stdin", "session_id": terminal_action.process_id,
            "chars": terminal_action.input, "cwd": terminal_action.cwd, "tty": terminal_action.tty,
            "sandbox_permissions": terminal_action.sandbox_permissions}
        if terminal_action.additional_permissions is not None:
            payload["additional_permissions"] = permission_profile_payload(terminal_action.additional_permissions)
        return cls.freeze(payload, call_id=terminal_action.id, approval_id=terminal_action.approval_id, turn_id=context.turn_id,
            request_digest=context.authorization_digest, policy_identity=context.authorization_policy_identity,
            environment_id=terminal_action.environment_id)
