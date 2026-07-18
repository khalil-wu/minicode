"""Preview server agent tool — start, stop, verify, detect, and query dev server status."""
from __future__ import annotations

import json
from typing import Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec


class PreviewServerTool(BaseTool):
    name = "preview_server"
    description = (
        "Manage a live preview dev server. Actions: "
        "'start' launches the configured dev server and waits until ready; "
        "'stop' terminates a running server; "
        "'verify' checks if a URL is responding; "
        "'detect' scans common ports for running servers; "
        "'status' returns current preview process state. "
        "Example: preview_server(action='start')"
    )
    permission = PermissionLevel.CONFIRM
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
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds for start/verify (default 30).",
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
            arg_roles={"action": "control", "url": "latest_url"},
            empty_args_policy="block",
        )

    async def execute(self, args: dict[str, Any], **kwargs: Any) -> ToolResult:
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
        return await handler(args)

    async def _start(self, args: dict[str, Any]) -> ToolResult:
        from backend.preview.launcher import start_preview_launch
        from backend.preview.verifier import wait_until_ready

        name = args.get("name")
        timeout = args.get("timeout", 30.0)
        try:
            proc = await start_preview_launch(self._workspace_root, name=name)
        except RuntimeError as exc:
            if str(exc) == "No preview launch configuration found":
                return ToolResult(
                    content=json.dumps(
                        {
                            "status": "skipped",
                            "reason": "no_launch_config",
                            "message": "No preview launch configuration found",
                        },
                        ensure_ascii=False,
                    ),
                    is_error=False,
                    display_summary="No preview launch configuration found",
                    result_kind="preview",
                    limitation="no preview launch configuration",
                    status="success",
                )
            return self._error_result(str(exc))

        verification = await wait_until_ready(proc.effective_url, timeout=timeout)
        status = "ready" if verification.ok else "timeout"
        return self._success_result(
            json.dumps({
                "status": status,
                "url": proc.effective_url,
                "port": proc.effective_port,
                "pid": proc.process.pid,
                "verification": verification.to_dict(),
            }, ensure_ascii=False)
        )

    async def _stop(self, args: dict[str, Any]) -> ToolResult:
        from backend.preview.launcher import stop_preview_launch

        name = args.get("name")
        stopped = await stop_preview_launch(name)
        if not stopped:
            return self._success_result("No matching preview server was running.")
        names = [p.config.name for p in stopped]
        return self._success_result(f"Stopped preview server(s): {', '.join(names)}")

    async def _verify(self, args: dict[str, Any]) -> ToolResult:
        from backend.preview.verifier import verify_preview_url

        url = args.get("url", "").strip()
        if not url:
            from backend.preview.launcher import running_preview_processes
            procs = running_preview_processes()
            if procs:
                url = procs[0].effective_url
            else:
                return self._error_result("No URL provided and no running preview server.")

        timeout = args.get("timeout", 8.0)
        result = await verify_preview_url(url, timeout=timeout)
        return self._success_result(json.dumps(result.to_dict(), ensure_ascii=False))

    async def _detect(self, args: dict[str, Any]) -> ToolResult:
        from backend.preview.detector import detect_dev_servers

        servers = await detect_dev_servers()
        if not servers:
            return self._success_result("No dev servers detected on common ports.")
        data = [s.to_dict() for s in servers]
        return self._success_result(json.dumps(data, ensure_ascii=False))

    async def _status(self, args: dict[str, Any]) -> ToolResult:
        from backend.preview.launcher import running_preview_processes

        procs = running_preview_processes()
        if not procs:
            return self._success_result("No preview servers currently running.")
        data = [p.to_dict() for p in procs]
        return self._success_result(json.dumps(data, ensure_ascii=False))
