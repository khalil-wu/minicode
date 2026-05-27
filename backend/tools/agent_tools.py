"""
Agent 辅助工具（DESIGN.md §8.2）。

  - ask_user:       主动向用户提问。权限: AUTO
  - read_artifact:  读取 artifact 全文。权限: AUTO
  - task:           委托隔离上下文的子 agent。权限: AUTO
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from backend.attachments.store import AttachmentStore
from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


class AskUserTool(BaseTool):
    """
    Agent 主动向用户提问。

    当 Agent 缺少关键信息无法继续时使用。
    权限: AUTO
    """

    name = "ask_user"
    description = (
        "向用户提出一个问题以获取缺失的关键信息。"
        "当你缺少完成任务所需的关键决策或信息时使用此工具。"
        "示例: ask_user(question='你想使用 TypeScript 还是 JavaScript？')。"
        "注意: 不要用于不必要的确认，只在真正需要用户决策时使用。"
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
                        "description": "要向用户提出的问题，应清晰明确",
                    },
                },
                "required": ["question"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        question = args.get("question", "")
        if not question:
            return self._error_result("缺少 question 参数")

        # ask_user 的实际实现由 Agent Loop 层拦截处理
        # 这里只返回一个标记，Agent Loop 会将其转发给前端
        return self._success_result(
            f"[等待用户回答] {question}"
        )


class ReadArtifactTool(BaseTool):
    """
    读取 Artifact Store 中存储的完整内容。

    当工具返回了 artifact_id 引用时，Agent 可用此工具获取全文。
    权限: AUTO
    """

    name = "read_artifact"
    description = (
        "读取 artifact 的完整内容。"
        "当其他工具因输出过长而将内容存入 artifact 时，"
        "使用此工具获取完整内容。"
        "示例: read_artifact(artifact_id='art_a1b2c3d4')。"
        "注意: artifact_id 由其他工具返回，在当前会话内有效。"
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

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "artifact_id": {
                        "type": "string",
                        "description": "artifact 的唯一标识符，如 'art_a1b2c3d4'",
                    },
                },
                "required": ["artifact_id"],
            },
            strict=True,
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        artifact_id = args.get("artifact_id", "")
        if not artifact_id:
            return self._error_result("缺少 artifact_id 参数")

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
            hint = f"可用的 artifact: {', '.join(ids)}" if ids else "当前没有可用的 artifact"
            return self._error_result(
                f"artifact '{artifact_id}' 不存在。{hint}"
            )

        return self._success_result(content)

    @staticmethod
    def _is_parse_error(content: str) -> bool:
        text = (content or "").strip().lower()
        return text.startswith(("错误:", "error:", "閿欒:"))

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
    """
    委托一个隔离上下文的子 agent 完成有边界的工作。

    这对应 Claude Code 的 Task/subagent 思路：主 agent 保持单一 ReAct
    循环，只在需要时把探索、分析或小范围执行交给独立上下文的子 agent。
    """

    name = "task"
    description = (
        "Delegate a focused task to a subagent running in its own isolated context. "
        "Use this for bounded side investigations, codebase exploration, or a clearly scoped implementation slice. "
        "Do not use it to force a global plan/accept/execute workflow; the main agent remains responsible for orchestration. "
        "The subagent returns a concise summary to continue the current conversation."
    )
    permission = PermissionLevel.AUTO

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
                        "enum": ["general-purpose", "explore", "plan", "implement"],
                        "description": "Optional subagent type. Use explore or plan for read-heavy investigation; implement for a focused code change.",
                    },
                },
                "required": ["description", "prompt"],
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        description = str(args.get("description") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        agent_type = str(args.get("agent_type") or "general-purpose").strip().lower()
        if agent_type == "general":
            agent_type = "general-purpose"
        if agent_type not in {"general-purpose", "explore", "plan", "implement"}:
            agent_type = "general-purpose"
        if not description:
            return self._error_result("缺少 description 参数")
        if not prompt:
            return self._error_result("缺少 prompt 参数")

        llm = self._resolve_llm()
        tool_registry = self._resolve_tool_registry()
        permission_checker = self._resolve_permission_checker()
        if llm is None or tool_registry is None or permission_checker is None:
            return self._error_result("task 子 agent 运行时尚未配置")

        subagent_id = f"subagent-{uuid4().hex[:8]}"
        parent_id = context.task_id if context and context.task_id else context.session_id if context else ""
        emit_event = context.emit_event if context else None
        if emit_event is not None:
            await emit_event(
                "subagent.start",
                AgentEvent.subagent_start(
                    subagent_id=subagent_id,
                    parent_id=parent_id,
                    role=agent_type,
                    prompt=description,
                ).data,
            )

        sub_settings = self._resolve_agent_settings()
        sub_budget = self._resolve_token_budget()
        sub_context = self._build_permission_context(agent_type, context)
        delegated_prompt = self._build_subagent_prompt(agent_type, prompt)
        sub_state = AgentState(user_message=delegated_prompt, max_iterations=sub_settings.max_iterations)
        sub_state.workspace_context = getattr(context, "metadata", {}).get("workspace_context") if context else None
        if context is not None:
            sub_state.conversation_id = context.conversation_id
            sub_state.checkpoint_manager = context.checkpoint_manager

        summary_parts: list[str] = []
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
                metadata=dict(context.metadata) if context else {},
                stream_callback=context.stream_callback if context else None,
                session_context=None,
            ):
                if event.type == "text_chunk":
                    summary_parts.append(str(event.data.get("content", "")))
                elif event.type == "error":
                    summary_parts.append(f"\nError: {event.data.get('message', '')}")

            summary = "".join(summary_parts).strip() or sub_state.reply.strip()
            if emit_event is not None:
                await emit_event(
                    "subagent.done",
                    AgentEvent.subagent_done(subagent_id=subagent_id, summary=summary).data,
                )
            return self._success_result(
                f"Subagent {subagent_id} ({agent_type}) completed.\n\n{summary}"
            )
        except asyncio.CancelledError:
            if emit_event is not None:
                await emit_event(
                    "subagent.done",
                    AgentEvent.subagent_done(subagent_id=subagent_id, error="cancelled").data,
                )
            raise
        except Exception as exc:
            if emit_event is not None:
                await emit_event(
                    "subagent.done",
                    AgentEvent.subagent_done(subagent_id=subagent_id, error=str(exc)).data,
                )
            return self._error_result(f"task 子 agent 执行失败: {exc}")

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
        if parent_permission.mode == "plan" or agent_type in {"explore", "plan"}:
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
    def _build_subagent_prompt(agent_type: str, prompt: str) -> str:
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
        else:
            role_note = (
                "You are a general-purpose subagent. Complete the bounded task and return a concise summary. "
                "Do not spawn more subagents."
            )
        return f"{role_note}\n\nTask:\n{prompt}"
