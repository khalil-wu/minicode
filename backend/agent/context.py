"""
Context 构建器（DESIGN.md §3 渐进式披露）。

核心职责：
  1. 按优先级组装 context（cinstr → cmem → ctools → cstate → cknow → history → cquery）
  2. Token 预算控制
  3. Compaction（压缩工具结果 → 摘要对话 → 滑动窗口）
  4. 对话历史管理
  5. Skills 注入（Layer 1 常驻 + Layer 2 激活内容）
  6. 被动 RAG（静默检索背景知识）
  7. 记忆索引注入（MEMORY.md 索引行）

黄金原则：任何一个组件注入 context 都必须问自己——
"如果去掉它，模型回答会变差吗？"如果不会，不要注入。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.agent.state import AgentState
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import LLMMessage, ToolCallEvent
from backend.tools.base import ToolResult

logger = logging.getLogger(__name__)

# ── 基础系统提示（~2K tokens）─────────────────────────────────
BASE_SYSTEM_PROMPT = """\
你是 MiniCode，一个专业的 AI 编程助手。

## 核心能力
- 阅读、编写、编辑代码文件
- 在目录中搜索代码和文本
- 执行 shell 命令
- 根据需要向用户提问

## 行为规范
- 先理解需求，再动手实现
- 修改文件前先读取当前内容，了解上下文
- 小范围修改用 edit_file（精确替换），大段重写用 write_file
- 执行命令前考虑安全性和副作用
- 遇到不确定的情况，使用 ask_user 向用户确认
- 输出简洁精炼，避免不必要的冗长解释

## 工具使用原则
- 每次只做一件事，观察结果后再决定下一步
- 如果工具返回了 artifact_id，说明完整结果已存储，需要时用 read_artifact 获取
- 不要重复调用相同参数的工具

## 输出格式
- 使用 Markdown 格式回复
- 代码块标注语言类型
- 修改文件后说明做了什么改动以及为什么
"""


class ContextBuilder:
    """
    渐进式 Context 组装器。

    管理对话历史、Token 预算、Compaction、Skills、RAG 和 Memory 注入。
    """

    def __init__(
        self,
        token_budget: TokenBudget | None = None,
        agent_settings: AgentSettings | None = None,
        skill_executor: Any | None = None,
        rag_pipeline: Any | None = None,
        memory_manager: Any | None = None,
    ) -> None:
        self._budget = token_budget or TokenBudget()
        self._agent_settings = agent_settings or AgentSettings()
        self._history: list[LLMMessage] = []
        self._compaction_count: int = 0
        # Phase 4-6 集成组件
        self._skill_executor = skill_executor  # SkillExecutor 实例
        self._rag_pipeline = rag_pipeline      # RAGPipeline 实例
        self._memory_manager = memory_manager   # FileMemory 实例

    def build(
        self,
        user_message: str,
        state: AgentState,
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> list[LLMMessage]:
        """
        组装完整 context（DESIGN.md §3.3）。

        组装顺序：
        1. cinstr: 基础系统提示 + 激活 Skill 指令
        2. cmem:   记忆索引（只有索引行，不含正文）
        3. ctools: 工具使用提示
        4. cstate: 当前任务状态（如有）
        5. cknow:  检索知识块（JIT 注入）
        6. history: 对话历史（按预算裁剪）
        7. cquery: 用户消息
        """
        messages: list[LLMMessage] = []

        # ── 1. cinstr: 基础系统提示 ─────────────────
        system_content = BASE_SYSTEM_PROMPT

        # ── 2. cinstr: Skills 注入（DESIGN.md §5）──
        # Layer 1 常驻摘要（所有可用 Skill 的 name+description）
        if self._skill_executor:
            layer1 = self._skill_executor.build_layer1_summary()
            if layer1:
                system_content += layer1

            # Layer 2 激活内容（完整指令）
            skill_content = self._skill_executor.build_skill_context(
                budget=self._budget.active_skills,
            )
            if skill_content:
                system_content += skill_content
        elif state.active_skills:
            # Fallback：直接列出名称（无 SkillExecutor 时）
            system_content += (
                "\n\n## 当前激活的 Skills\n"
                + "\n".join(f"- {s}" for s in state.active_skills)
            )

        # ── 3. cmem: 记忆索引注入（DESIGN.md §2.2）──
        if self._memory_manager:
            try:
                index = self._memory_manager.get_index()
                if index and index != "（记忆索引不可用）":
                    system_content += f"\n\n## 记忆索引\n{index}"
            except Exception as exc:
                logger.debug("记忆索引加载失败: %s", exc)

        # ── 4. cstate: 任务状态 ──────────────────────
        if state.task_summary:
            system_content += f"\n\n## 当前任务状态\n{state.task_summary}"

        # ── 5. cknow: 被动 RAG + 检索知识（DESIGN.md §4.1）──
        # 被动 RAG：静默检索记忆库
        if self._rag_pipeline and not state.retrieved_chunks:
            try:
                background = self._rag_pipeline.retrieve_context(user_message)
                if background:
                    state.retrieved_chunks = [background]
            except Exception as exc:
                logger.debug("被动 RAG 检索失败: %s", exc)

        if state.retrieved_chunks:
            system_content += "\n\n## 背景知识\n" + "\n---\n".join(
                state.retrieved_chunks
            )

        messages.append(LLMMessage(role="system", content=system_content))

        # ── 6. history: 对话历史（按预算裁剪）──────
        history = self._get_history_within_budget()
        messages.extend(history)

        # ── 7. cquery: 当前用户消息 ──────────────────
        messages.append(LLMMessage(role="user", content=user_message))

        return messages

    def append_user(self, content: str) -> None:
        """追加用户消息到历史。"""
        self._history.append(LLMMessage(role="user", content=content))

    def append_assistant(self, content: str) -> None:
        """追加助手回复到历史。"""
        self._history.append(LLMMessage(role="assistant", content=content))

    def append_assistant_tool_calls(
        self, tool_calls: list[ToolCallEvent]
    ) -> None:
        """追加助手的工具调用到历史。"""
        self._history.append(
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=tool_calls,
            )
        )

    def append_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        result: ToolResult,
    ) -> None:
        """追加工具结果到历史。"""
        self._history.append(
            LLMMessage(
                role="tool",
                content=result.to_context_string(),
                name=tool_name,
                tool_call_id=tool_call_id,
            )
        )

    @property
    def token_usage(self) -> int:
        """
        估算当前 token 使用量（粗略：4 字符 ≈ 1 token）。
        """
        total = len(BASE_SYSTEM_PROMPT) // 4
        for msg in self._history:
            total += len(msg.content) // 4
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    total += 20  # 工具调用元数据约 20 tokens
        return total

    def needs_compaction(self) -> bool:
        """检查是否需要 compaction。"""
        return self.token_usage > self._budget.total * self._agent_settings.compaction_threshold

    def compact(self) -> str:
        """
        Compaction 三阶段策略（DESIGN.md §2.1）：

        Step 1: 压缩 tool_result — 将长工具结果替换为摘要
        Step 2: 摘要早期对话 — 将前 N 轮压缩为一段摘要
        Step 3: 滑动窗口兜底 — 只保留最近 15 轮

        返回 compaction 摘要描述。
        """
        self._compaction_count += 1
        keep_recent = self._agent_settings.history_keep_recent

        if len(self._history) <= keep_recent:
            return "对话尚短，无需压缩"

        # Step 1: 压缩长工具结果
        for msg in self._history:
            if msg.role == "tool" and len(msg.content) > 500:
                # 截断工具结果，保留前 200 字符
                msg.content = (
                    msg.content[:200]
                    + "\n... [已压缩，使用 read_artifact 获取完整内容]"
                )

        # Step 2 & 3: 保留关键消息 + 最近 N 轮
        if len(self._history) > keep_recent:
            # 找出关键消息（含工具调用的轮次、用户约束指令）
            early = self._history[:-keep_recent]
            recent = self._history[-keep_recent:]

            # 将早期消息压缩为摘要
            summary_parts = []
            for msg in early:
                if msg.role == "user":
                    summary_parts.append(f"用户: {msg.content[:100]}")
                elif msg.role == "assistant" and msg.content:
                    summary_parts.append(f"助手: {msg.content[:100]}")
                elif msg.role == "tool":
                    summary_parts.append(f"工具结果: {msg.content[:80]}")

            compressed_summary = "\n".join(summary_parts[-10:])  # 最多保留 10 条摘要

            # 重建历史：一条摘要 + 最近轮次
            summary_msg = LLMMessage(
                role="user",
                content=f"[对话历史摘要 - 第 {self._compaction_count} 次压缩]\n{compressed_summary}",
            )
            self._history = [summary_msg] + recent

        compressed_count = len(self._history)
        return f"对话已压缩（第 {self._compaction_count} 次），当前保留 {compressed_count} 条消息"

    def _get_history_within_budget(self) -> list[LLMMessage]:
        """按 token 预算返回对话历史。"""
        budget = self._budget.history_budget
        result: list[LLMMessage] = []
        used = 0

        for msg in self._history:
            msg_tokens = len(msg.content) // 4 + 10  # 消息元数据约 10 tokens
            if used + msg_tokens > budget:
                break
            result.append(msg)
            used += msg_tokens

        return result

    @property
    def history_length(self) -> int:
        """返回当前历史消息数量。"""
        return len(self._history)

    def clear(self) -> None:
        """清空对话历史。"""
        self._history.clear()
        self._compaction_count = 0
