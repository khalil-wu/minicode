"""Review creation uses real independent conversations and the original executor."""
from unittest.mock import Mock

import pytest

from backend.agent.message import UserCommand
from backend.services.git_settings_service import GitPreferences, update_git_preferences
from backend.services.review_delivery_service import queue_detached_review
from backend.services.workspace_api_service import capture_workspace_execution
from backend.tests.test_executor_editor_api import editor_server
from backend.tests.test_executor_tool_admission import executor_host
from backend.workspace.executor_editor import ExecutorEditorService
from backend.ws.handlers.code_review import handle_code_review


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_review_delivery_retains_source_and_original_executor_through_separate_input(editor_server, monkeypatch):
    server = editor_server
    session = server.session
    source_id = session.active_conversation_id
    async with capture_workspace_execution(session, source_id) as context:
        original_environment = context.turn_environment.environment
        async with ExecutorEditorService(context, application_config=session.config) as service:
            async def git(*args):
                result = await service.run(["git", *args])
                assert result.returncode == 0, result.stderr

            await git("init", "-b", "main")
            await service.write_bytes("tracked.py", b"value = 1\n")
            await git("add", "tracked.py")
            await git("-c", "user.name=Review Owner", "-c", "user.email=review@example.invalid", "commit", "-m", "Baseline")
            await service.write_bytes("tracked.py", b"value = 2\n")
            await service.write_bytes("unsaved-local.txt", b"original user bytes\n")
    source = session.conversation_repo.get_conversation(source_id)
    source.model_selection = {"provider": "custom", "model": "original-model", "reasoning_effort": "low"}
    source.permission_deny_rules = ["edit_file"]
    source.permission_overrides = {"write_file": "deny"}
    source.agent_mode = "plan"
    session.conversation_repo.save_conversation(source)
    session.conversation_repo.append_transcript_message(source_id, {"id": "original-user", "role": "user", "content": "Keep original history"})
    source_before = session.conversation_repo.get_conversation(source_id).to_dict()
    prepared = None
    for delivery in ("inline", "detached"):
        update_git_preferences(GitPreferences(review_delivery=delivery))
        with (session.event_outbox.bind_client_command("review-" + delivery, "review.create"),
              session.event_outbox.capture_command_results(include_data=True) as results):
            await handle_code_review(session, {"conversation_id": source_id,
                "execution_cwd": str(server.view), "mode": "uncommitted", "request_message": "Only check discount correctness"}, command="review.create")
        result = results.results[-1]
        assert result["level"] == "success", result
        prepared = result["data"]
        assert prepared["delivery"] == delivery and prepared["sourceConversationId"] == source_id
        assert prepared["prompt"].startswith("## Code review guidelines:\n# Review Guidelines")
        assert "+value = 2" in prepared["diff"]
        assert session.active_conversation_id == source_id
        assert session.conversation_repo.get_conversation(source_id).to_dict() == source_before
        if delivery == "inline":
            assert prepared["reviewThreadId"] == source_id
            continue
        review_id = prepared["reviewThreadId"]
        assert review_id != source_id
        review = session.conversation_repo.get_conversation(review_id)
        assert review.parent_conversation_id == source_id and review.branch_kind == "review"
        assert not review.git_isolated and not review.worktree_path
        assert review.execution_cwd == prepared["scope"]["cwd"]
        assert review.permission_mode == source.permission_mode
        assert review.permission_deny_rules == source.permission_deny_rules
        assert review.permission_overrides == source.permission_overrides
        assert review.model_selection == source.model_selection
        assert not review.transcript and not review.context_snapshot
        async with capture_workspace_execution(session, review_id) as review_context:
            assert review_context.turn_environment.environment is original_environment
            async with ExecutorEditorService(review_context, application_config=session.config) as service:
                assert await service.read_bytes(service.cwd.joinpath("unsaved-local.txt")) == b"original user bytes\n"
    queue = session.run_manager.durable_client_commands
    original = UserCommand("user_message", {"client_command_id": "native-review", "conversation_id": source_id, "content": "/review"})
    assert queue.persist_client_command(original)
    assert queue.claim_client_command("native-review") is not None
    scheduled = Mock()
    monkeypatch.setattr(session.command_dispatcher, "_schedule_durable_client_command", scheduled)
    with session.event_outbox.bind_client_command("native-review", "user_message"):
        review_id = await queue_detached_review(session, source_id, prepared["scope"],
            prompt="Original input context\n" + prepared["prompt"], request_message=prepared["requestMessage"], input_id="native-review")
    queued = [item for item in queue.pending_client_commands() if item.type == "user_message" and item.data["conversation_id"] == review_id]
    assert len(queued) == 1 and review_id != source_id
    assert queued[0].data["content"] == "Original input context\n" + prepared["prompt"]
    assert queued[0].data["display_content"] == "Only check discount correctness"
    assert queued[0].data["code_review"] == prepared["scope"]
    with pytest.raises(ValueError, match="cannot change conversation owner"):
        queue.bind_client_command_owner("native-review", review_id)
    assert session.active_conversation_id == source_id
    assert session.conversation_repo.get_conversation(source_id).to_dict() == source_before
    scheduled.assert_called_once()
    assert queue.complete_client_command("native-review")
