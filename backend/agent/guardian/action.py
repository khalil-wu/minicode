"""Immutable action JSON and exact request identity supplied by the issuing host."""
from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.permissions.grants import normalize_permission_profile, permission_profile_payload
from backend.tools.apply_patch_parser import patch_target_paths
from backend.agent.final_tool_request import _canonical_json_value
from backend.workspace.executor_paths import ExecutorPath


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
    def from_tool_request(cls, request, context, *, mcp_metadata: dict[str, Any] | None = None):
        args = _canonical_json_value(request.arguments)
        workspace = Path(context.workspace_root)
        raw_cwd = args.get("cwd") or context.metadata.get("cwd") or workspace
        cwd = Path(raw_cwd).expanduser()
        cwd = cwd if cwd.is_absolute() else workspace / cwd
        cwd = Path(os.path.abspath(cwd))
        name = request.tool_name
        if name == "run_command":
            # Guardian reviews the frozen executable command: the executor's
            # argv, cwd and captured sandbox permissions. Source's
            # approval_request.rs keeps executor cwd/argv the same way; a
            # controller-side reconstruction reviewed a different action.
            if request.exec_policy is None:
                raise ValueError("A run_command Guardian action requires the captured execution policy")
            command = request.exec_policy.command
            executor_cwd = command.cwd if isinstance(command.cwd, ExecutorPath) else None
            payload: dict[str, Any] = {"tool": "exec_command", "command": list(command.policy_argv),
                "cwd": executor_cwd.native if executor_cwd is not None else str(command.cwd),
                "sandbox_permissions": command.sandbox_permissions, "tty": bool(args.get("tty", False))}
            requested = args.get("additional_permissions")
            if requested is not None:
                if executor_cwd is not None:
                    roots = tuple(ExecutorPath.parse(root, platform_os=executor_cwd.platform_os)
                        for root in context.permission.environment_scope.workspace_root_uris)
                else:
                    roots = (workspace.resolve(),)
                payload["additional_permissions"] = permission_profile_payload(
                    normalize_permission_profile(requested, cwd=command.cwd, workspace_roots=roots))
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
