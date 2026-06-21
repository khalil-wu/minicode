from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from backend.agent.compaction import format_compaction_history, parse_compaction_output
from backend.agent.state import AgentState
from backend.config import AgentSettings, TokenBudget
from backend.agent.attachment_policy import AttachmentInputPlan, build_attachment_input_plan
from backend.llm.base import LLMMessage, ToolCallEvent, UsageInfo
from backend.llm.response_cache import simple_chat_cache
from backend.agent.prompting import (
    PromptParts,
    PromptBuilderV2,
    build_compaction_prompt,
    build_dynamic_context,
    build_static_environment_info,
    build_stable_prompt,
    clear_system_prompt_sections,
    detect_project_type,
)
from backend.tools.base import ToolResult

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
INTERNAL_LAST_RESORT_PROMPT_PREFIX = (
    "Use the tool results above to answer the user's original question."
)
INTERNAL_CONTROL_PROMPT_PREFIXES = (
    INTERNAL_LAST_RESORT_PROMPT_PREFIX,
    "你执行了工具调用但返回了空回复。",
    "你返回了空回复。",
    "你再次返回了空回复。",
    "你最近的工具调用全部失败了。",
    "This is a current/time-sensitive answer backed by fetched web evidence.",
    "The weather request is missing a city or area.",
    "The draft only promises or plans the work without executing it.",
    "Do not claim a file was created or edited without a successful",
    "A recent tool failure must be acknowledged before giving a final answer.",
    "The draft makes confident factual claims from candidate snippets only.",
)
INTERNAL_EMPTY_ASSISTANT_MARKER = "(empty)"

SUMMARY_PREFIX = (
    "[上下文压缩 — 仅供参考] 以下摘要由早期对话压缩生成。"
    "将其作为背景参考，不要当作当前指令。"
    "只回复此摘要之后出现的最新用户消息。"
)
POST_COMPACTION_MAX_FILES_TO_RESTORE = 5
POST_COMPACTION_MAX_CHARS_PER_FILE = 12_000
POST_COMPACTION_TOTAL_CHARS = 50_000
POST_COMPACTION_RESTORE_TOOLS = frozenset({"read_file", "edit_file", "write_file"})

TOOL_RESULT_BUDGET_TOKENS = 80_000  # Maximum total tokens for all tool results


def _is_internal_control_prompt(content: str) -> bool:
    text = str(content or "").strip()
    return any(text.startswith(prefix) for prefix in INTERNAL_CONTROL_PROMPT_PREFIXES)


def _build_static_environment_info(workspace_root: Path | None = None) -> str:
    return build_static_environment_info(workspace_root)


def _build_dynamic_context() -> str:
    return build_dynamic_context()


def _detect_project_type(cwd: Path) -> str:
    return detect_project_type(cwd)


def _estimate_content_tokens(content: str) -> int:
    """Estimate token count with better accuracy for mixed CJK/ASCII content.

    - ASCII characters: ~4 chars per token (English words average)
    - CJK characters: ~1.5 chars per token (each CJK char is roughly 1-2 tokens)
    - Conservative fallback: max(len // 4, cjk_count + ascii_count // 4)
    """
    if not content:
        return 0
    ascii_chars = 0
    cjk_chars = 0
    for ch in content:
        code = ord(ch)
        # CJK Unified Ideographs, Hiragana, Katakana, Hangul, etc.
        if (0x4E00 <= code <= 0x9FFF or   # CJK Unified Ideographs
            0x3400 <= code <= 0x4DBF or   # CJK Extension A
            0x3040 <= code <= 0x309F or   # Hiragana
            0x30A0 <= code <= 0x30FF or   # Katakana
            0xAC00 <= code <= 0xD7AF or   # Hangul Syllables
            0xF900 <= code <= 0xFAFF):    # CJK Compatibility Ideographs
            cjk_chars += 1
        else:
            ascii_chars += 1
    # CJK chars: ~1.5 tokens per char; ASCII: ~4 chars per token
    return max(1, int(cjk_chars * 1.5 + ascii_chars / 4))


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
        self._cached_guidelines: str = ""
        self._cached_guidelines_signature: str = ""
        self._cached_guidelines_ts: float = 0.0
        self._last_actual_prompt_tokens = 0

    _GUIDELINES_CACHE_TTL = 10.0  # seconds

    def _get_project_guidelines(self, workspace_root: Path | None = None) -> str:
        """Return cached project guidelines, reloading only when TTL expires.

        Avoids repeated Path.stat() syscalls from load_project_guidelines()
        by short-circuiting within the TTL window.
        """
        import time
        now = time.monotonic()
        signature = str(workspace_root or "")
        if (
            self._cached_guidelines_ts > 0
            and self._cached_guidelines_signature == signature
            and (now - self._cached_guidelines_ts) < self._GUIDELINES_CACHE_TTL
        ):
            return self._cached_guidelines
        from backend.agent.claude_md import load_project_guidelines
        self._cached_guidelines = load_project_guidelines(workspace_root)
        self._cached_guidelines_signature = signature
        self._cached_guidelines_ts = now
        return self._cached_guidelines

    def _build_skill_context(self, state: AgentState) -> str:
        parts: list[str] = []
        if self._skill_manager:
            from backend.skills.executor import SkillExecutor

            executor = SkillExecutor(self._skill_manager)
            layer1 = executor.build_layer1_summary()
            if layer1:
                parts.append(layer1)
            if self._skill_manager.get_active_names():
                skill_content = executor.build_skill_context(
                    budget=self._budget.active_skills
                )
                if skill_content:
                    parts.append(skill_content)
        elif self._skill_executor:
            layer1 = self._skill_executor.build_layer1_summary()
            if layer1:
                parts.append(layer1)
            skill_content = self._skill_executor.build_skill_context(
                budget=self._budget.active_skills
            )
            if skill_content:
                parts.append(skill_content)
        elif state.active_skills:
            parts.append(
                "\n\n## Active Skills\n"
                + "\n".join(f"- {skill}" for skill in state.active_skills)
            )
        return "\n\n".join(part for part in parts if part.strip())

    def _build_memory_context(self) -> str:
        if not self._memory_manager:
            return ""
        try:
            index = self._memory_manager.load_index()
            if index and "不可用" not in index:
                return f"## Memory Index\n{index}"
        except Exception as exc:
            logger.debug("Failed to load memory index: %s", exc)
        return ""

    def _build_persistent_context(self) -> str:
        if not self._persistent_notes:
            return ""
        note_blocks: list[str] = []
        for note in self._persistent_notes:
            content = str(note.get("content", "")).strip()
            if not content:
                continue
            title = str(note.get("title") or "Persistent memory").strip()
            note_blocks.append(f"### {title}\n{content}")
        if not note_blocks:
            return ""
        return "## Inherited Memory\n" + "\n\n".join(note_blocks)

    async def build(self, user_message: str, state: AgentState) -> list[LLMMessage]:
        messages: list[LLMMessage] = []

        workspace_root = self._workspace_root_for_state(state)
        prompt_parts = self._build_prompt_parts(state, workspace_root)
        system_content = prompt_parts.render_system()

        # ── End of system prompt ────────────────────────────────────────────────
        # Keep retrieval agentic: Codex/Claude Code-style loops let the model
        # request memory or document context with tools instead of silently
        # injecting passive RAG into every turn. Explicitly populated chunks are
        # still honored below for command/tool driven workflows.

        messages.append(LLMMessage(role="system", content=system_content))
        history = self._get_history_within_budget()
        if (
            history
            and history[-1].role == "user"
            and not history[-1].tool_call_id
            and history[-1].content.strip() == str(user_message).strip()
        ):
            # The loop already appended this user turn to history; drop it here
            # so the trailing volatile user turn below is the only copy sent.
            history = history[:-1]
        messages.extend(history)

        attachment_plan = build_attachment_input_plan(state.attachments, llm=self._llm)
        user_message = self._with_attachment_text_fallback(user_message, attachment_plan)

        messages.append(
            LLMMessage(
                role="user",
                content=self._build_user_turn_content(user_message, state, prompt_parts),
                images=attachment_plan.images,
                documents=attachment_plan.documents,
            )
        )
        return messages

    @staticmethod
    def _workspace_root_for_state(state: AgentState) -> Path | None:
        if hasattr(state, "workspace_context") and state.workspace_context:
            return getattr(state.workspace_context, "root_path", None)
        return None

    def _build_prompt_parts(
        self,
        state: AgentState,
        workspace_root: Path | None,
    ) -> PromptParts:
        return PromptBuilderV2().build(
            state=state,
            workspace_root=workspace_root,
            project_guidelines=self._get_project_guidelines(workspace_root),
            skill_context=self._build_skill_context(state),
            memory_context=self._build_memory_context(),
            persistent_context=self._build_persistent_context(),
        )

    @staticmethod
    def _with_attachment_text_fallback(
        user_message: str,
        attachment_plan: AttachmentInputPlan,
    ) -> str:
        if not attachment_plan.text_hints:
            return user_message
        return (
            f"{user_message}\n\n"
            "Attachment text fallback:\n"
            + "\n".join(attachment_plan.text_hints)
        )

    @staticmethod
    def _build_user_turn_content(
        user_message: str,
        state: AgentState,
        prompt_parts: PromptParts,
    ) -> str:
        volatile_prefix = prompt_parts.render_volatile_prefix()
        user_content = f"{volatile_prefix}\n\n{user_message}" if volatile_prefix else user_message
        effective_user_message = ContextBuilder._build_effective_user_message(user_message, state)
        return user_content.replace(user_message, effective_user_message, 1)

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
        content_text = str(content)
        previous = self._history[-1] if self._history else None
        if (
            previous is not None
            and previous.role == "user"
            and content_text.strip()
            and content_text.strip() == previous.content.strip()
        ):
            return
        self._append_history_message(
            LLMMessage(role="user", content=content_text),
            raw_content=content,
        )

    def append_assistant(self, content: str) -> None:
        self._append_history_message(
            LLMMessage(role="assistant", content=str(content)),
            raw_content=content,
        )

    def append_assistant_tool_calls(self, tool_calls: list[ToolCallEvent], content: str = "") -> None:
        """Append assistant message with tool_calls. Optionally preserve preceding text."""
        self._append_history_message(
            LLMMessage(role="assistant", content=content, tool_calls=tool_calls)
        )

    def reconcile_dangling_tool_calls(self) -> int:
        """Keep tool messages valid for OpenAI-compatible providers.

        Tool result messages are only legal as replies to the immediately
        preceding assistant message's tool_calls. Interrupted streams and older
        snapshots can leave either dangling assistant tool_calls or orphan tool
        messages. Insert placeholders for the former and drop the latter before
        the next provider request.

        Returns the number of placeholders inserted.
        """

        def make_placeholder(call_id: str, tool_name: str) -> LLMMessage:
            return LLMMessage(
                role="tool",
                content=(
                    f"[Tool call '{tool_name}' did not complete. "
                    "Do not retry the same call; use the information you already have or try a different approach.]"
                ),
                name=tool_name,
                tool_call_id=call_id,
            )

        repaired: list[LLMMessage] = []
        pending_ids: dict[str, str] = {}
        pending_order: list[str] = []
        inserted = 0
        dropped = 0

        def flush_pending() -> None:
            nonlocal inserted
            if not pending_order:
                return
            for call_id in list(pending_order):
                tool_name = pending_ids.get(call_id, "unknown")
                repaired.append(make_placeholder(call_id, tool_name))
                inserted += 1
                logger.debug(
                    "Synthesized placeholder for dangling tool_call_id=%s (%s)",
                    call_id,
                    tool_name,
                )
            pending_ids.clear()
            pending_order.clear()

        for msg in self._history:
            if msg.role == "assistant":
                flush_pending()
                repaired.append(msg)
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        pending_ids[tc.id] = tc.name
                        pending_order.append(tc.id)
                continue

            if msg.role == "tool":
                call_id = str(msg.tool_call_id or "").strip()
                if call_id and call_id in pending_ids:
                    repaired.append(msg)
                    pending_ids.pop(call_id, None)
                    pending_order = [pending for pending in pending_order if pending != call_id]
                else:
                    dropped += 1
                    logger.debug("Dropped orphan tool message tool_call_id=%s", call_id or "<missing>")
                continue

            flush_pending()
            repaired.append(msg)

        flush_pending()

        if inserted or dropped:
            self._history = repaired
            self._rebuild_history_token_cache()

        return inserted

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
        "find_references", "write_file", "edit_file",
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
        total = len(build_stable_prompt()) // 4
        project_guidelines = self._get_project_guidelines()
        if project_guidelines:
            total += _estimate_content_tokens(project_guidelines)
        total += sum(_estimate_content_tokens(str(note.get("content", ""))) for note in self._persistent_notes)
        estimated = total + self._history_tokens_total
        return max(estimated, self._last_actual_prompt_tokens)

    def record_actual_usage(self, usage: UsageInfo | None) -> None:
        """Fold provider-reported prompt usage back into budget checks.

        Character estimates are still needed before the first request and after
        compaction, but real provider counts are a better floor once available.
        """
        if usage is None:
            return
        observed = int(getattr(usage, "input_tokens", 0) or 0)
        cached = int(getattr(usage, "cache_creation_input_tokens", 0) or 0) + int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )
        if observed <= 0 and cached > 0:
            observed = cached
        if observed > 0:
            self._last_actual_prompt_tokens = observed

    def apply_tool_result_budget(self, max_tokens: int | None = None) -> int:
        """Enforce a global budget on tool result tokens.

        Truncates the oldest tool results first (time-decay order) until the total
        is within budget. Recent results (last 5) are never truncated.

        Returns the number of results truncated.
        """
        budget = (
            max_tokens
            or getattr(self, "TOKEN_BUDGET_TOOL_RESULTS", None)
            or TOOL_RESULT_BUDGET_TOKENS
        )

        # Collect tool result messages with their indices
        tool_results: list[tuple[int, int, str]] = []
        for i, msg in enumerate(self._history):
            if msg.role == "tool":
                content_str = str(msg.content) if msg.content else ""
                tokens = len(content_str) // 4  # Rough token estimate
                tool_results.append((i, tokens, content_str))

        if not tool_results:
            return 0

        total_tokens = sum(t[1] for t in tool_results)
        if total_tokens <= budget:
            return 0

        # Keep the last 5 results untouched, truncate from oldest
        keep_recent = 5
        truncatable = tool_results[:-keep_recent] if len(tool_results) > keep_recent else []

        truncated = 0
        for idx, tokens, content in truncatable:
            if total_tokens <= budget:
                break
            # Replace content with a summary
            summary = f"[Tool result truncated — was {len(content)} chars]"
            self._history[idx] = LLMMessage(
                role="tool",
                content=summary,
                name=self._history[idx].name,
                tool_call_id=self._history[idx].tool_call_id,
            )
            total_tokens -= tokens
            total_tokens += len(summary) // 4
            truncated += 1

        if truncated > 0:
            self._rebuild_history_token_cache()
            logger.info(
                "[ToolBudget] Truncated %d/%d tool results (saved ~%d tokens)",
                truncated,
                len(tool_results),
                budget,
            )

        return truncated

    def get_budget_snapshot(
        self,
        state: AgentState,
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        workspace_root = self._workspace_root_for_state(state)
        system_tokens = len(build_stable_prompt(workspace_root)) // 4
        project_guidelines = self._get_project_guidelines(workspace_root)
        if project_guidelines:
            system_tokens += _estimate_content_tokens(project_guidelines)
        tool_runtime_guidance = str(
            getattr(state, "tool_runtime_guidance", "")
            or getattr(state, "harness_guidance", "")
            or ""
        )
        if tool_runtime_guidance:
            system_tokens += _estimate_content_tokens(tool_runtime_guidance)

        notes_tokens = sum(
            _estimate_content_tokens(str(note.get("content", ""))) for note in self._persistent_notes
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
        observed_actual = self._last_actual_prompt_tokens
        if observed_actual > used:
            used = observed_actual
        return {
            "used": used,
            "total": self._budget.total,
            "breakdown": {
                "system": system_tokens + notes_tokens,
                "skills": skills_tokens,
                "rag": rag_tokens,
                "history": history_tokens,
                "tools": tools_tokens,
                "observed_actual": observed_actual,
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

    def _restore_recent_files_after_compaction(self, state: AgentState | None = None) -> None:
        if state is None:
            return
        workspace_root = self._workspace_root_for_state(state)
        if workspace_root is None:
            return

        try:
            root = Path(workspace_root).resolve()
        except OSError:
            return

        restored_blocks: list[str] = []
        used_chars = 0
        for path in self._recent_workspace_file_paths(state, root):
            block = self._build_restored_file_block(path, root)
            if not block:
                continue
            if used_chars + len(block) > POST_COMPACTION_TOTAL_CHARS:
                break
            restored_blocks.append(block)
            used_chars += len(block)

        self._persistent_notes[:] = [
            note for note in self._persistent_notes
            if note.get("kind") != "post_compaction_restore"
        ]
        if not restored_blocks:
            return
        self._persistent_notes.append(
            {
                "kind": "post_compaction_restore",
                "title": "Post-compaction restored file context",
                "content": "\n\n".join(restored_blocks),
            }
        )

    def _recent_workspace_file_paths(self, state: AgentState, root: Path) -> list[Path]:
        paths: list[Path] = []
        seen: set[str] = set()
        for record in reversed(state.tool_calls):
            if record.status != "success" or record.tool_name not in POST_COMPACTION_RESTORE_TOOLS:
                continue
            raw_path = self._tool_record_path(record.tool_input)
            if not raw_path:
                continue
            try:
                candidate = Path(raw_path)
                resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            key = resolved.as_posix().lower()
            if key in seen or not resolved.is_file():
                continue
            seen.add(key)
            paths.append(resolved)
            if len(paths) >= POST_COMPACTION_MAX_FILES_TO_RESTORE:
                break
        return paths

    @staticmethod
    def _tool_record_path(args: dict[str, Any]) -> str:
        for key in ("file_path", "path", "target", "filename"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _build_restored_file_block(path: Path, root: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return ""
        rel = path.relative_to(root).as_posix()
        lines = text.splitlines()
        numbered: list[str] = []
        used = 0
        for index, line in enumerate(lines, 1):
            next_line = f"{index}: {line}"
            if used + len(next_line) + 1 > POST_COMPACTION_MAX_CHARS_PER_FILE:
                break
            numbered.append(next_line)
            used += len(next_line) + 1
        if len(numbered) < len(lines):
            numbered.append(f"... [{len(lines) - len(numbered)} lines omitted after post-compaction restore limit]")
        content = "\n".join(numbered)
        return f"### {rel}\n```text\n{content}\n```"

    async def compact(self, focus: str = "", restore_state: AgentState | None = None) -> str:
        """Time-decay compaction: compress old history, preserve recent context.

        Creates new compacted messages instead of mutating originals in-place
        (Hermes pattern: never irreversibly destroy information).
        """
        clear_system_prompt_sections()
        self._compaction_count += 1
        keep_recent = self._agent_settings.history_keep_recent

        if len(self._history) <= keep_recent:
            return "对话较短，无需压缩"

        total = len(self._history)

        # Build compacted versions of old messages — create new objects,
        # do NOT mutate the originals in-place.
        early_messages: list[LLMMessage] = []
        for idx, message in enumerate(self._history[:-keep_recent]):
            age_ratio = 1.0 - (idx / max(total, 1))  # 0=最新, 1=最旧

            if message.role == "tool" and message.content:
                tool_name = message.name or "unknown"
                content_len = len(message.content)

                if age_ratio > 0.7 and content_len > 100:
                    # Far history: keep tool name + brief summary
                    compacted = f"[{tool_name} 结果已压缩]"
                    # Preserve artifact reference if present
                    if "artifact" in message.content[:200].lower():
                        compacted += "（如需完整内容请使用 read_artifact）"
                    early_messages.append(LLMMessage(
                        role=message.role,
                        content=compacted,
                        name=message.name,
                        tool_call_id=message.tool_call_id,
                    ))
                elif age_ratio > 0.4 and content_len > 200:
                    # Mid history: truncate to 400 chars (enough for key info)
                    compacted = (
                        message.content[:400]
                        + "\n... [已压缩；使用 read_artifact 查看完整内容]"
                    )
                    early_messages.append(LLMMessage(
                        role=message.role,
                        content=compacted,
                        name=message.name,
                        tool_call_id=message.tool_call_id,
                    ))
                elif content_len > 800:
                    # Near history: truncate to 800 chars (preserve most context)
                    compacted = (
                        message.content[:800]
                        + "\n... [已压缩]"
                    )
                    early_messages.append(LLMMessage(
                        role=message.role,
                        content=compacted,
                        name=message.name,
                        tool_call_id=message.tool_call_id,
                    ))
                else:
                    early_messages.append(message)
            else:
                early_messages.append(message)

        compressed_summary = await self._summarize_early(early_messages, focus=focus)
        summary_message = LLMMessage(
            role="user",
            content=(
                f"{SUMMARY_PREFIX}\n\n"
                f"[对话历史摘要 - 第 {self._compaction_count} 次压缩]\n"
                f"{compressed_summary}"
            ),
        )
        recent = self._history[-keep_recent:]
        self._history = [summary_message] + recent
        self._rebuild_history_token_cache()
        self._last_actual_prompt_tokens = 0
        self._restore_recent_files_after_compaction(restore_state)
        return compressed_summary

    async def full_compact(self, restore_state: AgentState | None = None) -> str:
        """Emergency compaction for near-exhausted context windows.

        When the context window is ≥95% full, this method fires as a last resort.
        It first tries LLM-based summarization (same as compact()); if the LLM call
        fails or times out, it falls back to rule-based fact extraction.
        """
        import asyncio as _asyncio

        clear_system_prompt_sections()
        self._compaction_count += 1
        keep_recent = max(2, min(self._agent_settings.history_keep_recent, 6))
        original_len = len(self._history)
        original_tokens = self._history_tokens_total

        if original_len <= keep_recent:
            # Short history: rule-based truncation (no LLM needed)
            new_history: list[LLMMessage] = []
            for message in self._history:
                if message.role == "tool" and message.content and len(message.content) > 240:
                    compacted = (
                        f"[{message.name or 'tool'} 结果已压缩；"
                        "如需完整结果请重新调用或使用 read_artifact] "
                        f"{message.content[:160]}"
                    )
                    new_history.append(LLMMessage(
                        role=message.role,
                        content=compacted,
                        name=message.name,
                        tool_call_id=message.tool_call_id,
                    ))
                else:
                    new_history.append(message)
            self._history = new_history
            self._rebuild_history_token_cache()
            self._restore_recent_files_after_compaction(restore_state)
            saved = max(0, original_tokens - self._history_tokens_total)
            return f"紧急压缩：截断了旧工具结果，节省约 {saved} tokens"

        early = self._history[:-keep_recent]
        recent = self._history[-keep_recent:]

        # Try LLM-based summarization first (30s timeout)
        llm_summary = ""
        if self._llm is not None:
            try:
                llm_summary = await _asyncio.wait_for(
                    self._summarize_early(early, focus="紧急压缩，保留关键事实"),
                    timeout=30.0,
                )
            except (_asyncio.TimeoutError, Exception) as exc:
                logger.debug("Emergency LLM summarization failed, using fallback: %s", exc)

        # Fall back to rule-based extraction if LLM failed
        if not llm_summary:
            facts: list[str] = []
            for message in early[-12:]:
                text = (message.content or "").strip().replace("\n", " ")
                if not text:
                    continue
                if message.role == "user":
                    facts.append(f"用户问了：{text[:100]}")
                elif message.role == "assistant":
                    facts.append(f"助手回答了：{text[:100]}")
                elif message.role == "tool":
                    facts.append(f"工具 {message.name or 'unknown'} 返回了：{text[:90]}")
            llm_summary = (
                "\n".join(f"- {fact}" for fact in facts[-6:])
                or "- 早期对话已被移除以保护上下文窗口。"
            )

        summary_content = (
            f"{SUMMARY_PREFIX}\n\n"
            f"[紧急上下文压缩 第 {self._compaction_count} 次]\n"
            "因上下文窗口接近满载，较早的消息已被压缩为摘要。\n"
            f"{llm_summary}"
        )
        summary_message = LLMMessage(role="user", content=summary_content)

        # Build compacted history from [summary + recent] without mutating originals
        compacted_history: list[LLMMessage] = []
        prefix_block = f"{SUMMARY_PREFIX}\n\n"

        # Truncate summary if too long
        if len(summary_message.content) > 900:
            body = summary_message.content[len(prefix_block):]
            compacted_history.append(LLMMessage(
                role=summary_message.role,
                content=f"{prefix_block}{body[:700]}... [已截断]",
                name=summary_message.name,
                tool_call_id=summary_message.tool_call_id,
            ))
        else:
            compacted_history.append(summary_message)

        # Truncate recent messages if needed
        for message in recent:
            if message.role in {"user", "assistant"} and message.content and len(message.content) > 220:
                compacted_history.append(LLMMessage(
                    role=message.role,
                    content=f"{message.content[:180]}... [已压缩]",
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                ))
            elif message.role == "tool" and message.content and len(message.content) > 360:
                compacted_history.append(LLMMessage(
                    role=message.role,
                    content=(
                        f"[{message.name or 'tool'} 结果已压缩；完整输出已省略] "
                        f"{message.content[:260]}"
                    ),
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                ))
            else:
                compacted_history.append(message)

        self._history = compacted_history
        self._rebuild_history_token_cache()
        self._last_actual_prompt_tokens = 0
        self._restore_recent_files_after_compaction(restore_state)
        saved = max(0, original_tokens - self._history_tokens_total)
        return f"紧急压缩保留了 {len(self._history)}/{original_len} 条消息，节省约 {saved} tokens"

    async def _summarize_early(self, early: list[LLMMessage], focus: str = "") -> str:
        raw_text = format_compaction_history(early)
        if self._llm is not None and raw_text:
            try:
                prompt = build_compaction_prompt(
                    raw_text,
                    focus=focus,
                    include_memory_directives=bool(self._memory_manager),
                )

                output = await simple_chat_cache.simple_chat(
                    self._llm,
                    [LLMMessage(role="user", content=prompt)],
                )
                if output:
                    return self._consume_compaction_output(output)
            except Exception as exc:
                logger.debug("LLM summarization failed, using fallback: %s", exc)
        return raw_text

    def _consume_compaction_output(self, output: str) -> str:
        parsed = parse_compaction_output(
            output,
            parse_memory_directives=bool(self._memory_manager),
        )
        if parsed.memdir_facts:
            self._remember_memdir_facts(parsed.memdir_facts)
        return parsed.summary

    def _remember_memdir_facts(self, facts: list[str]) -> None:
        for fact in facts:
            if not fact:
                continue
            self._memory_manager.remember(fact, tags=["autocompact", "memdir"], importance=3)
            logger.info("AutoCompact extracted MemDir fact: %s", fact)

        v_mem = getattr(self._memory_manager, "_vector_memory", None)
        if v_mem and hasattr(v_mem, "flush"):
            v_mem.flush()

    def _get_history_within_budget(self) -> list[LLMMessage]:
        selected = [self._history[index] for index in self._get_history_within_budget_indices()]
        return self._repair_provider_tool_sequence(selected)

    @property
    def history_length(self) -> int:
        return len(self._history)

    def clear(self) -> None:
        clear_system_prompt_sections()
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
                    f"... [快照截断了 {len(content) - 1000} 字符；使用 artifact/read_artifact 查看完整输出] ...\n"
                    f"{content[-300:]}"
                )
            elif len(content) > 4000:
                content = f"{content[:2800]}\n... [快照截断了 {len(content) - 2800} 字符] ..."
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
            "history": self.sanitize_snapshot_history(history),
            "persistent_notes": [dict(note) for note in self._persistent_notes],
            "compaction_count": self._compaction_count,
        }

    def load_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        self.clear()
        if not snapshot:
            return

        self._load_snapshot_metadata(snapshot)
        self._history = self.deserialize_snapshot_history(
            self.sanitize_snapshot_history(snapshot.get("history", []))
        )
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
        raw_history = self.sanitize_snapshot_history(snapshot.get("history", []))
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
    def sanitize_snapshot_history(raw_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sanitized: list[dict[str, Any]] = []
        for raw in raw_history:
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role", "user"))
            content = str(raw.get("content", ""))
            if role == "user" and _is_internal_control_prompt(content):
                continue
            if (
                role == "assistant"
                and content.strip() == INTERNAL_EMPTY_ASSISTANT_MARKER
                and not raw.get("tool_calls")
            ):
                continue
            previous = sanitized[-1] if sanitized else None
            if (
                role == "user"
                and previous is not None
                and str(previous.get("role", "")) == "user"
                and content.strip()
                and content.strip() == str(previous.get("content", "")).strip()
            ):
                continue
            sanitized.append(raw)
        return sanitized

    @staticmethod
    def deserialize_snapshot_history(raw_history: list[dict[str, Any]]) -> list[LLMMessage]:
        parsed_history: list[LLMMessage] = []
        for raw in ContextBuilder.sanitize_snapshot_history(raw_history):
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

    @staticmethod
    def _repair_provider_tool_sequence(messages: list[LLMMessage]) -> list[LLMMessage]:
        repaired: list[LLMMessage] = []
        pending_ids: dict[str, str] = {}
        pending_order: list[str] = []

        def make_placeholder(call_id: str, tool_name: str) -> LLMMessage:
            return LLMMessage(
                role="tool",
                content=(
                    f"[Tool call '{tool_name}' did not complete. "
                    "Use available context; do not repeat the same call unless necessary.]"
                ),
                name=tool_name,
                tool_call_id=call_id,
            )

        def flush_pending() -> None:
            if not pending_order:
                return
            for call_id in list(pending_order):
                repaired.append(make_placeholder(call_id, pending_ids.get(call_id, "unknown")))
            pending_ids.clear()
            pending_order.clear()

        for message in messages:
            if message.role == "assistant":
                flush_pending()
                repaired.append(message)
                for tool_call in message.tool_calls or []:
                    pending_ids[tool_call.id] = tool_call.name
                    pending_order.append(tool_call.id)
                continue

            if message.role == "tool":
                call_id = str(message.tool_call_id or "").strip()
                if call_id and call_id in pending_ids:
                    repaired.append(message)
                    pending_ids.pop(call_id, None)
                    pending_order = [pending for pending in pending_order if pending != call_id]
                continue

            flush_pending()
            repaired.append(message)

        flush_pending()
        return repaired

    def _effective_compaction_threshold(self, state: AgentState | None = None) -> float:
        threshold = self._agent_settings.compaction_threshold
        if state is not None and state.iterations < 3:
            threshold = max(threshold, 0.75)  # was 0.85 — compact earlier
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
        if isinstance(content, str):
            return _estimate_content_tokens(content)
        try:
            return len(content) // 4
        except TypeError:
            return _estimate_content_tokens(str(content))
