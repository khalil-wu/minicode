"""Preview server agent tool — start, stop, verify, detect, and query dev server status."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import (
    TOOL_SIDE_EFFECT_EXTERNAL,
    TOOL_SIDE_EFFECT_NONE,
    BaseTool,
    PermissionLevel,
    ToolResult,
    ToolSchema,
)
from backend.tools.contracts import ToolSpec


class PreviewServerTool(BaseTool):
    name = "preview_server"
    result_kind = "preview"
    activity_kind = "genericTool"
    display_label = "Preview server"
    description = (
        "Manage a live preview dev server. Actions: "
        "'start' launches the configured dev server and returns its process state; "
        "'stop' terminates only a preview server owned by the current session and conversation; "
        "'verify' checks if a URL is responding; "
        "'detect' scans common ports for running servers; "
        "'status' returns current preview process state. "
        "For a standalone HTML file, use "
        "preview_server(action='start', path='snake.html')."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    open_world = True
    workspace_path_fields = ("path",)
    should_defer = True
    search_hint = "preview dev server localhost browser verify screenshot frontend visual"

    def __init__(self, workspace_root: str | None = None) -> None:
        self._workspace_root = workspace_root

    def is_read_only(self, args: dict[str, Any] | None = None) -> bool:
        action = str((args or {}).get("action") or "").strip().lower()
        return action in {"verify", "detect", "status"}

    def get_side_effect_kind(self, args: dict[str, Any] | None = None) -> str:
        return TOOL_SIDE_EFFECT_NONE if self.is_read_only(args) else TOOL_SIDE_EFFECT_EXTERNAL

    def is_idempotent(self, args: dict[str, Any] | None = None) -> bool:
        action = str((args or {}).get("action") or "").strip().lower()
        return action in {"verify", "detect", "status", "stop"}

    def check_permission(self, args: dict[str, Any] | None = None, context=None) -> PermissionLevel | None:
        action = str((args or {}).get("action") or "").strip().lower()
        # Starting a server opens a long-lived local network listener. All read
        # actions and stopping the exact owned preview are safe automatic calls.
        return PermissionLevel.CONFIRM if action == "start" else PermissionLevel.AUTO

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start", "stop", "verify", "detect", "status"],
                        "description": "The action to perform.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Server name (for start/stop). Uses first config if omitted.",
                    },
                    "url": {
                        "type": "string",
                        "description": "URL to verify (for 'verify' action).",
                    },
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative HTML file to serve for a standalone static preview.",
                    },
                    "timeout": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": (
                            "Optional HTTP request timeout in seconds for start/verify. "
                            "When omitted, start returns immediately and verify uses no adapter timeout."
                        ),
                    },
                },
                "required": ["action"],
            },
        )

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="preview.manage",
            toolset="default",
            exposure="deferred",
            required_args=("action",),
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        action = args.get("action", "").strip()
        if not action:
            return self._error_result("Missing required parameter: action")

        dispatch = {
            "start": self._start,
            "stop": self._stop,
            "verify": self._verify,
            "detect": self._detect,
            "status": self._status,
        }
        handler = dispatch.get(action)
        if handler is None:
            return self._error_result(
                f"Unknown action '{action}'. Must be one of: start, stop, verify, detect, status"
            )
        return await handler(args, context)

    @staticmethod
    def _owner(context: ToolExecutionContext | None) -> tuple[str, str]:
        if context is None:
            return "", ""
        return str(context.session_id or ""), str(context.conversation_id or "")

    def _workspace(self, context: ToolExecutionContext | None) -> str | None:
        """Use the turn owner workspace, matching the tool execution context.

        The registry-level root is only a legacy fallback for callers that do
        not provide a turn context.  A live turn (including a child turn) must
        never start or inspect a preview in whichever workspace happened to
        construct the shared tool registry.
        """
        if context is not None and getattr(context, "workspace_root", None):
            return str(context.workspace_root)
        return self._workspace_root

    async def _start(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        from backend.preview.launcher import mark_preview_ready, start_preview_launch, start_static_preview
        from backend.preview.verifier import wait_until_ready

        name = args.get("name")
        path = str(args.get("path") or "").strip()
        raw_timeout = args.get("timeout")
        timeout = float(raw_timeout) if raw_timeout is not None else None
        session_id, conversation_id = self._owner(context)
        workspace_root = self._workspace(context)
        if workspace_root is None:
            return self._error_result(
                "Starting a preview requires an open workspace."
            )
        try:
            proc = (
                await start_static_preview(
                    workspace_root,
                    path,
                    session_id=session_id,
                    conversation_id=conversation_id,
                    sandbox_policy=(context.sandbox_policy if context is not None else None),
                )
                if path
                else await start_preview_launch(
                    workspace_root,
                    name=name,
                    session_id=session_id,
                    conversation_id=conversation_id,
                    sandbox_policy=(context.sandbox_policy if context is not None else None),
                )
            )
        except RuntimeError as exc:
            if str(exc) == "No preview launch configuration found":
                return self._error_result(
                    "No preview launch configuration found. For a standalone HTML file, "
                    "call preview_server(action='start', path='<workspace-relative file>.html')."
                )
            return self._error_result(str(exc))

        verification = None
        if timeout is not None and proc.effective_url:
            verification = await wait_until_ready(proc.effective_url, timeout=timeout)
            if verification.ok:
                await mark_preview_ready(proc)
        status = "ready" if proc.status == "ready" else "starting"
        payload = {
            "status": status,
            "url": proc.effective_url,
            "port": proc.effective_port,
            "pid": proc.process.pid,
            **({"verification": verification.to_dict()} if verification is not None else {}),
        }
        return self._success_result(
            json.dumps(payload, ensure_ascii=False),
            display_summary=(
                f"预览服务已就绪：{proc.effective_url}"
                if status == "ready"
                else f"预览服务启动中：{proc.effective_url}"
            ),
        )

    async def _stop(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        from backend.preview.launcher import stop_preview_launch

        name = args.get("name")
        session_id, conversation_id = self._owner(context)
        try:
            stopped = await stop_preview_launch(
                name,
                session_id=session_id,
                conversation_id=conversation_id,
                workspace_root=self._workspace(context),
            )
        except RuntimeError as exc:
            # The preview tree's exit could not be proven, so the server may
            # still be serving and writing. Report the unfinished stop.
            return self._error_result(str(exc))
        if not stopped:
            return self._success_result("No matching preview server was running.")
        names = [p.config.name for p in stopped]
        return self._success_result(f"Stopped preview server(s): {', '.join(names)}")

    async def _verify(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        from backend.permissions.network import assess_network_url
        from backend.preview.launcher import preview_url_is_owned
        from backend.preview.verifier import verify_preview_url

        url = args.get("url", "").strip()
        session_id, conversation_id = self._owner(context)
        if not url:
            from backend.preview.launcher import running_preview_processes
            procs = running_preview_processes(
                session_id=session_id,
                conversation_id=conversation_id,
                workspace_root=self._workspace(context),
            )
            if procs:
                url = procs[0].effective_url
            else:
                return self._error_result("No URL provided and no running preview server.")

        assessment = await asyncio.to_thread(assess_network_url, url)
        if not assessment.allowed and not preview_url_is_owned(
            url,
            session_id=session_id,
            conversation_id=conversation_id,
            workspace_root=self._workspace(context),
        ):
            return self._error_result(
                "Preview verification of a local, private, credential-bearing, or "
                "unresolved target is allowed only for a preview owned by this "
                f"conversation. {assessment.reason}"
            )

        raw_timeout = args.get("timeout")
        timeout = float(raw_timeout) if raw_timeout is not None else None
        result = await verify_preview_url(url, timeout=timeout)
        return self._success_result(json.dumps(result.to_dict(), ensure_ascii=False))

    async def _detect(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        from backend.preview.detector import detect_dev_servers

        servers = await detect_dev_servers()
        if not servers:
            return self._success_result("No dev servers detected on common ports.")
        data = [s.to_dict() for s in servers]
        return self._success_result(json.dumps(data, ensure_ascii=False))

    async def _status(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        from backend.preview.launcher import running_preview_processes

        session_id, conversation_id = self._owner(context)
        procs = running_preview_processes(
            session_id=session_id,
            conversation_id=conversation_id,
            workspace_root=self._workspace(context),
        )
        if not procs:
            return self._success_result("No preview servers currently running.")
        data = [p.to_dict() for p in procs]
        return self._success_result(json.dumps(data, ensure_ascii=False))
