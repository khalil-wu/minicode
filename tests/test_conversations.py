import asyncio
import inspect
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.llm.base import LLMAdapter
from backend.main import app
from backend.memory.file_memory import FileMemory


class _ConversationNoopLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        if False:
            yield None

    async def simple_chat(self, messages):
        return ""


def _install_noop_llm(monkeypatch) -> None:
    # CI intentionally has no developer settings.json. Keep these transport
    # tests focused on websocket behavior by selecting an explicit catalog
    # model at the test boundary.
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
    monkeypatch.setenv("OPENAI_AVAILABLE_MODELS", "gpt-5.4")
    factory = lambda config, model_override=None, **kwargs: _ConversationNoopLLM()
    monkeypatch.setattr("backend.main._create_session_llm", factory)
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", factory)


def _receive_next_non_task_update(ws, *, max_attempts: int = 20) -> dict[str, object]:
    bookkeeping_events = {
        "task.update",
        "file.changed",
        "session.state_changed",
        "agent.run.started",
        "agent.run.completed",
        # Lifecycle/control commands now return structured acknowledgements so
        # the frontend can settle pending operations. Data-plane assertions in
        # this helper intentionally continue to the next projected state event.
        "command.result",
    }
    for _ in range(max_attempts):
        payload = ws.receive_json()
        if payload.get("type") in bookkeeping_events:
            continue
        return payload
    raise AssertionError("did not receive a non task.update websocket event in time")


def _receive_until_event(ws, event_type: str, *, max_attempts: int = 20) -> dict[str, object]:
    for _ in range(max_attempts):
        payload = ws.receive_json()
        if payload.get("type") == event_type:
            return payload
    raise AssertionError(f"did not receive {event_type!r} websocket event in time")


def _receive_conversation_switched(
    ws,
    conversation_id: str,
    *,
    max_attempts: int = 20,
) -> dict[str, object]:
    for _ in range(max_attempts):
        payload = ws.receive_json()
        if (
            payload.get("type") == "conversation.switched"
            and payload.get("conversation_id") == conversation_id
        ):
            return payload
    raise AssertionError(f"did not receive conversation.switched for {conversation_id!r} in time")


def _receive_created_conversation(ws, *, max_attempts: int = 20) -> dict[str, object]:
    """Consume the authoritative create lifecycle in wire order.

    Creation activates the new conversation first, then publishes the inventory
    snapshot. Keeping this assertion in one helper prevents individual tests
    from accidentally reintroducing the old list-before-switch race contract.
    """
    switched = _receive_next_non_task_update(ws, max_attempts=max_attempts)
    assert switched["type"] == "conversation.switched"
    listing = _receive_until_event(ws, "conversation.list", max_attempts=max_attempts)
    assert listing["active_conversation_id"] == switched["conversation_id"]
    return listing


def _receive_conversation_list_for_active(
    ws,
    conversation_id: str,
    *,
    max_attempts: int = 20,
) -> dict[str, object]:
    for _ in range(max_attempts):
        payload = ws.receive_json()
        if (
            payload.get("type") == "conversation.list"
            and payload.get("active_conversation_id") == conversation_id
        ):
            return payload
    raise AssertionError(f"did not receive conversation.list for active {conversation_id!r} in time")


def _receive_permission_rules_updated(
    ws,
    conversation_id: str,
    *,
    max_attempts: int = 20,
) -> dict[str, object]:
    for _ in range(max_attempts):
        payload = ws.receive_json()
        if (
            payload.get("type") == "permission.rules.updated"
            and payload.get("conversation_id") == conversation_id
        ):
            return payload
    raise AssertionError(f"did not receive permission.rules.updated for {conversation_id!r} in time")


def test_conversation_repository_round_trip(tmp_path) -> None:
    from backend.conversations.repository import ConversationRepository

    repo = ConversationRepository(base_dir=tmp_path)

    created = repo.create_conversation(memory_mode="disabled")
    assert created.memory_mode == "disabled"
    assert created.permission_mode == "confirm"
    assert created.compaction_state == "clean"
    assert created.id

    repo.append_transcript_message(
        created.id,
        {
            "id": "msg_1",
            "role": "user",
            "content": "hello",
            "timestamp": "10:00",
        },
    )
    repo.update_summary(created.id, "asked about hello")
    repo.update_permission_mode(created.id, "plan")
    planned = repo.get_conversation(created.id)
    assert planned is not None
    assert planned.permission_previous_mode == "confirm"
    repo.update_permission_rules(
        created.id,
        deny_rules=["run_*"],
        overrides={"write_file": "confirm"},
    )
    repo.update_compaction(created.id, "compacted", "older turns were summarized")

    restored = repo.get_conversation(created.id)
    assert restored is not None
    assert restored.summary == "asked about hello"
    assert restored.permission_mode == "plan"
    assert restored.permission_previous_mode == "confirm"
    assert restored.permission_deny_rules == ["run_*"]
    assert restored.permission_overrides == {"write_file": "confirm"}
    assert restored.compaction_state == "compacted"
    assert restored.compaction_summary == "older turns were summarized"
    assert restored.transcript[0]["content"] == "hello"

    listed = repo.list_conversations()
    assert [item.id for item in listed] == [created.id]

    repo.update_permission_mode(created.id, "auto")
    restored_after_exit = repo.get_conversation(created.id)
    assert restored_after_exit is not None
    assert restored_after_exit.permission_mode == "auto"
    assert restored_after_exit.permission_previous_mode == ""

    assert repo.delete_conversation(created.id) is True
    assert repo.get_conversation(created.id) is None


def test_conversation_repository_upserts_one_assistant_lifecycle_by_id(tmp_path) -> None:
    from backend.conversations.repository import ConversationRepository

    repo = ConversationRepository(base_dir=tmp_path)
    created = repo.create_conversation(conversation_id="conv_upsertlife")
    repo.append_transcript_message(
        created.id,
        {"id": "user-1", "role": "user", "content": "inspect the project"},
    )

    repo.upsert_transcript_message(
        created.id,
        {
            "id": "assistant-1",
            "role": "assistant",
            "content": "",
            "terminal_status": "partial",
            "blocks": [{"type": "process", "id": "step-1", "content": "Reading files"}],
        },
    )
    repo.upsert_transcript_message(
        created.id,
        {
            "id": "assistant-1",
            "role": "assistant",
            "content": "Done.",
            "terminal_status": "completed",
            "blocks": [{"type": "text", "content": "Done.", "status": "completed"}],
        },
    )

    restored = ConversationRepository(base_dir=tmp_path).get_conversation(created.id)
    assert restored is not None
    assert [message["id"] for message in restored.transcript] == ["user-1", "assistant-1"]
    assert restored.transcript[-1]["terminal_status"] == "completed"
    assert restored.transcript[-1]["content"] == "Done."


def test_context_fork_branch_metadata_round_trips(tmp_path) -> None:
    from backend.conversations.repository import ConversationRepository

    repo = ConversationRepository(base_dir=tmp_path)
    parent = repo.create_conversation(
        conversation_id="conv_parent",
        title="Parent",
        transcript=[{"id": "m1", "role": "user", "content": "start"}],
    )
    branch = repo.create_conversation(
        conversation_id="fork_branch_1",
        title="Parent · 分支",
        transcript=list(parent.transcript),
        context_snapshot={"history": [{"role": "user", "content": "start"}]},
        parent_conversation_id=parent.id,
        parent_message_index=0,
        fork_id="fork_1234567890abcdef",
        branch_kind="context_fork",
    )

    restored = repo.get_conversation(branch.id)
    assert restored is not None
    assert restored.parent_conversation_id == parent.id
    assert restored.parent_message_index == 0
    assert restored.fork_id == "fork_1234567890abcdef"
    assert restored.branch_kind == "context_fork"
    assert restored.context_snapshot["history"][0]["content"] == "start"


def test_conversation_clone_is_deep_and_does_not_duplicate_worktree_ownership(tmp_path) -> None:
    from backend.conversations.repository import ConversationRepository

    repo = ConversationRepository(base_dir=tmp_path)
    parent = repo.create_conversation(
        conversation_id="conv_clone_parent",
        title="Protected parent",
        transcript=[{"id": "m1", "role": "user", "content": {"text": "start"}}],
        context_snapshot={"history": [{"role": "user", "content": "start"}]},
        workspace_root="C:/repo",
        worktree_path="C:/repo/.minicode/worktrees/conv_clone_parent",
        git_isolated=True,
    )

    clone = repo.clone_conversation(parent.id)
    assert clone is not None
    assert clone.parent_conversation_id == parent.id
    assert clone.parent_message_index == 0
    assert clone.branch_kind == "clone"
    assert clone.workspace_root == parent.worktree_path
    assert clone.worktree_path == ""
    assert clone.git_isolated is False

    clone.transcript[0]["content"]["text"] = "changed in clone"
    clone.context_snapshot["history"][0]["content"] = "changed context"
    restored_parent = repo.get_conversation(parent.id)
    assert restored_parent is not None
    assert restored_parent.transcript[0]["content"]["text"] == "start"
    assert restored_parent.context_snapshot["history"][0]["content"] == "start"


def test_conversation_merge_fast_forwards_only_an_unchanged_parent(tmp_path) -> None:
    from backend.conversations.repository import ConversationRepository

    repo = ConversationRepository(base_dir=tmp_path)
    parent = repo.create_conversation(
        conversation_id="conv_merge_parent",
        title="Parent",
        transcript=[{"id": "m1", "role": "user", "content": "start"}],
        context_snapshot={"history": [{"role": "user", "content": "start"}]},
    )
    branch = repo.clone_conversation(parent.id)
    assert branch is not None
    repo.append_transcript_message(branch.id, {"id": "m2", "role": "assistant", "content": "branch answer"})
    branch = repo.get_conversation(branch.id)
    assert branch is not None
    branch.context_snapshot = {"history": [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "branch answer"},
    ]}
    repo.save_conversation(branch)

    merged_source, merged_target, status = repo.merge_conversation_fast_forward(branch.id, parent.id)
    assert status == "merged"
    assert merged_source is not None and merged_source.merged_into_conversation_id == parent.id
    assert merged_source.merged_at
    assert merged_target is not None
    assert [message["id"] for message in merged_target.transcript] == ["m1", "m2"]
    assert merged_target.context_snapshot == branch.context_snapshot

    divergent_parent = repo.create_conversation(
        conversation_id="conv_merge_diverged",
        title="Diverged",
        transcript=[{"id": "d1", "role": "user", "content": "base"}],
    )
    divergent_branch = repo.clone_conversation(divergent_parent.id)
    assert divergent_branch is not None
    repo.append_transcript_message(divergent_parent.id, {"id": "d2-parent", "role": "assistant", "content": "parent"})
    repo.append_transcript_message(divergent_branch.id, {"id": "d2-branch", "role": "assistant", "content": "branch"})
    _, unchanged_target, conflict = repo.merge_conversation_fast_forward(divergent_branch.id, divergent_parent.id)
    assert conflict == "target_diverged"
    assert unchanged_target is not None
    assert [message["id"] for message in unchanged_target.transcript] == ["d1", "d2-parent"]


def test_conversation_tree_export_contains_ancestry_descendants_and_provenance(tmp_path) -> None:
    import json

    from backend.conversations.repository import ConversationRepository

    repo = ConversationRepository(base_dir=tmp_path)
    root = repo.create_conversation(conversation_id="conv_export_root", title="Root")
    child = repo.clone_conversation(root.id)
    assert child is not None
    grandchild = repo.clone_conversation(child.id)
    sibling = repo.clone_conversation(root.id)
    assert grandchild is not None and sibling is not None

    payload = repo.export_conversation_tree(child.id, include_descendants=True)
    assert payload is not None
    assert payload["schema"] == "minicode.conversation.export"
    assert payload["version"] == 1
    assert payload["root_conversation_id"] == root.id
    assert payload["selected_conversation_id"] == child.id
    exported_ids = {item["id"] for item in payload["conversations"]}
    assert exported_ids == {root.id, child.id, grandchild.id, sibling.id}
    json.dumps(payload, ensure_ascii=False)
def test_session_restore_prefers_conversation_workspace_over_stale_client_workspace(tmp_path) -> None:
    from backend.conversations.repository import ConversationRepository
    from backend.workspace.state import clear_active_workspace_root, set_active_workspace_root
    from backend.ws.session_restore import SessionRestoreManager

    repo = ConversationRepository(base_dir=tmp_path / "conversations")
    conversation_workspace = tmp_path / "conversation-workspace"
    stale_client_workspace = tmp_path / "stale-client-workspace"
    conversation_workspace.mkdir()
    stale_client_workspace.mkdir()
    conversation = repo.create_conversation(
        conversation_id="conv_restore_workspace",
        workspace_root=str(conversation_workspace),
    )
    set_active_workspace_root(stale_client_workspace)

    try:
        result = asyncio.run(
            SessionRestoreManager(repo).restore_session(
                "session-restore-workspace",
                last_conversation_id=conversation.id,
                last_workspace_root=str(stale_client_workspace),
            )
        )
    finally:
        clear_active_workspace_root()

    assert result["conversation"]["workspace_root"] == str(conversation_workspace)
    assert result["workspace"]["root_path"] == str(conversation_workspace.resolve())


def test_session_restore_ignores_last_workspace_when_conversation_is_unbound(tmp_path) -> None:
    from backend.conversations.repository import ConversationRepository
    from backend.workspace.state import clear_active_workspace_root, set_active_workspace_root
    from backend.ws.session_restore import SessionRestoreManager

    repo = ConversationRepository(base_dir=tmp_path / "conversations")
    active_workspace = tmp_path / "active-workspace"
    requested_workspace = tmp_path / "requested-workspace"
    active_workspace.mkdir()
    requested_workspace.mkdir()
    conversation = repo.create_conversation(conversation_id="conv_restore_unbound")
    set_active_workspace_root(active_workspace)

    try:
        result = asyncio.run(
            SessionRestoreManager(repo).restore_session(
                "session-restore-unbound",
                last_conversation_id=conversation.id,
                last_workspace_root=str(requested_workspace),
            )
        )
    finally:
        clear_active_workspace_root()

    assert result["conversation"]["workspace_root"] == ""
    assert result["workspace"] is None


def test_conversation_repository_repairs_legacy_mojibake_on_load(tmp_path) -> None:
    import json

    from backend.conversations.repository import ConversationRepository

    base_dir = tmp_path / "conversations"
    base_dir.mkdir()
    conversation_id = "conv_mojibake"
    bad_hello = "\u6d63\u72b2\u30bd"
    good_hello = "\u4f60\u597d"
    bad_project_name = "Minist\u93b5\u5b2a\u5553\u93c1\u677f\u74e7\u7487\u55d7\u57c6"
    good_project_name = "Minist\u624b\u5199\u6570\u5b57\u8bc6\u522b"
    workspace_root = tmp_path / bad_project_name

    meta = {
        "id": conversation_id,
        "title": bad_hello,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "message_count": 1,
        "workspace_root": str(workspace_root),
        "summary": bad_hello,
    }
    transcript = {
        "id": "user_mojibake",
        "role": "user",
        "content": bad_hello,
        "blocks": [{"type": "text", "content": bad_hello}],
    }
    snapshot = {"history": [{"role": "user", "content": bad_hello}]}

    (base_dir / f"{conversation_id}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False),
        encoding="utf-8",
    )
    (base_dir / f"{conversation_id}.transcript.jsonl").write_text(
        json.dumps(transcript, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (base_dir / f"{conversation_id}.snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False),
        encoding="utf-8",
    )

    record = ConversationRepository(base_dir=base_dir).get_conversation(conversation_id)

    assert record is not None
    assert record.title == good_hello
    assert record.summary == good_hello
    assert good_project_name in record.workspace_root
    assert record.transcript[0]["content"] == good_hello
    assert record.transcript[0]["blocks"][0]["content"] == good_hello
    assert record.context_snapshot["history"][0]["content"] == good_hello


def test_conversation_repository_does_not_fabricate_text_for_legacy_tool_only_assistant(tmp_path) -> None:
    import json

    from backend.conversations.repository import ConversationRepository

    conversation_id = "conv_legacy_tool_only"
    meta = {
        "id": conversation_id,
        "title": "Legacy tool-only chat",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "message_count": 2,
    }
    transcript = [
        {
            "id": "user_legacy_tool_only",
            "role": "user",
            "content": "check weather",
        },
        {
            "id": "assistant_legacy_tool_only",
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "tool_search_failed",
                    "name": "web_search",
                    "args": {"query": "weather"},
                    "status": "failed",
                    "summary": "network unavailable",
                }
            ],
            "blocks": [
                {
                    "type": "tool_call",
                    "record": {
                        "id": "tool_search_failed",
                        "name": "web_search",
                        "args": {"query": "weather"},
                        "status": "failed",
                        "summary": "network unavailable",
                    },
                }
            ],
        },
    ]

    (tmp_path / f"{conversation_id}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False),
        encoding="utf-8",
    )
    transcript_path = tmp_path / f"{conversation_id}.transcript.jsonl"
    transcript_path.write_text(
        "\n".join(json.dumps(message, ensure_ascii=False) for message in transcript) + "\n",
        encoding="utf-8",
    )
    raw_transcript = transcript_path.read_text(encoding="utf-8")

    record = ConversationRepository(base_dir=tmp_path).get_conversation(conversation_id)

    assert record is not None
    assistant_message = record.transcript[1]
    assert assistant_message["content"] == ""
    assert all(block["type"] != "text" for block in assistant_message["blocks"])
    assert transcript_path.read_text(encoding="utf-8") == raw_transcript


def test_conversation_repository_does_not_duplicate_legacy_tool_step_with_final_reply(tmp_path) -> None:
    import json

    from backend.conversations.repository import ConversationRepository

    conversation_id = "conv_legacy_tool_then_final"
    meta = {
        "id": conversation_id,
        "title": "Legacy tool then final",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "message_count": 3,
    }
    transcript = [
        {"id": "user_legacy_tool_then_final", "role": "user", "content": "check weather"},
        {
            "id": "assistant_legacy_tool_step",
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "tool_search_success",
                    "name": "web_search",
                    "args": {"query": "weather"},
                    "status": "success",
                    "summary": "found weather",
                }
            ],
            "blocks": [
                {
                    "type": "tool_call",
                    "record": {
                        "id": "tool_search_success",
                        "name": "web_search",
                        "args": {"query": "weather"},
                        "status": "success",
                        "summary": "found weather",
                    },
                }
            ],
        },
        {
            "id": "assistant_legacy_final",
            "role": "assistant",
            "content": "The weather source says it is sunny.",
            "blocks": [{"type": "text", "content": "The weather source says it is sunny."}],
        },
    ]

    (tmp_path / f"{conversation_id}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / f"{conversation_id}.transcript.jsonl").write_text(
        "\n".join(json.dumps(message, ensure_ascii=False) for message in transcript) + "\n",
        encoding="utf-8",
    )

    record = ConversationRepository(base_dir=tmp_path).get_conversation(conversation_id)

    assert record is not None
    tool_step = record.transcript[1]
    assert tool_step["content"] == ""
    assert not any(
        block.get("type") == "text" and block.get("content")
        for block in tool_step["blocks"]
    )
    assert record.transcript[2]["content"] == "The weather source says it is sunny."


def test_conversation_repository_accepts_frontend_generated_ids(tmp_path) -> None:
    from backend.conversations.repository import ConversationRepository

    repo = ConversationRepository(base_dir=tmp_path)

    created = repo.create_conversation(conversation_id="conv-mabc123-abcdef", title="Frontend")

    assert created.id == "conv-mabc123-abcdef"
    assert repo.get_conversation("conv-mabc123-abcdef") is not None


def test_conversation_repository_commits_meta_transcript_and_snapshot_as_one_generation(tmp_path) -> None:
    import json

    from backend.conversations.repository import ConversationRepository

    repo = ConversationRepository(base_dir=tmp_path)
    created = repo.create_conversation(memory_mode="disabled")

    repo.append_transcript_message(
        created.id,
        {
            "id": "msg_1",
            "role": "user",
            "content": "hello split storage",
            "timestamp": "10:00",
        },
    )
    repo.save_context_snapshot(
        created.id,
        {
            "history": [{"role": "user", "content": "hello split storage"}],
            "compaction_count": 1,
        },
    )

    manifest_path = tmp_path / f"{created.id}.manifest.json"
    legacy_path = tmp_path / f"{created.id}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    current_generation = manifest["current_generation"]
    previous_generation = manifest["previous_generation"]
    meta_path = tmp_path / f"{created.id}.g{current_generation}.meta.json"
    transcript_path = tmp_path / f"{created.id}.g{current_generation}.transcript.jsonl"
    snapshot_path = tmp_path / f"{created.id}.g{current_generation}.snapshot.json"

    assert manifest_path.exists()
    assert meta_path.exists()
    assert transcript_path.exists()
    assert snapshot_path.exists()
    assert not legacy_path.exists()
    assert previous_generation == current_generation - 1

    meta_payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "transcript" not in meta_payload
    assert "context_snapshot" not in meta_payload
    assert meta_payload["message_count"] == 1

    transcript_lines = transcript_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(transcript_lines) == 1
    assert json.loads(transcript_lines[0])["content"] == "hello split storage"

    restored = repo.get_conversation(created.id)
    assert restored is not None
    assert restored.transcript[0]["content"] == "hello split storage"
    assert restored.context_snapshot["compaction_count"] == 1


def test_conversation_repository_uses_atomic_replaces_and_releases_lock(tmp_path, monkeypatch) -> None:
    import json
    from pathlib import Path

    import backend.atomic_io as atomic_io_module
    from backend.conversations.repository import ConversationRepository

    real_replace = atomic_io_module.os.replace
    replaced: list[tuple[Path, Path]] = []

    def recording_replace(src: str | Path, dst: str | Path) -> None:
        replaced.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(atomic_io_module.os, "replace", recording_replace)

    repo = ConversationRepository(base_dir=tmp_path)
    created = repo.create_conversation()
    repo.append_transcript_message(
        created.id,
        {"id": "msg_1", "role": "user", "content": "atomic hello"},
    )
    repo.save_context_snapshot(created.id, {"history": [{"role": "user", "content": "atomic hello"}]})

    replaced_targets = {target.name for _source, target in replaced}
    manifest_path = tmp_path / f"{created.id}.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    generation = manifest["current_generation"]
    assert f"{created.id}.g{generation}.meta.json" in replaced_targets
    assert f"{created.id}.g{generation}.transcript.jsonl" in replaced_targets
    assert f"{created.id}.g{generation}.snapshot.json" in replaced_targets
    assert manifest_path.name in replaced_targets
    second = ConversationRepository(base_dir=tmp_path)
    assert second.rename_conversation(created.id, "Lock released") is not None
    transcript_path = tmp_path / f"{created.id}.g{generation}.transcript.jsonl"
    assert json.loads(transcript_path.read_text(encoding="utf-8").strip())["content"] == "atomic hello"


def test_conversation_manifest_is_the_commit_point_when_generation_write_fails(tmp_path, monkeypatch) -> None:
    import json

    import pytest

    from backend.conversations.repository import ConversationRepository

    repo = ConversationRepository(base_dir=tmp_path)
    created = repo.create_conversation(title="Before failure")
    manifest_path = tmp_path / f"{created.id}.manifest.json"
    original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    original_write = repo._safe_write_text
    failing_generation = original_manifest["current_generation"] + 1

    def fail_snapshot(path, text, encoding="utf-8"):
        if path.name == f"{created.id}.g{failing_generation}.snapshot.json":
            raise OSError("simulated generation failure")
        return original_write(path, text, encoding=encoding)

    monkeypatch.setattr(repo, "_safe_write_text", fail_snapshot)

    with pytest.raises(OSError, match="simulated generation failure"):
        repo.append_transcript_message(
            created.id,
            {"id": "uncommitted", "role": "user", "content": "must not publish"},
        )

    assert json.loads(manifest_path.read_text(encoding="utf-8")) == original_manifest
    in_process = repo.get_conversation(created.id)
    restored = ConversationRepository(base_dir=tmp_path).get_conversation(created.id)
    assert in_process is not None and in_process.transcript == []
    assert restored is not None and restored.transcript == []


def test_conversation_reader_falls_back_whole_generation_when_current_is_invalid(tmp_path) -> None:
    import json

    from backend.conversations.repository import ConversationRepository

    for corruption in ("missing", "malformed", "message_count"):
        base_dir = tmp_path / corruption
        repo = ConversationRepository(base_dir=base_dir)
        created = repo.create_conversation(title=f"Fallback {corruption}")
        repo.append_transcript_message(
            created.id,
            {"id": "current-only", "role": "user", "content": corruption},
        )
        manifest = json.loads(
            (base_dir / f"{created.id}.manifest.json").read_text(encoding="utf-8")
        )
        current = manifest["current_generation"]
        previous = manifest["previous_generation"]
        assert previous is not None

        if corruption == "missing":
            (base_dir / f"{created.id}.g{current}.snapshot.json").unlink()
        elif corruption == "malformed":
            (base_dir / f"{created.id}.g{current}.transcript.jsonl").write_text(
                "{not-json}\n",
                encoding="utf-8",
            )
        else:
            meta_path = base_dir / f"{created.id}.g{current}.meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["message_count"] = 99
            meta_path.write_text(json.dumps(meta), encoding="utf-8")

        restored = ConversationRepository(base_dir=base_dir).get_conversation(created.id)
        assert restored is not None
        assert restored.transcript == []
        assert restored.message_count == 0


@pytest.mark.parametrize("corruption", ["missing", "malformed"])
def test_conversation_repository_recovers_write_from_readable_previous_generation(
    tmp_path,
    corruption: str,
) -> None:
    import json

    from backend.conversations.repository import ConversationRepository

    base_dir = tmp_path / corruption
    repo = ConversationRepository(base_dir=base_dir)
    created = repo.create_conversation(title="Recoverable write")
    repo.append_transcript_message(
        created.id,
        {"id": "first", "role": "user", "content": "first"},
    )
    repo.append_transcript_message(
        created.id,
        {"id": "second", "role": "assistant", "content": "second"},
    )
    manifest_path = base_dir / f"{created.id}.manifest.json"
    manifest_before = json.loads(manifest_path.read_text(encoding="utf-8"))
    current = int(manifest_before["current_generation"])
    previous = int(manifest_before["previous_generation"])
    assert current == previous + 1
    current_snapshot = base_dir / f"{created.id}.g{current}.snapshot.json"
    current_transcript = base_dir / f"{created.id}.g{current}.transcript.jsonl"
    if corruption == "missing":
        current_snapshot.unlink()
    else:
        current_transcript.write_text("{not-json}\n", encoding="utf-8")

    recovering_repo = ConversationRepository(base_dir=base_dir)
    recovered = recovering_repo.get_conversation(created.id)
    assert recovered is not None
    assert [message["id"] for message in recovered.transcript] == ["first"]

    committed = recovering_repo.append_transcript_message(
        created.id,
        {"id": "third", "role": "assistant", "content": "third"},
    )
    assert committed is not None
    assert committed.revision == current + 1
    assert [message["id"] for message in committed.transcript] == ["first", "third"]
    manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_after["current_generation"] == current + 1
    assert manifest_after["previous_generation"] == previous
    if corruption == "missing":
        assert not current_snapshot.exists()
    else:
        assert current_transcript.read_text(encoding="utf-8") == "{not-json}\n"

    restored = ConversationRepository(base_dir=base_dir).get_conversation(created.id)
    assert restored is not None
    assert [message["id"] for message in restored.transcript] == ["first", "third"]


def test_conversation_repository_rejects_write_when_current_and_previous_generations_are_corrupt(
    tmp_path,
) -> None:
    import json

    from backend.conversations.repository import (
        ConversationStorageCorruptError,
        ConversationRepository,
    )

    repo = ConversationRepository(base_dir=tmp_path)
    created = repo.create_conversation(title="Corrupt write")
    repo.append_transcript_message(
        created.id,
        {"id": "first", "role": "user", "content": "first"},
    )
    repo.append_transcript_message(
        created.id,
        {"id": "second", "role": "assistant", "content": "second"},
    )
    manifest_path = tmp_path / f"{created.id}.manifest.json"
    manifest_before = json.loads(manifest_path.read_text(encoding="utf-8"))
    current = int(manifest_before["current_generation"])
    previous = int(manifest_before["previous_generation"])
    current_meta = tmp_path / f"{created.id}.g{current}.meta.json"
    previous_snapshot = tmp_path / f"{created.id}.g{previous}.snapshot.json"
    current_meta.write_text("{broken-current}\n", encoding="utf-8")
    previous_snapshot.write_text("{broken-previous}\n", encoding="utf-8")
    before_files = {
        path.name: path.read_bytes()
        for path in tmp_path.glob(f"{created.id}.g*")
    }

    with pytest.raises(ConversationStorageCorruptError):
        ConversationRepository(base_dir=tmp_path).append_transcript_message(
            created.id,
            {"id": "must-not-publish", "role": "user", "content": "no overwrite"},
        )

    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest_before
    assert {
        path.name: path.read_bytes()
        for path in tmp_path.glob(f"{created.id}.g*")
    } == before_files
    assert not any(tmp_path.glob(f"{created.id}.g{current + 1}.*"))


def test_conversation_repository_keeps_detached_writer_revision_conflict(
    tmp_path,
) -> None:
    from backend.conversations.repository import ConversationRepository, ConversationWriteConflict

    repo = ConversationRepository(base_dir=tmp_path)
    created = repo.create_conversation(title="Detached writer")
    stale = repo.get_conversation(created.id)
    assert stale is not None
    repo.append_transcript_message(
        created.id,
        {"id": "winning", "role": "user", "content": "newer revision"},
    )

    with pytest.raises(ConversationWriteConflict):
        repo.commit_turn_projection(
            created.id,
            assistant_message={
                "id": "stale-answer",
                "role": "assistant",
                "content": "stale writer",
            },
            context_snapshot={},
            expected_revision=stale.revision,
        )


def test_legacy_split_record_migrates_with_previous_generation_fallback(tmp_path) -> None:
    import json

    from backend.conversations.models import ConversationRecord
    from backend.conversations.repository import ConversationRepository

    legacy = ConversationRecord(
        id="conv_legacy_split",
        title="Legacy split",
        transcript=[{"id": "legacy", "role": "user", "content": "before migration"}],
        context_snapshot={"history": [{"role": "user", "content": "before migration"}]},
    )
    (tmp_path / f"{legacy.id}.meta.json").write_text(
        json.dumps(legacy.to_meta_dict()),
        encoding="utf-8",
    )
    (tmp_path / f"{legacy.id}.transcript.jsonl").write_text(
        json.dumps(legacy.transcript[0]) + "\n",
        encoding="utf-8",
    )
    (tmp_path / f"{legacy.id}.snapshot.json").write_text(
        json.dumps(legacy.context_snapshot),
        encoding="utf-8",
    )

    repo = ConversationRepository(base_dir=tmp_path)
    repo.append_transcript_message(
        legacy.id,
        {"id": "new", "role": "assistant", "content": "after migration"},
    )

    manifest = json.loads(
        (tmp_path / f"{legacy.id}.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["current_generation"] == 2
    assert manifest["previous_generation"] == 1
    assert not (tmp_path / f"{legacy.id}.meta.json").exists()
    previous_lines = (
        tmp_path / f"{legacy.id}.g1.transcript.jsonl"
    ).read_text(encoding="utf-8").strip().splitlines()
    current_lines = (
        tmp_path / f"{legacy.id}.g2.transcript.jsonl"
    ).read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["content"] for line in previous_lines] == ["before migration"]
    assert [json.loads(line)["content"] for line in current_lines] == [
        "before migration",
        "after migration",
    ]


def test_conversation_generation_retention_and_delete_cleanup(tmp_path) -> None:
    import json

    from backend.conversations.repository import ConversationRepository

    repo = ConversationRepository(base_dir=tmp_path)
    created = repo.create_conversation()
    for index in range(4):
        repo.append_transcript_message(
            created.id,
            {"id": f"msg-{index}", "role": "user", "content": str(index)},
        )

    manifest = json.loads(
        (tmp_path / f"{created.id}.manifest.json").read_text(encoding="utf-8")
    )
    kept = {manifest["current_generation"], manifest["previous_generation"]}
    generation_files = list(tmp_path.glob(f"{created.id}.g*"))
    assert len(generation_files) == 6
    assert {
        int(path.name.split(".g", 1)[1].split(".", 1)[0])
        for path in generation_files
    } == kept

    assert repo.delete_conversation(created.id) is True
    remaining = list(tmp_path.glob(f"{created.id}*"))
    assert [path.name for path in remaining] == [f"{created.id}.manifest.json"]
    tombstone = json.loads(remaining[0].read_text(encoding="utf-8"))
    assert tombstone["deleted"] is True
    assert tombstone["deletion_generation"] == manifest["current_generation"] + 1
    assert ConversationRepository(base_dir=tmp_path).get_conversation(created.id) is None


def test_conversation_repository_reads_legacy_single_file_records(tmp_path) -> None:
    import json

    from backend.conversations.models import ConversationRecord
    from backend.conversations.repository import ConversationRepository

    legacy = ConversationRecord(
        id="conv_legacy123",
        title="Legacy chat",
        summary="legacy summary",
        transcript=[
            {
                "id": "msg_1",
                "role": "user",
                "content": "legacy transcript",
                "timestamp": "09:00",
            }
        ],
        context_snapshot={"history": [{"role": "user", "content": "legacy transcript"}]},
    )
    (tmp_path / f"{legacy.id}.json").write_text(
        json.dumps(legacy.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    repo = ConversationRepository(base_dir=tmp_path)
    restored = repo.get_conversation(legacy.id)

    assert restored is not None
    assert restored.title == "Legacy chat"
    assert restored.summary == "legacy summary"
    assert restored.transcript[0]["content"] == "legacy transcript"
    assert restored.context_snapshot["history"][0]["content"] == "legacy transcript"


def test_conversation_repository_reuses_cached_summary_index_for_repeat_list(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    from backend.conversations.repository import ConversationRepository

    repo = ConversationRepository(base_dir=tmp_path)
    first = repo.create_conversation(title="First")
    second = repo.create_conversation(title="Second")

    original_read_text = Path.read_text
    read_calls: list[str] = []

    def counting_read_text(self: Path, *args, **kwargs):
        read_calls.append(self.name)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    listed_once = repo.list_conversations()
    first_pass_reads = len(read_calls)

    listed_twice = repo.list_conversations()

    assert {item.id for item in listed_once} == {first.id, second.id}
    assert [item.id for item in listed_twice] == [item.id for item in listed_once]
    assert first_pass_reads >= 2
    assert read_calls[first_pass_reads:] == [
        ".conversation-store.instance",
        ".conversation-store.revision",
    ]


def test_conversation_repository_updates_cached_index_after_mutation(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    from backend.conversations.repository import ConversationRepository

    repo = ConversationRepository(base_dir=tmp_path)
    created = repo.create_conversation()

    repo.list_conversations()

    original_read_text = Path.read_text
    read_calls: list[str] = []

    def counting_read_text(self: Path, *args, **kwargs):
        read_calls.append(self.name)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    repo.append_transcript_message(
        created.id,
        {
            "id": "msg_1",
            "role": "user",
            "content": "refresh the cached title",
            "timestamp": "10:00",
        },
    )
    mutation_reads = len(read_calls)

    listed = repo.list_conversations()

    assert listed[0].id == created.id
    assert listed[0].title == "refresh the cached title"
    assert listed[0].message_count == 1
    assert mutation_reads >= 3
    assert read_calls[mutation_reads:] == [
        ".conversation-store.instance",
        ".conversation-store.revision",
    ]


def test_conversation_list_order_stays_fixed_when_existing_sessions_update(tmp_path) -> None:
    from backend.conversations.repository import ConversationRepository

    repo = ConversationRepository(base_dir=tmp_path)
    first = repo.create_conversation(title="First")
    first.created_at = "2026-07-19T08:00:00+00:00"
    repo.save_conversation(first)
    second = repo.create_conversation(title="Second")
    second.created_at = "2026-07-19T09:00:00+00:00"
    repo.save_conversation(second)

    assert [item.id for item in repo.list_conversations()] == [second.id, first.id]

    repo.append_transcript_message(first.id, {
        "id": "msg_first_update",
        "role": "user",
        "content": "Update the older conversation",
        "timestamp": "10:00",
    })
    repo.rename_conversation(first.id, "Renamed first")
    repo.patch_context_snapshot(first.id, {"goal": {"text": "Keep the row fixed"}})

    assert [item.id for item in repo.list_conversations()] == [second.id, first.id]

    third = repo.create_conversation(title="Third")
    third.created_at = "2026-07-19T10:00:00+00:00"
    repo.save_conversation(third)
    assert [item.id for item in repo.list_conversations()] == [third.id, second.id, first.id]


def test_websocket_exposes_conversation_lifecycle(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_conversations") as ws:
            first = ws.receive_json()
            assert first["type"] == "mcp_status"
            second = ws.receive_json()
            assert second["type"] == "llm.model.updated"

            ws.send_json({"type": "conversation.list"})
            third = ws.receive_json()
            assert third["type"] == "conversation.list"
            assert third["conversations"] == []
            assert third["active_conversation_id"] is None
            assert third["active_conversation"] is None

            ws.send_json({"type": "conversation.create", "memory_mode": "none"})
            original = _receive_created_conversation(ws)
            assert original["type"] == "conversation.list"
            assert len(original["conversations"]) == 1
            original_id = original["active_conversation_id"]

            ws.send_json({"type": "conversation.create", "memory_mode": "summary"})
            created = _receive_until_event(ws, "conversation.list")
            assert created["type"] == "conversation.list"
            assert len(created["conversations"]) == 2
            assert created["active_conversation_id"] != original_id

            new_id = created["active_conversation_id"]
            ws.send_json({"type": "conversation.switch", "conversation_id": original_id})
            switched = _receive_conversation_switched(ws, original_id)
            assert switched["type"] == "conversation.switched"
            assert switched["conversation"]["id"] == original_id

            ws.send_json({"type": "conversation.delete", "conversation_id": new_id})
            deleted = _receive_until_event(ws, "conversation.list")
            assert deleted["type"] == "conversation.list"
            assert len(deleted["conversations"]) == 1
            assert deleted["active_conversation_id"] == original_id

            ws.send_json({"type": "conversation.delete", "conversation_id": original_id})
            blank = _receive_until_event(ws, "conversation.list")
            assert blank["type"] == "conversation.list"
            assert blank["conversations"] == []
            assert blank["active_conversation_id"] is None
            assert blank["active_conversation"] is None


def test_websocket_context_fork_resolves_frontend_message_id(monkeypatch, tmp_path) -> None:
    """Exercise the actual command route and wire event projection."""
    from backend.agent.context import ContextBuilder
    from backend.config import AgentSettings, TokenBudget
    from backend.conversations.repository import ConversationRepository

    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    repo = ConversationRepository(base_dir=tmp_path)
    context = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
    context.append_user("start")
    context.append_assistant("answer")
    parent = repo.create_conversation(
        conversation_id="conv_fork_ws",
        title="Fork websocket",
        transcript=[
            {"id": "user-start", "role": "user", "content": "start"},
            {"id": "assistant-answer", "role": "assistant", "content": "answer"},
        ],
        context_snapshot=context.export_snapshot(),
    )

    session_id = f"session_test_context_fork_{tmp_path.name}"
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?session_id={session_id}") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "conversation.switch", "conversation_id": parent.id})
            switched = _receive_conversation_switched(ws, parent.id)
            assert switched["conversation"]["id"] == parent.id

            # Deliberately send a stale UI index. The persisted message id is
            # authoritative and must select the assistant answer.
            ws.send_json(
                {
                    "type": "context.fork",
                    "message_id": "assistant-answer",
                    "message_index": 0,
                    "create_branch": True,
                }
            )
            forked = _receive_until_event(ws, "context_forked")

    assert forked["message_id"] == "assistant-answer"
    assert forked["message_index"] == 1
    assert forked["context_history_index"] == 1
    branch = repo.get_conversation(str(forked["branch_conversation_id"]))
    assert branch is not None
    assert branch.parent_conversation_id == parent.id
    assert branch.parent_message_index == 1
    assert branch.transcript[-1]["id"] == "assistant-answer"


def test_websocket_session_tree_clone_merge_and_export_are_end_to_end(monkeypatch, tmp_path) -> None:
    import json

    from backend.conversations.repository import ConversationRepository

    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    repo = ConversationRepository(base_dir=tmp_path)
    parent = repo.create_conversation(
        conversation_id="conv_tree_parent",
        title="Tree parent",
        transcript=[{"id": "tree-m1", "role": "user", "content": "start"}],
        context_snapshot={"history": [{"role": "user", "content": "start"}]},
    )
    branch = repo.clone_conversation(parent.id, title="Tree branch")
    assert branch is not None
    repo.append_transcript_message(
        branch.id,
        {"id": "tree-m2", "role": "assistant", "content": "branch answer"},
    )
    branch = repo.get_conversation(branch.id)
    assert branch is not None
    branch.context_snapshot = {
        "history": [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "branch answer"},
        ]
    }
    repo.save_conversation(branch)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_session_tree") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "conversation.clone", "conversation_id": parent.id})
            cloned_list = _receive_until_event(ws, "conversation.list")
            clone_ids = {
                item["id"] for item in cloned_list["conversations"]
                if item.get("parent_conversation_id") == parent.id and item["id"] != branch.id
            }
            assert len(clone_ids) == 1
            clone_result = _receive_until_event(ws, "command.result")
            assert clone_result["command"] == "conversation.clone"
            assert clone_result["data"]["conversation_id"] in clone_ids

            ws.send_json({
                "type": "conversation.export",
                "conversation_id": branch.id,
                "include_descendants": True,
            })
            export_result = _receive_until_event(ws, "command.result")
            assert export_result["command"] == "conversation.export"
            export_payload = json.loads(export_result["data"]["content"])
            assert export_payload["schema"] == "minicode.conversation.export"
            assert export_payload["root_conversation_id"] == parent.id
            assert {item["id"] for item in export_payload["conversations"]} >= {
                parent.id,
                branch.id,
                *clone_ids,
            }

            ws.send_json({
                "type": "conversation.merge",
                "conversation_id": branch.id,
                "target_conversation_id": parent.id,
            })
            _receive_until_event(ws, "conversation.list")
            merge_result = _receive_until_event(ws, "command.result")
            assert merge_result["command"] == "conversation.merge"
            assert merge_result["data"]["status"] == "merged"

    restored_repo = ConversationRepository(base_dir=tmp_path)
    restored_parent = restored_repo.get_conversation(parent.id)
    restored_branch = restored_repo.get_conversation(branch.id)
    assert restored_parent is not None
    assert [message["id"] for message in restored_parent.transcript] == ["tree-m1", "tree-m2"]
    assert restored_branch is not None
    assert restored_branch.merged_into_conversation_id == parent.id


def test_websocket_session_restore_emits_active_conversation_snapshot(monkeypatch, tmp_path) -> None:
    from backend.conversations.repository import ConversationRepository

    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    workspace_root = tmp_path / "project"
    workspace_root.mkdir()
    repo = ConversationRepository(base_dir=tmp_path)
    conversation = repo.create_conversation(
        conversation_id="conv_restore_active",
        title="Project chat",
        workspace_root=str(workspace_root),
        transcript=[{
            "id": "user_restore_active",
            "role": "user",
            "content": "hello from restored project",
        }],
    )

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_restore_active") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({
                "type": "session.restore",
                "last_conversation_id": conversation.id,
            })
            restored = _receive_until_event(ws, "session.restored")

    assert restored["active_conversation_id"] == conversation.id
    assert restored["active_conversation"]["id"] == conversation.id
    assert restored["active_conversation"]["transcript"][0]["content"] == "hello from restored project"
    assert restored["session"]["active_conversation_id"] == conversation.id
    assert restored["session"]["workspace_root"] == str(workspace_root.resolve())
    assert restored["messages"][0]["content"] == "hello from restored project"


def test_websocket_completed_conversation_restores_after_same_session_reconnect(
    monkeypatch,
    tmp_path,
) -> None:
    from backend.conversations.repository import ConversationRepository

    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    repo = ConversationRepository(base_dir=tmp_path)
    conversation = repo.create_conversation(
        conversation_id="conv_restore_completed",
        title="Completed provider turn",
        transcript=[
            {
                "id": "user_restore_completed",
                "role": "user",
                "content": "verify provider projection",
            },
            {
                "id": "assistant_restore_completed",
                "role": "assistant",
                "content": "provider projection completed",
                "terminal_status": "completed",
                "blocks": [{
                    "type": "text",
                    "itemId": "agent-message",
                    "content": "provider projection completed",
                    "source": "model_final",
                    "status": "completed",
                    "isStreaming": False,
                }],
            },
        ],
        context_snapshot={
            "ui_agent_state": {
                "plan": None,
                "todos": [],
                "subagents": [],
                "agentProgress": [{
                    "id": "provider:web-search",
                    "stage": "tool",
                    "status": "completed",
                    "message": "Web search completed",
                    "detail": "1 source",
                    "timestamp": 2,
                }],
            },
            "_ui_agent_state_revision": 1,
        },
    )
    session_id = "session_test_restore_completed_reconnect"

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?session_id={session_id}") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"
            ws.send_json({
                "type": "session.restore",
                "last_conversation_id": conversation.id,
            })
            first_restored = _receive_until_event(ws, "session.restored")
            first_switched = _receive_conversation_switched(ws, conversation.id)

        # Reconnect inside the manager grace period with the same renderer
        # session id.  This is the browser-refresh path, not a fresh session.
        with client.websocket_connect(f"/ws?session_id={session_id}") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"
            ws.send_json({
                "type": "session.restore",
                "last_conversation_id": conversation.id,
            })
            reconnected = _receive_until_event(ws, "session.restored")
            switched = _receive_conversation_switched(ws, conversation.id)

    assert first_restored["session"]["active_stream_conversation_ids"] == []
    assert first_switched["session"]["active_stream_conversation_ids"] == []
    assert reconnected["active_conversation_id"] == conversation.id
    assert [message["id"] for message in reconnected["messages"]] == [
        "user_restore_completed",
        "assistant_restore_completed",
    ]
    assert reconnected["messages"][-1]["terminal_status"] == "completed"
    assert reconnected["session"]["active_task_id"] is None
    assert reconnected["session"]["active_stream_conversation_ids"] == []
    assert switched["conversation"]["transcript"][-1]["content"] == "provider projection completed"
    assert switched["conversation"]["context_snapshot"]["ui_agent_state"]["agentProgress"] == [{
        "id": "provider:web-search",
        "stage": "tool",
        "status": "completed",
        "message": "Web search completed",
        "detail": "1 source",
        "timestamp": 2,
    }]


def test_websocket_session_restore_rebases_a_client_cursor_ahead_of_server_history(
    monkeypatch,
    tmp_path,
) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_cursor_rebase") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "session.restore", "last_seq": 42})
            restored = _receive_until_event(ws, "session.restored")

    assert restored["cursor_reset"] is True
    assert restored["requested_last_seq"] == 42
    assert restored["last_seq"] == restored["current_seq"] == 0
    assert restored["replayed_events"] == 0
    assert restored["missed_events"] is True
    assert restored["event_log_gap"] is True
    assert restored["snapshot_required"] is True


def test_websocket_conversation_list_keeps_blank_active_session_with_existing_history(monkeypatch, tmp_path) -> None:
    from backend.conversations.repository import ConversationRepository

    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    repo = ConversationRepository(base_dir=tmp_path)
    conversation = repo.create_conversation(
        conversation_id="conv_existing_recent",
        title="Existing chat",
        transcript=[{
            "id": "user_existing_recent",
            "role": "user",
            "content": "remember this existing chat",
        }],
    )

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_existing_list_restore") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "conversation.list"})
            listing = _receive_until_event(ws, "conversation.list")

    assert [item["id"] for item in listing["conversations"]] == [conversation.id]
    assert listing["active_conversation_id"] is None
    assert listing["active_conversation"] is None
    assert listing["session"]["active_conversation_id"] is None


def test_websocket_conversation_list_does_not_activate_archived_history(monkeypatch, tmp_path) -> None:
    from backend.conversations.repository import ConversationRepository

    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    repo = ConversationRepository(base_dir=tmp_path)
    conversation = repo.create_conversation(
        conversation_id="conv_archived_only",
        title="Archived only",
        transcript=[{
            "id": "assistant_archived_only",
            "role": "assistant",
            "content": "this should stay hidden",
        }],
    )
    repo.set_archived(conversation.id, True)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_archived_only_list") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "conversation.list"})
            listing = _receive_until_event(ws, "conversation.list")

    assert [item["id"] for item in listing["conversations"]] == [conversation.id]
    assert listing["conversations"][0]["archived"] is True
    assert listing["active_conversation_id"] is None
    assert listing["active_conversation"] is None
    assert listing["session"]["active_conversation_id"] is None


def test_websocket_archiving_last_active_conversation_returns_blank(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_archive_last_active") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "conversation.create", "memory_mode": "none"})
            created = _receive_until_event(ws, "conversation.list")
            conversation_id = created["active_conversation_id"]

            ws.send_json({"type": "conversation.archive", "conversation_id": conversation_id})
            listing = _receive_until_event(ws, "conversation.list")

    assert [item["id"] for item in listing["conversations"]] == [conversation_id]
    assert listing["conversations"][0]["archived"] is True
    assert listing["active_conversation_id"] is None
    assert listing["active_conversation"] is None
    assert listing["session"]["active_conversation_id"] is None


def test_blank_session_goal_command_does_not_create_conversation(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_blank_goal") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "conversation.goal.set", "text": "ship it"})
            result = _receive_until_event(ws, "command.result")
            assert result["command"] == "goal"
            assert result["level"] == "warning"

            ws.send_json({"type": "conversation.list"})
            listing = _receive_until_event(ws, "conversation.list")
            assert listing["conversations"] == []
            assert listing["active_conversation_id"] is None
            assert listing["active_conversation"] is None


def test_blank_session_permission_rules_do_not_create_conversation(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_blank_permission_rules") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json(
                {
                    "type": "conversation.permission.rules.add",
                    "rule_kind": "deny",
                    "pattern": "run_*",
                }
            )
            result = _receive_until_event(ws, "command.result")
            assert result["command"] == "permissions.rules.add"
            assert result["level"] == "warning"

            ws.send_json({"type": "conversation.list"})
            listing = _receive_until_event(ws, "conversation.list")
            assert listing["conversations"] == []
            assert listing["active_conversation_id"] is None
            assert listing["active_conversation"] is None


def test_websocket_restores_permission_mode_when_switching_conversation(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_permission_mode_switch") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "conversation.create", "memory_mode": "none"})
            initial = _receive_created_conversation(ws)
            assert initial["type"] == "conversation.list"
            original_id = initial["active_conversation_id"]
            assert initial["active_conversation"]["permission_mode"] == "confirm"

            ws.send_json({"type": "conversation.permission_mode.set", "mode": "plan"})
            mode_updated = False
            listing_after_mode = None
            for _ in range(12):
                payload = ws.receive_json()
                if payload.get("type") == "permission.mode.updated":
                    mode_updated = payload.get("mode") == "plan"
                elif payload.get("type") == "conversation.list":
                    listing_after_mode = payload
                if mode_updated and listing_after_mode is not None:
                    break

            assert mode_updated is True
            assert listing_after_mode is not None
            assert listing_after_mode["active_conversation"]["permission_mode"] == "plan"

            ws.send_json({"type": "conversation.create", "memory_mode": "none"})
            created = _receive_until_event(ws, "conversation.list")
            assert created["type"] == "conversation.list"
            second_id = created["active_conversation_id"]
            assert second_id != original_id
            assert created["active_conversation"]["permission_mode"] == "confirm"

            ws.send_json({"type": "conversation.switch", "conversation_id": original_id})
            switched = _receive_conversation_switched(ws, original_id)
            assert switched["type"] == "conversation.switched"
            assert switched["conversation"]["id"] == original_id
            assert switched["conversation"]["permission_mode"] == "plan"


def test_websocket_restores_permission_rules_when_switching_conversation(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_permission_rules_switch") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "conversation.create", "memory_mode": "none"})
            initial = _receive_created_conversation(ws)
            assert initial["type"] == "conversation.list"
            first_id = initial["active_conversation_id"]

            ws.send_json(
                {
                    "type": "conversation.permission.rules.add",
                    "rule_kind": "deny",
                    "pattern": "run_*",
                    "source": "test-suite",
                }
            )
            added_rules = _receive_next_non_task_update(ws)
            assert added_rules["type"] == "permission.rules.updated"
            assert added_rules["conversation_id"] == first_id
            assert any(item["pattern"] == "run_*" for item in added_rules["rules"]["session_deny"])
            permission_result = _receive_until_event(ws, "command.result")
            assert permission_result["command"] == "permissions.rules.add"

            ws.send_json({"type": "conversation.create", "memory_mode": "none"})
            created = _receive_until_event(ws, "conversation.list")
            assert created["type"] == "conversation.list"
            second_id = created["active_conversation_id"]
            assert second_id != first_id

            ws.send_json({"type": "conversation.permission.rules.list"})
            second_rules = _receive_permission_rules_updated(ws, second_id)
            assert second_rules["type"] == "permission.rules.updated"
            assert second_rules["conversation_id"] == second_id
            assert all(item["pattern"] != "run_*" for item in second_rules["rules"]["session_deny"])

            ws.send_json({"type": "conversation.switch", "conversation_id": first_id})
            switched = _receive_conversation_switched(ws, first_id)
            assert switched["type"] == "conversation.switched"
            assert switched["conversation"]["id"] == first_id

            ws.send_json({"type": "conversation.permission.rules.list"})
            restored_rules = _receive_permission_rules_updated(ws, first_id)
            assert restored_rules["type"] == "permission.rules.updated"
            assert restored_rules["conversation_id"] == first_id
            assert any(item["pattern"] == "run_*" for item in restored_rules["rules"]["session_deny"])


def test_agent_events_include_conversation_context(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", _fake_conversation_agent_loop)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_conversation_events") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"
            ws.send_json({"type": "conversation.create", "memory_mode": "none"})
            initial = _receive_created_conversation(ws)
            conversation_id = initial["active_conversation_id"]

            ws.send_json({"type": "user_message", "content": "hello"})

            compaction_updated = _receive_next_non_task_update(ws)
            compacted = _receive_next_non_task_update(ws)
            text = _receive_next_non_task_update(ws)
            done = _receive_next_non_task_update(ws)

    assert compaction_updated["type"] == "conversation.compaction.updated"
    assert compaction_updated["conversation_id"] == conversation_id
    assert compaction_updated["state"] == "compacted"
    assert compaction_updated["summary"] == "older turns summarized"

    assert compacted["type"] == "context_compacted"
    assert compacted["conversation_id"] == conversation_id
    assert compacted["summary"] == "older turns summarized"

    assert text["type"] == "item.completed"
    assert text["conversation_id"] == conversation_id
    assert text["item"]["text"] == "reply from active conversation"
    assert done["type"] == "done"
    assert done["conversation_id"] == conversation_id


def test_new_conversation_does_not_inherit_previous_turn_memory(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr(
        "backend.agent.query_engine.run_agent_loop",
        _fake_attachment_summary_agent_loop,
    )
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    image_attachment = {
        "id": "att_image_1",
        "kind": "image",
        "file_name": "ikun.png",
        "media_type": "image/png",
        "artifact_id": "art_image_1",
        "doc_id": "doc_image_1",
        "indexed_chunks": 0,
        "size_bytes": 2048,
        "title": "ikun",
        "summary": "Image attachment",
    }

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_summary_memory_image") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json(
                {
                    "type": "user_message",
                    "content": "帮我看看这张图是什么梗",
                    "attachments": [image_attachment],
                }
            )

            assert _receive_next_non_task_update(ws)["type"] == "item.completed"
            assert _receive_next_non_task_update(ws)["type"] == "done"
            summary_event = _receive_next_non_task_update(ws)
            assert summary_event["type"] == "conversation.summary.updated"
            assert "ikun.png" in summary_event["summary"]
            assert "蔡徐坤" in summary_event["summary"]

            ws.send_json({"type": "conversation.create", "memory_mode": "enabled"})
            created = _receive_created_conversation(ws)
            assert created["type"] == "conversation.list"
            inherited_notes = created["active_conversation"]["context_snapshot"].get("persistent_notes", [])

    assert inherited_notes == []


def test_new_conversations_never_chain_legacy_fact_inheritance(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr(
        "backend.agent.query_engine.run_agent_loop",
        _fake_nested_memory_agent_loop,
    )
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    image_attachment = {
        "id": "att_image_1",
        "kind": "image",
        "file_name": "ikun.png",
        "media_type": "image/png",
        "artifact_id": "art_image_1",
        "doc_id": "doc_image_1",
        "indexed_chunks": 0,
        "size_bytes": 2048,
        "title": "ikun",
        "summary": "Image attachment",
    }

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_nested_summary_memory") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "conversation.create", "memory_mode": "none"})
            initial = _receive_created_conversation(ws)
            root_conversation_id = initial["active_conversation_id"]

            ws.send_json(
                {
                    "type": "user_message",
                    "content": "帮我看看这张图是什么梗",
                    "attachments": [image_attachment],
                }
            )
            assert _receive_next_non_task_update(ws)["type"] == "item.completed"
            assert _receive_next_non_task_update(ws)["type"] == "done"
            assert _receive_next_non_task_update(ws)["type"] == "conversation.summary.updated"

            ws.send_json({"type": "conversation.create", "memory_mode": "enabled"})
            second_listing = _receive_created_conversation(ws)
            second_conversation = second_listing["active_conversation"]
            second_conversation_id = second_conversation["id"]

            ws.send_json({"type": "user_message", "content": "以后都直接给结论，不要铺垫"})
            assert _receive_next_non_task_update(ws)["type"] == "item.completed"
            assert _receive_next_non_task_update(ws)["type"] == "done"
            assert _receive_next_non_task_update(ws)["type"] == "conversation.summary.updated"

            ws.send_json({"type": "conversation.create", "memory_mode": "enabled"})
            third_listing = _receive_created_conversation(ws)
            third_conversation = third_listing["active_conversation"]

    assert "inherited_facts" not in third_conversation
    assert third_conversation["context_snapshot"].get("persistent_notes", []) == []


def test_regenerate_replaces_transcript_tail_instead_of_appending_duplicate_turns(monkeypatch, tmp_path) -> None:
    _fake_regenerate_agent_loop.state["calls"] = 0
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr(
        "backend.agent.query_engine.run_agent_loop",
        _fake_regenerate_agent_loop,
    )
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_regenerate_replace") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "user_message", "content": "hello"})
            assert _receive_next_non_task_update(ws)["type"] == "item.completed"
            assert _receive_next_non_task_update(ws)["type"] == "done"
            assert _receive_next_non_task_update(ws)["type"] == "conversation.summary.updated"

            ws.send_json({"type": "conversation.list"})
            first_listing = _receive_next_non_task_update(ws)
            transcript = first_listing["active_conversation"]["transcript"]
            user_message = next(message for message in transcript if message["role"] == "user")

            ws.send_json(
                {
                    "type": "user_message",
                    "content": "hello",
                    "retry_from_message_id": user_message["id"],
                }
            )
            assert _receive_next_non_task_update(ws)["type"] == "item.completed"
            assert _receive_next_non_task_update(ws)["type"] == "done"
            assert _receive_next_non_task_update(ws)["type"] == "conversation.summary.updated"

            ws.send_json({"type": "conversation.list"})
            second_listing = _receive_next_non_task_update(ws)

    final_transcript = second_listing["active_conversation"]["transcript"]
    assert len(final_transcript) == 2
    assert [message["role"] for message in final_transcript] == ["user", "assistant"]
    assert final_transcript[0]["content"] == "hello"
    assert final_transcript[1]["content"] == "regenerated reply"
    assert "regenerated reply" in second_listing["active_conversation"]["summary"]
    assert "first reply" not in second_listing["active_conversation"]["summary"]
    assert "local_facts" not in second_listing["active_conversation"]
    snapshot_history = second_listing["active_conversation"]["context_snapshot"].get("history", [])
    assert all("first reply" not in str(message.get("content", "")) for message in snapshot_history)


def test_regenerate_with_invalid_source_message_returns_error_without_mutating_transcript(
    monkeypatch, tmp_path
) -> None:
    _fake_regenerate_agent_loop.state["calls"] = 0
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr(
        "backend.agent.query_engine.run_agent_loop",
        _fake_regenerate_agent_loop,
    )
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_regenerate_invalid_source") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "user_message", "content": "hello"})
            assert _receive_next_non_task_update(ws)["type"] == "item.completed"
            assert _receive_next_non_task_update(ws)["type"] == "done"
            assert _receive_next_non_task_update(ws)["type"] == "conversation.summary.updated"

            ws.send_json(
                {
                    "type": "user_message",
                    "content": "hello",
                    "retry_from_message_id": "missing_user_message",
                }
            )
            error_event = _receive_next_non_task_update(ws)
            assert error_event["type"] == "error"
            # Terminal for this request: no run starts, so no `done` follows.
            # The client treats a recoverable error as non-terminal evidence and
            # keeps the optimistic streaming assistant it created before the
            # send alive, which left the conversation permanently "running" and
            # blocked every later send until a reload.
            assert error_event["recoverable"] is False
            assert "missing_user_message" in error_event["message"]

            ws.send_json({"type": "conversation.list"})
            listing = _receive_next_non_task_update(ws)

    final_transcript = listing["active_conversation"]["transcript"]
    assert [message["role"] for message in final_transcript] == ["user", "assistant"]
    assert final_transcript[1]["content"] == "first reply"


def test_conversation_transcript_persists_assistant_process_blocks(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr(
        "backend.agent.query_engine.run_agent_loop",
        _fake_process_blocks_agent_loop,
    )
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_process_blocks_restore") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "user_message", "content": "optimize this project"})
            assert _receive_next_non_task_update(ws)["type"] == "thinking_delta"
            assert _receive_next_non_task_update(ws)["type"] == "agent.progress"
            assert _receive_next_non_task_update(ws)["type"] == "tool_call"
            assert _receive_next_non_task_update(ws)["type"] == "tool_output_delta"
            assert _receive_next_non_task_update(ws)["type"] == "tool_result"
            assert _receive_next_non_task_update(ws)["type"] == "item.completed"
            assert _receive_next_non_task_update(ws)["type"] == "done"
            assert _receive_next_non_task_update(ws)["type"] == "conversation.summary.updated"

            ws.send_json({"type": "conversation.list"})
            listing = _receive_next_non_task_update(ws)

    transcript = listing["active_conversation"]["transcript"]
    assistant_message = next(message for message in transcript if message["role"] == "assistant")
    assert assistant_message["tool_calls"][0]["name"] == "recall"
    assert assistant_message["tool_calls"][0]["status"] == "success"
    blocks = assistant_message["blocks"]
    assert [block["type"] for block in blocks] == ["thinking", "progress", "tool_call", "text"]
    assert blocks[0]["content"] == "Reading request and workspace context"
    assert blocks[1]["summary"] == "Choosing the next step"
    assert blocks[2]["record"]["outputPreview"] == "matching previous edits\n"
    assert blocks[2]["record"]["summary"] == "Found previous optimization edits"
    assert blocks[3]["content"] == "I found the previous optimization edits and can continue."
    assert blocks[3]["source"] == "model_final"
    assert blocks[3]["status"] == "completed"
    assert blocks[3]["isStreaming"] is False


def test_conversation_transcript_persists_web_evidence_citations(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr(
        "backend.agent.query_engine.run_agent_loop",
        _fake_web_evidence_agent_loop,
    )
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_web_evidence_citation") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "user_message", "content": "check weather"})
            assert _receive_next_non_task_update(ws)["type"] == "tool_call"
            citation_event = _receive_next_non_task_update(ws)
            assert citation_event["type"] == "citation.add"
            assert citation_event["url"] == "https://www.example.test/weather"
            assert citation_event["label"] == "example.test"
            assert _receive_next_non_task_update(ws)["type"] == "tool_result"
            assert _receive_next_non_task_update(ws)["type"] == "item.completed"
            assert _receive_next_non_task_update(ws)["type"] == "done"
            assert _receive_next_non_task_update(ws)["type"] == "conversation.summary.updated"

            ws.send_json({"type": "conversation.list"})
            listing = _receive_next_non_task_update(ws)

    transcript = listing["active_conversation"]["transcript"]
    assistant_message = next(message for message in transcript if message["role"] == "assistant")
    assert assistant_message["citations"] == [
        {
            "source": "https://www.example.test/weather",
            "url": "https://www.example.test/weather",
            "label": "example.test",
            "title": "example.test",
            "range": [0, 0],
        }
    ]
    assert assistant_message["tool_calls"][0]["sourceUrl"] == "https://www.example.test/weather"
    assert assistant_message["tool_calls"][0]["evidenceType"] == "fetched"


def test_conversation_transcript_keeps_failed_tool_turn_structured(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr(
        "backend.agent.query_engine.run_agent_loop",
        _fake_failed_tool_only_agent_loop,
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _ConversationNoopLLM(),
    )
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_failed_tool_only") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "user_message", "content": "check weather"})
            assert _receive_next_non_task_update(ws)["type"] == "tool_call"
            assert _receive_next_non_task_update(ws)["type"] == "tool_result"
            missing_final = _receive_next_non_task_update(ws)
            assert missing_final["type"] == "error"
            assert missing_final["error_type"] == "missing_final_answer"
            done = _receive_next_non_task_update(ws)
            assert done["type"] == "done"
            assert done["status"] == "failed"

            ws.send_json({"type": "conversation.list"})
            listing = _receive_until_event(ws, "conversation.list")

    transcript = listing["active_conversation"]["transcript"]
    assistant_message = next(message for message in transcript if message["role"] == "assistant")
    assert assistant_message["content"] == ""
    assert assistant_message["tool_calls"][0]["status"] == "failed"
    assert all(block["type"] != "text" for block in assistant_message["blocks"])


def test_conversation_transcript_keeps_successful_tool_without_fabricated_reply(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr(
        "backend.agent.query_engine.run_agent_loop",
        _fake_successful_tool_without_final_reply_agent_loop,
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _ConversationNoopLLM(),
    )
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_successful_tool_empty_reply") as ws:
            _receive_until_event(ws, "mcp_status")
            _receive_until_event(ws, "llm.model.updated")

            ws.send_json({"type": "user_message", "content": "check today's news"})
            assert _receive_next_non_task_update(ws)["type"] == "tool_call"
            assert _receive_next_non_task_update(ws)["type"] == "tool_result"
            missing_final = _receive_next_non_task_update(ws)
            assert missing_final["type"] == "error"
            assert missing_final["error_type"] == "missing_final_answer"
            done = _receive_next_non_task_update(ws)
            assert done["type"] == "done"
            assert done["status"] == "partial"
            assert done["reason"] == "missing_final_answer"

            ws.send_json({"type": "conversation.list"})
            listing = _receive_until_event(ws, "conversation.list")

    transcript = listing["active_conversation"]["transcript"]
    assistant_message = next(message for message in transcript if message["role"] == "assistant")
    assert assistant_message["content"] == ""
    assert assistant_message["tool_calls"][0]["status"] == "success"
    assert all(block["type"] != "text" for block in assistant_message["blocks"])


def test_conversation_transcript_keeps_run_error_separate_from_answer(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr(
        "backend.agent.query_engine.run_agent_loop",
        _fake_tool_call_then_error_only_agent_loop,
    )
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_failed_tool_call_by_error") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "user_message", "content": "check weather"})
            assert _receive_next_non_task_update(ws)["type"] == "tool_call"
            assert _receive_next_non_task_update(ws)["type"] == "error"
            terminal_validation = _receive_next_non_task_update(ws)
            assert terminal_validation["type"] == "error"
            assert terminal_validation["error_code"] == "agent.missing_final_answer"
            done = _receive_next_non_task_update(ws)
            assert done["type"] == "done"
            assert done["status"] == "failed"

            ws.send_json({"type": "conversation.list"})
            listing = _receive_until_event(ws, "conversation.list")

    transcript = listing["active_conversation"]["transcript"]
    assistant_message = next(message for message in transcript if message["role"] == "assistant")
    assert assistant_message["content"] == ""
    assert assistant_message["tool_calls"][0]["status"] == "failed"
    assert all(block["type"] != "text" for block in assistant_message["blocks"])


def test_conversation_transcript_marks_process_blocks_failed_on_agent_error(monkeypatch, tmp_path) -> None:
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr(
        "backend.agent.query_engine.run_agent_loop",
        _fake_failed_process_agent_loop,
    )
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_failed_process_blocks") as ws:
            assert ws.receive_json()["type"] == "mcp_status"
            assert ws.receive_json()["type"] == "llm.model.updated"

            ws.send_json({"type": "user_message", "content": "break during planning"})
            assert _receive_next_non_task_update(ws)["type"] == "agent.progress"
            error = _receive_next_non_task_update(ws)
            assert error["type"] == "error"
            assert _receive_next_non_task_update(ws)["type"] == "done"

            ws.send_json({"type": "conversation.list"})
            listing = _receive_next_non_task_update(ws)

    transcript = listing["active_conversation"]["transcript"]
    assistant_message = next(message for message in transcript if message["role"] == "assistant")
    blocks = assistant_message["blocks"]
    assert blocks[0]["type"] == "progress"
    assert blocks[0]["status"] == "failed"


def test_context_builder_restores_inherited_summary_memory(tmp_path) -> None:
    builder = ContextBuilder()
    builder.load_snapshot(
        {
            "history": [],
            "compaction_count": 0,
            "persistent_notes": [
                {
                    "kind": "summary",
                    "title": "Inherited conversation memory",
                    "content": "User prefers concise progress updates and external MCP first.",
                }
            ],
        }
    )

    messages = asyncio.run(
        builder.build(
            "Continue the refactor",
            AgentState(user_message="Continue the refactor"),
        )
    )

    assert messages[0].role == "system"
    assert "Inherited conversation memory" in messages[0].content
    assert "external MCP first" in messages[0].content
    assert all(msg.role != "user" or "Inherited conversation memory" not in msg.content for msg in messages[1:])


def test_context_builder_injects_profile_memory_without_visible_transcript(tmp_path) -> None:
    memory = FileMemory(memory_dir=tmp_path / "memory")
    (memory.memory_dir / "user_profile.md").write_text(
        "# User profile\n\n- Likes concise Chinese responses\n- Prefers ChatGPT-like UI\n",
        encoding="utf-8",
    )

    builder = ContextBuilder(memory_manager=memory)
    builder.load_snapshot(
        {
            "history": [],
            "compaction_count": 0,
            "persistent_notes": [
                {
                    "kind": "profile",
                    "title": "Inherited user profile",
                    "content": memory.read_file("user_profile.md"),
                }
            ],
        }
    )

    messages = asyncio.run(
        builder.build(
            "Start a new session",
            AgentState(user_message="Start a new session"),
        )
    )

    assert messages[0].role == "system"
    assert "Inherited user profile" in messages[0].content
    assert "Likes concise Chinese responses" in messages[0].content
    assert len(messages) == 2
    assert messages[1].role == "user"
    assert messages[1].content.startswith("<system-reminder>")
    assert "<environment_context>" in messages[1].content
    assert messages[-1].content.endswith("Start a new session")


async def _admit_fake_turn(kwargs) -> None:
    context = kwargs["context_builder"]
    history_start = context.history_length
    context.append_user(kwargs.get("user_message", ""))
    commit = kwargs.get("metadata", {}).get("commit_turn_admission")
    if callable(commit):
        result = commit(
            boundary_input=SimpleNamespace(consumed_steer=None),
            history_start=history_start,
            history_end=context.history_length,
        )
        if inspect.isawaitable(result):
            await result


async def _fake_conversation_agent_loop(*args, **kwargs) -> AsyncIterator[AgentEvent]:
    await _admit_fake_turn(kwargs)
    yield AgentEvent.context_compacted("older turns summarized")
    yield AgentEvent.agent_message_completed("reply from active conversation", source="model_final")
    yield AgentEvent.done(input_tokens=8, output_tokens=13)


async def _fake_attachment_summary_agent_loop(*args, **kwargs) -> AsyncIterator[AgentEvent]:
    await _admit_fake_turn(kwargs)
    yield AgentEvent.agent_message_completed(
        "我先根据附件整理关键信息。\n"
        "结论：`ikun.png` 是蔡徐坤相关梗图，画面主体是蔡徐坤本人，常被当作表情包使用。",
        source="model_final",
    )
    yield AgentEvent.done(input_tokens=9, output_tokens=22)


async def _fake_nested_memory_agent_loop(*args, **kwargs) -> AsyncIterator[AgentEvent]:
    await _admit_fake_turn(kwargs)
    user_message = kwargs.get("user_message", "")
    if "直接给结论" in user_message:
        yield AgentEvent.agent_message_completed("收到，后续我会直接给结论，不再先铺垫。", source="model_final")
    else:
        yield AgentEvent.agent_message_completed(
            "结论：`ikun.png` 是蔡徐坤相关梗图，通常被当作表情包传播。",
            source="model_final",
        )
    yield AgentEvent.done(input_tokens=10, output_tokens=18)


async def _fake_regenerate_agent_loop(*args, **kwargs) -> AsyncIterator[AgentEvent]:
    await _admit_fake_turn(kwargs)
    state = _fake_regenerate_agent_loop.state
    reply = "first reply" if state["calls"] == 0 else "regenerated reply"
    state["calls"] += 1
    yield AgentEvent.agent_message_completed(reply, source="model_final")
    yield AgentEvent.done(input_tokens=5, output_tokens=8)


_fake_regenerate_agent_loop.state = {"calls": 0}


async def _fake_process_blocks_agent_loop(*args, **kwargs) -> AsyncIterator[AgentEvent]:
    await _admit_fake_turn(kwargs)
    yield AgentEvent.thinking_chunk("Reading request and workspace context")
    yield AgentEvent.progress(
        "Choosing the next step",
        stage="planning",
        status="running",
        id="plan-step",
        phase="planning",
        label="Thinking",
        summary="Choosing the next step",
        visibility="compact",
    )
    yield AgentEvent.tool_call("tool_recall_1", "recall", {"query": "previous optimization edits"})
    yield AgentEvent.tool_output_delta("tool_recall_1", "matching previous edits\n")
    yield AgentEvent.tool_result(
        "tool_recall_1",
        "Found previous optimization edits",
    )
    yield AgentEvent.agent_message_completed(
        "I found the previous optimization edits and can continue.",
        source="model_final",
    )
    yield AgentEvent.done(input_tokens=7, output_tokens=11)


async def _fake_web_evidence_agent_loop(*args, **kwargs) -> AsyncIterator[AgentEvent]:
    yield AgentEvent.tool_call(
        "tool_fetch_1",
        "web_fetch",
        {"url": "https://www.example.test/weather"},
    )
    yield AgentEvent.tool_result(
        "tool_fetch_1",
        "Fetched weather source",
        source_url="https://www.example.test/weather",
        extraction_status="ok",
        content_preview="Beijing 18C",
        evidence_type="fetched",
    )
    yield AgentEvent.agent_message_completed(
        "The fetched weather source says Beijing is 18C.",
        source="model_final",
    )
    yield AgentEvent.done(input_tokens=7, output_tokens=11)


async def _fake_failed_tool_only_agent_loop(*args, **kwargs) -> AsyncIterator[AgentEvent]:
    yield AgentEvent.tool_call(
        "tool_search_failed",
        "web_search",
        {"query": "Beijing weather"},
        display_hint="Searching",
    )
    yield AgentEvent.tool_result(
        "tool_search_failed",
        "network unavailable",
        is_error=True,
        status="failed",
    )
    yield AgentEvent.done(input_tokens=7, output_tokens=0)


async def _fake_successful_tool_without_final_reply_agent_loop(*args, **kwargs) -> AsyncIterator[AgentEvent]:
    yield AgentEvent.tool_call(
        "tool_search_success",
        "web_search",
        {"query": "today's news"},
        display_hint="Searching",
    )
    yield AgentEvent.tool_result(
        "tool_search_success",
        "Searched web and found several current headlines.",
        status="success",
        result_kind="search",
        display_summary="Searched web: today's news",
    )
    yield AgentEvent.done(input_tokens=7, output_tokens=0)


async def _fake_tool_call_then_error_only_agent_loop(*args, **kwargs) -> AsyncIterator[AgentEvent]:
    yield AgentEvent.tool_call(
        "tool_search_failed_by_run_error",
        "web_search",
        {"query": "Beijing weather"},
        display_hint="Searching",
    )
    yield AgentEvent.error("network unavailable", recoverable=True, error_type="tool_error")
    yield AgentEvent.done(input_tokens=7, output_tokens=0)


async def _fake_failed_process_agent_loop(*args, **kwargs) -> AsyncIterator[AgentEvent]:
    yield AgentEvent.progress(
        "Choosing the next step",
        stage="planning",
        status="running",
        id="plan-step",
        phase="planning",
        label="Thinking",
        summary="Choosing the next step",
        visibility="compact",
    )
    yield AgentEvent.error("Model request failed", recoverable=True, error_type="api")
    yield AgentEvent.done(
        input_tokens=1,
        output_tokens=0,
        status="failed",
        reason="api_error",
    )


def test_repository_rebuilds_structured_assistant_content_from_final_blocks(tmp_path):
    from backend.conversations.repository import ConversationRepository

    repo = ConversationRepository(base_dir=tmp_path)
    created = repo.create_conversation(conversation_id="conv_typed_answer")
    repo.append_transcript_message(created.id, {
        "id": "assistant-1",
        "role": "assistant",
        "content": "我先查一下。最终答案。",
        "blocks": [
            {
                "type": "text",
                "itemId": "commentary-1",
                "content": "我先查一下。",
                "source": "commentary",
                "status": "completed",
                "isStreaming": False,
            },
            {
                "type": "text",
                "itemId": "final-1",
                "content": "最终答案。",
                "source": "model_final",
                "status": "completed",
                "isStreaming": False,
            },
        ],
    })

    restored = ConversationRepository(base_dir=tmp_path).get_conversation(created.id)

    assert restored is not None
    assert restored.transcript[0]["content"] == "最终答案。"
    assert restored.transcript[0]["blocks"][0]["content"] == "我先查一下。"
