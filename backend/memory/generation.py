"""MiniCode-style two-phase long-term memory generation coordinator."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from filelock import Timeout as FileLockTimeout

from backend.atomic_io import atomic_write_text
from backend.async_cleanup import cancel_and_drain
from backend.llm.base import LLMMessage, SideQueryOptions
from backend.memory.consolidation_agent import run_memory_consolidation_agent
from backend.memory.file_memory import FileMemory
from backend.memory.job_store import JobClaim, MEMORY_DB_NAME, MemoryJobStore, Stage1Output
from backend.memory.prompts import (
    STAGE1_SYSTEM_PROMPT,
    PHASE2_WORKSPACE_DIFF_FILE,
    build_consolidation_prompt,
    build_stage1_input,
)
from backend.runtime_env import sanitized_git_env
from backend.secret_redaction import redact_secrets


logger = logging.getLogger(__name__)

MAX_STAGE1_ROLLOUTS = 2
MAX_STAGE1_RUNNING = 8
MAX_ROLLOUT_AGE_DAYS = 10
MIN_ROLLOUT_IDLE_SECONDS = 6 * 60 * 60
STAGE1_LEASE_SECONDS = 60 * 60
STAGE1_RETRY_LIMIT = 3
STAGE1_RETRY_DELAY_SECONDS = 60 * 60
PHASE2_LEASE_SECONDS = 60 * 60
PHASE2_HEARTBEAT_SECONDS = 90
PHASE2_RETRY_LIMIT = 3
PHASE2_RETRY_DELAY_SECONDS = 60 * 60
PHASE2_SUCCESS_COOLDOWN_SECONDS = 6 * 60 * 60
PHASE2_MAX_OUTPUTS = 256
PHASE2_UNUSED_RETENTION_DAYS = 30
PHASE2_DIFF_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_ROLLOUT_TOKEN_LIMIT = 150_000

PHASE1_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "raw_memory": {"type": "string"},
        "rollout_summary": {"type": "string"},
        "rollout_slug": {"type": ["string", "null"]},
    },
    "required": ["raw_memory", "rollout_summary", "rollout_slug"],
    "additionalProperties": False,
}

_INJECTED_FRAGMENT_RES = (
    re.compile(r"<skill>.*?</skill>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<available_skills>.*?</available_skills>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<agents_md>.*?</agents_md>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<agent_instructions>.*?</agent_instructions>", re.IGNORECASE | re.DOTALL),
)
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()
_RESET_STATE_LOCK = threading.Lock()
_RESET_IN_PROGRESS = False


def memory_reset_in_progress() -> bool:
    """Return whether memory maintenance is currently behind a reset barrier."""

    with _RESET_STATE_LOCK:
        return _RESET_IN_PROGRESS


async def begin_memory_reset(*, timeout: float = 5.0) -> set[asyncio.Task[Any]] | None:
    """Stop all registered memory workers before destructive reset I/O.

    ``None`` means another reset/shutdown barrier already owns the lifecycle.
    A non-empty result is fail-closed: callers must not touch files or the
    repository while a cancellation-resistant worker is still alive.
    """

    global _RESET_IN_PROGRESS
    with _RESET_STATE_LOCK:
        if _RESET_IN_PROGRESS:
            return None
        _RESET_IN_PROGRESS = True
    pending = await cancel_and_drain(
        list(_BACKGROUND_TASKS),
        timeout=timeout,
        label="memory maintenance",
    )
    if pending:
        end_memory_reset()
    return pending


def end_memory_reset() -> None:
    global _RESET_IN_PROGRESS
    with _RESET_STATE_LOCK:
        _RESET_IN_PROGRESS = False


async def drain_memory_background_tasks(*, timeout: float = 5.0) -> set[asyncio.Task[Any]]:
    """Drain memory workers during application shutdown."""

    pending = await begin_memory_reset(timeout=timeout)
    if pending is None:
        return set(_BACKGROUND_TASKS)
    end_memory_reset()
    return pending


async def _to_thread_cancel_safe(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Keep registry ownership until the executor thread actually exits."""

    inner = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(inner)
    except asyncio.CancelledError:
        while not inner.done():
            try:
                await asyncio.shield(inner)
            except asyncio.CancelledError:
                continue
        try:
            inner.result()
        except BaseException:
            pass
        raise


class MemoryGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Phase1Result:
    raw_memory: str
    rollout_summary: str
    rollout_slug: str | None


@dataclass(frozen=True)
class Phase2WorkspaceState:
    changes: tuple[tuple[str, str], ...]
    artifacts_valid: bool


def schedule_memory_startup(
    *,
    repository: Any,
    llm: Any,
    workspace_root: Path | str | None,
    current_conversation_id: str,
    token_budget: int | None = None,
) -> asyncio.Task[Any] | None:
    """Start maintenance in a clean task context so turn usage is not inherited."""

    if llm is None or not workspace_root or memory_reset_in_progress():
        return None
    coordinator = MemoryGenerationCoordinator(
        repository=repository,
        llm=llm,
        workspace_root=workspace_root,
        token_budget=token_budget,
    )
    task = asyncio.create_task(
        coordinator.run_startup(current_conversation_id=current_conversation_id),
        name=f"memory-startup:{current_conversation_id}",
        context=contextvars.Context(),
    )
    _track_background_task(task)
    return task


def schedule_memory_forgetting(
    *,
    repository: Any,
    llm: Any,
    workspace_root: Path | str | None,
    conversation_id: str,
    token_budget: int | None = None,
) -> asyncio.Task[Any] | None:
    if llm is None or not workspace_root or not conversation_id or memory_reset_in_progress():
        return None
    coordinator = MemoryGenerationCoordinator(
        repository=repository,
        llm=llm,
        workspace_root=workspace_root,
        token_budget=token_budget,
    )
    task = asyncio.create_task(
        coordinator.forget_and_consolidate(
            conversation_id=conversation_id,
        ),
        name=f"memory-forget:{conversation_id}",
        context=contextvars.Context(),
    )
    _track_background_task(task)
    return task


def _track_background_task(task: asyncio.Task[Any]) -> None:
    _BACKGROUND_TASKS.add(task)

    def finished(done: asyncio.Task[Any]) -> None:
        _BACKGROUND_TASKS.discard(done)
        if done.cancelled():
            return
        try:
            error = done.exception()
        except (asyncio.CancelledError, RuntimeError):
            return
        if error is not None:
            logger.warning("Background memory maintenance failed: %s", error)

    task.add_done_callback(finished)


class MemoryGenerationCoordinator:
    def __init__(
        self,
        *,
        repository: Any,
        llm: Any,
        workspace_root: Path | str,
        token_budget: int | None = None,
        now: Any | None = None,
    ) -> None:
        self.repository = repository
        self.llm = llm
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.file_memory = FileMemory.for_workspace(self.workspace_root)
        self.memory_root = self.file_memory.memory_dir
        self.store = MemoryJobStore(
            self.memory_root / MEMORY_DB_NAME,
            reset_lock=self.file_memory.reset_lock,
        )
        self.worker_id = uuid.uuid4().hex
        self.token_budget = int(token_budget or 0)
        self._now_fn = now or time.time

    def _now(self) -> int:
        return int(self._now_fn())

    async def run_startup(self, *, current_conversation_id: str) -> None:
        """Run MiniCode's prune -> gate -> Phase 1 -> Phase 2 startup order."""

        now = self._now()
        await _to_thread_cancel_safe(
            self.store.prune_unselected_outputs,
            older_than=now - PHASE2_UNUSED_RETENTION_DAYS * 86_400,
            limit=512,
        )
        scoped = await _to_thread_cancel_safe(self._scoped_conversations)
        await self._reconcile_ineligible_outputs(scoped)

        candidates = [
            conversation
            for conversation in scoped
            if str(getattr(conversation, "id", "")) != current_conversation_id
            and self._eligible_for_stage1(conversation, now=now)
        ]
        candidates.sort(
            key=lambda item: self._source_updated_at(item),
            reverse=True,
        )
        await asyncio.gather(
            *(self._run_phase1(conversation) for conversation in candidates[:MAX_STAGE1_ROLLOUTS])
        )
        await self._run_phase2()

    async def forget_and_consolidate(
        self,
        *,
        conversation_id: str,
    ) -> None:
        await _to_thread_cancel_safe(
            self.store.remove_thread_output,
            conversation_id,
        )
        await self._run_phase2()

    def _scoped_conversations(self) -> list[Any]:
        results: list[Any] = []
        summaries = list(self.repository.list_conversations())
        summaries.sort(
            key=lambda item: str(getattr(item, "updated_at", "") or ""),
            reverse=True,
        )
        for summary in summaries[:5000]:
            root = str(getattr(summary, "workspace_root", "") or "").strip()
            if not root:
                continue
            try:
                candidate_root = FileMemory.workspace_memory_dir(root)
            except (OSError, ValueError):
                continue
            if candidate_root != self.memory_root:
                continue
            record = self.repository.get_conversation(str(summary.id))
            if record is not None:
                results.append(record)
        return results

    async def _reconcile_ineligible_outputs(self, conversations: Iterable[Any]) -> None:
        for conversation in conversations:
            if self._generation_mode(conversation) == "enabled" and not bool(
                getattr(conversation, "archived", False)
            ):
                continue
            await _to_thread_cancel_safe(
                self.store.remove_thread_output,
                str(conversation.id),
            )

    def _eligible_for_stage1(self, conversation: Any, *, now: int) -> bool:
        if str(getattr(conversation, "conversation_type", "main")) != "main":
            return False
        if bool(getattr(conversation, "archived", False)):
            return False
        if self._generation_mode(conversation) != "enabled":
            return False
        if not list(getattr(conversation, "transcript", []) or []):
            return False
        updated_at = self._source_updated_at(conversation)
        return (
            now - MAX_ROLLOUT_AGE_DAYS * 86_400 <= updated_at
            and updated_at <= now - MIN_ROLLOUT_IDLE_SECONDS
        )

    @staticmethod
    def _generation_mode(conversation: Any) -> str:
        explicit = str(getattr(conversation, "memory_mode", "") or "").strip().lower()
        if bool(getattr(conversation, "memory_polluted", False)):
            return "polluted"
        if explicit in {"enabled", "disabled", "polluted"}:
            return explicit
        if str(getattr(conversation, "conversation_type", "main")) != "main":
            return "disabled"
        return "enabled"

    @staticmethod
    def _source_revision(conversation: Any) -> int:
        raw = getattr(conversation, "revision", None)
        if isinstance(raw, bool) or raw is None:
            raise MemoryGenerationError("Conversation has no durable revision")
        revision = int(raw)
        if revision < 0:
            raise MemoryGenerationError("Conversation revision must be non-negative")
        return revision

    @staticmethod
    def _source_updated_at(conversation: Any) -> int:
        raw = str(getattr(conversation, "updated_at", "") or "").strip()
        if not raw:
            raise MemoryGenerationError("Conversation has no updated_at timestamp")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MemoryGenerationError(
                f"Conversation updated_at is invalid: {raw!r}"
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp())

    async def _run_phase1(self, conversation: Any) -> None:
        source_revision = self._source_revision(conversation)
        source_updated_at = self._source_updated_at(conversation)
        claim = await _to_thread_cancel_safe(
            self.store.claim_stage1,
            thread_id=str(conversation.id),
            source_revision=source_revision,
            worker_id=self.worker_id,
            lease_seconds=STAGE1_LEASE_SECONDS,
            retry_limit=STAGE1_RETRY_LIMIT,
            max_running_jobs=MAX_STAGE1_RUNNING,
        )
        if claim is None:
            return
        try:
            rollout = self._serialize_rollout(conversation)
            result = await self._extract_phase1(conversation, rollout)
            current = await _to_thread_cancel_safe(
                self.repository.get_conversation,
                str(conversation.id),
            )
            if current is None or self._generation_mode(current) != "enabled":
                result = Phase1Result("", "", None)
            elif self._source_revision(current) != source_revision:
                await _to_thread_cancel_safe(self.store.abandon_stage1, claim)
                return
            committed = await _to_thread_cancel_safe(
                self.store.complete_stage1,
                claim,
                raw_memory=result.raw_memory,
                rollout_summary=result.rollout_summary,
                rollout_slug=result.rollout_slug,
                source_updated_at=source_updated_at,
            )
            if not committed:
                logger.info("Memory Phase 1 ownership changed for %s", conversation.id)
        except asyncio.CancelledError:
            if not memory_reset_in_progress():
                await _to_thread_cancel_safe(
                    self.store.fail_stage1,
                    claim,
                    "cancelled",
                    retry_delay_seconds=STAGE1_RETRY_DELAY_SECONDS,
                )
            raise
        except Exception as exc:
            logger.warning("Memory Phase 1 failed for %s: %s", conversation.id, exc)
            await _to_thread_cancel_safe(
                self.store.fail_stage1,
                claim,
                type(exc).__name__,
                retry_delay_seconds=STAGE1_RETRY_DELAY_SECONDS,
            )

    def _serialize_rollout(self, conversation: Any) -> str:
        filtered: list[dict[str, Any]] = []
        for raw_message in list(getattr(conversation, "transcript", []) or []):
            if not isinstance(raw_message, dict):
                continue
            role = str(raw_message.get("role") or "").strip().lower()
            if role in {"system", "developer"} or role not in {"user", "assistant", "tool"}:
                continue
            content = self._strip_injected_fragments(str(raw_message.get("content") or ""))
            item: dict[str, Any] = {"role": role, "content": content}
            tool_calls = raw_message.get("tool_calls")
            if isinstance(tool_calls, list):
                item["tool_calls"] = [
                    self._filtered_tool_call(call)
                    for call in tool_calls
                    if isinstance(call, dict)
                ]
            filtered.append(item)
        serialized = json.dumps(filtered, ensure_ascii=False, separators=(",", ":"))
        budget = self._phase1_input_char_budget()
        return redact_secrets(_head_tail(serialized, budget))

    @staticmethod
    def _strip_injected_fragments(content: str) -> str:
        cleaned = content
        for pattern in _INJECTED_FRAGMENT_RES:
            cleaned = pattern.sub("[INJECTED_CONTEXT_OMITTED]", cleaned)
        return cleaned

    @staticmethod
    def _filtered_tool_call(call: dict[str, Any]) -> dict[str, Any]:
        filtered: dict[str, Any] = {
            "name": str(call.get("name") or call.get("tool_name") or ""),
            "status": str(call.get("status") or ""),
        }
        arguments = call.get("arguments")
        if arguments is None:
            arguments = call.get("input")
        if arguments is not None:
            filtered["arguments"] = _sanitize_rollout_value(arguments)
        output = call.get("output")
        if output is None:
            output = call.get("result")
        if output is not None:
            filtered["output"] = _sanitize_rollout_value(output)
        return filtered

    def _phase1_input_char_budget(self) -> int:
        if self.token_budget > 0:
            return max(1, int(self.token_budget * 4 * 0.70))
        return DEFAULT_ROLLOUT_TOKEN_LIMIT * 4

    async def _extract_phase1(self, conversation: Any, rollout: str) -> Phase1Result:
        rollout_path = self._rollout_path(str(conversation.id))
        prompt = build_stage1_input(
            rollout_path=rollout_path,
            rollout_cwd=str(getattr(conversation, "workspace_root", "") or self.workspace_root),
            rollout_contents=rollout,
        )
        messages = [
            LLMMessage(role="system", content=STAGE1_SYSTEM_PROMPT),
            LLMMessage(role="user", content=prompt),
        ]
        side_query = getattr(self.llm, "side_query", None)
        response = await (
            side_query(
                messages,
                options=SideQueryOptions(
                    operation="memory_phase1_extraction",
                    query_source="background",
                    enable_prompt_cache=True,
                    output_schema=PHASE1_OUTPUT_SCHEMA,
                ),
            )
            if callable(side_query)
            else self.llm.simple_chat(messages)
        )
        payload = _parse_exact_json(response, {"raw_memory", "rollout_summary", "rollout_slug"})
        raw_memory = payload.get("raw_memory")
        rollout_summary = payload.get("rollout_summary")
        rollout_slug = payload.get("rollout_slug")
        if not isinstance(raw_memory, str) or not isinstance(rollout_summary, str):
            raise MemoryGenerationError("Phase 1 response fields must be strings")
        if rollout_slug is not None and not isinstance(rollout_slug, str):
            raise MemoryGenerationError("Phase 1 rollout_slug must be a string or null")
        raw_memory = redact_secrets(raw_memory).strip()
        rollout_summary = redact_secrets(rollout_summary).strip()
        normalized_slug = redact_secrets(rollout_slug or "").strip() or None
        if not raw_memory or not rollout_summary:
            return Phase1Result("", "", None)
        return Phase1Result(raw_memory, rollout_summary, normalized_slug)

    def _rollout_path(self, conversation_id: str) -> str:
        resolver = getattr(self.repository, "transcript_path", None)
        if callable(resolver):
            try:
                return str(resolver(conversation_id))
            except Exception:
                pass
        resolver = getattr(self.repository, "_transcript_path", None)
        if callable(resolver):
            try:
                return str(resolver(conversation_id))
            except Exception:
                pass
        return conversation_id

    async def _run_phase2(self) -> None:
        claim = await _to_thread_cancel_safe(
            self.store.claim_phase2,
            worker_id=self.worker_id,
            lease_seconds=PHASE2_LEASE_SECONDS,
            retry_limit=PHASE2_RETRY_LIMIT,
            success_cooldown_seconds=PHASE2_SUCCESS_COOLDOWN_SECONDS,
        )
        if claim is None:
            return

        heartbeat = asyncio.create_task(self._heartbeat_phase2(claim))
        try:
            outputs = await _to_thread_cancel_safe(
                self.store.list_stage1_outputs,
                limit=None,
                max_unused_days=PHASE2_UNUSED_RETENTION_DAYS,
            )
            outputs = await _to_thread_cancel_safe(self._eligible_phase2_outputs, outputs)
            outputs = outputs[:PHASE2_MAX_OUTPUTS]
            outputs.sort(key=lambda output: output.thread_id)
            state = await _to_thread_cancel_safe(self._prepare_phase2_workspace, claim, outputs)
            if not state.changes and state.artifacts_valid:
                committed = await _to_thread_cancel_safe(
                    self._commit_phase2_without_changes,
                    claim,
                    outputs,
                )
                if not committed:
                    raise MemoryGenerationError("Phase 2 ownership changed before commit")
                return

            await self._consolidate_phase2()
            if not await _to_thread_cancel_safe(self._outputs_still_eligible, outputs):
                raise MemoryGenerationError("Phase 2 selection changed during consolidation")
            committed = await _to_thread_cancel_safe(
                self._commit_phase2_artifacts,
                claim,
                outputs,
            )
            if not committed:
                raise MemoryGenerationError("Phase 2 ownership changed before commit")
        except asyncio.CancelledError:
            if not memory_reset_in_progress():
                await _to_thread_cancel_safe(
                    self.store.fail_phase2,
                    claim,
                    "cancelled",
                    retry_delay_seconds=PHASE2_RETRY_DELAY_SECONDS,
                )
            raise
        except Exception as exc:
            logger.warning("Memory Phase 2 failed: %s", exc)
            await _to_thread_cancel_safe(
                self.store.fail_phase2,
                claim,
                type(exc).__name__,
                retry_delay_seconds=PHASE2_RETRY_DELAY_SECONDS,
            )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def _heartbeat_phase2(self, claim: JobClaim) -> None:
        while True:
            await asyncio.sleep(PHASE2_HEARTBEAT_SECONDS)
            owned = await _to_thread_cancel_safe(
                self.store.heartbeat_phase2,
                claim,
                lease_seconds=PHASE2_LEASE_SECONDS,
            )
            if not owned:
                return

    def _eligible_phase2_outputs(self, outputs: Iterable[Stage1Output]) -> list[Stage1Output]:
        eligible: list[Stage1Output] = []
        for output in outputs:
            conversation = self.repository.get_conversation(output.thread_id)
            if conversation is None or self._generation_mode(conversation) != "enabled":
                self.store.remove_thread_output(output.thread_id)
                continue
            if bool(getattr(conversation, "archived", False)):
                self.store.remove_thread_output(output.thread_id)
                continue
            if self._source_revision(conversation) != output.source_revision:
                continue
            eligible.append(output)
        return eligible

    def _outputs_still_eligible(self, outputs: Iterable[Stage1Output]) -> bool:
        for output in outputs:
            conversation = self.repository.get_conversation(output.thread_id)
            if conversation is None:
                return False
            if self._generation_mode(conversation) != "enabled":
                return False
            if bool(getattr(conversation, "archived", False)):
                return False
            if self._source_revision(conversation) != output.source_revision:
                return False
        return True

    def _prepare_phase2_workspace(
        self,
        claim: JobClaim,
        outputs: list[Stage1Output],
    ) -> Phase2WorkspaceState:
        lock = self.file_memory.reset_lock
        try:
            with lock.acquire(timeout=5.0):
                if not self.store.owns_phase2(claim):
                    raise MemoryGenerationError("Phase 2 ownership changed before workspace sync")
                self._ensure_git_workspace()
                self._sync_phase2_inputs(outputs)
                diff = self._workspace_diff()
                changes = tuple(self._workspace_changes())
                artifacts_valid = self._artifacts_valid()
                if changes or not artifacts_valid:
                    atomic_write_text(
                        self.memory_root / PHASE2_WORKSPACE_DIFF_FILE,
                        self._render_workspace_diff(changes, diff),
                    )
                return Phase2WorkspaceState(
                    changes=changes,
                    artifacts_valid=artifacts_valid,
                )
        except FileLockTimeout as exc:
            raise MemoryGenerationError("Timed out waiting for the memory reset lock") from exc

    def _ensure_git_workspace(self) -> None:
        self.memory_root.mkdir(parents=True, exist_ok=True)
        ignore_path = self.memory_root / ".gitignore"
        ignore = (
            f"/{MEMORY_DB_NAME}\n"
            f"/{MEMORY_DB_NAME}-shm\n"
            f"/{MEMORY_DB_NAME}-wal\n"
            f"/{PHASE2_WORKSPACE_DIFF_FILE}\n"
            "/*.lock\n"
        )
        if _read_text(ignore_path) != ignore:
            atomic_write_text(ignore_path, ignore)
        git_path = self.memory_root / ".git"
        if git_path.is_symlink():
            raise MemoryGenerationError("Refusing to use symlinked memory git metadata")
        if not git_path.exists():
            self._git("init", "--quiet")
            self._git("config", "user.name", "MiniCode Memory")
            self._git("config", "user.email", "memory@minicode.local")
        if self._git("rev-parse", "--verify", "HEAD", check=False).returncode != 0:
            self._git("add", "-A")
            self._git("commit", "--quiet", "--allow-empty", "-m", "memory baseline")

    def _sync_phase2_inputs(self, outputs: list[Stage1Output]) -> str:
        records = {
            output.thread_id: self.repository.get_conversation(output.thread_id)
            for output in outputs
        }
        sections = ["# Raw Memories", ""]
        summaries_dir = self.memory_root / "rollout_summaries"
        summaries_dir.mkdir(parents=True, exist_ok=True)
        expected_summaries: set[Path] = set()
        for output in sorted(outputs, key=lambda item: item.thread_id):
            record = records.get(output.thread_id)
            cwd = str(getattr(record, "workspace_root", "") or "")
            rollout_path = self._rollout_path(output.thread_id)
            updated_at = datetime.fromtimestamp(output.source_updated_at, UTC).isoformat()
            sections.extend(
                [
                    f"## Thread `{output.thread_id}`",
                    f"updated_at: {updated_at}",
                    f"cwd: {cwd}",
                    f"rollout_path: {rollout_path}",
                    f"rollout_summary_file: {_rollout_summary_file_stem(output)}.md",
                    "",
                    redact_secrets(output.raw_memory).strip(),
                    "",
                ]
            )
            summary_path = summaries_dir / f"{_rollout_summary_file_stem(output)}.md"
            expected_summaries.add(summary_path)
            summary = (
                f"thread_id: {output.thread_id}\n"
                f"updated_at: {updated_at}\n"
                f"rollout_path: {rollout_path}\n"
                f"cwd: {cwd}\n"
            )
            git_branch = str(getattr(record, "git_branch", "") or "")
            if git_branch:
                summary += f"git_branch: {git_branch}\n"
            summary += f"\n{redact_secrets(output.rollout_summary).strip()}\n"
            if _read_text(summary_path) != summary:
                atomic_write_text(summary_path, summary)
        for existing in summaries_dir.glob("*.md"):
            if existing not in expected_summaries:
                existing.unlink()
        raw_memories = (
            "\n".join(
                [
                    "# Raw Memories",
                    "",
                    "Merged stage-1 raw memories (stable ascending thread-id order):",
                    "",
                    *sections[2:],
                ]
            ).rstrip()
            + "\n"
            if outputs
            else "# Raw Memories\n\nNo raw memories yet.\n"
        )
        raw_path = self.memory_root / "raw_memories.md"
        if _read_text(raw_path) != raw_memories:
            atomic_write_text(raw_path, raw_memories)
        return raw_memories

    def _workspace_diff(self) -> str:
        diff_path = self.memory_root / PHASE2_WORKSPACE_DIFF_FILE
        if diff_path.exists():
            diff_path.unlink()
        self._git("add", "-N", "--", ".", check=False)
        result = self._git(
            "diff",
            "--no-ext-diff",
            "--no-color",
            "--",
            ".",
            check=False,
        )
        raw = result.stdout or ""
        encoded = raw.encode("utf-8", errors="replace")
        if len(encoded) <= PHASE2_DIFF_MAX_BYTES:
            return raw
        clipped = encoded[:PHASE2_DIFF_MAX_BYTES].decode("utf-8", errors="ignore")
        return clipped + "\n[workspace diff truncated at 4194304 bytes]\n"

    def _workspace_changes(self) -> list[tuple[str, str]]:
        result = self._git("diff", "--name-status", "--no-ext-diff", "--no-color", check=False)
        changes: list[tuple[str, str]] = []
        for line in (result.stdout or "").splitlines():
            status, _, path = line.partition("\t")
            if status and path:
                changes.append((status[0], path))
        return changes

    @staticmethod
    def _render_workspace_diff(
        changes: tuple[tuple[str, str], ...],
        diff: str,
    ) -> str:
        rendered = (
            "# Memory Workspace Diff\n\n"
            "Generated by MiniCode before Phase 2 memory consolidation. "
            "Read this file first and do not edit it.\n\n"
            "## Status\n"
        )
        if not changes:
            return rendered + "- none\n"
        rendered += "".join(f"- {status} {path}\n" for status, path in changes)
        rendered += "\n## Diff\n\n```diff\n"
        rendered += diff
        if diff and not diff.endswith("\n"):
            rendered += "\n"
        return rendered + "```\n"

    async def _consolidate_phase2(self) -> None:
        await run_memory_consolidation_agent(
            llm=self.llm,
            memory_root=self.memory_root,
            prompt=build_consolidation_prompt(self.memory_root),
            token_budget=self.token_budget,
        )

    def _commit_phase2_artifacts(
        self,
        claim: JobClaim,
        outputs: list[Stage1Output],
    ) -> bool:
        lock = self.file_memory.reset_lock
        try:
            with lock.acquire(timeout=5.0):
                if not self.store.owns_phase2(claim):
                    return False
                if not self._artifacts_valid():
                    raise MemoryGenerationError("Phase 2 artifacts failed validation")
                self._commit_git_baseline()
                return self.store.complete_phase2(
                    claim,
                    outputs,
                )
        except FileLockTimeout as exc:
            raise MemoryGenerationError("Timed out waiting for the memory reset lock") from exc

    def _commit_phase2_without_changes(
        self,
        claim: JobClaim,
        outputs: list[Stage1Output],
    ) -> bool:
        lock = self.file_memory.reset_lock
        try:
            with lock.acquire(timeout=5.0):
                if not self.store.owns_phase2(claim) or not self._artifacts_valid():
                    return False
                return self.store.complete_phase2(
                    claim,
                    outputs,
                )
        except FileLockTimeout as exc:
            raise MemoryGenerationError("Timed out waiting for the memory reset lock") from exc

    def _commit_git_baseline(self) -> None:
        diff_path = self.memory_root / PHASE2_WORKSPACE_DIFF_FILE
        if diff_path.exists():
            diff_path.unlink()
        git_path = self.memory_root / ".git"
        if git_path.is_symlink():
            raise MemoryGenerationError("Refusing to reset symlinked memory git metadata")
        if git_path.is_dir():
            shutil.rmtree(git_path, onerror=_remove_readonly_git_path)
        elif git_path.exists():
            git_path.unlink()
        self._git("init", "--quiet")
        self._git("config", "user.name", "MiniCode Memory")
        self._git("config", "user.email", "memory@minicode.local")
        self._git("add", "-A")
        self._git("commit", "--quiet", "--allow-empty", "-m", "memory baseline")

    def _artifacts_valid(self) -> bool:
        if not (self.memory_root / "MEMORY.md").is_file():
            return False
        summary = _read_text(self.memory_root / "memory_summary.md")
        return summary.splitlines()[:1] == ["v1"]

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = sanitized_git_env(self.memory_root)
        environment.update(
            {
                "GIT_AUTHOR_NAME": "MiniCode Memory",
                "GIT_AUTHOR_EMAIL": "memory@minicode.local",
                "GIT_COMMITTER_NAME": "MiniCode Memory",
                "GIT_COMMITTER_EMAIL": "memory@minicode.local",
            }
        )
        result = subprocess.run(
            ["git", *args],
            cwd=self.memory_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "git failed").strip()
            raise MemoryGenerationError(f"git {' '.join(args)} failed: {detail[:500]}")
        return result


def _parse_exact_json(raw: Any, expected_keys: set[str]) -> dict[str, Any]:
    text = str(raw or "").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MemoryGenerationError("Memory model returned invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise MemoryGenerationError(
            f"Memory model must return exactly {sorted(expected_keys)}"
        )
    return payload


def _sanitize_rollout_value(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, raw_item in value.items():
            key = str(raw_key)
            if key.lower() in {
                "data",
                "base64",
                "providerraw",
                "provider_raw",
                "reasoning",
                "thinking",
            }:
                cleaned[key] = "[OMITTED]"
            else:
                cleaned[key] = _sanitize_rollout_value(raw_item)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_rollout_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _head_tail(value: str, max_chars: int) -> str:
    text = str(value or "")
    limit = max(0, int(max_chars))
    if len(text) <= limit:
        return text
    if limit < 80:
        return text[:limit]
    marker = "\n...[MIDDLE_TRUNCATED]...\n"
    remaining = limit - len(marker)
    head = remaining * 3 // 5
    return text[:head] + marker + text[-(remaining - head) :]


def _rollout_summary_file_stem(output: Stage1Output) -> str:
    thread_id = str(output.thread_id)
    timestamp = datetime.fromtimestamp(output.source_updated_at, UTC)
    try:
        thread_uuid = uuid.UUID(thread_id)
    except ValueError:
        short_hash_seed = 0
        for byte in thread_id.encode("utf-8"):
            short_hash_seed = ((short_hash_seed * 31) + byte) & 0xFFFF_FFFF
    else:
        short_hash_seed = thread_uuid.int & 0xFFFF_FFFF
        if thread_uuid.version == 7:
            unix_millis = thread_uuid.int >> 80
            timestamp = datetime.fromtimestamp(unix_millis / 1000, UTC)
        elif thread_uuid.version == 1:
            unix_seconds = (thread_uuid.time - 0x01B21DD213814000) / 10_000_000
            timestamp = datetime.fromtimestamp(unix_seconds, UTC)

    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    short_hash_value = short_hash_seed % 14_776_336
    short_hash_chars = ["0"] * 4
    for index in range(3, -1, -1):
        short_hash_chars[index] = alphabet[short_hash_value % len(alphabet)]
        short_hash_value //= len(alphabet)
    file_prefix = f"{timestamp.strftime('%Y-%m-%dT%H-%M-%S')}-{''.join(short_hash_chars)}"

    raw_slug = output.rollout_slug
    if raw_slug is None:
        return file_prefix
    slug = "".join(
        char.lower() if char.isascii() and char.isalnum() else "_"
        for char in str(raw_slug)
    )[:60].rstrip("_")
    return f"{file_prefix}-{slug}" if slug else file_prefix


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _remove_readonly_git_path(function: Any, path: str, _error: Any) -> None:
    os_path = Path(path)
    os_path.chmod(os_path.stat().st_mode | stat.S_IWRITE)
    function(path)
