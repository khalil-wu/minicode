from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def provide_websocket_regression_model(monkeypatch: pytest.MonkeyPatch, request) -> None:
    """Give hand-built websocket sessions the same explicit model on CI."""

    if request.path.name in {
        "test_regressions_websocket.py",
        "test_smoke_api.py",
        "test_smoke_websocket.py",
    }:
        monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
        monkeypatch.setenv("OPENAI_AVAILABLE_MODELS", "gpt-5.4")


@pytest.fixture(autouse=True)
def isolate_runtime_data_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Keep websocket/API tests from writing into the desktop user's data dir."""
    # Checkpoints and background-task records resolve through
    # MINICODE_STATE_ROOT, so without this they land in the real ~/.minicode and
    # one run can read another run's leftovers.
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    conversations = tmp_path / "conversations"
    attachments = tmp_path / "attachments"
    artifacts = tmp_path / "artifacts"
    checkpoints = tmp_path / "checkpoints"
    settings_file = tmp_path / "settings.json"
    vault_file = tmp_path / "vault.json"

    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CUSTOM_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_BASE_URL",
        "CUSTOM_BASE_URL",
        "CUSTOM_MODEL",
        "CUSTOM_WIRE_API",
        "CUSTOM_REASONING_EFFORT",
        "CUSTOM_SMALL_FAST_MODEL",
        "CUSTOM_RESPONSES_REASONING_SUMMARY",
        "CUSTOM_PROMPT_CACHE_RETENTION",
        "CUSTOM_MAX_TOKENS",
        "CUSTOM_THINKING_BUDGET",
        "OPENAI_PROMPT_CACHE_RETENTION",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", settings_file, raising=False)
    monkeypatch.setattr("backend.vault.store.VAULT_FILE", vault_file, raising=False)
    monkeypatch.setattr("backend.conversations.repository.CONVERSATION_DATA_DIR", conversations, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversations, raising=False)
    monkeypatch.setattr("backend.attachments.store.ATTACHMENT_DATA_DIR", attachments, raising=False)
    monkeypatch.setattr("backend.artifact.store.ARTIFACT_DATA_DIR", artifacts, raising=False)
    monkeypatch.setattr("backend.checkpoint.store.CHECKPOINT_DATA_DIR", checkpoints, raising=False)
    runtime_root = tmp_path / "state" / "data" / "agent-runtime"
    monkeypatch.setattr(
        "backend.agent.runtime.METRICS_FILE",
        runtime_root / "metrics" / "agent_metrics.jsonl",
    )
    monkeypatch.setattr(
        "backend.agent.runtime.SWARM_DIR",
        runtime_root / "swarm",
    )
    monkeypatch.setattr(
        "backend.workspace.state.WORKSPACE_STATE_FILE",
        tmp_path / "active_workspace.json",
        raising=False,
    )
    # API/WebSocket tests exercise the application shell, not a developer's
    # configured provider. Tests covering model construction replace this.
    monkeypatch.setattr(
        "backend.main._create_session_llm",
        lambda config, model_override=None, **_kwargs: object(),
    )
    # Agent runs resolve an adapter again from the run-scoped provider/model
    # snapshot.  Keep the test composition root aligned with that production
    # seam as well; patching only the websocket's initial adapter would leave
    # the first user turn trying to construct a real provider without keys.
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: object(),
    )

    from backend.workspace.state import clear_active_workspace_root

    clear_active_workspace_root()
    yield

    clear_active_workspace_root()
    # Session isolation is a guarantee, not a best effort: a stale import here
    # silently leaked WebSocket session state across this whole suite. A test
    # that substitutes the manager entirely has nothing real to reset, so only
    # the genuine singleton is required to support the reset.
    from backend.api._state import ws_manager as _ws_manager
    from backend.ws.manager import WebSocketManager

    if isinstance(_ws_manager, WebSocketManager):
        _ws_manager.reset_for_tests()
