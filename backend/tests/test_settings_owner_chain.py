from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.config import AppConfig, LLMSettings
from backend.conversations.repository import ConversationRepository
from backend.ws.command_handlers import SessionCommandHandlersMixin
from backend.ws.handlers.misc import handle_llm_config_set


@pytest.mark.asyncio
@pytest.mark.parametrize("active_owner", ["conv-originA", "conv-targetB"])
async def test_provider_profile_applies_only_to_its_original_conversation(tmp_path, monkeypatch, active_owner):
    repository = ConversationRepository(tmp_path / "conversations")
    origin = repository.create_conversation(conversation_id="conv-originA", workspace_root=str(tmp_path), model_selection={"provider": "openai", "model": "old-A"})
    other = repository.create_conversation(conversation_id="conv-targetB", workspace_root=str(tmp_path), model_selection={"provider": "custom", "model": "pinned-B"})
    config = AppConfig(llm=LLMSettings(api_key=""))
    monkeypatch.setattr("backend.config.load_config", lambda **_kwargs: config)
    monkeypatch.setattr("backend.ws.agent_runner._config_with_runtime_model_budget", lambda config, **_kwargs: config)
    monkeypatch.setattr("backend.ws.agent_runner._get_or_create_session_llm", lambda *_args, **_kwargs: SimpleNamespace(supported_reasoning_efforts=lambda: ("off",)))
    monkeypatch.setattr("backend.ws.agent_runner._apply_thinking_level", lambda *_args: "off")
    session = SessionCommandHandlersMixin()
    session.ws_manager = None
    session.conversation_repo = repository
    session.active_conversation_id = active_owner
    session.provider = "custom"
    session.selected_model = "pinned-B"
    session._resolve_llm_provider = lambda *_args: "openai"
    session._resolve_available_models = lambda *_args: ["new-A"]
    session._resolve_models_source = lambda *_args: "saved"
    session._model_runtime_for_conversation = lambda _owner: None
    session.context_builder = SimpleNamespace(bind_llm=Mock(), bind_budget=Mock())
    session.session_lifecycle = SimpleNamespace(workspace_root_for_conversation=lambda _conversation: tmp_path, send_runtime_capabilities=AsyncMock())
    session.publish_live_model_execution = Mock()
    session.send_llm_state = AsyncMock()
    session.emit_command_result = AsyncMock()
    session.send_event = AsyncMock()
    await handle_llm_config_set(session, {
        "source": "settings.provider.save", "conversation_id": origin.id,
        "workspace_root": str(tmp_path), "provider": "openai", "model": "new-A",
    })
    assert repository.get_conversation(origin.id).model_selection["model"] == "new-A"
    assert repository.get_conversation(other.id).model_selection["model"] == "pinned-B"
    if active_owner == origin.id:
        assert session.selected_model == "new-A"
        session.send_llm_state.assert_awaited_once()
    else:
        assert session.provider == "custom"
        assert session.selected_model == "pinned-B"
        session.context_builder.bind_llm.assert_not_called()
        session.send_llm_state.assert_not_awaited()
    assert session.emit_command_result.await_args.kwargs["level"] == "success"


def test_side_chat_retry_reuses_committed_id_without_overwriting_content(tmp_path):
    repository = ConversationRepository(tmp_path)
    original = repository.create_conversation(
        conversation_id="side-retry01", conversation_type="side_chat", workspace_root="workspace-A",
        transcript=[{"role": "user", "content": "keep this text"}], context_snapshot={"keep": "context"}, reuse_existing=True,
    )
    retried = repository.create_conversation(
        conversation_id=original.id, conversation_type="side_chat", workspace_root="workspace-A", reuse_existing=True,
    )
    assert retried.id == original.id
    assert retried.transcript == original.transcript
    assert retried.context_snapshot == original.context_snapshot
    assert retried.revision == original.revision


def test_side_chat_retry_creates_uncommitted_id_but_never_reuses_deleted_id(tmp_path):
    repository = ConversationRepository(tmp_path)
    created = repository.create_conversation(conversation_id="side-retry02", conversation_type="side_chat", reuse_existing=True)
    assert created.id == "side-retry02"
    repository.delete_conversation(created.id)
    with pytest.raises(ValueError, match="deleted or unavailable"):
        repository.create_conversation(conversation_id=created.id, conversation_type="side_chat", reuse_existing=True)


def test_side_chat_retry_cannot_claim_another_workspace_or_main_conversation(tmp_path):
    repository = ConversationRepository(tmp_path)
    repository.create_conversation(conversation_id="side-retry03", workspace_root="workspace-A")
    with pytest.raises(ValueError, match="does not match"):
        repository.create_conversation(conversation_id="side-retry03", conversation_type="side_chat", workspace_root="workspace-A", reuse_existing=True)
