from backend.services.workspace_service import (
    parse_user_message_workspace_request,
    parse_workspace_activation_request,
)


def test_workspace_activation_requires_desktop_trust_ledger(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _path: False)

    activation = parse_workspace_activation_request(str(tmp_path))
    user_message = parse_user_message_workspace_request(
        str(tmp_path),
        conversation_id="conversation-1",
    )

    assert activation.error_event is not None
    assert activation.error_event.data["error_code"] == "workspace_untrusted"
    assert user_message.error_event is not None
    assert user_message.error_event.data["error_code"] == "workspace_untrusted"
    assert user_message.error_event.data["conversation_id"] == "conversation-1"
