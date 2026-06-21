"""Agent helper tools: user clarification, artifacts, and subagents."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

from backend.attachments.store import AttachmentStore
from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agents.loader import discover_agents, get_custom_agent
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


# Built-in subagent types. Custom agents (from .mini-code/agents/*.md) are
# accepted in addition to these via agents.loader.get_custom_agent.
_BUILTIN_AGENT_TYPE_ORDER = [
    "general-purpose",
    "explore",
    "plan",
    "implement",
    "verification",
]
_BUILTIN_AGENT_TYPES: set[str] = set(_BUILTIN_AGENT_TYPE_ORDER)


def _available_agent_types() -> list[str]:
    """Return built-in plus discovered custom subagent types for model schema."""
    try:
        custom = sorted(
            name
            for name in discover_agents().keys()
            if name and name not in _BUILTIN_AGENT_TYPES
        )
    except Exception:
        custom = []
    return [*_BUILTIN_AGENT_TYPE_ORDER, *custom]


def _artifact_content_preview(content: str, *, max_chars: int = 1600) -> str:
    text = str(content or "").strip()
    if len(text) <= max_chars:
        return text
    head = text[: max_chars - 80].rstrip()
    return f"{head}\n... [{len(text) - len(head)} chars omitted; expand/open artifact for full content] ..."


class AskUserTool(BaseTool):
    """Ask the user one concise clarification question."""

    name = "ask_user"
    description = (
        "Ask the user a focused clarification question when a required detail cannot be inferred from context or tools. "
        "Ask at most ONE question per turn. Include enough context for the user to answer without reading the entire conversation."
    )
    permission = PermissionLevel.AUTO

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
                },
                "required": ["question"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        question = args.get("question", "")
        if not question:
            return self._error_result("Missing question argument")
        return self._success_result(f"[waiting for user answer] {question}")


class BriefTool(BaseTool):
    """Send a concise user-facing reply into the main answer stream."""

    name = "send_message"
    description = (
        "Send a concise user-facing message as the main assistant reply. "
        "Use this for final results or proactive status that the user should definitely see. "
        "Do not use it for filler acknowledgements like 'I'll keep looking' or 'now I will answer'."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    mutates_workspace = False

    # Image extensions rendered inline (previewable). Everything else is
    # shown as a [file] chip with its size. Matches cc's IMAGE_EXTENSION_REGEX.
    _IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="user.reply",
            exposure="deferred",
            required_args=("message",),
            arg_roles={"message": "generated_content"},
            empty_args_policy="repair_or_block",
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
            payload: dict[str, Any] = {"content": message, "source": self.name}
            if attachments_meta is not None:
                payload["attachments"] = attachments_meta
            await emit_event("text_chunk", payload)

        summary = "Sent user-facing message."
        if str(args.get("status") or "normal") == "proactive":
            summary = "Sent proactive user-facing status."
        if attachments_meta:
            summary += f" ({len(attachments_meta)} attachment{'s' if len(attachments_meta) != 1 else ''} included)"
        return ToolResult(
            content=summary,
            result_kind="reply",
            display_scope="silent",
        )

    def _resolve_attachments(
        self,
        raw: Any,
        context: ToolExecutionContext | None,
    ) -> list[dict[str, Any]] | None:
        """Validate and stat attachment paths.

        Returns ``None`` when no attachments were supplied (so the event payload
        stays unchanged), or a list of ``{path, size, is_image}`` metadata dicts.
        Missing or inaccessible paths are skipped — best-effort graceful
        degradation, mirroring cc's resolveAttachments. The model can retry with
        corrected paths.
        """
        if not raw or not isinstance(raw, list):
            return None
        import fnmatch
        import os
        from pathlib import Path

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


class ReadArtifactTool(BaseTool):
    """Read full content from an artifact created by a previous tool."""

    name = "read_artifact"
    read_only = True
    description = (
        "Read complete artifact content when a previous tool returned an artifact_id. "
        "Only use artifact IDs that appeared in this conversation."
    )
    permission = PermissionLevel.AUTO

    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        attachment_store: AttachmentStore | None = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._attachment_store = attachment_store

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="artifact.read",
            required_args=("artifact_id",),
            arg_roles={"artifact_id": "latest_artifact"},
            empty_args_policy="repair_or_block",
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "artifact_id": {
                        "type": "string",
                        "description": "Artifact identifier, for example 'art_a1b2c3d4'.",
                    },
                },
                "required": ["artifact_id"],
            },
            strict=True,
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        artifact_id = args.get("artifact_id", "")
        if not artifact_id:
            return self._error_result("Missing artifact_id argument")

        content = self._artifact_store.get(artifact_id)
        if content is None and self._attachment_store is not None:
            content = self._attachment_store.get(artifact_id)
        if content is None and self._attachment_store is not None:
            resolved = self._attachment_store.resolve_content(artifact_id)
            if resolved is not None:
                _resolved_artifact_id, content, _metadata = resolved

        # If content is a parse error and we have the original binary, re-parse
        if content and self._is_parse_error(content) and self._attachment_store is not None:
            reparsed = self._try_reparse(artifact_id)
            if reparsed:
                content = reparsed

        if content is None:
            available = self._artifact_store.list_artifacts()
            ids = [a.artifact_id for a in available]
            hint = f"Available artifacts: {', '.join(ids)}" if ids else "No artifacts are currently available"
            return self._error_result(
                f"Artifact '{artifact_id}' does not exist. {hint}"
            )

        preview = _artifact_content_preview(content)
        return ToolResult(
            content=content,
            content_preview=preview,
            display_summary=f"Read artifact {artifact_id}",
        )

    @staticmethod
    def _is_parse_error(content: str) -> bool:
        text = (content or "").strip().lower()
        return text.startswith(("错误:", "error:", "parse error:", "pdf parse failed"))

    def _try_reparse(self, artifact_id: str) -> str | None:
        """Attempt to re-parse from stored native binary data."""
        import base64
        import tempfile
        import os

        if self._attachment_store is None:
            return None
        payload = self._attachment_store.find_payload(artifact_id)
        if not payload:
            return None
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return None
        attachment = metadata.get("attachment")
        if not isinstance(attachment, dict):
            return None
        native_data = attachment.get("data", "")
        file_name = attachment.get("file_name", "")
        if not native_data or not file_name:
            return None

        suffix = "." + file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        if suffix != ".pdf":
            return None

        try:
            raw_bytes = base64.b64decode(native_data)
        except Exception:
            return None

        fd, temp_path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw_bytes)
            from backend.mcp.servers.docparse import _parse_pdf
            parsed = _parse_pdf(temp_path)
            full_text = str(parsed.get("full_text", "")).strip()
            if not full_text or self._is_parse_error(full_text):
                return None
            # Update attachment store so future reads get the correct content
            self._attachment_store.save(
                artifact_id=artifact_id,
                content=full_text,
                metadata=metadata,
            )
            # Update artifact store's content file on disk
            from backend.artifact.store import ARTIFACT_DATA_DIR
            content_path = ARTIFACT_DATA_DIR / f"{artifact_id}.txt"
            if content_path.parent.exists():
                content_path.write_text(full_text, encoding="utf-8")
            return full_text
        except Exception:
            return None
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass


class TaskTool(BaseTool):
    """Delegate a bounded task to an isolated subagent."""

    name = "task"
    description = (
        "Delegate a sub-task to an independent agent. The sub-agent has its own context and tool access. "
        "Use for complex, independent work items that benefit from focused attention. "
        "Supports parallel sub-tasks via the parallel_tasks parameter (up to 5 concurrent). "
        "Results include the sub-agent's findings and a summary of tools used."
    )
    permission = PermissionLevel.AUTO

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="agent.delegate",
            toolset="agent",
            exposure="deferred",
            required_args=("description", "prompt"),
            arg_roles={
                "description": "generated_content",
                "prompt": "generated_content",
                "agent_type": "control",
            },
            repair_policy={
                "description": "needs_model_generation",
                "prompt": "needs_model_generation",
                "agent_type": "runtime_control",
            },
            empty_args_policy="repair_or_block",
        )

    def __init__(
        self,
        *,
        llm_provider: Any | None = None,
        tool_registry_provider: Any | None = None,
        artifact_store: ArtifactStore,
        permission_checker_provider: Any | None = None,
        agent_settings_provider: Any | None = None,
        token_budget_provider: Any | None = None,
    ) -> None:
        self._llm_provider = llm_provider
        self._tool_registry_provider = tool_registry_provider
        self._artifact_store = artifact_store
        self._permission_checker_provider = permission_checker_provider
        self._agent_settings_provider = agent_settings_provider
        self._token_budget_provider = token_budget_provider

    def get_schema(self) -> ToolSchema:
        agent_types = _available_agent_types()
        agent_type_description = (
            "Optional subagent type. Use explore or plan for read-heavy investigation, "
            "implement for a focused code change, and verification for adversarial read-only checks. "
            f"Available types: {', '.join(agent_types)}."
        )
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Short description of the delegated task, shown in the UI.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The complete, self-contained prompt for the subagent.",
                    },
                    "agent_type": {
                        "type": "string",
                        "enum": agent_types,
                        "description": agent_type_description,
                    },
                    "parallel_tasks": {
                        "type": "array",
                        "description": (
                            "Run multiple subtasks concurrently. Each item is an object with "
                            "'description', 'prompt', and optional 'agent_type'. "
                            "When provided, the single-task 'prompt'/'description' fields are ignored."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {
                                    "type": "string",
                                    "description": "Short description of this subtask.",
                                },
                                "prompt": {
                                    "type": "string",
                                    "description": "Complete prompt for this subtask.",
                                },
                                "agent_type": {
                                    "type": "string",
                                    "enum": agent_types,
                                    "description": agent_type_description,
                                },
                            },
                            "required": ["description", "prompt"],
                        },
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": (
                            "Wall-clock timeout in seconds per subtask (default 300, max 600). "
                            "If a subtask exceeds this, partial results are returned."
                        ),
                    },
                },
                "anyOf": [
                    {"required": ["description", "prompt"]},
                    {"required": ["parallel_tasks"]},
                ],
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        if self._is_recursive_subagent_call(context):
            return ToolResult(
                content=(
                    "Blocked recursive subagent delegation. Subagents cannot call the task tool; "
                    "return a concise summary to the parent agent instead."
                ),
                is_error=True,
                status="blocked",
                display_summary="Blocked recursive subagent",
                result_kind="subagent",
            )

        description = str(args.get("description") or "").strip()
        parallel_tasks = args.get("parallel_tasks")
        timeout_seconds = float(args.get("timeout_seconds") or 300.0)
        timeout_seconds = min(max(timeout_seconds, 30.0), 600.0)

        llm = self._resolve_llm()
        tool_registry = self._resolve_tool_registry()
        permission_checker = self._resolve_permission_checker()
        if llm is None or tool_registry is None or permission_checker is None:
            return self._error_result("Subagent runtime is not configured")

        # ── Parallel execution path ──
        if isinstance(parallel_tasks, list) and len(parallel_tasks) >= 2:
            tasks: list[dict[str, str]] = []
            for item in parallel_tasks[:5]:  # cap at 5 parallel subtasks
                if not isinstance(item, dict):
                    continue
                t_desc = str(item.get("description") or "").strip()
                t_prompt = str(item.get("prompt") or "").strip()
                t_type = str(item.get("agent_type") or "general-purpose").strip().lower()
                if t_desc and t_prompt:
                    tasks.append({
                        "description": t_desc,
                        "prompt": t_prompt,
                        "agent_type": t_type if (t_type in _BUILTIN_AGENT_TYPES or get_custom_agent(t_type)) else "general-purpose",
                    })
            if len(tasks) >= 2:
                return await self._run_parallel_subtasks(
                    tasks, context, timeout_seconds,
                )

        # ── Single execution path ──
        prompt = str(args.get("prompt") or "").strip()
        agent_type = str(args.get("agent_type") or "general-purpose").strip().lower()
        if agent_type == "general":
            agent_type = "general-purpose"
        if agent_type not in _BUILTIN_AGENT_TYPES and not get_custom_agent(agent_type):
            agent_type = "general-purpose"
        if not description:
            return self._error_result("Missing description argument")
        if not prompt:
            return self._error_result("Missing prompt argument")

        return await self._run_single_subtask(
            description=description,
            prompt=prompt,
            agent_type=agent_type,
            context=context,
            timeout_seconds=timeout_seconds,
        )

    # ------------------------------------------------------------------
    # Single subtask execution
    # ------------------------------------------------------------------

    async def _run_single_subtask(
        self,
        *,
        description: str,
        prompt: str,
        agent_type: str,
        context: ToolExecutionContext | None,
        timeout_seconds: float = 300.0,
        subtask_index: int | None = None,
        total_subtasks: int | None = None,
    ) -> ToolResult:
        """Run one isolated subagent loop with timeout and progress reporting.

        Returns a structured ``ToolResult`` that includes the subagent summary,
        duration, iteration count, and tool-call statistics.
        """
        llm = self._resolve_llm()
        tool_registry = self._resolve_tool_registry()
        permission_checker = self._resolve_permission_checker()

        subagent_id = f"subagent-{uuid4().hex[:8]}"
        parent_id = context.task_id if context and context.task_id else context.session_id if context else ""
        emit_event = context.emit_event if context else None
        runtime = self._runtime_from_context(context)
        parent_metadata = self._metadata_from_context(context)
        parent_run_id = str(parent_metadata.get("run_id", ""))
        subagent_record = runtime.start_subagent(
            subagent_id=subagent_id,
            parent_run_id=parent_run_id,
            agent_type=agent_type,
            prompt_summary=description,
        ) if runtime is not None else None

        if emit_event is not None:
            start_event = AgentEvent.subagent_start(
                subagent_id=subagent_id,
                parent_id=parent_id,
                role=agent_type,
                prompt=description,
            )
            if subagent_record is not None:
                start_event.data["record"] = subagent_record.to_dict()
                start_event.data["parent_run_id"] = parent_run_id
            await emit_event("subagent.start", start_event.data)

        sub_settings = self._resolve_agent_settings()
        if sub_settings.max_iterations > 8:
            sub_settings = AgentSettings(
                max_iterations=8,
                compaction_threshold=sub_settings.compaction_threshold,
                stagnation_limit=sub_settings.stagnation_limit,
                history_keep_recent=sub_settings.history_keep_recent,
                fallback_providers=sub_settings.fallback_providers,
                reflection_pass=sub_settings.reflection_pass,
                agent_mode=sub_settings.agent_mode,
                stream_timeout_seconds=sub_settings.stream_timeout_seconds,
                stream_max_attempts=sub_settings.stream_max_attempts,
                stream_retry_delay_seconds=sub_settings.stream_retry_delay_seconds,
                stream_retryable_substrings=sub_settings.stream_retryable_substrings,
                reflection_policy=sub_settings.reflection_policy,
                stream_retry_policy=sub_settings.stream_retry_policy,
            )
        sub_budget = self._resolve_token_budget()
        sub_context = self._build_permission_context(agent_type, context)
        delegated_prompt = self._build_subagent_prompt(agent_type, prompt)
        sub_state = AgentState(user_message=delegated_prompt, max_iterations=sub_settings.max_iterations)
        sub_state.workspace_context = parent_metadata.get("workspace_context")
        if context is not None:
            sub_state.conversation_id = context.conversation_id
            sub_state.checkpoint_manager = context.checkpoint_manager

        summary_parts: list[str] = []
        start_time = time.perf_counter()
        timed_out = False
        last_tool_name = ""

        try:
            from backend.agent.loop import run_agent_loop

            async def subagent_approval_handler(tool_call_id: str) -> dict[str, str]:
                return {
                    "action": "reject",
                    "guidance": (
                        f"Subagent {subagent_id} cannot request user approvals directly. "
                        "Return a summary and let the main agent decide the next action."
                    ),
                }

            try:
                async with asyncio.timeout(timeout_seconds):
                    async for event in run_agent_loop(
                        user_message=delegated_prompt,
                        llm=llm,
                        tool_registry=tool_registry,
                        artifact_store=self._artifact_store,
                        permission_checker=permission_checker,
                        agent_settings=sub_settings,
                        token_budget=sub_budget,
                        context_builder=ContextBuilder(
                            token_budget=sub_budget,
                            agent_settings=sub_settings,
                        ),
                        state=sub_state,
                        approval_handler=subagent_approval_handler,
                        permission_context=sub_context,
                        session_id=context.session_id if context else "",
                        task_id=subagent_id,
                        task_manager=context.task_manager if context else None,
                        emit_event=emit_event,
                        metadata={
                            **parent_metadata,
                            "agent_runtime": runtime,
                            "parent_run_id": parent_run_id,
                            "agent_role": f"subagent:{agent_type}",
                            "run_id": subagent_id,
                        },
                        stream_callback=context.stream_callback if context else None,
                        session_context=None,
                    ):
                        if event.type == "text_chunk":
                            summary_parts.append(str(event.data.get("content", "")))
                        elif event.type == "error":
                            summary_parts.append(f"\nError: {event.data.get('message', '')}")
                        elif event.type == "tool_result":
                            last_tool_name = str(event.data.get("id", ""))
                        # Emit progress at iteration boundaries
                        if emit_event is not None and event.type == "tool_result":
                            elapsed = time.perf_counter() - start_time
                            await emit_event(
                                "subagent.progress",
                                AgentEvent.subagent_progress(
                                    subagent_id=subagent_id,
                                    iteration=sub_state.iterations,
                                    max_iterations=sub_settings.max_iterations,
                                    tool_name=last_tool_name,
                                    detail=f"{elapsed:.1f}s elapsed",
                                ).data,
                            )
            except asyncio.TimeoutError:
                timed_out = True

            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            summary = "".join(summary_parts).strip() or sub_state.reply.strip()
            tool_call_count = len(sub_state.tool_calls)

            if emit_event is not None:
                done_event = AgentEvent.subagent_done(
                    subagent_id=subagent_id,
                    summary=summary[:500] if summary else "",
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                    tool_call_count=tool_call_count,
                    timed_out=timed_out,
                )
                if runtime is not None:
                    record = runtime.complete_subagent(
                        subagent_id,
                        "failed" if timed_out else "completed",
                        summary=summary[:500] if summary else "",
                        tool_count=tool_call_count,
                    )
                    if record is not None:
                        done_event.data["record"] = record.to_dict()
                await emit_event("subagent.done", done_event.data)

            if timed_out and not summary:
                return ToolResult(
                    content=(
                        f"Subagent {subagent_id} ({agent_type}) timed out after "
                        f"{timeout_seconds:.0f}s with no result. "
                        f"It completed {sub_state.iterations} iteration(s) and "
                        f"{tool_call_count} tool call(s)."
                    ),
                    is_error=True,
                    duration_ms=elapsed_ms,
                    display_summary=f"Subagent timed out: {description[:60]}",
                    result_kind="subagent",
                )

            result_text = self._build_subtask_result_summary(
                subagent_id=subagent_id,
                agent_type=agent_type,
                summary=summary,
                duration_ms=elapsed_ms,
                iterations=sub_state.iterations,
                tool_calls=sub_state.tool_calls,
                timed_out=timed_out,
                timeout_seconds=timeout_seconds,
            )
            return ToolResult(
                content=result_text,
                duration_ms=elapsed_ms,
                display_summary=f"Subagent ({agent_type}): {description[:60]}",
                result_kind="subagent",
            )
        except asyncio.CancelledError:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            if emit_event is not None:
                done_event = AgentEvent.subagent_done(
                    subagent_id=subagent_id,
                    error="cancelled",
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                )
                if runtime is not None:
                    record = runtime.complete_subagent(subagent_id, "cancelled", summary="cancelled", tool_count=len(sub_state.tool_calls))
                    if record is not None:
                        done_event.data["record"] = record.to_dict()
                await emit_event("subagent.done", done_event.data)
            raise
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            if emit_event is not None:
                done_event = AgentEvent.subagent_done(
                    subagent_id=subagent_id,
                    error=str(exc),
                    duration_ms=elapsed_ms,
                    iterations=sub_state.iterations,
                )
                if runtime is not None:
                    record = runtime.complete_subagent(subagent_id, "failed", summary=str(exc), tool_count=len(sub_state.tool_calls))
                    if record is not None:
                        done_event.data["record"] = record.to_dict()
                await emit_event("subagent.done", done_event.data)
            return ToolResult(
                content=(
                    f"Subagent {subagent_id} ({agent_type}) failed after "
                    f"{elapsed_ms}ms and {sub_state.iterations} iteration(s).\n"
                    f"Error: {type(exc).__name__}: {exc}"
                ),
                is_error=True,
                duration_ms=elapsed_ms,
                display_summary=f"Subagent failed: {description[:60]}",
                result_kind="subagent",
            )

    # ------------------------------------------------------------------
    # Parallel subtask execution
    # ------------------------------------------------------------------

    async def _run_parallel_subtasks(
        self,
        tasks: list[dict[str, str]],
        context: ToolExecutionContext | None,
        timeout_seconds: float,
    ) -> ToolResult:
        """Run multiple subtasks concurrently via ``asyncio.gather``.

        Each subtask gets its own wall-clock timeout.  An outer timeout
        ensures the entire parallel batch cannot run indefinitely.
        """
        emit_event = context.emit_event if context else None
        total = len(tasks)
        start_time = time.perf_counter()

        coros = [
            self._run_single_subtask(
                description=t["description"],
                prompt=t["prompt"],
                agent_type=t.get("agent_type", "general-purpose"),
                context=context,
                timeout_seconds=timeout_seconds,
                subtask_index=i,
                total_subtasks=total,
            )
            for i, t in enumerate(tasks)
        ]

        # Outer timeout: per-task timeout + 30s overhead, capped at 10 minutes
        outer_timeout = min(timeout_seconds + 30.0, 600.0)
        try:
            async with asyncio.timeout(outer_timeout):
                results = await asyncio.gather(*coros, return_exceptions=True)
        except asyncio.TimeoutError:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            if emit_event is not None:
                await emit_event(
                    "subagent.done",
                    AgentEvent.subagent_done(
                        subagent_id="parallel-batch",
                        error="outer timeout",
                        duration_ms=elapsed_ms,
                    ).data,
                )
            return ToolResult(
                content=(
                    f"Parallel subtasks timed out after {outer_timeout:.0f}s. "
                    f"Some tasks may not have completed."
                ),
                is_error=True,
                duration_ms=elapsed_ms,
                display_summary="Parallel subtasks timed out",
                result_kind="subagent",
            )

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)

        # Merge results
        parts: list[str] = [f"Parallel subtasks completed ({total} tasks, {elapsed_ms / 1000:.1f}s total):\n"]
        has_error = False
        for i, (task, result) in enumerate(zip(tasks, results), 1):
            if isinstance(result, Exception):
                has_error = True
                parts.append(f"--- Task {i}/{total}: {task['description']} ---\nFAILED: {result}\n")
            elif isinstance(result, ToolResult):
                if result.is_error:
                    has_error = True
                parts.append(f"--- Task {i}/{total}: {task['description']} ---\n{result.content}\n")
            else:
                parts.append(f"--- Task {i}/{total}: {task['description']} ---\nNo result returned.\n")

        return ToolResult(
            content="\n".join(parts),
            is_error=has_error,
            duration_ms=elapsed_ms,
            display_summary=f"Parallel subtasks: {total} tasks",
            result_kind="subagent",
        )

    # ------------------------------------------------------------------
    # Result formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _build_subtask_result_summary(
        *,
        subagent_id: str,
        agent_type: str,
        summary: str,
        duration_ms: int,
        iterations: int,
        tool_calls: list,
        timed_out: bool,
        timeout_seconds: float,
    ) -> str:
        """Build a structured result summary for the parent agent.

        Includes the subagent's text output, timing metadata, and a compact
        list of tool calls so the parent knows what the subagent actually did.
        """
        header = f"Subagent {subagent_id} ({agent_type})"
        if timed_out:
            header += f" [TIMED OUT after {timeout_seconds:.0f}s]"
        header += f" completed in {duration_ms / 1000:.1f}s, {iterations} iteration(s)."

        # Include up to 10 most recent tool calls as a compact summary
        tool_lines: list[str] = []
        if tool_calls:
            recent = tool_calls[-10:]
            for tc in recent:
                name = getattr(tc, "tool_name", "?")
                status = getattr(tc, "status", "?")
                # Build a short arg summary
                inp = getattr(tc, "tool_input", {})
                if isinstance(inp, dict):
                    arg_short = ", ".join(
                        f"{k}={str(v)[:50]}"
                        for k, v in list(inp.items())[:3]
                    )
                else:
                    arg_short = str(inp)[:80]
                tool_lines.append(f"  - {name}({arg_short}) [{status}]")

        parts = [header]
        if summary:
            parts.append(f"\n{summary}")
        if tool_lines:
            parts.append(f"\nTools used ({len(tool_calls)} total):")
            parts.extend(tool_lines)

        return "\n".join(parts)

    def _resolve_llm(self) -> LLMAdapter | None:
        if callable(self._llm_provider):
            return self._llm_provider()
        return self._llm_provider

    def _resolve_tool_registry(self) -> ToolRegistry | None:
        if callable(self._tool_registry_provider):
            return self._tool_registry_provider()
        return self._tool_registry_provider

    def _resolve_permission_checker(self) -> PermissionChecker | None:
        if callable(self._permission_checker_provider):
            return self._permission_checker_provider()
        return self._permission_checker_provider

    def _resolve_agent_settings(self) -> AgentSettings:
        if callable(self._agent_settings_provider):
            settings = self._agent_settings_provider()
            if isinstance(settings, AgentSettings):
                return settings
        if isinstance(self._agent_settings_provider, AgentSettings):
            return self._agent_settings_provider
        return AgentSettings(max_iterations=8, agent_mode="react")

    def _resolve_token_budget(self) -> TokenBudget:
        if callable(self._token_budget_provider):
            budget = self._token_budget_provider()
            if isinstance(budget, TokenBudget):
                return budget
        if isinstance(self._token_budget_provider, TokenBudget):
            return self._token_budget_provider
        return TokenBudget(total=64_000)

    @staticmethod
    def _build_permission_context(
        agent_type: str,
        parent_context: ToolExecutionContext | None,
    ) -> PermissionContext:
        parent_permission = parent_context.permission if parent_context else PermissionContext()
        if parent_permission.mode == "plan" or agent_type in {"explore", "plan", "verification"}:
            mode = "plan"
        else:
            mode = parent_permission.mode
        deny_rules = list(parent_permission.tool_deny_rules)
        if "task" not in deny_rules:
            deny_rules.append("task")
        return PermissionContext(
            mode=mode,
            session_overrides=dict(parent_permission.session_overrides),
            tool_deny_rules=deny_rules,
            filesystem_constraints=dict(parent_permission.filesystem_constraints),
            source=f"subagent:{agent_type}",
        )

    @staticmethod
    def _is_recursive_subagent_call(context: ToolExecutionContext | None) -> bool:
        if context is None:
            return False
        permission = context.permission
        if permission.source.startswith("subagent:"):
            return True
        if "task" in permission.tool_deny_rules:
            return True
        return str(context.task_id or "").startswith("subagent-")

    @staticmethod
    def _build_subagent_prompt(agent_type: str, prompt: str) -> str:
        # Custom agent (from .mini-code/agents/*.md): its body is the role prompt.
        if agent_type not in _BUILTIN_AGENT_TYPES:
            custom = get_custom_agent(agent_type)
            if custom and custom.prompt:
                return f"{custom.prompt}\n\nTask:\n{prompt}"
        if agent_type == "explore":
            role_note = (
                "You are a read-only exploration subagent. Inspect the codebase and return concise findings "
                "with file references. Do not edit files, run mutating commands, or spawn more subagents."
            )
        elif agent_type == "plan":
            role_note = (
                "You are a read-only planning research subagent. Gather context for the main agent's plan, "
                "return the relevant findings, and do not edit files, run mutating commands, or spawn more subagents."
            )
        elif agent_type == "implement":
            role_note = (
                "You are an implementation subagent. Complete only the bounded work in this prompt, "
                "then summarize changed files and verification. Do not spawn more subagents."
            )
        elif agent_type == "verification":
            role_note = (
                "You are a verification specialist. Try to break the result with real read-only checks. "
                "Do not modify project files, install packages, or spawn more subagents. "
                "Return VERDICT: PASS, VERDICT: FAIL, or VERDICT: PARTIAL with concise evidence."
            )
        else:
            role_note = (
                "You are a general-purpose subagent. Complete the bounded task and return a concise summary. "
                "Do not spawn more subagents."
            )
        return f"{role_note}\n\nTask:\n{prompt}"

    @staticmethod
    def _runtime_from_context(context: ToolExecutionContext | None) -> AgentRuntime | None:
        if context is None:
            return None
        runtime = context.metadata.get("agent_runtime") if isinstance(context.metadata, dict) else None
        return runtime if isinstance(runtime, AgentRuntime) else None

    @staticmethod
    def _metadata_from_context(context: ToolExecutionContext | None) -> dict[str, Any]:
        if context is None or not isinstance(context.metadata, dict):
            return {}
        return dict(context.metadata)
