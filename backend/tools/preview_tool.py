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
        "Manage previews scoped to the current session, conversation and workspace. "
        "start launches a configured dev server or an existing workspace .html/.htm "
        "file via path; starting a listener is subject to the current permission flow. "
        "start is asynchronous: status='starting' is a valid accepted launch, including "
        "for static HTML, not failure or readiness. Do not repeatedly start/restart "
        "just because it is starting. Without timeout, start does not await HTTP "
        "readiness; verify the current owned URL separately. A URL, PID or successful "
        "tool result alone is not readiness: status='ready' may be set by a URL in "
        "process output, not an HTTP check. status returns an owner-scoped process "
        "snapshot, not a health probe; stopped/crashed entries may already be removed. "
        "verify returns JSON with ok, the HTTP status and error; ok=true means the checked "
        "HTTP response was below 500 (including 4xx), not that the intended page or "
        "app works. verify does not change process status. Read any start verification "
        "result even when status says ready, then inspect the actual browser page. "
        "Startup errors, verification ok=false, and stopped/crashed/stopping or "
        "processes awaiting cleanup must not be presented as a working preview; there "
        "is no guaranteed literal 'failed' status in tool results. stop only targets "
        "owned previews. detect scans common ports but neither establishes ownership "
        "nor authorizes browser access. Use only fresh URLs returned by the owned "
        "preview, preserving any port/token/path; never construct one or reuse it "
        "after failure, stop or restart. Ownership and host metadata do not grant "
        "permission beyond the user's main task or override network policy. For a "
        "standalone file, browser_control(action='navigate', url='index.html') can "
        "start the owned static preview directly; use preview_server for explicit "
        "lifecycle control and HTTP verification."
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
                        "description": (
                            "start accepts an asynchronous launch and may return starting, including "
                            "for static HTML; this is not failed or ready. status reads owned process "
                            "metadata without probing "
                            "HTTP; verify checks HTTP and reports success, HTTP status and errors; stop "
                            "terminates owned previews. detect finds listeners only, not authorized "
                            "previews. A successful tool envelope is not proof of a working page."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "For start, an exact launch-configuration name; omitted selects the first "
                            "configuration unless path is supplied. For stop, an owned preview name "
                            "from status; omitted stops all matching previews in the current owner "
                            "scope, not just the first. Not an arbitrary command or external server id."
                        ),
                    },
                    "url": {
                        "type": "string",
                        "description": (
                            "For verify: a public HTTP(S) URL or the exact fresh URL of an active owned "
                            "loopback preview. An owned starting process can be checked; do not require "
                            "its status to become ready first. Omitted uses the first owned preview's "
                            "current URL, "
                            "or errors if none exists. Local/private/unresolved/credential-bearing "
                            "targets remain subject to ownership and verification policy; browser "
                            "preview-origin metadata alone does not register an owned process here. "
                            "Only same-origin redirects are followed. Preserve returned port/token/path; "
                            "do not guess a URL or reuse one after stop, failure or restart."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "For start: an existing .html/.htm file inside the active workspace; prefer "
                            "a workspace-relative path. Takes precedence over name and starts an owned "
                            "static preview with an allocated port and tokenized URL. Static start may "
                            "return starting; verify its actual URL rather than treating this as failure. "
                            "Does not run an "
                            "app backend. Alternatively pass the file directly to browser_control.navigate."
                        ),
                    },
                    "timeout": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": (
                            "Optional positive seconds. start polls HTTP readiness up to this deadline "
                            "when a URL is available and returns verification; a failed check can still "
                            "leave a starting/ready process, so inspect verification.ok/error. Without "
                            "timeout, start does not wait for HTTP and a starting result is normal. "
                            "verify uses this as its HTTP request "
                            "timeout; omitted defaults to 10 seconds per request. Neither proves rendering "
                            "or app correctness, and verify does not promote process status to ready."
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
        if context is not None:
            return str(context.workspace_root) if context.workspace_root else None
        return self._workspace_root

    async def _start(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        from backend.preview.launcher import mark_preview_ready, start_preview_launch, start_static_preview
        from backend.preview.verifier import PreviewProcessChangedError, wait_until_ready

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
            try:
                verification = await wait_until_ready(proc.effective_url, timeout=timeout, process=proc)
            except PreviewProcessChangedError as exc:
                return self._error_result(str(exc))
            if verification.ok:
                await mark_preview_ready(proc)
        if proc.process.returncode is not None:
            return self._error_result(
                f"Preview process exited with code {proc.process.returncode} before startup completed."
                + ("\n" + "\n".join(proc.stderr_tail) if proc.stderr_tail else "")
            )
        if not proc.is_active:
            return self._error_result("Preview process was stopped, restarted, or is awaiting cleanup before startup completed.")
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
        from backend.preview.launcher import find_preview_process
        from backend.preview.verifier import PreviewProcessChangedError, verify_preview_url

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
        process = find_preview_process(
            url,
            session_id=session_id,
            conversation_id=conversation_id,
            workspace_root=self._workspace(context),
        )
        if not assessment.allowed and process is None:
            return self._error_result(
                "Preview verification of a local, private, credential-bearing, or "
                "unresolved target is allowed only for a preview owned by this "
                f"conversation. {assessment.reason}"
            )

        raw_timeout = args.get("timeout")
        timeout = float(raw_timeout) if raw_timeout is not None else None
        try:
            result = await verify_preview_url(url, timeout=timeout, process=process)
        except PreviewProcessChangedError as exc:
            return self._error_result(str(exc))
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
