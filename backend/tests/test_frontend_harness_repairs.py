from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.diff.git_integration import GitCommandError, revert_patch
from backend.diff.unified import iter_unified_diff
from backend.documents.service import parse_document_preview
from backend.lsp import client as lsp
from backend.preview import launcher
from backend.services.conversation_payload_service import ConversationDeleteRequest
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.handlers import conversation as conversation_handlers
from backend.ws.handlers.diff import handle_diff_git_revert_patch


def _patch(path: str, before: str, after: str) -> str:
    return "".join(iter_unified_diff(before, after, fromfile=f"a/{path}", tofile=f"b/{path}"))


def test_reverse_patch_preserves_user_edits_before_and_after_agent_change(tmp_path: Path) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    baseline = "".join(f"line {index}\n" for index in range(40))
    before = baseline.replace("line 2\n", "USER BEFORE\n")
    after = before.replace("line 20\n", "AGENT CHANGE\n")
    live = after.replace("line 37\n", "USER AFTER\n")
    for name in ("one.txt", "two.txt"):
        (tmp_path / name).write_text(live, encoding="utf-8")
    patch = _patch("one.txt", before, after) + _patch("two.txt", before, after)

    assert asyncio.run(revert_patch(str(tmp_path), patch))
    for name in ("one.txt", "two.txt"):
        assert (tmp_path / name).read_text(encoding="utf-8") == before.replace("line 37\n", "USER AFTER\n")


def test_reverse_patch_conflict_leaves_the_whole_batch_unchanged(tmp_path: Path) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("agent\n", encoding="utf-8")
    second.write_text("newer user change\n", encoding="utf-8")
    patch = _patch("one.txt", "before\n", "agent\n") + _patch("two.txt", "before\n", "agent\n")
    with pytest.raises(GitCommandError):
        asyncio.run(revert_patch(str(tmp_path), patch))
    assert first.read_text(encoding="utf-8") == "agent\n"
    assert second.read_text(encoding="utf-8") == "newer user change\n"


def test_reverse_patch_handles_creation_and_missing_final_newline(tmp_path: Path) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    file = tmp_path / "new.txt"
    file.write_text("created", encoding="utf-8")
    patch = "".join(iter_unified_diff("", "created", fromfile="/dev/null", tofile="b/new.txt"))
    assert asyncio.run(revert_patch(str(tmp_path), patch))
    assert not file.exists()


def test_reverse_patch_command_rejects_a_stale_conversation_owner() -> None:
    session = SimpleNamespace(active_conversation_id="new-owner", send_event=AsyncMock())
    asyncio.run(handle_diff_git_revert_patch(session, {
        "conversation_id": "old-owner", "workspace_root": "C:/old", "patch": "patch", "confirmed": True,
    }))
    event = session.send_event.await_args.args[0]
    assert event.data["command"] == "diff.git_revert_patch"
    assert event.data["level"] == "error"


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_lsp_start_cancellation_cleans_or_retains_the_started_owner(tmp_path: Path, monkeypatch, cleanup_fails: bool) -> None:
    async def scenario():
        started = asyncio.Event()
        instances = []

        class StartingClient:
            def __init__(self, *args, **kwargs):
                self.stopped = False
                instances.append(self)

            async def start(self):
                started.set()
                await asyncio.Event().wait()

            async def stop(self):
                self.stopped = True
                if cleanup_fails:
                    raise RuntimeError("unproven exit")

        monkeypatch.setattr(lsp, "LSPClient", StartingClient)
        monkeypatch.setattr(lsp, "_resolve_server_executable", lambda *_: "fake-server")
        monkeypatch.setattr(lsp, "_lsp_sandbox_runner", lambda *_: SimpleNamespace(capability=lambda: SimpleNamespace(available=True)))
        manager = lsp.LSPManager()
        pending = asyncio.create_task(manager.get_client("file.py", str(tmp_path)))
        await started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert instances[0].stopped
        assert len(manager._clients) == int(cleanup_fails)

    asyncio.run(scenario())


def test_lsp_unproven_termination_does_not_replace_or_forget_process(tmp_path: Path) -> None:
    async def scenario():
        runner = SimpleNamespace(terminate=AsyncMock(return_value=False), spawn_interactive=AsyncMock())
        client = lsp.LSPClient("fake", [], str(tmp_path), sandbox_runner=runner)
        process = SimpleNamespace(returncode=None)
        client._process = process
        with pytest.raises(RuntimeError, match="unconfirmed"):
            await client.start()
        assert client._process is process
        runner.spawn_interactive.assert_not_called()
        with pytest.raises(RuntimeError, match="unconfirmed"):
            await client.stop()
        assert client._process is process

    asyncio.run(scenario())


def test_concurrent_preview_starts_share_one_process_and_stop_handle(tmp_path: Path, monkeypatch) -> None:
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        spawned = []

        class Runner:
            def __init__(self, policy):
                pass

            async def spawn_shell_interactive(self, *args, **kwargs):
                process = SimpleNamespace(pid=len(spawned) + 1, returncode=None)
                spawned.append(process)
                entered.set()
                await release.wait()
                return process

        monkeypatch.setattr(launcher, "SandboxRunner", Runner)
        monkeypatch.setattr(launcher, "_monitor_process", AsyncMock())
        monkeypatch.setattr(launcher, "_RUNNING", {})
        monkeypatch.setattr(launcher, "_START_LOCK", asyncio.Lock())
        config = launcher.PreviewLaunchConfig(name="same", command="fake", cwd=str(tmp_path))
        first = asyncio.create_task(launcher._start_preview_config(config, session_id="session", conversation_id="owner", workspace_root=tmp_path))
        await entered.wait()
        second = asyncio.create_task(launcher._start_preview_config(config, session_id="session", conversation_id="owner", workspace_root=tmp_path))
        await asyncio.sleep(0)
        assert len(spawned) == 1
        release.set()
        left, right = await asyncio.gather(first, second)
        assert left is right
        assert launcher._RUNNING[left.id] is left

    asyncio.run(scenario())


def test_document_source_whitespace_is_preserved() -> None:
    source = "    indented = 1\n\n"
    assert parse_document_preview("snippet.py", source.encode())["full_text"] == source


def test_deep_approval_projection_fits_the_existing_wire_depth_budget() -> None:
    value = "leaf"
    for _ in range(20):
        value = {"nested": value}
    projected = SessionApprovalRuntimeMixin()._sanitize_approval_args_for_client(value)

    def depth(item):
        return 1 + max(map(depth, item.values()), default=-1) if isinstance(item, dict) else 0

    assert depth(projected) <= 12


def test_delete_cleanup_uses_existing_control_response_waiter() -> None:
    class Session(SessionApprovalRuntimeMixin):
        approval_diff_cache = {}

        def _approval_timeout_seconds(self):
            return None

        async def send_payload(self, payload, **kwargs):
            request_id, response = self._normalize_control_response({
                "request_id": payload["request_id"], "conversation_id": payload["conversation_id"],
                "response": {"subtype": "success", "response": {"action": "approve"}},
            })
            assert payload["request"]["subtype"] == "conversation_resources_cleanup"
            assert not self.approval_response_owner_error(request_id, response)
            assert self._resolve_pending_approval(request_id, response)

    session = Session()
    target = SimpleNamespace(id="owner", workspace_root="C:/workspace", worktree_path="")
    result = asyncio.run(conversation_handlers._request_conversation_resource_cleanup(session, target))
    assert result["action"] == "approve"
    assert session.turn_wait_state.waiter_ids() == set()


@pytest.mark.parametrize("failure", ["preflight", "renderer", "physical", None])
def test_delete_checks_then_cleans_local_resources_then_removes_worktree(tmp_path: Path, monkeypatch, failure: str | None) -> None:
    from backend.tasks import scheduler

    events = []
    session = SimpleNamespace(
        emit_command_result=AsyncMock(),
        conversation_repo=SimpleNamespace(delete_conversation=lambda _: events.append("delete") or True),
    )
    request = ConversationDeleteRequest("owner", True, False, True)
    target = SimpleNamespace(id="owner")

    async def deadline(awaitable, **kwargs):
        await awaitable
        return True

    async def worktree(*args, check_only=False, **kwargs):
        events.append("preflight" if check_only else "physical")
        if failure == ("preflight" if check_only else "physical"):
            return {"removed": False, "error": "fixture refusal", "conversation_id": "owner"}
        return {"ready": True} if check_only else {"removed": True}

    async def renderer(*args):
        events.append("renderer")
        return {"action": "reject" if failure == "renderer" else "approve"}

    monkeypatch.setattr(scheduler, "get_global_scheduler", lambda: SimpleNamespace(destroy_for_conversation=AsyncMock()))
    monkeypatch.setattr(launcher, "stop_preview_launches_for_conversation", AsyncMock(return_value=[]))
    monkeypatch.setattr(conversation_handlers, "await_with_deadline", deadline)
    monkeypatch.setattr(conversation_handlers, "_conversation_cleanup_owner", lambda *args: None)
    monkeypatch.setattr(conversation_handlers, "_cleanup_conversation_worktree", worktree)
    monkeypatch.setattr(conversation_handlers, "_request_conversation_resource_cleanup", renderer)
    monkeypatch.setattr(conversation_handlers, "_purge_conversation_replay_state", AsyncMock(return_value=({}, [])))
    monkeypatch.setattr(conversation_handlers, "_schedule_long_term_memory_forgetting", lambda *args: None)
    monkeypatch.setattr(conversation_handlers, "_all_live_sessions", lambda *args: [])
    monkeypatch.setattr(conversation_handlers, "_broadcast_conversation_lists", AsyncMock(return_value=[]))
    asyncio.run(conversation_handlers._handle_conversation_delete_after_run_fence(session, request=request, target=target, owner_sessions=[]))
    expected = ["preflight", "renderer", "physical", "delete"]
    if failure is not None:
        expected = expected[:expected.index(failure) + 1]
    assert events == expected
    assert session.emit_command_result.await_args.args[0] == "conversation.delete"
