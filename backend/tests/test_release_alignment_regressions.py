from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from backend.agent.message import UserCommand
from backend.agent.tool_execution import run_tool_with_timeout
from backend.llm.base import LLMMessage, StreamEventType
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.secret_redaction import redact_secrets
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry
from backend.llm.base import ToolCallEvent
from backend.ws.durable_user_queue import DurableUserMessageQueue


def _git(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def _local_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return source


def _git_source(tmp_path: Path) -> tuple[Path, str]:
    source = _local_source(tmp_path)
    _git(["init", "-q"], cwd=source)
    _git(["config", "user.name", "MiniCode Test"], cwd=source)
    _git(["config", "user.email", "test@localhost"], cwd=source)
    _git(["add", "-A"], cwd=source)
    _git(["commit", "-qm", "baseline"], cwd=source)
    return source, _git(["rev-parse", "HEAD"], cwd=source).stdout.strip()


def _client_command(command_id: str) -> UserCommand:
    return UserCommand(
        type="user_message",
        data={"client_command_id": command_id, "content": command_id},
    )


def _directory_alias(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            pytest.skip("directory symlinks are unavailable")
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"directory junctions are unavailable: {(completed.stderr or completed.stdout).strip()}")


@pytest.mark.parametrize(
    "secret",
    [
        "sk_tr_SYNTHETIC_TOKEN_RHYTHM_KEY_123456789",
        "sk-proj-SYNTHETIC_OPENAI_PROJECT_KEY_123456789",
        "sk-SYNTHETIC_STANDARD_KEY_123456789",
    ],
)
def test_secret_redaction_covers_current_gateway_key_shapes(secret: str) -> None:
    redacted = redact_secrets(f"before {secret} after")

    assert secret not in redacted
    assert "REDACTED" in redacted


def test_durable_queue_merges_two_stale_process_owners(tmp_path: Path) -> None:
    first = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path)
    second = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path)
    first.load()
    second.load()

    assert first.persist_client_command(_client_command("first")) is True
    assert second.persist_client_command(_client_command("second")) is True

    verifier = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path)
    verifier.load()
    assert [
        command.data["client_command_id"]
        for command in verifier.pending_client_commands()
    ] == ["first", "second"]


def test_durable_queue_settles_a_stale_pending_duplicate_atomically(tmp_path: Path) -> None:
    queue = DurableUserMessageQueue(session_id="stale-duplicate", root_dir=tmp_path)
    command = _client_command("already-handled")

    assert queue.persist_client_command(command) is True
    assert queue.discard_pending_client_command("already-handled") is True
    assert queue.pending_client_commands() == []
    assert queue.discard_pending_client_command("already-handled") is False


def test_durable_queue_snapshot_save_preserves_other_conversations(tmp_path: Path) -> None:
    first = DurableUserMessageQueue(session_id="shared-snapshot", root_dir=tmp_path)
    second = DurableUserMessageQueue(session_id="shared-snapshot", root_dir=tmp_path)
    first.load()
    second.load()
    first_command = UserCommand(type="user_message", data={"content": "first"})
    second_command = UserCommand(type="user_message", data={"content": "second"})

    first.save({"conversation-first": [first_command]}, {})
    second.save({"conversation-second": [second_command]}, {})

    verifier = DurableUserMessageQueue(session_id="shared-snapshot", root_dir=tmp_path)
    queues, _inflight = verifier.load()
    assert {
        conversation_id: [command.data["content"] for command in commands]
        for conversation_id, commands in queues.items()
    } == {
        "conversation-first": ["first"],
        "conversation-second": ["second"],
    }

    first.save({}, {})
    after_clear = DurableUserMessageQueue(session_id="shared-snapshot", root_dir=tmp_path)
    queues_after_clear, _ = after_clear.load()
    assert {
        conversation_id: [command.data["content"] for command in commands]
        for conversation_id, commands in queues_after_clear.items()
    } == {"conversation-second": ["second"]}


def test_tool_timeout_returns_at_cleanup_deadline_and_retains_resistant_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ResistantTool(BaseTool):
        name = "release_alignment_resistant"
        permission = PermissionLevel.AUTO
        timeout_seconds = 0.001

        def __init__(self, release: asyncio.Event, finished: asyncio.Event) -> None:
            self.release = release
            self.finished = finished

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description="Ignore cancellation until explicitly released",
                parameters={"type": "object", "properties": {}},
            )

        async def execute(self, _args: dict[str, Any], **_kwargs: Any) -> ToolResult:
            try:
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        continue
                return ToolResult(content="released")
            finally:
                self.finished.set()

    async def scenario() -> tuple[ToolResult, bool, float, bool, bool, dict[str, Any]]:
        release = asyncio.Event()
        finished = asyncio.Event()
        registry = ToolRegistry()
        registry.register(ResistantTool(release, finished))
        context = ToolExecutionContext(permission=PermissionContext(mode="bypass"))

        async def release_later() -> None:
            await asyncio.sleep(0.05)
            release.set()

        releaser = asyncio.create_task(release_later())
        started = time.monotonic()
        result = await run_tool_with_timeout(
            ToolCallEvent(id="resistant", name="release_alignment_resistant", arguments={}),
            registry,
            context,
        )
        elapsed = time.monotonic() - started
        retained_while_running = len(context.pending_cleanup_tasks) == 1
        receipt_at_return = dict(result.cleanup_receipt)
        await releaser
        await asyncio.wait_for(finished.wait(), timeout=1.0)
        # Two yields: the resistant task settles first, and its retention
        # done-callback runs on the following loop iteration.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return (
            result,
            finished.is_set(),
            elapsed,
            retained_while_running,
            bool(context.pending_cleanup_tasks),
            receipt_at_return,
        )

    monkeypatch.setattr(
        "backend.agent.tool_execution.CANCELLATION_DRAIN_TIMEOUT_SECONDS",
        0.001,
    )
    (
        result,
        finished,
        elapsed,
        retained_while_running,
        retained_after_settlement,
        receipt_at_return,
    ) = asyncio.run(scenario())

    assert result.status == "timeout"
    assert finished is True
    assert elapsed < 0.04
    assert retained_while_running is True
    assert retained_after_settlement is False
    assert receipt_at_return["pending"] == 1
    assert receipt_at_return["completed"] is False
    assert receipt_at_return["timed_out"] is True
    assert receipt_at_return["retry_safe"] is False
    assert receipt_at_return["manual_recovery_required"] is True
    assert result.cleanup_receipt["pending"] == 0
    assert result.cleanup_receipt["completed"] is True
    assert result.cleanup_receipt["timed_out"] is True
    assert result.cleanup_receipt["cleanup_completed_after_deadline"] is True


class _PiModel:
    id = "release-alignment-model"
    api = "openai-completions"
    max_tokens = 64

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "api": self.api}


def _release_pi_usage(
    *,
    input_tokens: Any = 0,
    output_tokens: Any = 0,
    cache_read: Any = 0,
    cache_write: Any = 0,
    total_tokens: Any = 0,
    reasoning: Any | None = None,
    cost_total: Any = 0,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "totalTokens": total_tokens,
        "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "total": cost_total,
        },
    }
    if reasoning is not None:
        usage["reasoning"] = reasoning
    return usage


def _release_pi_message(
    content: list[dict[str, Any]],
    *,
    stop_reason: str = "stop",
    usage: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "api": "openai-completions",
        "provider": "release-alignment",
        "model": "release-alignment-model",
        "usage": usage if usage is not None else _release_pi_usage(),
        "stopReason": stop_reason,
        "timestamp": 1,
        **extra,
    }


async def _collect_async(stream: Any) -> list[Any]:
    return [event async for event in stream]
