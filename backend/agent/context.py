from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.agent.state import AgentState
from backend.config import AgentSettings, TokenBudget
from backend.agent.attachment_policy import build_attachment_input_plan
from backend.llm.base import LLMMessage, ToolCallEvent
from backend.llm.response_cache import simple_chat_cache
from backend.tools.base import ToolResult
from backend.agent.claude_md import load_project_guidelines

logger = logging.getLogger(__name__)

CONTINUATION_REQUESTS = {
    "继续",
    "继续吧",
    "接着",
    "接着做",
    "继续做",
    "continue",
    "go on",
}

BASE_SYSTEM_PROMPT = """\
You are MiniCode, an AI-powered development assistant. You help users with software engineering tasks by taking action directly.

# Doing tasks
- You are an interactive agent. Use tools to investigate, implement, and verify — don't just suggest.
- Prefer editing existing files over creating new ones.
- Don't add features, refactor, or introduce abstractions beyond what the task requires.
- Do not reveal hidden chain-of-thought.
- Do not write conversational progress preambles before tool calls (for example "I'll continue", "I'll first check", or "Next I will"). Tool cards and structured progress events already show the process.
- Be concise. Final answers should summarize what changed and what was verified.

# Tool use
- Read files before modifying them. Understand context first.
- Prefer edit_file for targeted changes, write_file for full rewrites.
- Observe each tool result before deciding the next action. Adapt based on what you find.
- If a tool returns artifact_id, use read_artifact to access the full content when needed.
- Read-only tools (read_file, list_files, grep_files, glob_files, fuzzy_search, git_status, git_diff, git_log) can run in parallel.
- Write tools (write_file, edit_file, run_command, git_commit) must run sequentially.
- For complex or multi-step tasks, create a todo list with todo_write before substantial work and keep it updated as you progress.
- When a side investigation has a clear, bounded prompt and can run in an isolated context, delegate it with the task tool instead of forcing a global plan.
- Don't repeat identical tool calls. Don't re-request approval for auto-allowed tools.

# Verification
- After code changes, run the build or relevant tests to confirm correctness.
- If something fails, investigate the root cause rather than making blind patches.
- If an approach fails twice, step back and try a fundamentally different approach.

# Safety
- Consider reversibility before acting. Destructive operations need user confirmation.
- Don't introduce security vulnerabilities (injection, XSS, etc).
- When uncertain, use ask_user.

# Output style
- Respond in the user's language.
- Use Markdown. Code blocks with language tags.
- Keep responses short and direct. No filler, no narration of your thought process.
- After making changes, state what changed and why in one or two sentences.
"""


def _build_static_environment_info(workspace_root: Path | None = None) -> str:
    """构建不含时间戳的静态环境信息（可被 prompt cache 缓存）。"""
    cwd = workspace_root or Path.cwd()
    os_name = "Windows" if os.name == "nt" else (sys.platform or os.name)
    shell = os.environ.get("SHELL") or os.environ.get("COMSPEC") or "unknown"

    lines = [
        "## 环境信息",
        f"- 操作系统: {os_name}",
        f"- 工作目录: {cwd}",
        f"- Shell: {shell}",
    ]

    project_hints = _detect_project_type(cwd)
    if project_hints:
        lines.append(f"- 项目类型: {project_hints}")

    return "\n".join(lines)


def _build_dynamic_context() -> str:
    """构建每次调用都会变化的动态上下文（不参与 cache）。"""
    now = datetime.now(timezone.utc).astimezone()
    return f"当前时间: {now.strftime('%Y-%m-%d %H:%M %Z')}"


def _detect_project_type(cwd: Path) -> str:
    """检测项目类型，提供上下文给 Agent。"""
    markers: list[str] = []
    if (cwd / "package.json").exists():
        markers.append("Node.js")
    if (cwd / "tsconfig.json").exists():
        markers.append("TypeScript")
    if (cwd / "requirements.txt").exists() or (cwd / "pyproject.toml").exists():
        markers.append("Python")
    if (cwd / "Cargo.toml").exists():
        markers.append("Rust")
    if (cwd / "go.mod").exists():
        markers.append("Go")
    if (cwd / "pom.xml").exists() or (cwd / "build.gradle").exists():
        markers.append("Java")
    if (cwd / ".git").exists():
        markers.append("Git")
    if (cwd / "Dockerfile").exists() or (cwd / "docker-compose.yml").exists():
        markers.append("Docker")
    return ", ".join(markers) if markers else ""


class ContextBuilder:
    """Builds the active prompt context while keeping history compact."""

    def __init__(
        self,
        token_budget: TokenBudget | None = None,
        agent_settings: AgentSettings | None = None,
        skill_executor: Any | None = None,
        rag_pipeline: Any | None = None,
        memory_manager: Any | None = None,
        llm: Any | None = None,
        skill_manager: Any | None = None,
        vector_memory: Any | None = None,
    ) -> None:
        self._budget = token_budget or TokenBudget()
        self._agent_settings = agent_settings or AgentSettings()
        self._history: list[LLMMessage] = []
        self._history_token_estimates: list[int] = []
        self._history_tokens_total = 0
        self._persistent_notes: list[dict[str, str]] = []
        self._compaction_count = 0
        self._skill_executor = skill_executor
        self._skill_manager = skill_manager
        self._rag_pipeline = rag_pipeline
        self._memory_manager = memory_manager
        self._llm = llm
        self._vector_memory = vector_memory

    async def build(self, user_message: str, state: AgentState) -> list[LLMMessage]:
        messages: list[LLMMessage] = []
        system_content = BASE_SYSTEM_PROMPT

        # Static environment info (cacheable - no timestamp)
        workspace_root = None
        if hasattr(state, 'workspace_context') and state.workspace_context:
            workspace_root = getattr(state.workspace_context, 'root_path', None)
        system_content += "\n\n" + _build_static_environment_info(workspace_root)

        # Inject WorkspaceContext (项目上下文)
        if hasattr(state, 'workspace_context') and state.workspace_context:
            workspace_summary = state.workspace_context.get_project_summary()
            if workspace_summary:
                system_content += "\n\n" + workspace_summary

        # Inject AGENTS.md / CLAUDE.md project guidelines from the active workspace.
        project_guidelines = load_project_guidelines(workspace_root)
        if project_guidelines:
            system_content += project_guidelines

        if self._skill_manager:
            from backend.skills.executor import SkillExecutor

            executor = SkillExecutor(self._skill_manager)
            layer1 = executor.build_layer1_summary()
            if layer1:
                system_content += layer1
            if self._skill_manager.get_active_names():
                skill_content = executor.build_skill_context(
                    budget=self._budget.active_skills
                )
                if skill_content:
                    system_content += skill_content
        elif self._skill_executor:
            layer1 = self._skill_executor.build_layer1_summary()
            if layer1:
                system_content += layer1
            skill_content = self._skill_executor.build_skill_context(
                budget=self._budget.active_skills
            )
            if skill_content:
                system_content += skill_content
        elif state.active_skills:
            system_content += (
                "\n\n## 当前激活的 Skills\n"
                + "\n".join(f"- {skill}" for skill in state.active_skills)
            )

        if self._memory_manager:
            try:
                index = self._memory_manager.load_index()
                if index and "不可用" not in index:
                    system_content += f"\n\n## 记忆索引\n{index}"
            except Exception as exc:
                logger.debug("Failed to load memory index: %s", exc)

        if self._persistent_notes:
            note_blocks: list[str] = []
            for note in self._persistent_notes:
                content = str(note.get("content", "")).strip()
                if not content:
                    continue
                title = str(note.get("title") or "Persistent memory").strip()
                note_blocks.append(f"### {title}\n{content}")
            if note_blocks:
                system_content += "\n\n## Inherited Memory\n" + "\n\n".join(note_blocks)

        # Keep retrieval agentic: Codex/Claude Code-style loops let the model
        # request memory or document context with tools instead of silently
        # injecting passive RAG into every turn. Explicitly populated chunks are
        # still honored below for command/tool driven workflows.

        messages.append(LLMMessage(role="system", content=system_content))
        messages.extend(self._get_history_within_budget())

        attachment_plan = build_attachment_input_plan(state.attachments, llm=self._llm)

        if attachment_plan.text_hints:
            user_message = (
                f"{user_message}\n\n"
                "Attachment text fallback:\n"
                + "\n".join(attachment_plan.text_hints)
            )

        # Inject dynamic context into user message prefix
        # (timestamp, task state, RAG chunks — all change per-turn, kept out of system prompt for caching)
        dynamic_parts = [_build_dynamic_context()]
        if state.task_summary:
            dynamic_parts.append(f"任务状态: {state.task_summary}")
        if state.retrieved_chunks:
            dynamic_parts.append("背景知识:\n" + "\n---\n".join(state.retrieved_chunks))
        dynamic_prefix = " | ".join(dynamic_parts[:1])  # timestamp on first line
        context_blocks = dynamic_parts[1:]  # task + RAG as separate blocks

        user_content = f"[{dynamic_prefix}]"
        if context_blocks:
            user_content += "\n\n" + "\n\n".join(context_blocks)
        user_content += f"\n\n{user_message}"

        effective_user_message = self._build_effective_user_message(user_message, state)

        messages.append(
            LLMMessage(
                role="user",
                content=user_content.replace(user_message, effective_user_message, 1),
                images=attachment_plan.images,
                documents=attachment_plan.documents,
            )
        )
        return messages

    @staticmethod
    def _build_effective_user_message(user_message: str, state: AgentState) -> str:
        stripped = user_message.strip()
        if stripped.lower() not in CONTINUATION_REQUESTS:
            return user_message
        task_summary = (state.task_summary or "").strip()
        recent = []
        for tc in state.tool_calls[-5:]:
            output = (tc.tool_output or "").strip().replace("\n", " ")
            if len(output) > 180:
                output = f"{output[:180]}..."
            recent.append(f"- {tc.tool_name} {tc.tool_input} [{tc.status}]: {output}")
        details = []
        if task_summary:
            details.append(f"Previous task summary:\n{task_summary}")
        if recent:
            details.append("Recent tool results:\n" + "\n".join(recent))
        suffix = "\n\n".join(details) if details else "Use the current conversation context."
        return (
            "Continue the previous unfinished task. Do not ask whether to continue. "
            "Choose and execute the next concrete step, or provide a concise final result "
            "only if the task is already complete.\n\n"
            f"{suffix}"
        )

    def append_user(self, content: str) -> None:
        self._append_history_message(
            LLMMessage(role="user", content=str(content)),
            raw_content=content,
        )

    def append_assistant(self, content: str) -> None:
        self._append_history_message(
            LLMMessage(role="assistant", content=str(content)),
            raw_content=content,
        )

    def append_assistant_tool_calls(self, tool_calls: list[ToolCallEvent]) -> None:
        self._append_history_message(
            LLMMessage(role="assistant", content="", tool_calls=tool_calls)
        )

    def append_system_note(self, content: str) -> None:
        self._append_history_message(
            LLMMessage(role="system", content=str(content)),
            raw_content=content,
        )

    # MicroCompact: 可压缩的只读工具（参考 Claude Code microCompact.ts COMPACTABLE_TOOLS）
    _COMPACTABLE_TOOLS = frozenset({
        "read_file", "list_files", "grep_files", "glob_files", "fuzzy_search",
        "git_status", "git_diff", "git_log", "web_fetch", "web_search",
        "run_command", "task", "read_artifact", "go_to_definition",
        "find_references", "list_mcp_resources", "read_mcp_resource",
    })
    _MICRO_COMPACT_THRESHOLD = 1500  # 默认阈值
    # 模型主动请求的内容（read_file, git_diff）用更高阈值，避免丢失关键上下文
    _HIGH_THRESHOLD_TOOLS = frozenset({
        "read_file", "git_diff", "read_artifact", "go_to_definition",
    })
    _HIGH_COMPACT_THRESHOLD = 3500
    _READ_FILE_COMPACT_THRESHOLD = 12_000

    def append_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        result: ToolResult,
    ) -> None:
        content = result.to_context_string()

        # MicroCompact: 对大型只读工具结果进行即时截断压缩
        content = self._micro_compact(tool_name, content)

        self._append_history_message(
            LLMMessage(
                role="tool",
                content=content,
                name=tool_name,
                tool_call_id=tool_call_id,
            ),
            raw_content=content,
        )

    def _micro_compact(self, tool_name: str, content: str) -> str:
        """
        对可压缩工具的大型结果进行即时截断（参考 Claude Code microCompact.ts）。

        策略：
        - 仅对 _COMPACTABLE_TOOLS 中的工具生效
        - 模型主动请求的内容（read_file 等）用更高阈值，保留更多上下文
        - 超过阈值时保留首部和尾部内容，中间用摘要替代
        - 如果有 artifact_id 引用，保留引用信息
        """
        if tool_name not in self._COMPACTABLE_TOOLS:
            return content
        threshold = (
            self._READ_FILE_COMPACT_THRESHOLD
            if tool_name == "read_file"
            else self._HIGH_COMPACT_THRESHOLD
            if tool_name in self._HIGH_THRESHOLD_TOOLS
            else self._MICRO_COMPACT_THRESHOLD
        )
        if len(content) <= threshold:
            return content

        # 保留首尾各 40% 的阈值空间
        head_size = int(threshold * 0.4)
        tail_size = int(threshold * 0.4)
        omitted = len(content) - head_size - tail_size

        head = content[:head_size]
        tail = content[-tail_size:]
        return (
            f"{head}\n"
            f"... [已压缩 {omitted} 字符，如需完整内容请使用 read_artifact 或重新调用工具] ...\n"
            f"{tail}"
        )

    @property
    def token_usage(self) -> int:
        total = len(BASE_SYSTEM_PROMPT) // 4
        project_guidelines = load_project_guidelines()
        if project_guidelines:
            total += len(project_guidelines) // 4
        total += sum(len(str(note.get("content", ""))) // 4 for note in self._persistent_notes)
        return total + self._history_tokens_total

    def get_budget_snapshot(
        self,
        state: AgentState,
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        system_tokens = len(BASE_SYSTEM_PROMPT) // 4
        project_guidelines = load_project_guidelines()
        if project_guidelines:
            system_tokens += len(project_guidelines) // 4

        notes_tokens = sum(
            len(str(note.get("content", ""))) // 4 for note in self._persistent_notes
        )
        skills_tokens = 0
        rag_tokens = 0
        history_tokens = 0
        tools_tokens = 0

        executor = None
        if self._skill_manager:
            try:
                from backend.skills.executor import SkillExecutor

                executor = SkillExecutor(self._skill_manager)
            except Exception:
                executor = None
        elif self._skill_executor:
            executor = self._skill_executor

        if executor:
            try:
                skills_tokens = (
                    len(executor.build_layer1_summary())
                    + len(executor.build_skill_context(budget=self._budget.active_skills))
                ) // 4
            except Exception:
                skills_tokens = 0

        if state.retrieved_chunks:
            rag_tokens = len("\n---\n".join(state.retrieved_chunks)) // 4

        for index in self._get_history_within_budget_indices():
            history_tokens += self._history_token_estimates[index]

        if tool_schemas:
            tools_tokens = len(str(tool_schemas)) // 4

        used = (
            system_tokens
            + notes_tokens
            + skills_tokens
            + rag_tokens
            + history_tokens
            + tools_tokens
        )
        return {
            "used": used,
            "total": self._budget.total,
            "breakdown": {
                "system": system_tokens + notes_tokens,
                "skills": skills_tokens,
                "rag": rag_tokens,
                "history": history_tokens,
                "tools": tools_tokens,
            },
        }

    def needs_compaction(
        self,
        state: AgentState | None = None,
        *,
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> bool:
        threshold = self._effective_compaction_threshold(state)
        total_budget = max(self._budget.total, 1)
        history_budget = max(self._budget.history_budget, 1)
        snapshot = self.get_budget_snapshot(state or AgentState(user_message=""), tool_schemas=tool_schemas)

        # The active prompt can be dominated by system, skill, RAG, or memory
        # sections, so the overall context ratio is the primary compaction
        # signal. Keep the history-budget check as a guard before history
        # selection starts silently dropping older messages.
        return (
            int(snapshot.get("used", 0)) > total_budget * threshold
            or self._history_tokens_total > history_budget * threshold
        )

    async def compact(self, focus: str = "") -> str:
        self._compaction_count += 1
        keep_recent = self._agent_settings.history_keep_recent

        if len(self._history) <= keep_recent:
            return "对话较短，无需压缩"

        # 分层时间衰减压缩（参考 Claude Code timeBasedMCConfig.ts）
        # 越旧的消息压缩得越狠
        total = len(self._history)
        for idx, message in enumerate(self._history):
            if message.role != "tool" or not message.content:
                continue
            age_ratio = 1.0 - (idx / max(total, 1))  # 0=最新, 1=最旧
            if age_ratio > 0.7 and len(message.content) > 100:
                # 远期：仅保留工具名称和简短结果
                tool_name = message.name or "unknown"
                message.content = f"[{tool_name} 结果已清除]"
            elif age_ratio > 0.4 and len(message.content) > 200:
                # 中期：截断到 200 字符
                message.content = (
                    message.content[:200]
                    + "\n... [已压缩，使用 read_artifact 获取完整内容]"
                )
            elif len(message.content) > 500:
                # 近期：截断到 500 字符
                message.content = (
                    message.content[:500]
                    + "\n... [已压缩]"
                )

        early = self._history[:-keep_recent]
        recent = self._history[-keep_recent:]
        compressed_summary = await self._summarize_early(early, focus=focus)
        summary_message = LLMMessage(
            role="user",
            content=(
                f"[对话历史摘要 - 第 {self._compaction_count} 次压缩]\n"
                f"{compressed_summary}"
            ),
        )
        self._history = [summary_message] + recent
        self._rebuild_history_token_cache()
        return compressed_summary

    def full_compact(self) -> str:
        """Emergency local-only compaction for near-exhausted context windows."""
        self._compaction_count += 1
        keep_recent = max(4, min(self._agent_settings.history_keep_recent, 10))
        original_len = len(self._history)
        original_tokens = self._history_tokens_total

        if original_len <= keep_recent:
            for message in self._history:
                if message.role == "tool" and message.content and len(message.content) > 240:
                    message.content = (
                        f"[{message.name or 'tool'} result locally compacted; "
                        "rerun the tool or use read_artifact if the full result is needed.] "
                        f"{message.content[:160]}"
                    )
            self._rebuild_history_token_cache()
            saved = max(0, original_tokens - self._history_tokens_total)
            return f"Emergency local compaction trimmed old tool results and saved about {saved} tokens."

        early = self._history[:-keep_recent]
        recent = self._history[-keep_recent:]
        facts: list[str] = []
        for message in early[-12:]:
            text = (message.content or "").strip().replace("\n", " ")
            if not text:
                continue
            if message.role == "user":
                facts.append(f"User asked: {text[:100]}")
            elif message.role == "assistant":
                facts.append(f"Assistant replied: {text[:100]}")
            elif message.role == "tool":
                facts.append(f"Tool {message.name or 'unknown'} returned: {text[:90]}")

        summary = "\n".join(f"- {fact}" for fact in facts[-6:]) or "- Earlier conversation was removed to protect the context window."
        summary_message = LLMMessage(
            role="user",
            content=(
                f"[Emergency local context compaction #{self._compaction_count}]\n"
                "Older messages were locally summarized because the context window was nearly full.\n"
                f"{summary}"
            ),
        )
        self._history = [summary_message] + recent
        for message in self._history:
            if message.role == "tool" and message.content and len(message.content) > 360:
                message.content = (
                    f"[{message.name or 'tool'} result locally compacted; full output omitted.] "
                    f"{message.content[:260]}"
                )
        self._rebuild_history_token_cache()
        saved = max(0, original_tokens - self._history_tokens_total)
        return f"Emergency local compaction kept {len(self._history)}/{original_len} messages and saved about {saved} tokens."

    async def _summarize_early(self, early: list[LLMMessage], focus: str = "") -> str:
        summary_parts: list[str] = []
        for message in early:
            if message.role == "user":
                summary_parts.append(f"用户: {message.content[:100]}")
            elif message.role == "assistant" and message.content:
                summary_parts.append(f"助手: {message.content[:100]}")
            elif message.role == "tool":
                summary_parts.append(
                    f"工具结果({message.name}): {message.content[:80]}"
                )

        raw_text = "\n".join(summary_parts[-10:])
        focus_instruction = f"\n\nFocus: preserve details related to: {focus}" if focus else ""
        if self._llm is not None and raw_text:
            try:
                if self._memory_manager:
                    prompt = (
                        "请将以下对话历史进行压缩和信息提取(AutoCompact & MemDir)。\n"
                        "要求：\n"
                        "1. 在 <summary> 标签内输出简洁的对话摘要（保留关键决策和约束，不要超过200字）。\n"
                        "2. 如果其中含有关于 codebase 环境、架构、项目偏好的重要新上下文事实，\n"
                        "请在 <memdir> 标签内分别用一条条事实列出（每行一条，没有则留空）。我们将持久化它们。\n\n"
                        "格式示例：\n"
                        "<summary>这里是摘要</summary>\n"
                        "<memdir>\n- 事实1\n- 事实2\n</memdir>\n\n"
                        "对话历史：\n"
                        + raw_text
                        + focus_instruction
                    )
                else:
                    prompt = (
                        "请将以下对话历史压缩成简洁摘要，保留关键决策、用户约束和重要结论，\n"
                        "不要超过 200 字：\n\n"
                        + raw_text
                        + focus_instruction
                    )

                output = await simple_chat_cache.simple_chat(
                    self._llm,
                    [LLMMessage(role="user", content=prompt)],
                )
                if output:
                    if self._memory_manager and ("<summary>" in output or "<memdir>" in output):
                        import re
                        summary_match = re.search(r"<summary>(.*?)</summary>", output, re.DOTALL)
                        memdir_match = re.search(r"<memdir>(.*?)</memdir>", output, re.DOTALL)

                        summary = summary_match.group(1).strip() if summary_match else output
                        memdir_text = memdir_match.group(1).strip() if memdir_match else ""

                        if memdir_text:
                            lines_fact = [line.strip("- *") for line in memdir_text.split("\n") if line.strip("- *")]
                            for line_fact in lines_fact:
                                if line_fact:
                                    self._memory_manager.remember(line_fact, tags=["autocompact", "memdir"], importance=3)
                                    logger.info("AutoCompact extracted MemDir fact: %s", line_fact)

                            v_mem = getattr(self._memory_manager, "_vector_memory", None)
                            if v_mem and hasattr(v_mem, "flush"):
                                v_mem.flush()
                        return summary
                    else:
                        return output
            except Exception as exc:
                logger.debug("LLM summarization failed, using fallback: %s", exc)
        return raw_text

    def _get_history_within_budget(self) -> list[LLMMessage]:
        return [self._history[index] for index in self._get_history_within_budget_indices()]

    @property
    def history_length(self) -> int:
        return len(self._history)

    def clear(self) -> None:
        self._history.clear()
        self._history_token_estimates.clear()
        self._history_tokens_total = 0
        self._persistent_notes.clear()
        self._compaction_count = 0

    def export_snapshot(self) -> dict[str, Any]:
        history: list[dict[str, Any]] = []
        for message in self._history:
            content = message.content
            if message.role == "tool" and content and len(content) > 1200:
                content = (
                    f"{content[:700]}\n"
                    f"... [snapshot truncated {len(content) - 1000} chars; use artifact/read_artifact for full output] ...\n"
                    f"{content[-300:]}"
                )
            elif len(content) > 4000:
                content = f"{content[:2800]}\n... [snapshot truncated {len(content) - 2800} chars] ..."
            history.append(
                {
                    "role": message.role,
                    "content": content,
                    "name": message.name,
                    "tool_call_id": message.tool_call_id,
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "arguments": tool_call.arguments,
                        }
                        for tool_call in (message.tool_calls or [])
                    ],
                }
            )
        return {
            "history": history,
            "persistent_notes": [dict(note) for note in self._persistent_notes],
            "compaction_count": self._compaction_count,
        }

    def load_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        self.clear()
        if not snapshot:
            return

        self._load_snapshot_metadata(snapshot)
        self._history = self.deserialize_snapshot_history(snapshot.get("history", []))
        self._rebuild_history_token_cache()

    def load_snapshot_partial(
        self,
        snapshot: dict[str, Any] | None,
        *,
        recent_history_count: int = 20,
    ) -> list[dict[str, Any]]:
        self.clear()
        if not snapshot:
            return []

        self._load_snapshot_metadata(snapshot)
        raw_history = list(snapshot.get("history", []))
        if recent_history_count <= 0:
            recent_history: list[dict[str, Any]] = []
            pending_history = raw_history
        else:
            recent_history = raw_history[-recent_history_count:]
            pending_history = raw_history[:-recent_history_count]

        self._history = self.deserialize_snapshot_history(recent_history)
        self._rebuild_history_token_cache()
        return pending_history

    def prepend_history_messages(self, messages: list[LLMMessage]) -> None:
        if not messages:
            return
        self._history = list(messages) + self._history
        self._rebuild_history_token_cache()

    @staticmethod
    def deserialize_snapshot_history(raw_history: list[dict[str, Any]]) -> list[LLMMessage]:
        parsed_history: list[LLMMessage] = []
        for raw in raw_history:
            tool_calls = raw.get("tool_calls") or None
            parsed_tool_calls = None
            if tool_calls:
                parsed_tool_calls = [
                    ToolCallEvent(
                        id=str(tool_call["id"]),
                        name=str(tool_call["name"]),
                        arguments=dict(tool_call.get("arguments") or {}),
                    )
                    for tool_call in tool_calls
                ]
            parsed_history.append(
                LLMMessage(
                    role=str(raw.get("role", "user")),
                    content=str(raw.get("content", "")),
                    name=raw.get("name"),
                    tool_call_id=raw.get("tool_call_id"),
                    tool_calls=parsed_tool_calls,
                )
            )
        return parsed_history

    def _load_snapshot_metadata(self, snapshot: dict[str, Any]) -> None:
        self._persistent_notes = [
            {
                "kind": str(note.get("kind", "")),
                "title": str(note.get("title", "")),
                "content": str(note.get("content", "")),
            }
            for note in snapshot.get("persistent_notes", [])
            if str(note.get("content", "")).strip()
        ]
        self._compaction_count = int(snapshot.get("compaction_count", 0))

    def _append_history_message(
        self,
        message: LLMMessage,
        *,
        raw_content: Any | None = None,
    ) -> None:
        self._history.append(message)
        estimate = self._estimate_message_tokens(
            raw_content if raw_content is not None else message.content,
            message.tool_calls,
        )
        self._history_token_estimates.append(estimate)
        self._history_tokens_total += estimate

    def _rebuild_history_token_cache(self) -> None:
        self._history_token_estimates = [
            self._estimate_message_tokens(message.content, message.tool_calls)
            for message in self._history
        ]
        self._history_tokens_total = sum(self._history_token_estimates)

    def _get_history_within_budget_indices(self) -> list[int]:
        budget = self._budget.history_budget
        selected: list[int] = []
        used = 0

        for index in range(len(self._history) - 1, -1, -1):
            message_tokens = self._history_token_estimates[index] + 10
            if used + message_tokens > budget:
                break
            selected.append(index)
            used += message_tokens

        selected.reverse()
        return selected

    def _effective_compaction_threshold(self, state: AgentState | None = None) -> float:
        threshold = self._agent_settings.compaction_threshold
        if state is not None and state.iterations < 3:
            threshold = max(threshold, 0.85)
        if self._compaction_count > 0:
            threshold = min(threshold, 0.70)
        if state is not None and len(state.tool_calls) > 10:
            threshold = min(threshold, 0.65)
        return threshold

    @staticmethod
    def _estimate_message_tokens(
        content: Any,
        tool_calls: list[ToolCallEvent] | None = None,
    ) -> int:
        return ContextBuilder._estimate_content_tokens(content) + (len(tool_calls or []) * 20)

    @staticmethod
    def _estimate_content_tokens(content: Any) -> int:
        try:
            content_length = len(content)
        except TypeError:
            content_length = len(str(content))
        return content_length // 4
