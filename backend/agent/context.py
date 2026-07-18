from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from copy import copy, deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from backend.agent.compaction import format_compaction_history, parse_compaction_output
from backend.agent.context_ledger import (
    ContextLedger,
    ContextLedgerCategory,
    ContextLedgerEntry,
    estimate_native_attachments,
)
from backend.agent.state import AgentState
from backend.config import AgentSettings, TokenBudget
from backend.agent.attachment_policy import AttachmentInputPlan, build_attachment_input_plan
from backend.attachments.store import AttachmentStore
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
    select_prompt_packs,
    summarize_prompt_sections,
)
from backend.agent.prompt_cache import prompt_cache_usage_stats
from backend.agent.tool_result_compaction import (
    ToolResultCacheEditConfig,
    compact_old_tool_results_by_id,
)
from backend.agent.tool_result_persistence import (
    force_persist_for_compaction,
    try_persist_tool_result,
)
from backend.agent.context_edit import (
    ContextEditConfig,
    apply_context_edit,
)
from backend.tools.base import ToolResult

logger = logging.getLogger(__name__)


def clone_context_builder(builder: "ContextBuilder") -> "ContextBuilder":
    """Clone reusable prompt/history state for branch-style agent runs."""
    cloned = copy(builder)
    for name in (
        "_history",
        "_history_token_estimates",
        "_persistent_notes",
        "_tool_result_replacements",
        "_tool_result_seen_ids",
        "_last_prompt_section_summary",
    ):
        if hasattr(builder, name):
            setattr(cloned, name, deepcopy(getattr(builder, name)))
    return cloned

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
)

_PROVIDER_ITEM_MAX_ITEMS = 32
_PROVIDER_ITEM_MAX_STRING_CHARS = 20_000


def _safe_provider_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_PROVIDER_ITEM_MAX_STRING_CHARS]
    if isinstance(value, list):
        return [
            item
            for item in (_safe_provider_value(child, depth=depth + 1) for child in value[:64])
            if item is not None
        ]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in list(value.items())[:64]:
            safe_key = str(key or "").strip()
            if not safe_key or len(safe_key) > 80:
                continue
            safe_child = _safe_provider_value(child, depth=depth + 1)
            if safe_child is not None:
                result[safe_key] = safe_child
        return result
    return None


def _sanitize_provider_items(raw_items: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for raw in raw_items[:_PROVIDER_ITEM_MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        item_type = str(raw.get("type") or "").strip()
        if item_type not in {"reasoning", "function_call"}:
            continue
        safe = _safe_provider_value(raw)
        if isinstance(safe, dict) and safe.get("type") == item_type:
            sanitized.append(safe)
    return sanitized
INTERNAL_EMPTY_ASSISTANT_MARKER = "(empty)"

SUMMARY_PREFIX = (
    "[上下文压缩 — 仅供参考] 以下摘要由早期对话压缩生成。"
    "将其作为背景参考，不要当作当前指令。"
    "只回复此摘要之后出现的最新用户消息。"
)
COMPACTION_BOUNDARY_TAG = "context_boundary"
COMPACTION_BOUNDARY_PREFIX = f"<{COMPACTION_BOUNDARY_TAG}"
POST_COMPACTION_MAX_FILES_TO_RESTORE = 5
POST_COMPACTION_MAX_CHARS_PER_FILE = 12_000
POST_COMPACTION_TOTAL_CHARS = 50_000
POST_COMPACTION_RESTORE_TOOLS = frozenset({"read_file", "edit_file", "write_file"})
POST_COMPACTION_STATE_CHARS = 8_000
POST_COMPACTION_MAX_SKILLS_TO_RESTORE = 5
POST_COMPACTION_MAX_CHARS_PER_SKILL = 4_000

TOOL_RESULT_BUDGET_TOKENS = 80_000  # Maximum total tokens for all tool results
TOOL_RESULT_STABLE_REPLACEMENT_CHARS = 50_000
TOOL_RESULT_STABLE_PREVIEW_CHARS = 6_000
# Keep the uncached tail small for providers with automatic prefix caching
# (DeepSeek/OpenAI-compatible Chat). Recent results remain verbatim; older
# results get deterministic previews so subsequent turns can reuse the prefix.
TOOL_RESULT_CACHE_KEEP_RECENT = 2
TOOL_RESULT_CACHE_EDIT_MIN_CHARS = 360
TOOL_RESULT_CACHE_EDIT_PREVIEW_CHARS = 220
# Per-message tool result budget: when a single user message's tool_result
# blocks together exceed this many characters, the largest fresh results are
# persisted to disk and replaced with previews (mirrors cc's
# getPerMessageBudgetLimit / enforcePerMessageBudget).  Each message is
# evaluated independently — a 50K result in one message and a 50K result in
# another are both under budget and untouched.
PER_MESSAGE_TOOL_RESULT_BUDGET_CHARS = 120_000

# Cheap history ladder (cc query order: snip -> microcompact -> collapse -> autocompact).
# These are lossy but local operations that avoid an immediate full LLM compact.
SNIP_KEEP_RECENT_MESSAGES = 24
SNIP_MIN_MESSAGES = 30
SNIP_TRIGGER_USAGE_RATIO = 0.70
COLLAPSE_KEEP_RECENT_MESSAGES = 18
COLLAPSE_MIN_MESSAGES = 24
COLLAPSE_TRIGGER_USAGE_RATIO = 0.82
COLLAPSE_PREVIEW_CHARS = 240
RUNTIME_CONTEXT_STRIP_KEEP_RECENT_USER_TURNS = 1
RUNTIME_CONTEXT_OMITTED_PLACEHOLDER = "[runtime context omitted]"
_RUNTIME_BLOCK_RE = re.compile(
    r"\A(?:\s*(?:"
    r"<permissions instructions>[\s\S]*?</permissions instructions>|"
    r"<environment_context>[\s\S]*?</environment_context>|"
    r"<collaboration_mode>[\s\S]*?</collaboration_mode>|"
    r"<agent_mode>[\s\S]*?</agent_mode>|"
    r"<turn_aborted>[\s\S]*?</turn_aborted>|"
    r"<tool_runtime_context>[\s\S]*?</tool_runtime_context>|"
    r"Current time:[^\n]*(?:\n|$)|"
    r"Conversation language:[^\n]*(?:\n|$)"
    r")\s*)+",
    re.IGNORECASE,
)


def _compaction_boundary(kind: str, count: int, compacted_messages: int) -> str:
    safe_kind = "emergency" if kind == "emergency" else "auto"
    return (
        f'<{COMPACTION_BOUNDARY_TAG} kind="{safe_kind}" '
        f'count="{max(0, int(count))}" compacted_messages="{max(0, int(compacted_messages))}" />'
    )


def _is_internal_control_prompt(content: str) -> bool:
    text = str(content or "").strip()
    return any(text.startswith(prefix) for prefix in INTERNAL_CONTROL_PROMPT_PREFIXES)


def _is_prompt_instruction_role(role: Any) -> bool:
    return str(role or "").strip().lower() in {"system", "developer"}


def _normalize_message_role(role: Any) -> str:
    normalized = str(role or "user").strip().lower()
    return normalized or "user"


def _message_content_text(content: Any) -> str:
    """Return text from persisted message content, including OpenAI part arrays."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    return str(content or "")


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


def _xml_text(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _strip_leading_runtime_context(content: str) -> str:
    text = str(content or "")
    if not _has_leading_runtime_context(text):
        return text
    stripped = _RUNTIME_BLOCK_RE.sub("", text, count=1).lstrip()
    return stripped


def _has_leading_runtime_context(content: str) -> bool:
    text = str(content or "").lstrip()
    return bool(_RUNTIME_BLOCK_RE.match(text))


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
        self._token_calibration_factor = 1.0  # EMA of actual / estimated
        self._persistent_notes: list[dict[str, str]] = []
        self._compaction_count = 0
        self._skill_executor = skill_executor
        self._skill_manager = skill_manager
        self._rag_pipeline = rag_pipeline
        self._memory_manager = memory_manager
        self._llm = llm
        self._vector_memory = vector_memory
        self._attachment_store = AttachmentStore()
        self._cached_guidelines: str = ""
        self._cached_guidelines_signature: str = ""
        self._cached_guidelines_ts: float = 0.0
        self._last_actual_prompt_tokens = 0
        self._tool_result_replacements: dict[str, str] = {}
        self._tool_result_seen_ids: set[str] = set()
        self._last_tool_result_cache_edit_saved_tokens = 0
        self._tool_result_cache_edit_saved_tokens_total = 0
        self._tool_result_cache_edit_compacted_total = 0
        # Monotonic timestamp of the last assistant message appended to history.
        # Used by _maybe_time_based_microcompact to detect cold-cache gaps.
        self._last_assistant_ts: float = 0.0
        self._prefer_stateful_history = False
        self._last_sent_runtime_context = ""
        self._prepared_prompt_parts: PromptParts | None = None
        self._prepared_prompt_state: AgentState | None = None
        self._last_prompt_section_summary: dict[str, Any] = {}

    _GUIDELINES_CACHE_TTL = 10.0  # seconds

    def _get_project_guidelines(self, workspace_root: Path | None = None) -> str:
        """Return cached project guidelines, reloading only when TTL expires.

        Avoids repeated Path.stat() syscalls from load_project_guidelines()
        by short-circuiting within the TTL window.
        """
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
            if self._skill_manager.get_active_names():
                skill_content = executor.build_skill_context(
                    budget=self._budget.active_skills,
                    consume=True,
                )
                if skill_content:
                    parts.append(skill_content)
        elif self._skill_executor:
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

    def set_compaction_summary_note(self, summary: str) -> None:
        """Replace (or remove) the compaction-summary persistent note.

        Called by the agent runner to back-fill the conversation's compaction
        summary so that even if the context snapshot is lost, the model can
        still read the high-level conclusions from previous turns.
        """
        summary = str(summary or "").strip()
        self._persistent_notes[:] = [
            note for note in self._persistent_notes
            if note.get("kind") != "compaction_summary"
        ]
        if summary:
            self._persistent_notes.append(
                {
                    "kind": "compaction_summary",
                    "title": "Compacted conversation memory",
                    "content": summary,
                }
            )

    def set_prefer_stateful_history(self, enabled: bool) -> None:
        """Prefer provider-side continuation over rewriting old tool results."""
        self._prefer_stateful_history = bool(enabled)

    async def start_turn(self, user_message: str, state: AgentState) -> None:
        """Render and store one model-visible user turn."""
        user_message = str(user_message or "")
        if not user_message.strip() and not state.attachments:
            return
        workspace_root = self._workspace_root_for_state(state)
        prompt_parts = self._build_prompt_parts(state, workspace_root)
        runtime_context_prefix = self._build_runtime_context_prefix(state)
        attachment_plan = build_attachment_input_plan(
            state.attachments,
            llm=self._llm,
            attachment_store=self._attachment_store,
        )
        user_turn_content = self._build_user_turn_content(
            self._with_attachment_text_fallback(user_message, attachment_plan),
            state,
            prompt_parts,
            runtime_prefix=runtime_context_prefix,
        )
        if not user_turn_content.strip() and not attachment_plan.images and not attachment_plan.documents:
            return

        self._compact_old_user_runtime_context_for_cache()
        self._append_history_message(
            LLMMessage(
                role="user",
                content=user_turn_content,
                images=attachment_plan.images,
                documents=attachment_plan.documents,
            ),
            raw_content=user_turn_content,
        )
        self._last_sent_runtime_context = runtime_context_prefix.strip()
        self._compact_old_user_runtime_context_for_cache(
            keep_recent_user_turns=RUNTIME_CONTEXT_STRIP_KEEP_RECENT_USER_TURNS,
        )
        self._prepared_prompt_parts = prompt_parts
        self._prepared_prompt_state = state

    def append_user_context(self, content: str) -> None:
        """Append hook or runtime context after the active user turn."""
        self.append_user(content)

    async def build(self, state: AgentState) -> list[LLMMessage]:
        messages: list[LLMMessage] = []
        if not self._prefer_stateful_history:
            self._compact_old_tool_results_for_cache()
            self._enforce_per_message_tool_budget()
            self._maybe_time_based_microcompact()

        workspace_root = self._workspace_root_for_state(state)
        if self._prepared_prompt_state is state and self._prepared_prompt_parts is not None:
            prompt_parts = self._prepared_prompt_parts
        else:
            prompt_parts = self._build_prompt_parts(state, workspace_root)
        self._prepared_prompt_parts = None
        self._prepared_prompt_state = None
        system_content = prompt_parts.render_system()
        runtime_context_prefix = self._build_runtime_context_prefix(state)
        self._append_runtime_context_update_if_changed(runtime_context_prefix)
        self._compact_old_user_runtime_context_for_cache()

        # ── End of system prompt ────────────────────────────────────────────────
        # Keep retrieval agentic: Codex/Claude Code-style loops let the model
        # request memory or document context with tools instead of silently
        # injecting passive RAG into every turn. Explicitly populated chunks are
        # still honored below for command/tool driven workflows.

        messages.append(LLMMessage(role="system", content=system_content))
        history = self._get_history_within_budget()
        messages.extend(history)

        # API-level context edit: strip old thinking blocks and compact old
        # recoverable tool results without mutating internal history. Provider
        # replay items are preserved by default because stateless Responses API
        # calls may need them.
        if len(history) >= 6:
            edited_messages, edit_stats = apply_context_edit(
                messages,
                # In the default (non-stateful) path the history-level compactor
                # (_compact_old_tool_results_for_cache) already rewrote old tool
                # results, so only let apply_context_edit compact them when that
                # path was skipped (stateful history). This avoids a duplicate
                # full scan and double-wrapping. Thinking-strip always runs.
                config=ContextEditConfig(
                    compact_tool_results=self._prefer_stateful_history,
                ),
                token_estimator=self._estimate_content_tokens,
            )
            if edit_stats.total_edits > 0:
                messages = edited_messages

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
        builder = PromptBuilderV2()
        sections = builder.build_sections(
            state=state,
            workspace_root=workspace_root,
            project_guidelines=self._get_project_guidelines(workspace_root),
            skill_context=self._build_skill_context(state),
            memory_context=self._build_memory_context(),
            persistent_context=self._build_persistent_context(),
        )
        self._last_prompt_section_summary = summarize_prompt_sections(sections)
        state.prompt_context["prompt_section_summary"] = self._last_prompt_section_summary
        return PromptParts.from_sections(sections)

    @staticmethod
    def _with_attachment_text_fallback(
        user_message: str,
        attachment_plan: AttachmentInputPlan,
    ) -> str:
        # Inline small text attachments verbatim into the user message so the
        # model sees them directly (and they persist in history for follow-up
        # turns), then append read_artifact hints for any remaining large docs.
        chunks: list[str] = [user_message] if user_message else []
        if not user_message.strip() and (
            attachment_plan.inlined_texts
            or attachment_plan.text_hints
            or attachment_plan.images
            or attachment_plan.documents
        ):
            chunks.append(
                "The user sent these attachments without a separate prompt. Treat the attachment "
                "contents as the user's message, inspect them before responding, and answer based on "
                "their actual contents rather than attachment metadata."
            )
        for inlined in attachment_plan.inlined_texts:
            name = str(inlined.get("file_name") or "attachment")
            content = str(inlined.get("content") or "")
            chunks.append(
                f'<attachment file_name="{name}">\n{content}\n</attachment>'
            )
        if attachment_plan.text_hints:
            chunks.append("Attachment text fallback:\n" + "\n".join(attachment_plan.text_hints))
        if len(chunks) <= 1:
            return user_message
        return "\n\n".join(chunk for chunk in chunks if chunk and chunk.strip())

    @staticmethod
    def _build_user_turn_content(
        user_message: str,
        state: AgentState,
        prompt_parts: PromptParts,
        *,
        runtime_prefix: str | None = None,
    ) -> str:
        runtime_prefix = (
            ContextBuilder._build_runtime_context_prefix(state)
            if runtime_prefix is None
            else runtime_prefix
        )
        volatile_prefix = prompt_parts.render_volatile_prefix()
        effective_user_message = ContextBuilder._build_effective_user_message(user_message, state)
        return "\n\n".join(
            part
            for part in (
                runtime_prefix,
                volatile_prefix,
                effective_user_message,
            )
            if str(part or "").strip()
        )

    def _append_runtime_context_update_if_changed(self, runtime_context: str) -> bool:
        content = str(runtime_context or "").strip()
        if not content or content == self._last_sent_runtime_context:
            return False
        previous = self._history[-1] if self._history else None
        if (
            previous is not None
            and previous.role == "user"
            and not previous.tool_call_id
            and not previous.tool_calls
            and str(previous.content or "").strip() == content
        ):
            self._last_sent_runtime_context = content
            return False
        self._append_history_message(
            LLMMessage(role="user", content=content),
            raw_content=content,
        )
        self._last_sent_runtime_context = content
        return True

    @staticmethod
    def _build_runtime_context_prefix(state: AgentState) -> str:
        blocks = [
            ContextBuilder._build_environment_context_xml(state),
            ContextBuilder._build_collaboration_mode_block(state),
            ContextBuilder._build_agent_mode_block(state),
            ContextBuilder._build_turn_aborted_block(state),
            ContextBuilder._build_tool_runtime_context_block(state),
        ]
        return "\n\n".join(block for block in blocks if block.strip())

    @staticmethod
    def _prompt_context(state: AgentState) -> dict[str, Any]:
        value = getattr(state, "prompt_context", None)
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _build_environment_context_xml(state: AgentState) -> str:
        prompt_context = ContextBuilder._prompt_context(state)
        environment = prompt_context.get("environment")
        if not isinstance(environment, dict):
            environment = {}

        workspace_root = ContextBuilder._workspace_root_for_state(state)
        cwd = str(
            environment.get("cwd")
            or prompt_context.get("cwd")
            or workspace_root
            or Path.cwd()
        )
        workspace_roots = environment.get("workspace_roots")
        if not isinstance(workspace_roots, list):
            workspace_roots = prompt_context.get("workspace_roots")
        if not isinstance(workspace_roots, list):
            workspace_roots = (
                [str(workspace_root or cwd)]
                if str(workspace_root or cwd).strip()
                else []
            )
        normalized_roots = [str(root) for root in workspace_roots if str(root or "").strip()]

        shell = str(
            environment.get("shell")
            or prompt_context.get("shell")
            or os.environ.get("SHELL")
            or os.environ.get("COMSPEC")
            or ("powershell" if os.name == "nt" else "unknown")
        ).strip()
        now = datetime.now().astimezone()
        current_date = str(
            environment.get("current_date")
            or prompt_context.get("current_date")
            or now.strftime("%Y-%m-%d")
        )
        timezone = str(
            environment.get("timezone")
            or prompt_context.get("timezone")
            or os.environ.get("TZ")
            or now.tzname()
            or "local"
        )

        permission = environment.get("permission")
        if not isinstance(permission, dict):
            permission = prompt_context.get("permission")
        if not isinstance(permission, dict):
            permission = {}
        mode = (
            str(permission.get("mode") or prompt_context.get("permission_mode") or "default").strip()
            or "default"
        )
        source = (
            str(permission.get("source") or prompt_context.get("permission_source") or "runtime").strip()
            or "runtime"
        )
        workspace_scope = (
            str(
                permission.get("workspace_scope")
                or prompt_context.get("workspace_scope")
                or ("project" if normalized_roots else "computer")
            ).strip()
            or "project"
        )
        file_system_type = str(
            permission.get("file_system") or permission.get("file_system_type") or ""
        ).strip()
        if not file_system_type:
            if mode in {"bypass", "full_access", "full-access", "danger-full-access"}:
                file_system_type = "unrestricted"
            elif mode == "plan":
                file_system_type = "read_only"
            elif normalized_roots:
                file_system_type = "workspace"
            else:
                file_system_type = "computer"

        if normalized_roots:
            root_lines = "\n".join(f"      <root>{_xml_text(root)}</root>" for root in normalized_roots)
            workspace_roots_block = f"    <workspace_roots>\n{root_lines}\n    </workspace_roots>"
        else:
            workspace_roots_block = "    <workspace_roots />"

        return (
            "<environment_context>\n"
            f"  <cwd>{_xml_text(cwd)}</cwd>\n"
            f"  <shell>{_xml_text(shell)}</shell>\n"
            f"  <current_date>{_xml_text(current_date)}</current_date>\n"
            f"  <timezone>{_xml_text(timezone)}</timezone>\n"
            "  <filesystem>\n"
            f"{workspace_roots_block}\n"
            f"    <permission_profile type=\"{_xml_text(mode)}\" source=\"{_xml_text(source)}\">\n"
            f"      <file_system type=\"{_xml_text(file_system_type)}\" "
            f"workspace_scope=\"{_xml_text(workspace_scope)}\" />\n"
            "    </permission_profile>\n"
            "  </filesystem>\n"
            "</environment_context>"
        )

    @staticmethod
    def _build_collaboration_mode_block(state: AgentState) -> str:
        prompt_context = ContextBuilder._prompt_context(state)
        environment = prompt_context.get("environment")
        permission: dict[str, Any] = {}
        if isinstance(environment, dict) and isinstance(environment.get("permission"), dict):
            permission = environment["permission"]
        elif isinstance(prompt_context.get("permission"), dict):
            permission = prompt_context["permission"]
        raw_mode = str(
            prompt_context.get("collaboration_mode")
            or permission.get("mode")
            or prompt_context.get("permission_mode")
            or "default"
        ).strip().lower()
        active_mode = "plan" if raw_mode in {"plan", "planning"} else "default"
        if active_mode == "plan":
            lines = [
                "# Collaboration Mode: Plan",
                "Inspect, reason, and design without making workspace changes. Use read-only discovery; when ready, call exit_plan_mode with concise steps and wait for approval.",
            ]
        else:
            lines = [
                "# Collaboration Mode: Default",
                "Implement directly when the user asks for work, then verify and report the result. Use visible planning only when requested or materially useful; stay in review/brainstorming/no-change mode when asked.",
            ]
        return "<collaboration_mode>\n" + "\n".join(lines) + "\n</collaboration_mode>"

    @staticmethod
    def _build_agent_mode_block(state: AgentState) -> str:
        prompt_context = ContextBuilder._prompt_context(state)
        raw_mode = str(prompt_context.get("agent_mode") or "build").strip().lower()
        mode = raw_mode if raw_mode in {"build", "plan", "review", "explore"} else "build"
        guidance = {
            "build": [
                "# Agent Mode: Build",
                "Implement the requested change directly, verify it, and report the result. Use a brief plan only when it reduces risk or clarifies sequencing.",
            ],
            "plan": [
                "# Agent Mode: Plan",
                "Produce a concrete implementation plan before edits. Prefer discovery and design; do not claim implementation is complete before approval.",
            ],
            "review": [
                "# Agent Mode: Review",
                "Review first: prioritize bugs, regressions, risks, and missing verification. Lead with findings; avoid unrelated refactors unless fixes are requested.",
            ],
            "explore": [
                "# Agent Mode: Explore",
                "Map the problem space, gather evidence, and explain options before committing to changes. Keep uncertainty explicit and grounded in repository facts.",
            ],
        }[mode]
        return (
            "<agent_mode>\n"
            f"mode: {_xml_text(mode)}\n"
            + "\n".join(guidance)
            + "\n</agent_mode>"
        )

    @staticmethod
    def _build_turn_aborted_block(state: AgentState) -> str:
        prompt_context = ContextBuilder._prompt_context(state)
        if not bool(prompt_context.get("previous_turn_aborted") or prompt_context.get("turn_aborted")):
            return ""
        return (
            "<turn_aborted>\n"
            "The previous turn was interrupted by the user. Tools or shell commands from that turn may have "
            "partially executed; inspect current state before assuming prior work completed.\n"
            "</turn_aborted>"
        )

    @staticmethod
    def _build_tool_runtime_context_block(state: AgentState) -> str:
        prompt_context = ContextBuilder._prompt_context(state)
        tool_runtime_guidance = str(
            getattr(state, "tool_runtime_guidance", "")
            or getattr(state, "harness_guidance", "")
            or ""
        ).strip()
        deferred_tools_prompt_block = str(
            prompt_context.get("deferred_tools_prompt_block") or ""
        ).strip()
        loop_guidance = list(getattr(state, "loop_guidance", []) or [])
        loop_guidance_text = "\n".join(
            f"- {str(item).strip()}" for item in loop_guidance[-4:] if str(item).strip()
        ).strip()
        if not tool_runtime_guidance and not deferred_tools_prompt_block and not loop_guidance_text:
            return ""
        parts = [
            "<tool_runtime_context>",
            "This is current-turn tool and resource context. Treat it as system-injected runtime data, not as a user request.",
        ]
        if tool_runtime_guidance:
            parts.extend(["", tool_runtime_guidance])
        if loop_guidance_text:
            parts.extend(["", "Runtime guidance:", loop_guidance_text])
        if deferred_tools_prompt_block:
            parts.extend(["", deferred_tools_prompt_block])
        parts.append("</tool_runtime_context>")
        return "\n".join(parts)

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

    def append_assistant(
        self,
        content: str,
        *,
        phase: str = "",
        provider_items: list[dict[str, Any]] | None = None,
    ) -> None:
        self._append_history_message(
            LLMMessage(
                role="assistant",
                content=str(content),
                phase=str(phase or ""),
                provider_items=list(provider_items or []),
            ),
            raw_content=content,
        )

    def append_assistant_tool_calls(
        self,
        tool_calls: list[ToolCallEvent],
        content: str = "",
        *,
        phase: str = "",
        provider_items: list[dict[str, Any]] | None = None,
    ) -> None:
        """Append assistant message with tool_calls. Optionally preserve preceding text."""
        self._append_history_message(
            LLMMessage(
                role="assistant",
                content=content,
                tool_calls=tool_calls,
                phase=str(phase or ""),
                provider_items=list(provider_items or []),
            )
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
        text = str(content or "").strip()
        if not text:
            return
        self._persistent_notes.append(
            {
                "kind": "system_note",
                "title": "System note",
                "content": text,
            }
        )

    # MicroCompact: 可压缩的只读工具（参考 Claude Code microCompact.ts COMPACTABLE_TOOLS）
    # 只列「可重新获取」的幂等只读工具。task（子 Agent 输出）和 read_artifact
    # （一次性 artifact 引用）不可复现，头尾截断会永久丢内容且占位符会谎称可重取，
    # 因此排除在外（Claude Code 上下文管理 §4.3：不可复现信息不能轻易删）。
    _COMPACTABLE_TOOLS = frozenset({
        "read_file", "list_files", "grep_files", "glob_files", "fuzzy_search",
        "git_status", "git_diff", "git_log", "web_fetch", "web_search",
        "run_command", "go_to_definition",
        "find_references", "write_file", "edit_file",
    })
    _MICRO_COMPACT_THRESHOLD = 1500  # 默认阈值
    # 中段丢弃量达到此值才落盘兜底（小幅省略靠诚实提示即可，避免大量小文件）
    _MICRO_COMPACT_MIN_PERSIST_OMITTED = 2000
    # 模型主动请求的内容（read_file, git_diff）用更高阈值，避免丢失关键上下文
    _HIGH_THRESHOLD_TOOLS = frozenset({
        "read_file", "git_diff", "go_to_definition",
    })
    _LOW_THRESHOLD_TOOLS = frozenset({
        "web_search", "web_fetch", "list_files", "glob_files", "grep_files",
    })
    _HIGH_COMPACT_THRESHOLD = 3500
    _LOW_COMPACT_THRESHOLD = 900
    _READ_FILE_COMPACT_THRESHOLD = 12_000

    # Time-based microcompact: when the gap since the last assistant message
    # exceeds this threshold (in minutes), the server prompt cache has expired
    # and the full prefix will be rewritten regardless — so content-clear old
    # tool results now to shrink what gets rewritten (mirrors cc's
    # evaluateTimeBasedTrigger / maybeTimeBasedMicrocompact).
    _TIME_BASED_MC_GAP_MINUTES = 15.0
    _TIME_BASED_MC_KEEP_RECENT = 3

    def _tool_result_compaction_hint(self, tool_name: str, *, artifact_hint: bool = False) -> str:
        """Return a truthful recovery hint for compacted tool output."""
        if tool_name in self._COMPACTABLE_TOOLS:
            if artifact_hint:
                return "如需完整内容，请读取相关文件、查看 artifact，或用安全的只读方式重新获取。"
            return "如需完整内容，请读取相关文件或用安全的只读方式重新获取。"
        if artifact_hint:
            return "这是状态性或不可复现输出，仅保留预览；如有 artifact 引用可尝试读取，但不要假设可重取。"
        return "这是状态性或不可复现输出，仅保留预览；不要假设可重新获取。"

    def append_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        result: ToolResult,
    ) -> None:
        content = result.to_context_string()
        content = self._stable_tool_result_replacement(tool_call_id, tool_name, content)

        # Disk persistence: if the result is very large, write the full
        # content to disk and replace the inline text with a compact preview
        # that includes a file path the model can read_file to recover.
        # This runs before _micro_compact so the latter sees the smaller preview.
        content = try_persist_tool_result(content, tool_call_id, tool_name)

        # MicroCompact: 对大型只读工具结果进行即时截断压缩
        content = self._micro_compact(tool_name, content)

        # Codex-style: add structured status prefix matching function_call_result format
        status = "error" if result.is_error else "completed"
        if not content.startswith("<persisted-"):
            content = (
                f'<function_call_result status="{status}" call_id="{tool_call_id}">\n'
                f"{content}\n"
                f"</function_call_result>"
            )

        self._append_history_message(
            LLMMessage(
                role="tool",
                content=content,
                name=tool_name,
                tool_call_id=tool_call_id,
            ),
            raw_content=content,
        )
        if not self._prefer_stateful_history:
            self._compact_old_tool_results_for_cache()
            self._enforce_per_message_tool_budget()
            self._compact_old_user_runtime_context_for_cache()

    def _compact_old_user_runtime_context_for_cache(
        self,
        *,
        keep_recent_user_turns: int = RUNTIME_CONTEXT_STRIP_KEEP_RECENT_USER_TURNS,
    ) -> int:
        """Remove repeated per-turn runtime wrappers from old user turns.

        Current-turn runtime context is required for correctness. Older turns do
        not need their old cwd/date/permission wrappers repeated forever; their
        user text remains enough conversation history, and stateful providers
        already preserve the exact older request through previous_response_id.
        """
        keep_recent_user_turns = max(0, int(keep_recent_user_turns))
        user_indices = [
            index
            for index, message in enumerate(self._history)
            if message.role == "user" and not message.tool_call_id and not message.tool_calls
        ]
        if len(user_indices) <= keep_recent_user_turns:
            return 0
        protected = set(user_indices[-keep_recent_user_turns:]) if keep_recent_user_turns else set()
        if keep_recent_user_turns:
            for index in reversed(user_indices):
                if _has_leading_runtime_context(str(self._history[index].content or "")):
                    protected.add(index)
                    break
        changed = 0
        for index in user_indices:
            if index in protected:
                continue
            message = self._history[index]
            content = str(message.content or "")
            stripped = _strip_leading_runtime_context(content)
            if stripped == content:
                continue
            replacement = stripped or RUNTIME_CONTEXT_OMITTED_PLACEHOLDER
            self._history[index] = LLMMessage(
                role=message.role,
                content=replacement,
                name=message.name,
                tool_call_id=message.tool_call_id,
                tool_calls=message.tool_calls,
                images=list(message.images),
                documents=list(message.documents),
            )
            changed += 1
        if changed:
            self._rebuild_history_token_cache()
        return changed

    def _compact_old_tool_results_for_cache(self) -> int:
        compacted_history, stats = compact_old_tool_results_by_id(
            self._history,
            replacements=self._tool_result_replacements,
            token_estimator=self._estimate_content_tokens,
            config=ToolResultCacheEditConfig(
                keep_recent=TOOL_RESULT_CACHE_KEEP_RECENT,
                min_chars=TOOL_RESULT_CACHE_EDIT_MIN_CHARS,
                preview_chars=TOOL_RESULT_CACHE_EDIT_PREVIEW_CHARS,
            ),
        )
        if stats.compacted <= 0:
            return 0
        self._history = compacted_history
        self._last_tool_result_cache_edit_saved_tokens = stats.saved_tokens
        self._tool_result_cache_edit_saved_tokens_total += stats.saved_tokens
        self._tool_result_cache_edit_compacted_total += stats.compacted
        self._last_actual_prompt_tokens = 0
        self._rebuild_history_token_cache()
        logger.info(
            "[ToolResultCacheEdit] Compacted %d old tool results by tool_call_id, saved ~%d tokens",
            stats.compacted,
            stats.saved_tokens,
        )
        return stats.compacted

    def _enforce_per_message_tool_budget(self) -> int:
        """Enforce a per-message aggregate budget on tool result content.

        Mirrors cc's ``enforcePerMessageBudget``: for each user message whose
        adjacent tool_result messages together exceed the budget, the largest
        *fresh* (never-before-seen) results are persisted to disk and replaced
        with previews.  Once a result is seen its fate is frozen — previously
        replaced results get the same replacement re-applied (byte-identical
        for cache stability) and previously-unreplaced results are never
        replaced later (would break prompt cache).

        Returns the number of tool results persisted this call.
        """
        if not self._history:
            return 0

        budget = PER_MESSAGE_TOOL_RESULT_BUDGET_CHARS
        newly_persisted = 0

        # Walk history and group tool results by the preceding user message.
        # In the agent loop, tool results follow an assistant tool_calls message
        # which follows a user message. We group all consecutive tool messages
        # after each user turn.
        i = 0
        while i < len(self._history):
            msg = self._history[i]
            if msg.role != "user":
                i += 1
                continue

            # Collect consecutive tool messages after this user message
            # (there may be an assistant tool_calls message in between).
            tool_indices: list[int] = []
            j = i + 1
            while j < len(self._history):
                next_msg = self._history[j]
                if next_msg.role == "tool":
                    tool_indices.append(j)
                    j += 1
                elif next_msg.role == "assistant" and next_msg.tool_calls:
                    j += 1  # skip assistant tool_calls message
                else:
                    break

            if not tool_indices:
                i = j
                continue

            # Calculate aggregate size
            total_chars = sum(
                len(str(self._history[idx].content or ""))
                for idx in tool_indices
            )

            if total_chars <= budget:
                i = j
                continue

            # Over budget: persist the largest fresh results until under budget.
            # Sort by size descending.
            sized = sorted(
                tool_indices,
                key=lambda idx: len(str(self._history[idx].content or "")),
                reverse=True,
            )

            current_total = total_chars
            for idx in sized:
                if current_total <= budget:
                    break

                tool_msg = self._history[idx]
                call_id = str(tool_msg.tool_call_id or "").strip()
                tool_name = str(tool_msg.name or "unknown")
                content = str(tool_msg.content or "")

                # Skip already-persisted or already-compacted results
                if (
                    not call_id
                    or content.startswith("<persisted-tool-result>")
                    or content.startswith("<persisted-tool-result-preview>")
                    or content.lstrip().startswith("<tool_result_cache_entry")
                    or call_id in self._tool_result_seen_ids
                ):
                    continue

                self._tool_result_seen_ids.add(call_id)
                new_content = try_persist_tool_result(content, call_id, tool_name)
                if new_content != content:
                    self._history[idx] = LLMMessage(
                        role=tool_msg.role,
                        content=new_content,
                        name=tool_msg.name,
                        tool_call_id=tool_msg.tool_call_id,
                        tool_calls=tool_msg.tool_calls,
                        images=list(tool_msg.images),
                        documents=list(tool_msg.documents),
                    )
                    current_total -= len(content) - len(new_content)
                    newly_persisted += 1

            i = j

        if newly_persisted > 0:
            self._rebuild_history_token_cache()
            self._last_actual_prompt_tokens = 0
            logger.info(
                "[PerMessageBudget] Persisted %d tool results to disk (budget: %d chars)",
                newly_persisted,
                budget,
            )

        return newly_persisted

    def _stable_tool_result_replacement(self, tool_call_id: str, tool_name: str, content: str) -> str:
        """Freeze very large tool result replacements by tool_call_id.

        This mirrors Claude Code's prompt-cache-friendly rule: once a tool
        result has been compacted, future passes for the same id reuse the exact
        same replacement string rather than deriving a fresh truncation.
        """
        if not tool_call_id:
            return content
        replacement = self._tool_result_replacements.get(tool_call_id)
        if replacement is not None:
            return replacement
        if tool_call_id in self._tool_result_seen_ids:
            return content
        self._tool_result_seen_ids.add(tool_call_id)
        if len(content) <= TOOL_RESULT_STABLE_REPLACEMENT_CHARS:
            return content

        head_size = int(TOOL_RESULT_STABLE_PREVIEW_CHARS * 0.7)
        tail_size = TOOL_RESULT_STABLE_PREVIEW_CHARS - head_size
        omitted = max(0, len(content) - head_size - tail_size)
        compacted = (
            "<persisted-tool-result-preview>\n"
            f"Tool result from {tool_name} was too large for stable inline context "
            f"({len(content)} chars). Showing a deterministic preview; use a narrower tool call, "
            "pagination parameters, or the referenced artifact when available for more detail.\n\n"
            f"{content[:head_size]}\n"
            f"... [{omitted} chars omitted from stable tool result preview] ...\n"
            f"{content[-tail_size:]}\n"
            "</persisted-tool-result-preview>"
        )
        self._tool_result_replacements[tool_call_id] = compacted
        return compacted

    def _micro_compact(self, tool_name: str, content: str) -> str:
        """
        对可压缩工具的大型结果进行即时截断（参考 Claude Code microCompact.ts）。

        策略：
        - 仅对 _COMPACTABLE_TOOLS 中的工具生效
        - 模型主动请求的内容（read_file 等）用更高阈值，保留更多上下文
        - 超过阈值时保留首部和尾部内容，中间用摘要替代
        - 如果有 artifact_id 引用，保留引用信息
        """
        # Matches both this method's own preview tag and the disk-persistence
        # tag (<persisted-tool-result>) so already-compacted content is not
        # truncated a second time.
        if content.startswith("<persisted-tool-result"):
            return content
        if tool_name not in self._COMPACTABLE_TOOLS:
            return content
        threshold = (
            self._READ_FILE_COMPACT_THRESHOLD
            if tool_name == "read_file"
            else self._HIGH_COMPACT_THRESHOLD
            if tool_name in self._HIGH_THRESHOLD_TOOLS
            else self._LOW_COMPACT_THRESHOLD
            if tool_name in self._LOW_THRESHOLD_TOOLS
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

        # Disk fallback before dropping the middle. cc never truncates a large
        # result irrecoverably — the full text is stored and the preview is a
        # reference. try_persist_tool_result above only covers PERSISTABLE_TOOLS
        # (>20k); this catches the remainder (e.g. write_file/edit_file, or
        # persistable results below the 20k persist threshold) so the middle is
        # still recoverable via read_file rather than lost forever.
        recovery_ref = ""
        if omitted >= self._MICRO_COMPACT_MIN_PERSIST_OMITTED:
            filepath = force_persist_for_compaction(content, tool_name)
            if filepath:
                recovery_ref = f"完整原文已落盘：{filepath}（可用 read_file 读取）；"
        # If落盘失败或省略量较小，退回诚实提示（不假装完整、不假装可重取）。
        return (
            f"{head}\n"
            f"... [已压缩 {omitted} 字符；{recovery_ref}"
            f"{self._tool_result_compaction_hint(tool_name)}] ...\n"
            f"{tail}"
        )

    def _maybe_time_based_microcompact(self) -> int:
        """Time-based microcompact: clear old tool results when the cache is cold.

        Mirrors cc's ``maybeTimeBasedMicrocompact``: when the gap since the
        last assistant message exceeds a threshold, the server-side prompt
        cache has expired and the full prefix will be rewritten regardless.
        Content-clearing old tool results now shrinks what gets rewritten,
        saving tokens without any cache-breaking cost.

        Returns the number of tool results cleared.
        """
        if not self._history:
            return 0
        # Use the tracked timestamp of the last assistant message (set in
        # _append_history_message).  LLMMessage doesn't carry a timestamp
        # field, so we maintain it as a separate instance variable.
        last_asst_ts = self._last_assistant_ts

        if last_asst_ts <= 0:
            return 0

        gap_minutes = (time.monotonic() - last_asst_ts) / 60.0
        if gap_minutes < self._TIME_BASED_MC_GAP_MINUTES:
            return 0

        # Cache is cold: content-clear old compactable tool results.
        # Keep the most recent N results untouched.
        compactable_indices: list[int] = []
        for i, msg in enumerate(self._history):
            if msg.role == "tool" and (msg.name or "") in self._COMPACTABLE_TOOLS:
                content = str(msg.content or "")
                # Skip already-cleared results
                if (
                    content.startswith("<persisted-tool-result")
                    or content.lstrip().startswith("<tool_result_cache_entry")
                    or "[Old tool result content cleared]" in content[:60]
                ):
                    continue
                compactable_indices.append(i)

        if not compactable_indices:
            return 0

        keep_recent = max(1, self._TIME_BASED_MC_KEEP_RECENT)
        keep_set = set(compactable_indices[-keep_recent:])
        clear_set = set(compactable_indices) - keep_set

        if not clear_set:
            return 0

        cleared = 0
        for idx in clear_set:
            msg = self._history[idx]
            call_id = str(msg.tool_call_id or "").strip()
            # Use the stable replacement mechanism so it's byte-identical
            # on subsequent calls (cache stability)
            cleared_content = "[Old tool result content cleared]"
            self._tool_result_replacements[call_id] = cleared_content
            self._history[idx] = LLMMessage(
                role=msg.role,
                content=cleared_content,
                name=msg.name,
                tool_call_id=msg.tool_call_id,
                tool_calls=msg.tool_calls,
                images=list(msg.images),
                documents=list(msg.documents),
            )
            cleared += 1

        if cleared > 0:
            self._rebuild_history_token_cache()
            self._last_actual_prompt_tokens = 0
            logger.info(
                "[TimeBasedMC] gap %.1fmin > %.1fmin, cleared %d old tool results, kept last %d",
                gap_minutes,
                self._TIME_BASED_MC_GAP_MINUTES,
                cleared,
                len(keep_set),
            )

        return cleared

    @property
    def token_usage(self) -> int:
        total = _estimate_content_tokens(build_stable_prompt())
        project_guidelines = self._get_project_guidelines()
        if project_guidelines:
            total += _estimate_content_tokens(project_guidelines)
        total += sum(_estimate_content_tokens(str(note.get("content", ""))) for note in self._persistent_notes)
        estimated = total + self._history_tokens_total
        return max(estimated, self._last_actual_prompt_tokens)

    def context_ledger(self) -> ContextLedger:
        """Project the active ContextBuilder state into an inspectable ledger."""
        categories: dict[ContextLedgerCategory, dict[str, Any]] = {
            "system_runtime": {"label": "System & runtime", "sources": [], "tokens": 0},
            "guidelines": {"label": "Project guidelines", "sources": [], "tokens": 0},
            "skills": {"label": "Active skills", "sources": [], "tokens": 0},
            "files_attachments": {"label": "Files & attachments", "sources": [], "tokens": 0},
            "history": {"label": "History", "sources": [], "tokens": 0},
            "tool_results": {"label": "Tool results", "sources": [], "tokens": 0},
            "memory": {"label": "Memory", "sources": [], "tokens": 0},
            "compaction_summaries": {"label": "Compaction summaries", "sources": [], "tokens": 0},
        }
        item_counts = {category: 0 for category in categories}

        for row in self._last_prompt_section_summary.get("sections", []):
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            chars = max(0, int(row.get("chars") or 0))
            if name == "stable_system" or name in {"current_time", "task_status"}:
                category = "system_runtime"
            elif name in {"workspace_summary", "project_guidelines"}:
                category = "guidelines"
            elif name == "skill_context" or name.startswith("prompt_pack:"):
                category = "skills"
            elif name in {"memory_context", "persistent_context", "retrieved_chunks"}:
                category = "memory"
            else:
                category = "system_runtime"
            categories[category]["sources"].append(name)
            categories[category]["tokens"] += _estimate_content_tokens("x" * chars)

        native_attachment_tokens = 0
        native_attachment_count = 0
        for index, message in enumerate(self._history):
            estimate = (
                self._history_token_estimates[index]
                if index < len(self._history_token_estimates)
                else self._estimate_message_tokens(message.content, message.tool_calls)
            )
            attachment_tokens, attachment_count, attachment_sources = estimate_native_attachments(
                message.images,
                message.documents,
            )
            attachment_tokens = int(attachment_tokens * self._token_calibration_factor)
            if message.role == "tool":
                category = "tool_results"
                source = str(message.name or "tool")
            else:
                category = "history"
                source = str(message.role or "message")
            categories[category]["tokens"] += max(0, int(estimate) - attachment_tokens)
            categories[category]["sources"].append(source)
            item_counts[category] += 1
            if attachment_count:
                native_attachment_tokens += attachment_tokens
                native_attachment_count += attachment_count
                item_counts["files_attachments"] += attachment_count
                categories["files_attachments"]["tokens"] += attachment_tokens
                categories["files_attachments"]["sources"].extend(attachment_sources)

        for note in self._persistent_notes:
            content = str(note.get("content") or "")
            kind = str(note.get("kind") or "")
            category = "compaction_summaries" if kind == "compaction_summary" else "memory"
            categories[category]["tokens"] += _estimate_content_tokens(content)
            categories[category]["sources"].append(str(note.get("title") or kind or "note"))
            item_counts[category] += 1

        entries: list[ContextLedgerEntry] = []
        for category, values in categories.items():
            sources = list(dict.fromkeys(str(source) for source in values["sources"] if source))
            entries.append({
                "category": cast(ContextLedgerCategory, category),
                "label": values["label"],
                "estimated_tokens": int(values["tokens"]),
                "item_count": item_counts[category],
                "source_count": len(sources),
                "sources": sources[:12],
            })
        return {
            "schema_version": 1,
            "estimated_tokens": max(self.token_usage, self._last_actual_prompt_tokens),
            "actual_tokens": self._last_actual_prompt_tokens,
            "compaction_count": self._compaction_count,
            "native_attachment_tokens": native_attachment_tokens,
            "native_attachment_count": native_attachment_count,
            "entries": entries,
        }

    def record_actual_usage(self, usage: UsageInfo | None, provider_raw: dict[str, Any] | None = None) -> None:
        """Fold provider-reported prompt usage back into budget checks.

        Character estimates are still needed before the first request and after
        compaction, but real provider counts are a better floor once available.
        """
        if usage is None:
            return
        stats = prompt_cache_usage_stats(usage, provider_raw)
        observed = int(stats.get("prompt_cache_total_tokens") or 0)
        cache_hit_pct = float(stats.get("prompt_cache_hit_rate") or 0.0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        if observed > 0:
            self._last_actual_prompt_tokens = observed
            # Update the token estimation calibration factor via exponential
            # moving average (EMA). The heuristic (_estimate_content_tokens)
            # uses character counts with CJK/ASCII ratios; the calibration
            # factor lets it learn from the provider's real tokenizer over
            # time. Keep calibrating for the whole session (cc anchors every
            # threshold check on the latest real usage — freezing after a few
            # turns lets long sessions drift ±30%). ``_history_tokens_total`` is
            # already calibrated, so ``observed / estimated`` is the *residual*
            # correction; fold it multiplicatively so the factor converges to
            # the true multiplier instead of stalling at its square root.
            estimated = self._history_tokens_total
            if estimated > 0:
                residual = observed / estimated
                target = max(0.5, min(2.0, self._token_calibration_factor * residual))
                self._token_calibration_factor = max(
                    0.5,
                    min(2.0, self._token_calibration_factor * 0.9 + target * 0.1),
                )
        # Cache-hit diagnostic: log cache efficiency when provider reports cache reads
        if cache_read > 0 and observed > 0:
            logger.info(
                "[PromptCache] cache_read=%d cache_write=%d input=%d total_prompt=%d hit_rate=%.1f%%",
                cache_read,
                cache_creation,
                max(0, int(getattr(usage, "input_tokens", 0) or 0)),
                observed,
                cache_hit_pct,
            )


    def snip_history_if_needed(
        self,
        *,
        usage_ratio: float | None = None,
        force: bool = False,
    ) -> dict[str, int]:
        """Drop very old history turns before microcompact / full compact.

        Mirrors the intent of Claude Code HISTORY_SNIP: free tokens cheaply by
        removing protected-tail-outside messages, while keeping recent tool-call
        groups intact via ``_aligned_recent_start``.
        """
        total_budget = max(1, int(getattr(self._budget, "total", 0) or 0))
        ratio = (
            float(usage_ratio)
            if usage_ratio is not None
            else (self.token_usage / total_budget)
        )
        if not force and ratio < SNIP_TRIGGER_USAGE_RATIO:
            return {"removed": 0, "kept": len(self._history), "saved_tokens": 0}
        if len(self._history) < SNIP_MIN_MESSAGES:
            return {"removed": 0, "kept": len(self._history), "saved_tokens": 0}

        keep_recent = max(
            int(getattr(self._agent_settings, "history_keep_recent", 15) or 15),
            SNIP_KEEP_RECENT_MESSAGES,
        )
        recent_start = self._aligned_recent_start(keep_recent)
        if recent_start <= 0:
            return {"removed": 0, "kept": len(self._history), "saved_tokens": 0}

        before_tokens = int(self._history_tokens_total or 0)
        removed = recent_start
        boundary = LLMMessage(
            role="user",
            content=(
                f"{SUMMARY_PREFIX}\n\n"
                f"{_compaction_boundary('snip', self._compaction_count + 1, removed)}\n"
                f"[History snip] Removed {removed} older message(s) to free context. "
                "Recent turns and tool evidence are preserved; re-read files if earlier details are required."
            ),
        )
        self._history = [boundary] + self._history[recent_start:]
        self._rebuild_history_token_cache()
        saved = max(0, before_tokens - int(self._history_tokens_total or 0))
        logger.info(
            "[HistorySnip] removed=%d kept=%d saved~%d tokens ratio=%.3f",
            removed,
            len(self._history),
            saved,
            ratio,
        )
        return {
            "removed": removed,
            "kept": len(self._history),
            "saved_tokens": saved,
        }

    def collapse_old_tool_results(
        self,
        *,
        usage_ratio: float | None = None,
        force: bool = False,
    ) -> dict[str, int]:
        """Cheap staged collapse of old tool results before full autocompact.

        Unlike the last-resort hard truncate path, this keeps a
        short preview and tool identity so the parent can still decide what to
        re-fetch. Recent results and unconsumed tool batches stay intact.
        """
        total_budget = max(1, int(getattr(self._budget, "total", 0) or 0))
        ratio = (
            float(usage_ratio)
            if usage_ratio is not None
            else (self.token_usage / total_budget)
        )
        if not force and ratio < COLLAPSE_TRIGGER_USAGE_RATIO:
            return {"collapsed": 0, "saved_tokens": 0}
        if len(self._history) < COLLAPSE_MIN_MESSAGES:
            return {"collapsed": 0, "saved_tokens": 0}

        keep_recent = max(
            int(getattr(self._agent_settings, "history_keep_recent", 15) or 15),
            COLLAPSE_KEEP_RECENT_MESSAGES,
        )
        protect_start = self._aligned_recent_start(keep_recent)
        before_tokens = int(self._history_tokens_total or 0)

        # Preserve the latest unconsumed tool batch (assistant tool_calls + results).
        unconsumed: set[int] = set()
        for idx in range(len(self._history) - 1, -1, -1):
            msg = self._history[idx]
            if msg.role == "tool":
                unconsumed.add(idx)
                continue
            if msg.role == "assistant" and msg.tool_calls:
                unconsumed.add(idx)
                # include preceding consecutive tools already collected; stop after assistant
                break
            if msg.role in {"user", "assistant"}:
                break

        collapsed = 0
        for idx, msg in enumerate(self._history):
            if idx >= protect_start or idx in unconsumed:
                continue
            if msg.role != "tool":
                continue
            content = str(msg.content or "")
            if len(content) <= COLLAPSE_PREVIEW_CHARS:
                continue
            if content.startswith("[Tool result truncated") or content.startswith("[Old tool result"):
                continue
            if content.lstrip().startswith("<tool_result_cache_entry") or content.lstrip().startswith("<persisted-tool-result"):
                continue
            if content.lstrip().startswith("<context_edit_compact"):
                continue
            tool_name = str(msg.name or "tool")
            preview = content[:COLLAPSE_PREVIEW_CHARS].rstrip()
            replacement = (
                f"[Collapsed tool result: {tool_name} — was {len(content)} chars]\n"
                f"{preview}\n"
                f"... [re-run tool or read source if full output is required]"
            )
            self._history[idx] = LLMMessage(
                role=msg.role,
                content=replacement,
                name=msg.name,
                tool_call_id=msg.tool_call_id,
                tool_calls=msg.tool_calls,
                images=list(getattr(msg, "images", []) or []),
                documents=list(getattr(msg, "documents", []) or []),
            )
            call_id = str(msg.tool_call_id or "").strip()
            if call_id:
                self._tool_result_replacements[call_id] = replacement
                self._tool_result_seen_ids.add(call_id)
            collapsed += 1

        if collapsed:
            self._rebuild_history_token_cache()
        saved = max(0, before_tokens - int(self._history_tokens_total or 0))
        if collapsed:
            logger.info(
                "[ContextCollapse] collapsed=%d saved~%d tokens ratio=%.3f",
                collapsed,
                saved,
                ratio,
            )
        return {"collapsed": collapsed, "saved_tokens": saved}

    def strip_historical_media(self, *, keep_recent_user_turns: int = 1) -> dict[str, int]:
        """Drop image/document attachments from older history turns.

        Used for media-size recovery: oversized attachments that already failed
        the provider request are removed so the next attempt can continue with
        text-only historical context. The most recent user turn attachments are
        preserved by default so the active request can still be inspected.
        """
        keep_recent = max(0, int(keep_recent_user_turns))
        user_indices = [
            idx for idx, msg in enumerate(self._history) if msg.role == "user"
        ]
        # Protect only the latest user turn when there is older history to strip.
        # If the conversation has a single user turn, still strip its media —
        # that is the only recovery lever for a media-size rejection.
        protected: set[int] = set()
        if keep_recent and len(user_indices) > keep_recent:
            protected = set(user_indices[-keep_recent:])

        stripped_messages = 0
        stripped_images = 0
        stripped_documents = 0
        changed = False
        for idx, msg in enumerate(self._history):
            if idx in protected:
                continue
            images = list(getattr(msg, "images", []) or [])
            documents = list(getattr(msg, "documents", []) or [])
            if not images and not documents:
                continue
            stripped_images += len(images)
            stripped_documents += len(documents)
            stripped_messages += 1
            changed = True
            note_bits: list[str] = []
            if images:
                note_bits.append(f"{len(images)} image(s)")
            if documents:
                note_bits.append(f"{len(documents)} document(s)")
            note = (
                f"[media-size recovery] Removed historical {' and '.join(note_bits)} "
                "from context after a provider media-size rejection. "
                "Re-attach a smaller asset if the original media is still required."
            )
            content = str(msg.content or "").rstrip()
            if note not in content:
                content = f"{content}\n\n{note}".strip() if content else note
            self._history[idx] = LLMMessage(
                role=msg.role,
                content=content,
                name=msg.name,
                tool_call_id=msg.tool_call_id,
                tool_calls=msg.tool_calls,
                images=[],
                documents=[],
                phase=getattr(msg, "phase", None),
                provider_items=getattr(msg, "provider_items", None),
            )

        if changed:
            self._rebuild_history_token_cache()
            self._last_actual_prompt_tokens = 0
            logger.info(
                "[MediaSizeRecovery] stripped_messages=%d images=%d documents=%d",
                stripped_messages,
                stripped_images,
                stripped_documents,
            )
        return {
            "messages": stripped_messages,
            "images": stripped_images,
            "documents": stripped_documents,
        }

    def apply_cheap_context_ladder(
        self,
        *,
        usage_ratio: float | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Run snip then collapse before expensive full compaction.

        Order intentionally mirrors Claude Code query.ts:
        tool-budget (caller) -> snip -> collapse drain -> autocompact.
        """
        snip_stats = self.snip_history_if_needed(usage_ratio=usage_ratio, force=force)
        # Recompute ratio after snip so collapse threshold reflects freed space.
        total_budget = max(1, int(getattr(self._budget, "total", 0) or 0))
        post_snip_ratio = (
            float(usage_ratio)
            if usage_ratio is not None and not snip_stats.get("removed")
            else (self.token_usage / total_budget)
        )
        collapse_stats = self.collapse_old_tool_results(
            usage_ratio=post_snip_ratio,
            force=force or bool(snip_stats.get("removed")),
        )
        return {
            "snip": snip_stats,
            "collapse": collapse_stats,
            "saved_tokens": int(snip_stats.get("saved_tokens", 0) or 0)
            + int(collapse_stats.get("saved_tokens", 0) or 0),
        }

    def apply_tool_result_budget(self, max_tokens: int | None = None) -> int:
        """Enforce a global budget on tool-result tokens.

        Replaces the oldest large tool results first until the total is within
        budget. Recent results (last 5) stay intact.

        Prefer recoverable contentReplacement (disk-backed preview or stable
        preview) over hard one-line truncation so the model can re-read the
        original payload. Hard truncation is only a last-resort fallback when
        no recoverable replacement shrinks the content.

        Returns the number of results replaced/truncated.
        """
        budget = (
            max_tokens
            or getattr(self, "TOKEN_BUDGET_TOOL_RESULTS", None)
            or int(self._budget.total * 0.4)
        )

        tool_results: list[tuple[int, int, str]] = []
        for i, msg in enumerate(self._history):
            if msg.role == "tool":
                content_str = str(msg.content) if msg.content else ""
                tokens = _estimate_content_tokens(content_str)
                tool_results.append((i, tokens, content_str))

        if not tool_results:
            return 0

        total_tokens = sum(t[1] for t in tool_results)
        original_total = total_tokens
        if total_tokens <= budget:
            return 0

        keep_recent = 5
        truncatable = tool_results[:-keep_recent] if len(tool_results) > keep_recent else []

        replaced = 0
        for idx, tokens, content in truncatable:
            if total_tokens <= budget:
                break

            msg = self._history[idx]
            call_id = str(msg.tool_call_id or "").strip()
            tool_name = str(msg.name or "unknown")
            content = str(msg.content or "")
            if not content:
                continue

            # Already compact / previously replaced — keep frozen content.
            if (
                content.startswith("[Tool result truncated")
                or content.startswith("[Old tool result")
                or content.startswith("[Collapsed tool result:")
                or content.lstrip().startswith("<tool_result_cache_entry")
                or content.startswith("<persisted-tool-result>")
                or content.startswith("<persisted-tool-result-preview>")
            ):
                continue

            # Frozen replacement for previously seen tool_call_id.
            replacement = None
            if call_id and call_id in self._tool_result_replacements:
                cached = self._tool_result_replacements[call_id]
                if cached and cached != content and len(cached) < len(content):
                    replacement = cached

            # Prefer disk-backed recoverable preview (cc contentReplacement).
            if replacement is None and call_id:
                persisted = try_persist_tool_result(content, call_id, tool_name)
                if (
                    persisted
                    and persisted != content
                    and len(persisted) < len(content)
                    and (
                        persisted.startswith("<persisted-tool-result>")
                        or persisted.startswith("<persisted-tool-result-preview>")
                    )
                ):
                    replacement = persisted

            # Stable deterministic preview when disk persistence is unavailable.
            if replacement is None and call_id:
                stable = self._stable_tool_result_replacement(call_id, tool_name, content)
                if stable and stable != content and len(stable) < len(content):
                    replacement = stable

            # Last resort: hard one-line truncate when no recoverable path works.
            if replacement is None:
                if tokens <= 8:
                    continue
                replacement = (
                    f"[Tool result truncated — was {len(content)} chars; "
                    "re-run the tool with a narrower query if full output is needed]"
                )

            if not replacement or replacement == content or len(replacement) >= len(content):
                continue

            self._history[idx] = LLMMessage(
                role="tool",
                content=replacement,
                name=msg.name,
                tool_call_id=msg.tool_call_id,
                tool_calls=msg.tool_calls,
                images=list(msg.images),
                documents=list(msg.documents),
            )
            total_tokens -= tokens
            total_tokens += _estimate_content_tokens(replacement)
            replaced += 1
            if call_id:
                self._tool_result_replacements[call_id] = replacement
                self._tool_result_seen_ids.add(call_id)

        if replaced > 0:
            self._rebuild_history_token_cache()
            saved = original_total - total_tokens
            logger.info(
                "[ToolBudget] Replaced %d/%d tool results with recoverable previews "
                "(saved ~%d tokens)",
                replaced,
                len(tool_results),
                saved,
            )

        return replaced


    def get_budget_snapshot(
        self,
        state: AgentState,
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        workspace_root = self._workspace_root_for_state(state)
        system_tokens = _estimate_content_tokens(build_stable_prompt(workspace_root))
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
        for pack in select_prompt_packs(prompt_context=getattr(state, "prompt_context", None)):
            system_tokens += _estimate_content_tokens(pack.content)

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
                skills_tokens = _estimate_content_tokens(
                    executor.build_skill_context(budget=self._budget.active_skills)
                )
            except Exception:
                skills_tokens = 0

        if state.retrieved_chunks:
            rag_tokens = _estimate_content_tokens("\n---\n".join(state.retrieved_chunks))

        for index in self._get_history_within_budget_indices():
            history_tokens += self._history_token_estimates[index]

        if tool_schemas:
            tools_tokens = _estimate_content_tokens(str(tool_schemas))

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
                "tool_result_cache_saved": self._tool_result_cache_edit_saved_tokens_total,
                "tool_result_cache_compacted": self._tool_result_cache_edit_compacted_total,
                "tool_result_cache_last_saved": self._last_tool_result_cache_edit_saved_tokens,
            },
        }

    def needs_compaction(
        self,
        state: AgentState | None = None,
        *,
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> bool:
        threshold = self._effective_compaction_threshold(state)
        # Reserve room for the model's reply before comparing overall usage to
        # the threshold — mirrors cc autoCompact subtracting the output budget,
        # and matches history_budget (which already subtracts response_reserve).
        # Without this the overall-context ratio fires too late and can leave no
        # room for the response. The active prompt can be dominated by system,
        # skill, RAG, or memory sections, so this ratio is still the primary
        # signal; the history-budget check guards against silent oldest-message
        # drops during history selection.
        effective_total = max(self._budget.total - self._budget.response_reserve, 1)
        history_budget = max(self._budget.history_budget, 1)
        snapshot = self.get_budget_snapshot(state or AgentState(user_message=""), tool_schemas=tool_schemas)

        return (
            int(snapshot.get("used", 0)) > effective_total * threshold
            or self._history_tokens_total > history_budget * threshold
        )

    def _restore_recent_files_after_compaction(self, state: AgentState | None = None) -> None:
        self._persistent_notes[:] = [
            note for note in self._persistent_notes
            if note.get("kind") != "post_compaction_restore"
        ]
        if state is None:
            self._restore_structured_state_after_compaction(state)
            return
        workspace_root = self._workspace_root_for_state(state)
        if workspace_root is None:
            self._restore_structured_state_after_compaction(state)
            return

        try:
            root = Path(workspace_root).resolve()
        except OSError:
            self._restore_structured_state_after_compaction(state)
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

        if restored_blocks:
            self._persistent_notes.append(
                {
                    "kind": "post_compaction_restore",
                    "title": "Post-compaction restored file context",
                    "content": "\n\n".join(restored_blocks),
                }
            )
        self._restore_structured_state_after_compaction(state, root)

    def _restore_structured_state_after_compaction(
        self,
        state: AgentState | None = None,
        root: Path | None = None,
    ) -> None:
        self._persistent_notes[:] = [
            note for note in self._persistent_notes
            if note.get("kind") != "post_compaction_structured_state"
        ]
        if state is None:
            return

        blocks = self._post_compaction_structured_state_blocks(state, root)
        if not blocks:
            return
        content = "\n\n".join(blocks)
        if len(content) > POST_COMPACTION_STATE_CHARS:
            content = content[:POST_COMPACTION_STATE_CHARS] + "\n... [structured state truncated]"
        self._persistent_notes.append(
            {
                "kind": "post_compaction_structured_state",
                "title": "Post-compaction structured task state",
                "content": content,
            }
        )

    def _post_compaction_structured_state_blocks(
        self,
        state: AgentState,
        root: Path | None = None,
    ) -> list[str]:
        prompt_context = self._prompt_context(state)
        session_id = str(
            prompt_context.get("session_id")
            or prompt_context.get("minicode_session_id")
            or prompt_context.get("conversation_id")
            or ""
        ).strip()
        root = root or self._workspace_root_for_state(state)
        session_ids = [session_id] if session_id else ["default"]

        blocks: list[str] = []
        for title, payload in (
            ("Active plan snapshot", self._load_first_plan_snapshot(root, session_ids)),
            ("Active todo snapshot", self._load_first_todo_snapshot(root, session_ids)),
        ):
            if payload is None:
                continue
            rendered = json.dumps(payload, ensure_ascii=False, indent=2)
            blocks.append(
                f"### {title}\n"
                "This is structured session state restored after compaction. "
                "Use it as the current plan/todo checkpoint, not just prose summary.\n"
                f"```json\n{rendered}\n```"
            )
        invoked_skills = self._build_invoked_skills_state_block(state)
        if invoked_skills:
            blocks.append(invoked_skills)
        return blocks

    def _build_invoked_skills_state_block(self, state: AgentState) -> str:
        skills = self._collect_invoked_skill_payloads(state)
        if not skills:
            return ""
        rendered: list[str] = [
            "### Invoked skills snapshot",
            "The following SKILL.md workflows were invoked in this session. "
            "Continue to follow these guidelines unless newer user instructions supersede them.",
        ]
        for skill in skills[:POST_COMPACTION_MAX_SKILLS_TO_RESTORE]:
            name = str(skill.get("name") or "").strip()
            if not name:
                continue
            path = str(skill.get("path") or "").strip()
            content = str(skill.get("content") or "").strip()
            if len(content) > POST_COMPACTION_MAX_CHARS_PER_SKILL:
                omitted = len(content) - POST_COMPACTION_MAX_CHARS_PER_SKILL
                content = (
                    content[:POST_COMPACTION_MAX_CHARS_PER_SKILL].rstrip()
                    + f"\n... [{omitted} skill chars omitted after post-compaction restore limit]"
                )
            rendered.append(
                f'<skill name="{_xml_text(name)}" path="{_xml_text(path)}">\n'
                f"{content}\n"
                "</skill>"
            )
        return "\n\n".join(part for part in rendered if part.strip())

    def _collect_invoked_skill_payloads(self, state: AgentState) -> list[dict[str, Any]]:
        manager = self._skill_manager
        if manager is None:
            return []

        payloads: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(raw: Any) -> None:
            if not isinstance(raw, dict):
                return
            name = str(raw.get("name") or raw.get("skill_name") or "").strip()
            if not name or name in seen:
                return
            content = str(raw.get("content") or "").strip()
            if not content:
                return
            seen.add(name)
            payloads.append(
                {
                    "name": name,
                    "path": str(raw.get("path") or raw.get("source_path") or ""),
                    "content": content,
                }
            )

        get_invoked = getattr(manager, "get_invoked_skills", None)
        if callable(get_invoked):
            try:
                for raw in get_invoked():
                    add(raw)
            except Exception as exc:
                logger.debug("Failed to collect invoked skills: %s", exc)

        get_payload = getattr(manager, "get_skill_payload", None)
        if callable(get_payload):
            for name in getattr(state, "active_skills", []) or []:
                if str(name or "").strip() in seen:
                    continue
                try:
                    add(get_payload(str(name)))
                except Exception as exc:
                    logger.debug("Failed to hydrate invoked skill %s: %s", name, exc)
        return payloads

    @classmethod
    def _load_first_plan_snapshot(
        cls,
        root: Path | None,
        session_ids: list[str],
    ) -> dict[str, Any] | None:
        for session_id in session_ids:
            payload = cls._load_persisted_plan_snapshot(root, session_id)
            if payload is not None:
                return payload
        return None

    @classmethod
    def _load_first_todo_snapshot(
        cls,
        root: Path | None,
        session_ids: list[str],
    ) -> list[dict[str, str]] | None:
        for session_id in session_ids:
            payload = cls._load_persisted_todo_snapshot(root, session_id)
            if payload is not None:
                return payload
        return None

    @staticmethod
    def _load_persisted_plan_snapshot(root: Path | None, session_id: str) -> dict[str, Any] | None:
        if root is None or not session_id:
            return None
        path = root / ".minicode" / "plans" / f"{session_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        steps = payload.get("steps")
        if not isinstance(steps, list) or not steps:
            return None
        return {
            "plan_id": str(payload.get("plan_id") or f"plan-{session_id}"),
            "status": str(payload.get("status") or ""),
            "current_step": payload.get("current_step"),
            "steps": [
                {
                    "id": str(step.get("id") or ""),
                    "title": str(step.get("title") or step.get("step") or ""),
                    "status": str(step.get("status") or ""),
                }
                for step in steps
                if isinstance(step, dict) and str(step.get("title") or step.get("step") or "").strip()
            ],
        }

    @staticmethod
    def _load_persisted_todo_snapshot(root: Path | None, session_id: str) -> list[dict[str, str]] | None:
        if root is None or not session_id:
            return None
        path = root / ".minicode" / "todos" / f"{session_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, list) or not payload:
            return None
        todos: list[dict[str, str]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            todos.append(
                {
                    "id": str(item.get("id") or ""),
                    "content": content,
                    "activeForm": str(item.get("activeForm") or ""),
                    "status": str(item.get("status") or ""),
                    "priority": str(item.get("priority") or ""),
                }
            )
        return todos or None

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

    # ------------------------------------------------------------------
    # Context Ledger: manual compact, fork, and side query (plan §12)
    # ------------------------------------------------------------------

    def fork_from(self, message_index: int) -> ContextBuilder:
        """Create a branched context from a specific message index.

        Returns a new ContextBuilder whose history contains only messages
        up to and including ``message_index``.  The original builder is
        not modified.

        This enables "fork from turn N" — the user can explore an
        alternative path without losing the main conversation.
        """
        cloned = clone_context_builder(self)
        if message_index < 0:
            message_index = max(0, len(self._history) + message_index)
        cloned._history = list(self._history[: max(0, message_index + 1)])
        cloned._rebuild_history_token_cache()
        cloned._last_actual_prompt_tokens = 0
        return cloned

    async def side_query(self, query: str, *, focus: str = "") -> str:
        """Run a transient read-only query without modifying the main context.

        Clones the current context, optionally compacts it with a focus
        string, and sends a single LLM request.  The result is returned
        as a string; the original context is untouched.

        Use cases:
        * "Does the current context contain enough information to answer X?"
        * "Summarize what we know about Y so far."
        * "What files have we read?"
        """
        forked = clone_context_builder(self)
        if focus:
            await forked.compact(focus=focus)
        messages = forked.build_messages()
        messages.append(LLMMessage(role="user", content=query))
        if self._llm is None:
            return "No LLM available for side query."
        try:
            response = await self._llm.chat(messages)
            content = getattr(response, "content", None)
            if isinstance(content, list):
                # Extract text from multimodal response
                return "\n".join(
                    block.get("text", "") for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            return str(content or "")
        except Exception as exc:
            logger.warning("Side query failed: %s", exc)
            return f"Side query failed: {exc}"

    def _aligned_recent_start(self, keep_recent: int) -> int:
        """Start index of the recent-history slice, aligned to tool-call groups.

        A naive ``-keep_recent`` cut can land inside an assistant(tool_calls) →
        tool → tool group; the stranded tool results would then be dropped as
        orphans by reconcile_dangling_tool_calls. Move the cut earlier so the
        group's assistant message stays with its tool results.
        """
        start = len(self._history) - keep_recent
        while start > 0 and self._history[start].role == "tool":
            start -= 1
        return start

    async def compact(self, focus: str = "", restore_state: AgentState | None = None) -> str:
        """Time-decay compaction: compress old history, preserve recent context.

        Creates new compacted messages instead of mutating originals in-place
        (Hermes pattern: never irreversibly destroy information).
        """
        clear_system_prompt_sections()
        keep_recent = self._agent_settings.history_keep_recent
        recent_start = self._aligned_recent_start(keep_recent)

        if recent_start <= 0:
            return "对话较短，无需压缩"

        self._compaction_count += 1

        total = len(self._history)

        # Build compacted versions of old messages — create new objects,
        # do NOT mutate the originals in-place.
        early_messages: list[LLMMessage] = []
        for idx, message in enumerate(self._history[:recent_start]):
            age_ratio = 1.0 - (idx / max(total, 1))  # 0=最新, 1=最旧

            if message.role == "tool" and message.content:
                tool_name = message.name or "unknown"
                content_len = len(message.content)

                if age_ratio > 0.7 and content_len > 100:
                    # Far history: keep tool name + brief summary
                    compacted = f"[{tool_name} 结果已压缩]"
                    # Preserve artifact reference if present
                    artifact_hint = "artifact" in message.content[:200].lower()
                    compacted += f"（{self._tool_result_compaction_hint(tool_name, artifact_hint=artifact_hint)}）"
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
                        + f"\n... [已压缩；{self._tool_result_compaction_hint(tool_name, artifact_hint='artifact' in message.content[:200].lower())}]"
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
        boundary = _compaction_boundary("auto", self._compaction_count, len(early_messages))
        summary_message = LLMMessage(
            role="user",
            content=(
                f"{SUMMARY_PREFIX}\n\n"
                f"{boundary}\n"
                f"[对话历史摘要 - 第 {self._compaction_count} 次压缩]\n"
                f"{compressed_summary}"
            ),
        )
        recent = self._history[recent_start:]
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
        recent_start = self._aligned_recent_start(keep_recent)

        if recent_start <= 0:
            # Short history: rule-based truncation (no LLM needed)
            new_history: list[LLMMessage] = []
            for message in self._history:
                if message.role == "tool" and message.content and len(message.content) > 240:
                    tool_name = message.name or "tool"
                    compacted = (
                        f"[{tool_name} 结果已压缩；"
                        f"{self._tool_result_compaction_hint(tool_name, artifact_hint='artifact' in message.content[:200].lower())}] "
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

        early = self._history[:recent_start]
        recent = self._history[recent_start:]

        # Try LLM-based summarization first (30s timeout).
        # If the LLM call itself hits a prompt-too-long error, retry by
        # dropping the oldest API-round message groups (mirrors cc's
        # truncateHeadForPTLRetry). This prevents the compaction request
        # itself from deadlocking when the conversation is very large.
        MAX_PTL_RETRIES = 2
        llm_summary = ""
        summarizable = early
        if self._llm is not None:
            for ptl_attempt in range(MAX_PTL_RETRIES + 1):
                try:
                    llm_summary = await _asyncio.wait_for(
                        self._summarize_early(summarizable, focus="紧急压缩，保留关键事实"),
                        timeout=30.0,
                    )
                    break  # success
                except Exception as exc:
                    exc_str = str(exc).lower()
                    is_ptl = any(
                        marker in exc_str
                        for marker in ("prompt too long", "context_length", "too long", "413")
                    )
                    if is_ptl and ptl_attempt < MAX_PTL_RETRIES and len(summarizable) > 4:
                        # Drop oldest ~25% of messages and retry
                        drop_count = max(1, len(summarizable) // 4)
                        while (
                            drop_count < len(summarizable)
                            and summarizable[drop_count].role == "tool"
                        ):
                            drop_count += 1
                        summarizable = summarizable[drop_count:]
                        logger.info(
                            "[Compaction] PTL retry %d: dropped %d oldest messages, %d remaining",
                            ptl_attempt + 1, drop_count, len(summarizable),
                        )
                        continue
                    logger.debug("Emergency LLM summarization failed, using fallback: %s", exc)
                    break

        # Fall back to rule-based extraction if LLM failed
        if not llm_summary:
            facts: list[str] = []
            for message in summarizable[-12:]:
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
            f"{_compaction_boundary('emergency', self._compaction_count, len(early))}\n"
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
                    phase=message.phase,
                    name=message.name,
                    tool_calls=message.tool_calls,
                    tool_call_id=message.tool_call_id,
                    provider_items=list(message.provider_items),
                    images=list(message.images),
                    documents=list(message.documents),
                ))
            elif message.role == "tool" and message.content and len(message.content) > 360:
                tool_name = message.name or "tool"
                compacted_history.append(LLMMessage(
                    role=message.role,
                    content=(
                        f"[{tool_name} 结果已压缩；"
                        f"{self._tool_result_compaction_hint(tool_name, artifact_hint='artifact' in message.content[:200].lower())}] "
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

                # Cache-sharing optimisation (mirrors cc's runForkedAgent for
                # compact): instead of sending a bare user message that shares
                # zero prefix with the main conversation, prepend the system
                # prompt and the most recent 2 history messages as a cache
                # prefix.  When the provider has automatic prefix caching
                # (Anthropic, DeepSeek, OpenAI-compatible), the compaction
                # request hits the same cache as the main loop and avoids a
                # full re-process of the system prompt + tools schema.
                cache_messages: list[LLMMessage] = []
                # Reuse the stable system prompt as a cache prefix
                stable_prompt = build_stable_prompt()
                if stable_prompt:
                    cache_messages.append(
                        LLMMessage(role="system", content=stable_prompt)
                    )
                # Include the most recent messages from early as a cacheable
                # prefix — these are the same messages the main loop sends.
                recent_for_cache = early[-2:] if len(early) > 2 else early
                cache_messages.extend(recent_for_cache)
                # The actual compaction request
                cache_messages.append(LLMMessage(role="user", content=prompt))

                output = await simple_chat_cache.simple_chat(
                    self._llm,
                    cache_messages,
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
        clean = [fact for fact in facts if fact]
        if not clean:
            return
        # CC-aligned: autocompact facts are semantic conclusions, so they go to
        # the durable file track (auto_facts.md) rather than a vector store.
        append_facts = getattr(self._memory_manager, "append_facts", None)
        if callable(append_facts):
            try:
                append_facts(clean)
            except Exception as exc:
                logger.debug("Failed to append MemDir facts to file memory: %s", exc)
        for fact in clean:
            logger.info("AutoCompact extracted MemDir fact: %s", fact)

    def _get_history_within_budget(self) -> list[LLMMessage]:
        selected = [
            self._history[index]
            for index in self._get_history_within_budget_indices()
            if not _is_prompt_instruction_role(self._history[index].role)
        ]
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
        self._tool_result_replacements.clear()
        self._tool_result_seen_ids.clear()
        self._last_tool_result_cache_edit_saved_tokens = 0
        self._tool_result_cache_edit_saved_tokens_total = 0
        self._tool_result_cache_edit_compacted_total = 0
        self._last_assistant_ts = 0.0
        self._last_sent_runtime_context = ""
        self._prepared_prompt_parts = None
        self._prepared_prompt_state = None
        self._last_prompt_section_summary = {}

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
                    "phase": message.phase,
                    "provider_items": _sanitize_provider_items(message.provider_items),
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
            "context_ledger": self.context_ledger(),
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
        self._history = [
            message
            for message in messages
            if not _is_prompt_instruction_role(getattr(message, "role", ""))
        ] + self._history
        self._rebuild_history_token_cache()

    @staticmethod
    def sanitize_snapshot_history(raw_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sanitized: list[dict[str, Any]] = []
        for raw in raw_history:
            if not isinstance(raw, dict):
                continue
            role = _normalize_message_role(raw.get("role", "user"))
            content = _message_content_text(raw.get("content", ""))
            if _is_prompt_instruction_role(role):
                continue
            raw = {**raw, "role": role, "content": content}
            raw["provider_items"] = _sanitize_provider_items(raw.get("provider_items"))
            raw["phase"] = str(raw.get("phase") or "")[:40]
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
                    role=_normalize_message_role(raw.get("role", "user")),
                    content=_message_content_text(raw.get("content", "")),
                    name=raw.get("name"),
                    tool_call_id=raw.get("tool_call_id"),
                    tool_calls=parsed_tool_calls,
                    phase=str(raw.get("phase") or "")[:40],
                    provider_items=_sanitize_provider_items(raw.get("provider_items")),
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
        # Track monotonic process time so system clock corrections cannot
        # _maybe_time_based_microcompact can detect cold-cache gaps.
        if message.role == "assistant":
            self._last_assistant_ts = time.monotonic()
        estimate = int(
            (
                self._estimate_message_tokens(
                    raw_content if raw_content is not None else message.content,
                    message.tool_calls,
                )
                + estimate_native_attachments(message.images, message.documents)[0]
            )
            * self._token_calibration_factor
        )
        self._history_token_estimates.append(estimate)
        self._history_tokens_total += estimate

    def _rebuild_history_token_cache(self) -> None:
        self._history_token_estimates = [
            int(
                (
                    self._estimate_message_tokens(message.content, message.tool_calls)
                    + estimate_native_attachments(message.images, message.documents)[0]
                )
                * self._token_calibration_factor
            )
            for message in self._history
        ]
        self._history_tokens_total = sum(self._history_token_estimates)

    def _first_user_anchor_index(self) -> int | None:
        """Index of the earliest user message — the original task anchor."""
        for index, message in enumerate(self._history):
            if message.role == "user":
                return index
        return None

    def _get_history_within_budget_indices(self) -> list[int]:
        budget = self._budget.history_budget
        selected: list[int] = []
        used = 0

        for index in range(len(self._history) - 1, -1, -1):
            message_tokens = self._history_token_estimates[index] + 10
            if selected and used + message_tokens > budget:
                break
            selected.append(index)
            used += message_tokens

        selected.reverse()
        # Never silently drop the original task: if the recency window pushed the
        # earliest user request out of budget, keep it as an anchor so the model
        # never loses what it was asked to do (cc never drops the task silently).
        anchor = self._first_user_anchor_index()
        if anchor is not None and anchor not in selected:
            selected.insert(0, anchor)
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
        # For all non-string types (dict, list, tuple, set, int, etc.),
        # convert to string representation and use the CJK-aware estimator.
        # Previously this used len(content) // 4 for sized objects, which
        # returned the number of items (e.g., dict keys) instead of character
        # count, causing massive token underestimation for dict/set objects.
        return _estimate_content_tokens(str(content))
