from __future__ import annotations

import html
import json
import logging
import os
import re
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
from backend.agent.prompting import (
    COMPACTION_SYSTEM_PROMPT,
    PromptParts,
    PromptBuilderV2,
    build_compaction_prompt,
    build_static_environment_info,
    build_stable_prompt,
    clear_system_prompt_sections,
    detect_project_type,
    summarize_prompt_sections,
)
from backend.agent.prompt_cache import prompt_cache_usage_stats
from backend.agent.tool_result_persistence import persist_tool_result, try_persist_tool_result
from backend.tools.base import ToolResult

logger = logging.getLogger(__name__)


def clone_context_builder(builder: "ContextBuilder") -> "ContextBuilder":
    """Clone reusable prompt/history state for branch-style agent runs."""
    cloned = copy(builder)
    for name in (
        "_history",
        "_history_token_estimates",
        "_persistent_notes",
        "_read_file_hashes",
        "_last_prompt_section_summary",
    ):
        if hasattr(builder, name):
            setattr(cloned, name, deepcopy(getattr(builder, name)))
    return cloned

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
POST_COMPACTION_RESTORE_TOOLS = frozenset({"read_file", "edit_file", "write_file"})

# Per-message tool result budget: when a single user message's tool_result
# blocks together exceed this many characters, the largest fresh results are
# persisted to disk and replaced with previews (mirrors cc's
# getPerMessageBudgetLimit / enforcePerMessageBudget).  Each message is
# evaluated independently — a 50K result in one message and a 50K result in
# another are both under budget and untouched.
PER_MESSAGE_TOOL_RESULT_BUDGET_CHARS = 200_000

RUNTIME_CONTEXT_STRIP_KEEP_RECENT_USER_TURNS = 1
_RUNTIME_BLOCK_RE = re.compile(
    r"\A(?:\s*(?:"
    r"<system-reminder>[\s\S]*?</system-reminder>|"
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
_TRAILING_RUNTIME_BLOCK_RE = re.compile(
    r"(?:\s*\n?\s*)?<system-reminder>[\s\S]*?</system-reminder>\s*\Z",
    re.IGNORECASE,
)
_RUNTIME_REMINDER_MARKERS = (
    "<environment_context>",
    "<collaboration_mode>",
    "<agent_mode>",
    "<turn_aborted>",
    "<tool_runtime_context>",
    "<task_status>",
    "<retrieved_context>",
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


def _detect_project_type(cwd: Path) -> str:
    return detect_project_type(cwd)


def _estimate_content_tokens(content: str) -> int:
    """Estimate tokens with Pi's provider-neutral ``chars / 4`` heuristic."""
    if not content:
        return 0
    return (len(content) + 3) // 4


def _xml_text(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _strip_leading_runtime_context(content: str) -> str:
    text = str(content or "")
    if not _has_leading_runtime_context(text):
        return text
    stripped = _RUNTIME_BLOCK_RE.sub("", text, count=1).lstrip()
    return stripped


def _strip_runtime_context(content: str) -> str:
    """Remove MiniCode's old and current reminder forms.

    Current turns use CC's leading ``<system-reminder>`` prefix. Older
    snapshots from the previous implementation placed that same block after
    the user's text, so the trailing compatibility pass is intentionally
    limited to reminders containing a runtime marker. User-authored XML is
    therefore not treated as runtime state merely because it uses the same
    tag name.
    """
    text = str(content or "")
    stripped = _strip_leading_runtime_context(text)
    if stripped != text:
        return stripped
    match = _TRAILING_RUNTIME_BLOCK_RE.search(text)
    if not match:
        return text
    block = match.group(0).lower()
    if not any(marker in block for marker in _RUNTIME_REMINDER_MARKERS):
        return text
    return text[: match.start()].rstrip()


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
        memory_manager: Any | None = None,
        llm: Any | None = None,
        skill_manager: Any | None = None,
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
        self._memory_manager = memory_manager
        self._llm = llm
        self._attachment_store = AttachmentStore()
        self._last_actual_prompt_tokens = 0
        # Claude Code keeps readFileState across user turns so an interrupted
        # task can resume edits without rereading unchanged files. The content
        # hash remains an optimistic guard: writes still fail if the file has
        # changed since the last successful read.
        self._read_file_hashes: dict[str, str] = {}
        self._prefer_stateful_history = False
        self._prepared_prompt_parts: PromptParts | None = None
        self._prepared_prompt_state: AgentState | None = None
        self._last_prompt_section_summary: dict[str, Any] = {}

    def _get_project_guidelines(self, workspace_root: Path | None = None) -> str:
        """Load guidelines through the signature-validated shared cache.

        ``claude_md.load_project_guidelines`` already reuses unchanged bundles
        and invalidates them from the file watcher.  A second time-based cache
        here delayed authoritative instruction changes and added an unsourced
        freshness threshold.
        """
        from backend.agent.claude_md import load_project_guidelines

        return load_project_guidelines(workspace_root)

    def _consume_skill_injections(self, state: AgentState) -> list[str]:
        """Render explicit skills as Codex-style contextual user fragments."""
        prompt_context = state.prompt_context if isinstance(state.prompt_context, dict) else {}
        payloads = prompt_context.pop("skill_injections", [])
        if not isinstance(payloads, list):
            return []
        fragments: list[str] = []
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            name = str(payload.get("name") or "").strip()
            path = str(payload.get("path") or "").strip()
            content = str(payload.get("content") or "").strip()
            if not name or not path or not content:
                continue
            fragments.append(
                "<skill>\n"
                f"<name>{_xml_text(name)}</name>\n"
                f"<path>{_xml_text(path)}</path>\n"
                f"{content}\n"
                "</skill>"
            )
        return fragments

    def _build_skill_catalog(self) -> str:
        executor = self._skill_executor
        if executor is None and self._skill_manager is not None:
            from backend.skills.executor import SkillExecutor

            executor = SkillExecutor(self._skill_manager)
        build_catalog = getattr(executor, "build_layer1_summary", None)
        return str(build_catalog() or "") if callable(build_catalog) else ""

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
        skill_injections = self._consume_skill_injections(state)
        workspace_root = self._workspace_root_for_state(state)
        prompt_parts = self._build_prompt_parts(state, workspace_root)
        attachment_plan = build_attachment_input_plan(
            state.attachments,
            llm=self._llm,
            attachment_store=self._attachment_store,
        )
        user_turn_content = self._build_user_turn_content(
            self._with_attachment_text_fallback(user_message, attachment_plan),
            state,
        )
        if not user_turn_content.strip() and not attachment_plan.images and not attachment_plan.documents:
            return

        self._compact_old_user_runtime_context_for_cache()
        for skill_injection in skill_injections:
            self._append_history_message(
                LLMMessage(role="user", content=skill_injection),
                raw_content=skill_injection,
            )
        self._append_history_message(
            LLMMessage(
                role="user",
                content=user_turn_content,
                images=attachment_plan.images,
                documents=attachment_plan.documents,
            ),
            raw_content=user_turn_content,
        )
        self._compact_old_user_runtime_context_for_cache(
            keep_recent_user_turns=RUNTIME_CONTEXT_STRIP_KEEP_RECENT_USER_TURNS,
        )
        self._prepared_prompt_parts = prompt_parts
        self._prepared_prompt_state = state

    def append_user_context(self, content: str) -> None:
        """Append hook or runtime context after the active user turn."""
        text = str(content or "").strip()
        if not text:
            return
        if text.startswith("<system-reminder>"):
            self.append_user(text)
            return
        self.append_user(f"<system-reminder>\n{text}\n</system-reminder>")

    async def build(
        self,
        user_message: str | AgentState,
        state: AgentState | None = None,
    ) -> list[LLMMessage]:
        if isinstance(user_message, AgentState):
            active_state = user_message
        else:
            if state is None:
                raise TypeError("state is required when build() receives a user message")
            active_state = state
            await self.start_turn(user_message, active_state)

        messages: list[LLMMessage] = []
        if not self._prefer_stateful_history:
            self._enforce_per_message_tool_budget()

        workspace_root = self._workspace_root_for_state(active_state)
        if self._prepared_prompt_state is active_state and self._prepared_prompt_parts is not None:
            prompt_parts = self._prepared_prompt_parts
        else:
            prompt_parts = self._build_prompt_parts(active_state, workspace_root)
        self._prepared_prompt_parts = None
        self._prepared_prompt_state = None
        system_content = prompt_parts.render_system()
        plugin_instructions = self._build_plugin_instructions(active_state)
        self._refresh_active_user_runtime_context(active_state)
        self._compact_old_user_runtime_context_for_cache()

        # ── End of system prompt ────────────────────────────────────────────────
        # Retrieval remains agentic: memory and document context enter through
        # explicit tool results instead of an implicit per-turn injection.

        messages.append(LLMMessage(role="system", content=system_content))
        if plugin_instructions.strip():
            # Codex models an explicit plugin selection as a turn-scoped
            # developer fragment, ahead of the user's input and history.
            messages.append(
                LLMMessage(role="developer", content=plugin_instructions.strip())
            )
        history = self._get_history_within_budget()
        messages.extend(history)

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
            skill_context=self._build_skill_catalog(),
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
    ) -> str:
        runtime_context = ContextBuilder._build_runtime_context_prefix(state).strip()
        if not runtime_context:
            return user_message
        reminder = f"<system-reminder>\n{runtime_context}\n</system-reminder>"
        # CC treats the wrapper prefix as the reliable discriminator for
        # system-injected attachment/reminder text (ensureSystemReminderWrap /
        # smooshSystemReminderSiblings). Keep the real user text after that
        # prefix so old-turn cleanup can remove only injected context without
        # guessing at arbitrary tags inside the user's request.
        return f"{reminder}\n\n{user_message}" if user_message.strip() else reminder

    def _refresh_active_user_runtime_context(self, state: AgentState) -> bool:
        """Refresh the current turn's CC-style reminder before each model call."""
        for index in range(len(self._history) - 1, -1, -1):
            message = self._history[index]
            if message.role != "user" or message.tool_call_id or message.tool_calls:
                continue
            content = str(message.content or "")
            user_text = _strip_runtime_context(content)
            # Standalone hook/system reminders strip to empty. The active user
            # turn is the latest wrapped message that still has real user text.
            if user_text == content or not user_text:
                continue
            refreshed = self._build_user_turn_content(user_text, state)
            if refreshed == content:
                return False
            self._history[index] = LLMMessage(
                role=message.role,
                content=refreshed,
                name=message.name,
                tool_call_id=message.tool_call_id,
                tool_calls=message.tool_calls,
                images=list(message.images),
                documents=list(message.documents),
            )
            self._rebuild_history_token_cache()
            self._last_actual_prompt_tokens = 0
            return True
        return False

    @staticmethod
    def _build_runtime_context_prefix(state: AgentState) -> str:
        blocks = [
            ContextBuilder._build_environment_context_xml(state),
            ContextBuilder._build_collaboration_mode_block(state),
            ContextBuilder._build_agent_mode_block(state),
            ContextBuilder._build_turn_aborted_block(state),
            ContextBuilder._build_tool_runtime_context_block(state),
            ContextBuilder._build_task_status_block(state),
            ContextBuilder._build_retrieved_context_block(state),
        ]
        return "\n\n".join(block for block in blocks if block.strip())

    @staticmethod
    def _build_plugin_instructions(state: AgentState) -> str:
        prompt_context = ContextBuilder._prompt_context(state)
        plugins = prompt_context.get("plugin_injections")
        if not isinstance(plugins, list):
            return ""
        rendered: list[str] = []
        for plugin in plugins:
            if not isinstance(plugin, dict):
                continue
            display_name = str(plugin.get("display_name") or plugin.get("config_name") or "").strip()
            if not display_name:
                continue
            lines = [f"Capabilities from the `{display_name}` plugin:"]
            if bool(plugin.get("has_skills")):
                lines.append(f"- Skills from this plugin are prefixed with `{display_name}:`.")
            servers = sorted({
                str(name).strip()
                for name in (plugin.get("mcp_server_names") or [])
                if str(name).strip()
            }, key=str.casefold)
            if servers:
                lines.append(
                    "- MCP servers from this plugin available in this session: "
                    + ", ".join(f"`{name}`" for name in servers)
                    + "."
                )
            apps = sorted({
                str(name).strip()
                for name in (plugin.get("available_apps") or [])
                if str(name).strip()
            }, key=str.casefold)
            if apps:
                lines.append(
                    "- Apps from this plugin available in this session: "
                    + ", ".join(f"`{name}`" for name in apps)
                    + "."
                )
            if len(lines) == 1:
                continue
            lines.append("Use these plugin-associated capabilities to help solve the task.")
            rendered.append("\n".join(lines))
        return "\n\n".join(rendered)

    @staticmethod
    def _build_task_status_block(state: AgentState) -> str:
        summary = str(getattr(state, "task_summary", "") or "").strip()
        return f"Task status:\n{summary}" if summary else ""

    @staticmethod
    def _build_retrieved_context_block(state: AgentState) -> str:
        chunks = [
            str(chunk).strip()
            for chunk in (getattr(state, "retrieved_chunks", []) or [])
            if str(chunk).strip()
        ]
        return "Background knowledge:\n" + "\n---\n".join(chunks) if chunks else ""

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

        user_directories = environment.get("user_directories")
        if not isinstance(user_directories, dict):
            user_directories = prompt_context.get("user_directories")
        if not isinstance(user_directories, dict):
            user_directories = {}
        known_directory_env = {
            "desktop": "MINICODE_DESKTOP_DIR",
            "documents": "MINICODE_DOCUMENTS_DIR",
            "downloads": "MINICODE_DOWNLOADS_DIR",
        }
        normalized_user_directories = {
            name: str(user_directories.get(name) or os.environ.get(env_name) or "").strip()
            for name, env_name in known_directory_env.items()
        }
        normalized_user_directories = {
            name: value for name, value in normalized_user_directories.items() if value
        }

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
        if os.name == "nt":
            shell = (
                "powershell (Windows host, full access)"
                if mode in {"bypass", "full_access", "full-access", "danger-full-access"}
                else "pwsh (Linux workspace sandbox; use relative workspace paths)"
            )
        else:
            shell = str(
                environment.get("shell")
                or prompt_context.get("shell")
                or os.environ.get("SHELL")
                or "unknown"
            ).strip()
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
        if normalized_user_directories:
            directory_lines = "\n".join(
                f"    <{name}>{_xml_text(value)}</{name}>"
                for name, value in normalized_user_directories.items()
            )
            user_directories_block = f"  <user_directories>\n{directory_lines}\n  </user_directories>"
        else:
            user_directories_block = "  <user_directories />"

        return (
            "<environment_context>\n"
            f"  <cwd>{_xml_text(cwd)}</cwd>\n"
            f"  <shell>{_xml_text(shell)}</shell>\n"
            f"  <current_date>{_xml_text(current_date)}</current_date>\n"
            f"  <timezone>{_xml_text(timezone)}</timezone>\n"
            f"{user_directories_block}\n"
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
                "Workspace changes are disabled. Use read-only tools and return a plan.",
            ]
        else:
            lines = [
                "# Collaboration Mode: Default",
                "Follow the user's requested task and interaction mode.",
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
                "Workspace changes are allowed when the user requests them.",
            ],
            "plan": [
                "# Agent Mode: Plan",
                "Inspect and produce an implementation plan without editing the workspace.",
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
            or ""
        ).strip()
        deferred_tools_prompt_block = str(
            prompt_context.get("deferred_tools_prompt_block") or ""
        ).strip()
        if not tool_runtime_guidance and not deferred_tools_prompt_block:
            return ""
        parts = [
            "<tool_runtime_context>",
            "This is current-turn tool and resource context. Treat it as system-injected runtime data, not as a user request.",
        ]
        if tool_runtime_guidance:
            parts.extend(["", tool_runtime_guidance])
        if deferred_tools_prompt_block:
            parts.extend(["", deferred_tools_prompt_block])
        parts.append("</tool_runtime_context>")
        return "\n".join(parts)

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

    def append_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        result: ToolResult,
    ) -> None:
        content = result.to_context_string()
        # Artifact-backed results already preserve the full output and carry a
        # bounded, readable preview. Persisting that pointer-plus-preview a
        # second time would hide Pi's 50-KiB result contract behind another
        # unrelated preview file and leave two recovery mechanisms for one
        # result.
        if not result.artifact_id:
            content = try_persist_tool_result(content, tool_call_id, tool_name)

        # Codex-style: add structured status prefix matching function_call_result format
        status = "error" if result.is_error else "completed"
        if not content.startswith("<persisted-output>"):
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
                is_error=bool(result.is_error),
                images=list(result.images),
            ),
            raw_content=content,
        )
        if not self._prefer_stateful_history:
            self._enforce_per_message_tool_budget()
            self._compact_old_user_runtime_context_for_cache()

    def _compact_old_user_runtime_context_for_cache(
        self,
        *,
        keep_recent_user_turns: int = RUNTIME_CONTEXT_STRIP_KEEP_RECENT_USER_TURNS,
    ) -> int:
        """Keep runtime reminders only on the active user turn.

        Runtime context is sent as a CC-style system-reminder prefix on every
        request. Historical snapshots may contain it inside user turns or as a
        standalone synthetic user message. Strip the former and remove the
        latter so restored conversations cannot manufacture a user steer between
        a tool result and the next assistant response.
        """
        keep_recent = max(0, int(keep_recent_user_turns))
        wrapped_turn_indexes: list[int] = []
        for index, message in enumerate(self._history):
            if message.role != "user" or message.tool_call_id or message.tool_calls:
                continue
            content = str(message.content or "")
            stripped = _strip_runtime_context(content)
            if stripped != content and stripped:
                wrapped_turn_indexes.append(index)
        keep_indexes = set(wrapped_turn_indexes[-keep_recent:]) if keep_recent else set()
        active_turn_index = wrapped_turn_indexes[-1] if wrapped_turn_indexes else -1
        next_history: list[LLMMessage] = []
        changed = 0
        for index, message in enumerate(self._history):
            if message.role != "user" or message.tool_call_id or message.tool_calls:
                next_history.append(message)
                continue
            if index in keep_indexes:
                next_history.append(message)
                continue
            content = str(message.content or "")
            stripped = _strip_runtime_context(content)
            if stripped == content:
                next_history.append(message)
                continue
            if not stripped and index > active_turn_index:
                # Hook feedback and other standalone system reminders appended
                # after the active user turn belong to the current iteration.
                next_history.append(message)
                continue
            changed += 1
            if not stripped:
                continue
            next_history.append(
                LLMMessage(
                    role=message.role,
                    content=stripped,
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                    tool_calls=message.tool_calls,
                    images=list(message.images),
                    documents=list(message.documents),
                )
            )
        if changed:
            self._history = next_history
            self._rebuild_history_token_cache()
            self._last_actual_prompt_tokens = 0
        return changed

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

                # Skip results already replaced by disk-backed previews.
                if (
                    not call_id
                    or content.startswith("<persisted-output>")
                ):
                    continue

                persisted = persist_tool_result(content, call_id, tool_name, force=True)
                if persisted is not None:
                    new_content = persisted.preview
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
            elif name == "skill_context":
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

    def quarantine_latest_external_web_results(self) -> int:
        """Isolate the latest web-tool batch after a provider content refusal.

        Claude Code keeps hosted web search in a side query, and Codex marks web
        search as external-context pollution. Custom providers do not always
        offer hosted search, so preserve that boundary locally: remove only the
        latest paired web results while keeping the user request and assistant
        tool-call item intact for a different-source retry.
        """
        marker = "[External web result withheld after provider content-safety rejection.]"
        tool_indices: list[int] = []
        found_tool_call = False
        for idx in range(len(self._history) - 1, -1, -1):
            message = self._history[idx]
            if message.role == "tool":
                tool_indices.append(idx)
                continue
            if message.role == "assistant" and message.tool_calls:
                found_tool_call = True
            break

        if not found_tool_call:
            return 0

        changed = 0
        for idx in tool_indices:
            message = self._history[idx]
            tool_name = str(message.name or "").strip()
            if tool_name not in {"web_search", "web_fetch"}:
                continue
            if marker in str(message.content or ""):
                continue
            call_id = str(message.tool_call_id or "").strip()
            replacement = (
                f'<function_call_result status="completed" call_id="{call_id}">\n'
                f"{marker}\n"
                "Do not reuse the same query or URL. Search a different reputable "
                "source with a narrower query, then continue the user's original request.\n"
                "</function_call_result>"
            )
            self._history[idx] = LLMMessage(
                role=message.role,
                content=replacement,
                name=message.name,
                tool_call_id=message.tool_call_id,
                tool_calls=message.tool_calls,
                phase=message.phase,
                provider_items=list(message.provider_items),
                images=list(message.images),
                documents=list(message.documents),
            )
            changed += 1

        if changed:
            self._rebuild_history_token_cache()
            self._last_actual_prompt_tokens = 0
            logger.info("[ContentFilterRecovery] quarantined_web_results=%d", changed)
        return changed

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
            or ""
        )
        if tool_runtime_guidance:
            system_tokens += _estimate_content_tokens(tool_runtime_guidance)
        notes_tokens = sum(
            _estimate_content_tokens(str(note.get("content", ""))) for note in self._persistent_notes
        )
        skills_tokens = 0
        retrieved_tokens = 0
        history_tokens = 0
        tools_tokens = 0

        try:
            skills_tokens = _estimate_content_tokens(self._build_skill_catalog())
        except Exception:
            skills_tokens = 0

        if state.retrieved_chunks:
            retrieved_tokens = _estimate_content_tokens("\n---\n".join(state.retrieved_chunks))

        for index in self._get_history_within_budget_indices():
            history_tokens += self._history_token_estimates[index]

        if tool_schemas:
            tools_tokens = _estimate_content_tokens(str(tool_schemas))

        used = (
            system_tokens
            + notes_tokens
            + skills_tokens
            + retrieved_tokens
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
                "retrieved": retrieved_tokens,
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
        # Pi's session host owns compaction and uses one explicit reserve:
        # contextTokens > contextWindow - reserveTokens.
        snapshot = self.get_budget_snapshot(state or AgentState(user_message=""), tool_schemas=tool_schemas)
        trigger = max(1, self._budget.total - self._budget.response_reserve)
        return int(snapshot.get("used", 0)) > trigger

    def _restore_recent_files_after_compaction(self, state: AgentState | None = None) -> None:
        self._persistent_notes[:] = [
            note for note in self._persistent_notes
            if note.get("kind") not in {"post_compaction_restore", "post_compaction_structured_state"}
        ]
        if state is None:
            return
        workspace_root = self._workspace_root_for_state(state)
        if workspace_root is None:
            return

        try:
            root = Path(workspace_root).resolve()
        except OSError:
            return

        # Restore only what fits in the same context budget used by the turn
        # kernel. A separate fixed post-compaction quota would create a second
        # hidden context ladder.
        available_tokens = max(
            0,
            int(self._budget.total)
            - int(self._budget.response_reserve)
            - int(self.token_usage),
        )
        structured_blocks = self._post_compaction_structured_state_blocks(state, root)
        if structured_blocks:
            structured_content = "\n\n".join(structured_blocks)
            structured_tokens = self._estimate_content_tokens(structured_content)
            if structured_tokens <= available_tokens:
                self._persistent_notes.append(
                    {
                        "kind": "post_compaction_structured_state",
                        "title": "Post-compaction structured task state",
                        "content": structured_content,
                    }
                )
                available_tokens -= structured_tokens

        restored_blocks: list[str] = []
        for path in self._recent_workspace_file_paths(state, root):
            block = self._build_restored_file_block(path, root)
            if not block:
                continue
            block_tokens = self._estimate_content_tokens(block)
            if block_tokens > available_tokens:
                continue
            restored_blocks.append(block)
            available_tokens -= block_tokens

        if restored_blocks:
            self._persistent_notes.append(
                {
                    "kind": "post_compaction_restore",
                    "title": "Post-compaction restored file context",
                    "content": "\n\n".join(restored_blocks),
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
        return blocks

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
        numbered = [f"{index}: {line}" for index, line in enumerate(text.splitlines(), 1)]
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
            return str(await self._llm.simple_chat(messages)).strip()
        except Exception as exc:
            logger.warning("Side query failed: %s", exc)
            return f"Side query failed: {exc}"

    def _aligned_recent_start(self, keep_recent_tokens: int) -> int:
        """Find Pi's token-based cut point, aligned to tool-call groups.

        Walk backwards until the configured recent-token budget is reached.
        Never leave tool results without their assistant tool-call message.
        """
        start = len(self._history)
        accumulated = 0
        target = max(0, int(keep_recent_tokens))
        for index in range(len(self._history) - 1, -1, -1):
            start = index
            accumulated += self._history_token_estimates[index]
            if accumulated >= target:
                break
        while start > 0 and self._history[start].role == "tool":
            start -= 1
        if start > 0 and self._history[start].role == "assistant" and self._history[start].tool_calls:
            start -= 1
        return start

    async def compact(self, focus: str = "", restore_state: AgentState | None = None) -> str:
        """Summarize older entries while preserving a token-bounded recent tail."""
        clear_system_prompt_sections()
        keep_recent = self._agent_settings.compaction_keep_recent_tokens
        recent_start = self._aligned_recent_start(keep_recent)

        if recent_start <= 0:
            return "对话较短，无需压缩"

        self._compaction_count += 1

        early_messages = self._history[:recent_start]

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
        """Overflow recovery uses the same session compaction contract."""
        return await self.compact(restore_state=restore_state)

    async def _summarize_early(self, early: list[LLMMessage], focus: str = "") -> str:
        raw_text = format_compaction_history(early)
        if self._llm is not None and raw_text:
            try:
                prompt = build_compaction_prompt(
                    raw_text,
                    focus=focus,
                )

                cache_messages = [
                    LLMMessage(role="system", content=COMPACTION_SYSTEM_PROMPT),
                    LLMMessage(role="user", content=prompt),
                ]

                # Compaction is a forked model task in Claude Code and is not
                # served from a process-global result cache. Provider prefix
                # caching still applies to the repeated message prefix.
                output = (await self._llm.simple_chat(cache_messages)).strip()
                if output:
                    return self._consume_compaction_output(output)
                raise RuntimeError("Compaction model returned an empty summary")
            except Exception as exc:
                # A failed compaction must be visible to the caller. Returning
                # the complete pre-compact transcript as a "summary" makes the
                # context larger while hiding a retry failure from the circuit
                # breaker.
                logger.debug("LLM summarization failed: %s", exc)
                raise
        return raw_text

    def _consume_compaction_output(self, output: str) -> str:
        return parse_compaction_output(output).summary

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
        self._read_file_hashes.clear()
        self._compaction_count = 0
        self._prepared_prompt_parts = None
        self._prepared_prompt_state = None
        self._last_prompt_section_summary = {}
        # Provider-observed measurements describe the conversation being
        # discarded. A ContextBuilder is reused across conversation switches
        # (load_snapshot_partial calls clear), and token_usage takes the max of
        # the estimate and the last observed value, so leaving these set makes a
        # fresh conversation inherit the previous one's prompt size and compact
        # on its first turn.
        self._last_actual_prompt_tokens = 0

    def export_snapshot(
        self,
        *,
        max_messages: int | None = None,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """Export a resume snapshot with optional cheap pre-serialization bounds.

        Checkpoint persistence runs on the event-loop thread so it can remain
        cancellation-safe. Bounding history here prevents that synchronous
        durability boundary from first constructing and hashing an arbitrarily
        large conversation only to truncate it later in ``save_checkpoint``.
        """
        source_history = self._history
        if max_messages is not None:
            message_limit = max(0, int(max_messages))
            source_history = source_history[-message_limit:] if message_limit else []
        history_reversed: list[dict[str, Any]] = []
        remaining_chars = None if max_chars is None else max(0, int(max_chars))
        for message in reversed(source_history):
            content = message.content
            if message.role == "tool" and content and len(content) > 1200:
                content = (
                    f"{content[:700]}\n"
                    f"... [快照截断了 {len(content) - 1000} 字符；使用 artifact/read_artifact 查看完整输出] ...\n"
                    f"{content[-300:]}"
                )
            elif len(content) > 4000:
                content = f"{content[:2800]}\n... [快照截断了 {len(content) - 2800} 字符] ..."
            if remaining_chars is not None:
                if remaining_chars <= 0:
                    break
                content = content[:remaining_chars]
                remaining_chars -= len(content)
            history_reversed.append(
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
        history = list(reversed(history_reversed))
        return {
            "history": self.sanitize_snapshot_history(history),
            "persistent_notes": [dict(note) for note in self._persistent_notes],
            # CC bounds readFileState to 100 entries. Preserve the most recent
            # insertion order here so conversation snapshots cannot grow
            # without limit.
            "read_file_hashes": dict(list(self._read_file_hashes.items())[-100:]),
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
        raw_hashes = snapshot.get("read_file_hashes")
        if isinstance(raw_hashes, dict):
            self._read_file_hashes = {
                str(path): str(file_hash)
                for path, file_hash in list(raw_hashes.items())[-100:]
                if str(path).strip() and str(file_hash).strip()
            }

    def read_file_hashes(self) -> dict[str, str]:
        """Return the session-owned optimistic read state used by file tools."""
        return self._read_file_hashes

    def _append_history_message(
        self,
        message: LLMMessage,
        *,
        raw_content: Any | None = None,
    ) -> None:
        self._history.append(message)
        estimate = int(
            self._estimate_message_tokens(
                raw_content if raw_content is not None else message.content,
                message.tool_calls,
            )
            + estimate_native_attachments(message.images, message.documents)[0]
        )
        self._history_token_estimates.append(estimate)
        self._history_tokens_total += estimate

    def _rebuild_history_token_cache(self) -> None:
        self._history_token_estimates = [
            int(
                self._estimate_message_tokens(message.content, message.tool_calls)
                + estimate_native_attachments(message.images, message.documents)[0]
            )
            for message in self._history
        ]
        self._history_tokens_total = sum(self._history_token_estimates)

    def _get_history_within_budget_indices(self) -> list[int]:
        # Pi compacts the session before the provider call; it does not silently
        # drop older messages through a second history-only budget.
        return list(range(len(self._history)))

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
        # convert to string representation before applying the same estimator.
        # Previously this used len(content) // 4 for sized objects, which
        # returned the number of items (e.g., dict keys) instead of character
        # count, causing massive token underestimation for dict/set objects.
        return _estimate_content_tokens(str(content))
