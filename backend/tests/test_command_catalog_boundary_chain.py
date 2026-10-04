from unittest.mock import AsyncMock

import pytest

from backend.commands.catalog import _parse_file_command
from backend.commands.slash_commands import _substitute_command_arguments
from backend.feature_flags import load_feature_flags
from backend.tests.test_user_input_presentation_chain import session_for
from backend.agent.message import UserCommand


@pytest.mark.asyncio
async def test_ws_command_uses_its_workspace_and_replay_retains_accepted_template(tmp_path, monkeypatch):
    a, b = tmp_path / "workspace-a", tmp_path / "workspace-b"
    for root, body in ((a, "A owned prompt"), (b, "B foreign prompt")):
        directory = root / ".minicode" / "commands"
        directory.mkdir(parents=True)
        (root / ".git").mkdir()
        (directory / "owned.md").write_text(body, encoding="utf-8")
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("backend.commands.catalog._get_managed_minicode_dir", lambda: tmp_path / "managed")
    monkeypatch.setattr("backend.commands.catalog.get_explicit_active_workspace_root", lambda: b)
    session, conversation, _ = session_for(tmp_path, monkeypatch)
    session.conversation_repo.update_workspace_binding(conversation.id, workspace_root=str(a))
    monkeypatch.setattr(session, "_ensure_extension_commands_for_conversation", AsyncMock(return_value=None))
    start = AsyncMock()
    monkeypatch.setattr(session, "start_agent_run", start)
    packet = {"content": "/owned", "display_content": "/owned", "conversation_id": conversation.id,
        "client_command_id": "source-command", "user_message_id": "source-user", "assistant_message_id": "source-assistant"}
    try:
        await session.command_dispatcher._handle_command_inner(UserCommand("user_message", dict(packet)))
        assert start.await_args.args[0] == "A owned prompt"
        metadata = start.await_args.kwargs["metadata"]
        assert metadata["submitted_content"] == "/owned"
        session.conversation_repo.commit_turn_admission(conversation.id,
            user_message={"id": "source-user", "role": "user", "content": "A owned prompt",
                "display_content": "/owned", "submitted_content": "/owned", "attachments": []},
            context_snapshot={"turn_admissions": {"source-user": {"client_command_id": "source-command"}}})
        (a / ".minicode" / "commands" / "owned.md").write_text("A changed after admission", encoding="utf-8")
        await session.command_dispatcher._handle_command_inner(UserCommand("user_message", dict(packet)))
        assert start.await_args.args[0] == "A owned prompt"
        assert start.await_args.kwargs["metadata"]["_turn_admission_restored"] is True
    finally:
        await session.session_lifecycle.shutdown(reason="catalog_audit")


@pytest.mark.asyncio
async def test_command_preparation_keeps_context_prefix_and_original_display(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    directory = root / ".minicode" / "commands"
    directory.mkdir(parents=True)
    (root / ".git").mkdir()
    (directory / "owned.md").write_text("Prepared $ARGUMENTS", encoding="utf-8")
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("backend.commands.catalog._get_managed_minicode_dir", lambda: tmp_path / "managed")
    session, conversation, _ = session_for(tmp_path, monkeypatch)
    session.conversation_repo.update_workspace_binding(conversation.id, workspace_root=str(root))
    monkeypatch.setattr(session, "_ensure_extension_commands_for_conversation", AsyncMock(return_value=None))
    start = AsyncMock()
    monkeypatch.setattr(session, "start_agent_run", start)
    prefix = "<context>file reference</context>\n\nQuoted message (assistant): fixture\n\n"
    command_line = "/owned literal"
    try:
        await session.command_dispatcher._handle_command_inner(UserCommand("user_message", {
            "conversation_id": conversation.id, "content": prefix + command_line,
            "display_content": command_line, "user_message_id": "prefix-user", "assistant_message_id": "prefix-assistant"}))
        assert start.await_args.args[0] == prefix + "Prepared literal"
        assert start.await_args.kwargs["metadata"]["display_content"] == command_line
        assert start.await_args.kwargs["metadata"]["submitted_content"] == prefix + command_line
    finally:
        await session.session_lifecycle.shutdown(reason="catalog_audit")


@pytest.mark.parametrize("body", ["---\ndescription: [unfinished\nbody", "---\n- list\n---\nbody", "---\n42\n---\nbody"])
def test_invalid_command_frontmatter_is_not_an_executable_prompt(tmp_path, body):
    path = tmp_path / "command.md"
    path.write_text(body, encoding="utf-8")
    assert _parse_file_command(path, "project") is None


def test_argument_text_is_literal_in_the_single_template_pass():
    assert _substitute_command_arguments("value=$name; other=$ARGUMENTS[1]", '"$ARGUMENTS[1]" second',
        argument_names=["name"]) == "value=$ARGUMENTS[1]; other=second"
    assert _substitute_command_arguments("first=$0 all=$ARGUMENTS", '"$ARGUMENTS"') == 'first=$ARGUMENTS all="$ARGUMENTS"'


@pytest.mark.parametrize("source,error", [("settings", OSError), ("requirements", ValueError)])
def test_unreadable_feature_policy_cannot_enable_default_capabilities(monkeypatch, source, error):
    if source == "settings":
        monkeypatch.setattr("backend.config._load_effective_settings_json", lambda: (_ for _ in ()).throw(error("unreadable")))
        kwargs = {"managed_requirements": {}}
    else:
        monkeypatch.setattr("backend.config.get_config_requirements", lambda: (_ for _ in ()).throw(error("unreadable")))
        kwargs = {"settings_data": {}}
    with pytest.raises(error, match="unreadable"):
        load_feature_flags(**kwargs)
