from __future__ import annotations

from typing import Any

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.untrusted import wrap_untrusted_content


class ReadTerminalTool(BaseTool):
    """Read a bounded snapshot of an existing desktop terminal session."""

    name = "read_terminal"
    description = (
        "Read recent output from an existing terminal session. Use this to inspect "
        "dev server, build, test, or shell status that is already visible in the terminal panel."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    max_result_chars = None

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Terminal session id. Omit to read the most recent terminal session.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum recent output characters to return. Default 20000.",
                    },
                },
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        manager = getattr(context, "terminal_manager", None) if context else None
        if manager is None:
            return self._error_result("No terminal manager is available in this session.")

        conv_id = getattr(context, "conversation_id", "") or ""
        session_id = str(args.get("session_id") or "").strip()
        if not session_id:
            try:
                sessions = manager.list_sessions(conversation_id=conv_id)
            except TypeError:
                sessions = manager.list_sessions()
            session_id = sessions[-1].session_id if sessions else ""
        if not session_id:
            return self._error_result("No terminal session is available for this conversation.")

        try:
            max_chars = int(args.get("max_chars") or 20_000)
        except (TypeError, ValueError):
            max_chars = 20_000
        snapshot = manager.snapshot(session_id, max_chars=max_chars)
        if snapshot is None:
            return self._error_result(f"Terminal session '{session_id}' not found.")

        status = "running" if snapshot.get("is_alive") else "exited"
        header = (
            f"Terminal {session_id} ({status})\n"
            f"cwd: {snapshot.get('cwd') or ''}\n"
            f"shell: {snapshot.get('shell') or ''}\n"
        )
        if snapshot.get("truncated"):
            header += f"[showing last {snapshot.get('output_chars', 0)} of {snapshot.get('total_output_chars', 0)} chars]\n"
        output = str(snapshot.get("output") or "")
        if not output:
            body = "<no recent terminal output>"
        else:
            # Terminal output is untrusted text (a dev server / test / command
            # can print attacker-controlled bytes). Wrap it so the model treats
            # it as data, not instructions — same contract as web_fetch.
            body = wrap_untrusted_content(output, "read_terminal")
        return self._success_result(f"{header}\n{body}")
