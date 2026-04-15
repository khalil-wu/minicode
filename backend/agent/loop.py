"""
Agent Loop — 四级进化的核心循环（DESIGN.md §7）。

Level 1: 简单对话 — user_message → LLM → text response
Level 2: 工具循环 — user_message → LLM → [tool_call → execute → inject]* → text response
Level 3: 审批循环 — Level 2 + CONFIRM/DIFF_REVIEW 权限拦截
Level 4: 完整 Loop — Level 3 + Compaction + 停滞检测 + 四种终止条件

四种终止条件（DESIGN.md §7 终止条件表）：
  1. 正常终止: Agent 完成任务，不再调用工具
  2. 预算终止: loop_count > MAX_ITERATIONS 或 token > 0.95 * budget
  3. 停滞终止: 同一工具+同一参数调用 ≥ 3 次
  4. 异常终止: LLM API 报错 / 工具 panic
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Callable

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import (
    LLMAdapter,
    StreamEvent,
    StreamEventType,
    ToolCallEvent,
)
from backend.permissions.checker import PermissionChecker
from backend.permissions.review import generate_edit_diff, generate_file_diff
from backend.tools.base import PermissionLevel, ToolResult
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


async def run_agent_loop(
    user_message: str,
    llm: LLMAdapter,
    tool_registry: ToolRegistry,
    artifact_store: ArtifactStore,
    permission_checker: PermissionChecker,
    agent_settings: AgentSettings | None = None,
    token_budget: TokenBudget | None = None,
    context_builder: ContextBuilder | None = None,
    state: AgentState | None = None,
    approval_handler: Callable | None = None,
    skill_manager: Any | None = None,
) -> AsyncIterator[AgentEvent]:
    """
    Agent Loop 主入口。

    异步生成器：yield AgentEvent 事件流。
    前端通过 WebSocket 消费这些事件来实现实时渲染。

    Args:
        user_message: 用户消息
        llm: LLM 适配器
        tool_registry: 工具注册中心
        artifact_store: 产物存储
        permission_checker: 权限检查器
        agent_settings: Agent 运行参数
        token_budget: Token 预算配置
        context_builder: Context 构建器（可复用以保持历史）
        state: Agent 状态（可复用以保持上下文）
        approval_handler: 审批回调（Level 3，接收 AgentEvent 返回 approve/reject）
        skill_manager: Skills 管理器（可选，用于自动检测和激活）

    Yields:
        AgentEvent 事件流
    """
    settings = agent_settings or AgentSettings()
    budget = token_budget or TokenBudget()
    ctx = context_builder or ContextBuilder(token_budget=budget, agent_settings=settings)
    state = state or AgentState(
        user_message=user_message, max_iterations=settings.max_iterations
    )

    # ── Skills 自动检测（DESIGN.md §3.2 动态触发）──
    if skill_manager:
        try:
            to_activate = skill_manager.auto_detect(user_message)
            for skill_name in to_activate:
                if skill_manager.activate(skill_name):
                    state.active_skills.append(skill_name)
                    yield AgentEvent(
                        type="skill_activated",
                        data={
                            "skill_name": skill_name,
                            "description": f"自动激活 Skill: {skill_name}",
                        },
                    )
        except Exception as exc:
            logger.debug("Skills 自动检测失败: %s", exc)

    # 记录用户消息到对话历史
    ctx.append_user(user_message)

    # 获取工具 schemas
    tool_schemas = tool_registry.get_schemas(budget=budget.tool_schemas)

    # ── 主循环 ────────────────────────────────────────
    while True:
        # ── 终止条件检查 ──

        # 预算终止
        if state.iterations >= settings.max_iterations:
            logger.warning("预算终止: 达到最大迭代次数 %d", settings.max_iterations)
            yield AgentEvent.error(
                message=f"已达到最大迭代次数限制（{settings.max_iterations}次）。当前进度已保存。",
                recoverable=True,
                error_type="budget",
            )
            state.stopped_reason = "max_iterations"
            break

        # 停滞终止
        if state.is_stagnant(limit=settings.stagnation_limit):
            detail = state.get_stagnation_detail()
            logger.warning("停滞终止: %s", detail)
            yield AgentEvent.error(
                message=f"检测到循环: {detail}。请尝试换个方式描述您的需求。",
                recoverable=True,
                error_type="stagnant",
            )
            state.stopped_reason = "stagnation"
            break

        # Token 预算检查 & Compaction
        if ctx.needs_compaction():
            summary = ctx.compact()
            logger.info("Compaction: %s", summary)
            yield AgentEvent.context_compacted(summary=summary)

        # ── 构建 Context 并调用 LLM ──
        messages = ctx.build(
            user_message=user_message,
            state=state,
            tool_schemas=tool_schemas,
        )

        state.iterations += 1
        full_text = ""
        pending_tool_calls: list[ToolCallEvent] = []

        try:
            async for event in llm.stream_chat(messages, tools=tool_schemas):
                if event.type == StreamEventType.TEXT_CHUNK:
                    full_text += event.content
                    yield AgentEvent.text_chunk(event.content)

                elif event.type == StreamEventType.TOOL_CALL:
                    pending_tool_calls = event.tool_calls

                elif event.type == StreamEventType.ERROR:
                    yield AgentEvent.error(
                        message=event.content,
                        recoverable=True,
                        error_type="api",
                    )
                    state.stopped_reason = "api_error"
                    # 记录助手消息（即使出错也要记录已生成的文本）
                    if full_text:
                        ctx.append_assistant(full_text)
                    return

                elif event.type == StreamEventType.DONE:
                    # 记录 usage
                    pass

        except Exception as exc:
            # 异常终止
            logger.error("LLM 调用异常: %s", exc, exc_info=True)
            yield AgentEvent.error(
                message=f"LLM 调用异常: {exc}",
                recoverable=True,
                error_type="api",
            )
            state.stopped_reason = "api_error"
            if full_text:
                ctx.append_assistant(full_text)
            return

        # ── 处理 LLM 响应 ──

        if not pending_tool_calls:
            # 正常终止: LLM 完成任务，不调用工具
            if full_text:
                ctx.append_assistant(full_text)
                state.reply = full_text
            state.stopped_reason = "completed"
            yield AgentEvent.done()
            break

        # ── 处理工具调用（Level 2 + Level 3）──
        if full_text:
            ctx.append_assistant(full_text)

        ctx.append_assistant_tool_calls(pending_tool_calls)

        for tc in pending_tool_calls:
            # 发送工具调用事件
            yield AgentEvent.tool_call(id=tc.id, name=tc.name, args=tc.arguments)

            # ── Level 3: 权限检查 ──
            perm_level = permission_checker.check(tc.name, tc.arguments)

            # 路径检查
            denial = permission_checker.get_denial_reason(tc.name, tc.arguments)
            if denial:
                result = ToolResult(content=denial, is_error=True)
                ctx.append_tool_result(tc.id, tc.name, result)
                yield AgentEvent.tool_result(id=tc.id, summary=denial)
                state.record_tool_call(tc.name, tc.arguments, denial, is_error=True)
                continue

            if perm_level == PermissionLevel.ALWAYS_DENY:
                result = ToolResult(
                    content=f"工具 '{tc.name}' 已被禁止使用。",
                    is_error=True,
                )
                ctx.append_tool_result(tc.id, tc.name, result)
                yield AgentEvent.tool_result(id=tc.id, summary=result.content)
                state.record_tool_call(
                    tc.name, tc.arguments, result.content, is_error=True
                )
                continue

            if perm_level in (PermissionLevel.CONFIRM, PermissionLevel.DIFF_REVIEW):
                # 生成 diff（如果是文件操作）
                diff = None
                if perm_level == PermissionLevel.DIFF_REVIEW:
                    diff = _generate_diff(tc.name, tc.arguments)

                yield AgentEvent.approval_request(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    args=tc.arguments,
                    diff=diff,
                )

                # 等待审批
                if approval_handler:
                    approval = await approval_handler(tc.id)
                    if approval.get("action") == "reject":
                        guidance = approval.get("guidance", "用户拒绝了此操作")
                        result = ToolResult(
                            content=f"操作被用户拒绝: {guidance}",
                            is_error=True,
                        )
                        ctx.append_tool_result(tc.id, tc.name, result)
                        yield AgentEvent.tool_result(
                            id=tc.id, summary=result.content
                        )
                        state.record_tool_call(
                            tc.name, tc.arguments, result.content, is_error=True
                        )
                        continue
                # 没有 approval_handler 时默认通过（开发模式）

            # ── 执行工具 ──
            result = await tool_registry.execute(tc.name, tc.arguments)

            # 记录结果
            ctx.append_tool_result(tc.id, tc.name, result)
            yield AgentEvent.tool_result(
                id=tc.id,
                summary=result.content,
                artifact_id=result.artifact_id,
            )
            state.record_tool_call(
                tc.name,
                tc.arguments,
                result.content,
                artifact_id=result.artifact_id,
                is_error=result.is_error,
            )

    # 最终完成
    if state.stopped_reason == "completed":
        pass  # done event 已经在循环中发出
    return


def _generate_diff(tool_name: str, args: dict[str, Any]) -> str | None:
    """为文件操作工具生成 diff。"""
    if tool_name == "write_file":
        file_path = args.get("file_path", "")
        content = args.get("content", "")
        if file_path and content:
            return generate_file_diff(file_path, content)

    elif tool_name == "edit_file":
        file_path = args.get("file_path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        if file_path and old_string:
            return generate_edit_diff(file_path, old_string, new_string)

    return None
