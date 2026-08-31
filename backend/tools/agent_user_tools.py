from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.permissions.checker import PermissionChecker
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec


class AskUserTool(BaseTool):
    """Ask the user one concise clarification question."""

    name = "ask_user"
    # Required clarifications are part of the base interaction protocol, not a
    # capability the model should have to discover after it becomes blocked.
    should_defer = False
    search_hint = "clarify ambiguity ask user question preference decision requirement missing detail"
    result_kind = "generic"
    activity_kind = "genericTool"
    display_label = "Ask user"
    description = (
        "Ask the user a focused clarification question when a required detail cannot be inferred from context or tools. "
        "Ask at most ONE question per turn. Include enough context for the user to answer without reading the entire conversation. "
        "When the user is making a decision, provide 2-4 short options plus leave room for a custom answer."
    )
    permission = PermissionLevel.AUTO

    def model_description(self) -> str:
        return "Ask the user one clarification question."

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The concise, self-contained question to ask the user.",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional 2-4 short answer choices. The UI also allows a custom answer.",
                        "minItems": 2,
                        "maxItems": 4,
                    },
                },
                "required": ["question"],
            },
        )

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="user.ask",
            toolset="core",
            exposure="core",
            required_args=("question",),
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The concise, self-contained question to ask the user. Include relevant context so the user can answer without scrolling back.",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional short answer choices, such as ['删除', '不删除']. The UI will also allow a custom answer.",
                        "maxItems": 4,
                    },
                },
                "required": ["question"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        question = args.get("question", "")
        if not question:
            return self._error_result("Missing question argument")
        hook_mgr = (
            context.run_context.hook_manager
            if context is not None and context.run_context is not None
            else None
        )
        if hook_mgr:
            hook_result = await hook_mgr.run_elicitation(str(question), elicitation_id=self.name)
            if hook_result.blocked:
                message = hook_result.message or hook_result.feedback or "elicitation blocked by hook"
                return ToolResult(content=f"Elicitation blocked by hook: {message}", is_error=True)
        return self._success_result(f"[waiting for user answer] {question}")


class BriefTool(BaseTool):
    """Send an appropriately scoped user-facing reply into the main answer stream."""

    name = "reply"
    result_kind = "reply"
    activity_kind = ""
    display_label = "Reply"
    description = (
        "Send a user-facing message as the main assistant reply. "
        "Be concise by default, but provide a complete answer when the user asks for a full solution, detailed derivation, proof, tutorial, or exhaustive explanation. "
        "Prefer direct assistant text for long or final answers so the UI can stream them token-by-token. "
        "Use this for short proactive status or replies that need attachments. "
        "Do not use it for filler acknowledgements like 'I'll keep looking' or 'now I will answer'."
    )
    permission = PermissionLevel.AUTO
    read_only = False
    mutates_workspace = False
    mutates_external_state = True
    side_effect_kind = "external"
    idempotent = False
    deferred_catalog_scopes = ()

    _IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="user.reply",
            exposure="deferred",
            required_args=("message",),
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Markdown message to show in the main assistant reply area.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["normal", "proactive"],
                        "default": "normal",
                        "description": "Use 'proactive' for a brief status update before more work; otherwise use 'normal'.",
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional file paths (absolute or relative to the workspace root) to attach "
                            "alongside the message. Use for screenshots, diffs, logs, or any file the user "
                            "should see with this reply."
                        ),
                    },
                },
                "required": ["message"],
            },
            strict=True,
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        message = str(args.get("message") or "").strip()
        if not message:
            return self._error_result("Missing message argument")

        attachments_meta = self._resolve_attachments(args.get("attachments"), context)

        emit_event = context.emit_event if context else None
        if emit_event is not None:
            tool_call_id = str(context.tool_call_id or "").strip()
            item_id = f"agent-message:{tool_call_id}" if tool_call_id else "agent-message"
            await emit_event("item.started", {
                "item": {
                    "id": item_id,
                    "type": "agent_message",
                    "text": "",
                    "status": "in_progress",
                },
            })
            payload: dict[str, Any] = {
                "item": {
                    "id": item_id,
                    "type": "agent_message",
                    "text": message,
                    "source": "reply",
                    "status": "completed",
                },
            }
            if attachments_meta is not None:
                payload["attachments"] = attachments_meta
            await emit_event("item.completed", payload)

        summary = "Sent user-facing message."
        if str(args.get("status") or "normal") == "proactive":
            summary = "Sent proactive user-facing status."
        if attachments_meta:
            summary += f" ({len(attachments_meta)} attachment{'s' if len(attachments_meta) != 1 else ''} included)"
        return ToolResult(
            content=summary,
            result_kind="reply",
        )

    def _resolve_attachments(
        self,
        raw: Any,
        context: ToolExecutionContext | None,
    ) -> list[dict[str, Any]] | None:
        if not raw or not isinstance(raw, list):
            return None
        import fnmatch
        import os

        ws = getattr(context, "workspace_root", None) if context else None
        workspace_root = (Path(str(ws)) if ws else Path(os.getcwd())).resolve()
        checker = getattr(context, "permission_checker", None) if context else None
        if checker is None:
            from backend.config import PermissionSettings

            checker = PermissionChecker(PermissionSettings(), workspace_root)
        else:
            checker = checker.with_workspace_root(workspace_root)
        denylist = checker.policy_snapshot().get("path_denylist", [])
        permission_context = getattr(context, "permission", None) if context else None
        constraints = getattr(permission_context, "filesystem_constraints", {}) or {}
        if "denylist" in constraints:
            denylist = list(constraints["denylist"])

        resolved: list[dict[str, Any]] = []
        for item in raw:
            path_str = str(item or "").strip()
            if not path_str:
                continue
            requested_path = Path(path_str)
            if any(part == ".." for part in requested_path.parts):
                continue
            full_path = requested_path
            if not full_path.is_absolute():
                full_path = workspace_root / full_path
            try:
                real_path = full_path.resolve()
                rel_path = real_path.relative_to(workspace_root).as_posix()
                if self._matches_attachment_denylist(path_str, rel_path, real_path.name, denylist, fnmatch):
                    continue
                allowed, _reason = checker.validate_file_operation(str(real_path), "read")
                if not allowed or not real_path.is_file():
                    continue
                size = real_path.stat().st_size
            except OSError:
                continue
            except ValueError:
                continue
            resolved.append({
                "path": str(real_path),
                "size": size,
                "is_image": real_path.suffix.lower() in self._IMAGE_EXTENSIONS,
            })
        return resolved if resolved else None

    @staticmethod
    def _matches_attachment_denylist(
        raw_path: str,
        rel_path: str,
        file_name: str,
        denylist: list[str],
        fnmatch_module: Any,
    ) -> bool:
        raw_normalized = raw_path.replace("\\", "/").strip()
        rel_normalized = rel_path.replace("\\", "/").strip()
        for pattern in denylist:
            normalized = str(pattern).replace("\\", "/").strip()
            while normalized.startswith("./"):
                normalized = normalized[2:]
            if not normalized:
                continue
            if normalized.endswith("/") and rel_normalized.startswith(normalized):
                return True
            if fnmatch_module.fnmatch(raw_normalized, normalized):
                return True
            if fnmatch_module.fnmatch(rel_normalized, normalized):
                return True
            if fnmatch_module.fnmatch(file_name, normalized):
                return True
        return False
