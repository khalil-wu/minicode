"""Preview server agent tool — start, stop, verify, detect, and query dev server status."""
from __future__ import annotations

import json
from typing import Any

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec


class PreviewServerTool(BaseTool):
    name = "preview_server"
    result_kind = "preview"
    activity_kind = "genericTool"
    display_label = "Preview server"
    description = (
        "Manage a live preview dev server. Actions: "
        "'start' launches the configured dev server and returns its process state; "
        "'stop' terminates a running server; "
        "'verify' checks if a URL is responding; "
        "'detect' scans common ports for running servers; "
        "'status' returns current preview process state. "
        "For a standalone HTML file, use "
        "preview_server(action='start', path='snake.html')."
    )
    permission = PermissionLevel.CONFIRM
    workspace_path_fields = ("path",)
    should_defer = True
    search_hint = "preview dev server localhost browser verify screenshot frontend visual"

    def __init__(self, workspace_root: str | None = None) -> None:
        self._workspace_root = workspace_root

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

    async def _start(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        from backend.preview.launcher import mark_preview_ready, start_preview_launch, start_static_preview
        from backend.preview.verifier import wait_until_ready

        name = args.get("name")
        path = str(args.get("path") or "").strip()
        raw_timeout = args.get("timeout")
        timeout = float(raw_timeout) if raw_timeout is not None else None
        session_id, conversation_id = self._owner(context)
        try:
            proc = (
                await start_static_preview(
                    self._workspace_root,
                    path,
                    session_id=session_id,
                    conversation_id=conversation_id,
                )
                if path
                else await start_preview_launch(
                    self._workspace_root,
                    name=name,
                    session_id=session_id,
                    conversation_id=conversation_id,
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
        return self._success_result(
            json.dumps({
                "status": status,
                "url": proc.effective_url,
                "port": proc.effective_port,
                "pid": proc.process.pid,
                **({"verification": verification.to_dict()} if verification is not None else {}),
            }, ensure_ascii=False)
        )

    async def _stop(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        from backend.preview.launcher import stop_preview_launch

        name = args.get("name")
        session_id, conversation_id = self._owner(context)
        stopped = await stop_preview_launch(
            name,
            session_id=session_id,
            conversation_id=conversation_id,
            workspace_root=self._workspace_root,
        )
        if not stopped:
            return self._success_result("No matching preview server was running.")
        names = [p.config.name for p in stopped]
        return self._success_result(f"Stopped preview server(s): {', '.join(names)}")

    async def _verify(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        from backend.preview.verifier import verify_preview_url

        url = args.get("url", "").strip()
        if not url:
            from backend.preview.launcher import running_preview_processes
            session_id, conversation_id = self._owner(context)
            procs = running_preview_processes(
                session_id=session_id,
                conversation_id=conversation_id,
                workspace_root=self._workspace_root,
            )
            if procs:
                url = procs[0].effective_url
            else:
                return self._error_result("No URL provided and no running preview server.")

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
            workspace_root=self._workspace_root,
        )
        if not procs:
            return self._success_result("No preview servers currently running.")
        data = [p.to_dict() for p in procs]
        return self._success_result(json.dumps(data, ensure_ascii=False))
