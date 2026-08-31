import asyncio
import json
from pathlib import Path
from typing import Any

import backend.commands.catalog as command_catalog
from backend.commands.catalog import (
    get_composer_command_catalog,
    get_file_command_catalog,
)
from backend.commands.registry import CommandRegistry
from backend.commands.slash_commands import (
    SKILL_DIR_TOKEN,
    _build_template_handler,
    register_all_slash_commands,
)


class _FakeSession:
    def __init__(self) -> None:
        self.command_registry = CommandRegistry()
        self.active_conversation_id = "conv_test"
        self.command_results: list[dict[str, Any]] = []

    async def emit_command_result(
        self,
        command: str,
        message: str,
        *,
        level: str = "info",
        title: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "command": command,
            "message": message,
            "level": level,
        }
        if title is not None:
            payload["title"] = title
        if data is not None:
            payload["data"] = data
        self.command_results.append(payload)

    def _ensure_active_conversation(self) -> None:
        if not self.active_conversation_id:
            self.active_conversation_id = "conv_test"


def _register_recorder(
    registry: CommandRegistry,
    command_name: str,
    calls: list[dict[str, Any]],
) -> None:
    async def _handler(payload: dict[str, Any]) -> bool:
        calls.append(dict(payload))
        return True

    registry.register(command_name, _handler)


def _dispatch_slash(
    session: _FakeSession,
    slash: str,
    arg: str = "",
    attachments: list[Any] | None = None,
) -> tuple[bool, str]:
    return asyncio.run(
        session.command_registry.dispatch_slash(
            session,
            slash,
            arg,
            [] if attachments is None else attachments,
        )
    )


def test_catalog_commands_are_registered_as_slash_commands() -> None:
    registry = CommandRegistry()
    register_all_slash_commands(registry)

    expected = {
        f"/{str(entry.get('command', '')).strip().lower()}"
        for entry in get_composer_command_catalog()
        if bool(entry.get("enabled", True)) and str(entry.get("command", "")).strip()
    }
    assert expected
    for command_name in expected:
        assert registry.dispatch_slash_sync(command_name)


def test_template_slash_expands_prompt_with_extra_context() -> None:
    session = _FakeSession()
    register_all_slash_commands(session.command_registry)

    handled, next_content = _dispatch_slash(
        session,
        "/review",
        "focus websocket runtime state",
    )

    assert handled is False
    review_template = next(
        str(entry.get("template", ""))
        for entry in get_composer_command_catalog()
        if str(entry.get("command", "")).strip().lower() == "review"
    )
    assert next_content.startswith(review_template)
    assert "ARGUMENTS: focus websocket runtime state" in next_content
    assert session.command_results[-1]["command"] == "review"


def test_skill_dir_token_expands_without_warning() -> None:
    session = _FakeSession()
    handler = _build_template_handler(
        "demo",
        f"Read {SKILL_DIR_TOKEN}/reference.md",
        base_dir=r"C:\skills\demo",
        is_skill_file=True,
    )

    handled, next_content = asyncio.run(handler(session, "", []))

    assert handled is False
    assert "Read C:/skills/demo/reference.md" in next_content
    assert [result["level"] for result in session.command_results] == ["info"]


def test_plan_is_registered_but_bypass_is_not_a_slash_command() -> None:
    registry = CommandRegistry()
    register_all_slash_commands(registry)

    assert registry.dispatch_slash_sync("/plan")
    assert not registry.dispatch_slash_sync("/bypass")


def test_plan_is_enabled_but_bypass_is_not_a_composer_command() -> None:
    enabled_commands = {
        str(entry.get("command", "")).strip().lower()
        for entry in get_composer_command_catalog()
        if bool(entry.get("enabled", True))
    }

    assert "plan" in enabled_commands
    assert "bypass" not in enabled_commands


def test_effort_dispatches_reasoning_effort_config_command(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({
            "llm": {
                "provider": "openai",
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-5",
                    "wire_api": "responses",
                },
            }
        }),
        encoding="utf-8",
    )
    session = _FakeSession()
    calls: list[dict[str, Any]] = []
    _register_recorder(session.command_registry, "llm.config.set", calls)
    register_all_slash_commands(session.command_registry)

    handled, next_content = _dispatch_slash(session, "/effort", "max")

    assert handled is True
    assert next_content == ""
    assert calls == [{"reasoning_effort": "max", "source": "slash:/effort"}]
    assert session.command_results[-1]["data"] == {
        "reasoning_effort": "max",
        "applied": False,
    }


def test_effort_warns_without_success_for_chat_provider(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({
            "llm": {
                "provider": "custom",
                "custom": {
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-flash",
                    "wire_api": "chat",
                },
            }
        }),
        encoding="utf-8",
    )
    session = _FakeSession()
    calls: list[dict[str, Any]] = []
    _register_recorder(session.command_registry, "llm.config.set", calls)
    register_all_slash_commands(session.command_registry)

    handled, next_content = _dispatch_slash(session, "/effort", "max")

    assert handled is True
    assert next_content == ""
    assert calls == [{"reasoning_effort": "max", "source": "slash:/effort"}]
    assert session.command_results[-1]["level"] == "warning"
    assert session.command_results[-1]["data"] == {"reasoning_effort": "max", "applied": False}
    assert "not applied" in session.command_results[-1]["message"]


def test_goal_dispatches_authoritative_conversation_goal_command() -> None:
    session = _FakeSession()
    calls: list[dict[str, Any]] = []
    _register_recorder(session.command_registry, "conversation.goal.set", calls)
    register_all_slash_commands(session.command_registry)

    handled, next_content = _dispatch_slash(session, "/goal", "对标 Codex 桌面端")

    assert handled is True
    assert next_content == ""
    assert calls == [
        {
            "conversation_id": "conv_test",
            "action": "set",
            "text": "对标 Codex 桌面端",
            "source": "slash:/goal",
        }
    ]


def test_goal_pause_dispatches_goal_pause_action() -> None:
    session = _FakeSession()
    calls: list[dict[str, Any]] = []
    _register_recorder(session.command_registry, "conversation.goal.set", calls)
    register_all_slash_commands(session.command_registry)

    handled, next_content = _dispatch_slash(session, "/goal", "pause")

    assert handled is True
    assert next_content == ""
    assert calls == [
        {
            "conversation_id": "conv_test",
            "action": "pause",
            "text": "",
            "source": "slash:/goal",
        }
    ]


def test_permissions_rules_add_override_dispatches_authoritative_command() -> None:
    session = _FakeSession()
    calls: list[dict[str, Any]] = []
    _register_recorder(session.command_registry, "conversation.permission.rules.add", calls)
    register_all_slash_commands(session.command_registry)

    handled, next_content = _dispatch_slash(
        session,
        "/permissions",
        "rules add override write_file diff_review",
    )

    assert handled is True
    assert next_content == ""
    assert calls == [
        {
            "rule_kind": "override",
            "pattern": "write_file",
            "level": "diff",
            "conversation_id": "conv_test",
            "source": "slash:/permissions",
        }
    ]


def test_permissions_rules_add_override_invalid_usage_returns_warning() -> None:
    session = _FakeSession()
    register_all_slash_commands(session.command_registry)

    handled, next_content = _dispatch_slash(
        session,
        "/permissions",
        "rules add override write_file",
    )

    assert handled is True
    assert next_content == ""
    warning = session.command_results[-1]
    assert warning["command"] == "permissions"
    assert warning["level"] == "warning"
    assert "Usage:" in warning["message"]


def test_permissions_auto_alias_matches_frontend_auto_mode() -> None:
    session = _FakeSession()
    calls: list[dict[str, Any]] = []
    _register_recorder(session.command_registry, "conversation.permission_mode.set", calls)
    register_all_slash_commands(session.command_registry)

    handled, next_content = _dispatch_slash(session, "/permissions", "auto")

    assert handled is True
    assert next_content == ""
    assert calls == [{"mode": "auto", "source": "slash:/permissions"}]


def test_skills_without_arg_requests_skill_lists_and_opens_marketplace() -> None:
    session = _FakeSession()
    calls: list[dict[str, Any]] = []
    _register_recorder(session.command_registry, "skills.list", calls)
    _register_recorder(session.command_registry, "skills.marketplace.list", calls)
    register_all_slash_commands(session.command_registry)

    handled, next_content = _dispatch_slash(session, "/skills")

    assert handled is True
    assert next_content == ""
    assert calls == [
        {"source": "slash:/skills"},
        {"source": "slash:/skills"},
    ]
    result = session.command_results[-1]
    assert result["command"] == "skills"
    assert result["data"] == {"ui_action": "open_skills_marketplace"}


def test_skills_with_arg_returns_explicit_skill_activation_prompt() -> None:
    session = _FakeSession()
    register_all_slash_commands(session.command_registry)

    handled, next_content = _dispatch_slash(session, "/skills", "user-workflow")

    # Codex-style skills use explicit activation in the next model turn; the
    # slash command does not execute a second local skill runtime.
    assert handled is False
    assert next_content == "$user-workflow"
    assert session.command_results == []


def test_usage_dispatches_session_usage_inspect() -> None:
    session = _FakeSession()
    calls: list[dict[str, Any]] = []
    _register_recorder(session.command_registry, "session.usage.inspect", calls)
    register_all_slash_commands(session.command_registry)

    handled, next_content = _dispatch_slash(session, "/usage")

    assert handled is True
    assert next_content == ""
    assert calls == [{"source": "slash:/usage"}]


def _write_file_command(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(
        f"---\ndescription: {body}\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_file_command_precedence_is_managed_then_user_then_closest_project(
    tmp_path: Path,
    monkeypatch,
) -> None:
    managed_root = tmp_path / "managed"
    user_root = tmp_path / "user-minicode"
    project = tmp_path / "project"
    nested = project / "packages" / "app"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(user_root))
    monkeypatch.setattr(
        command_catalog,
        "_get_managed_minicode_dir",
        lambda: managed_root,
    )
    managed = _write_file_command(
        managed_root / "commands",
        "audit",
        "managed command",
    )
    user = _write_file_command(user_root / "commands", "audit", "user command")
    closest = _write_file_command(
        nested / ".minicode" / "commands",
        "audit",
        "closest project command",
    )
    _write_file_command(
        project / ".minicode" / "commands",
        "audit",
        "root project command",
    )

    assert get_file_command_catalog(nested)[0]["source_path"] == str(managed)
    managed.unlink()
    assert get_file_command_catalog(nested)[0]["source_path"] == str(user)
    user.unlink()
    assert get_file_command_catalog(nested)[0]["source_path"] == str(closest)


def test_file_commands_deduplicate_hard_links_across_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    managed_root = tmp_path / "managed"
    user_root = tmp_path / "user-minicode"
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(user_root))
    monkeypatch.setattr(
        command_catalog,
        "_get_managed_minicode_dir",
        lambda: managed_root,
    )
    original = _write_file_command(
        managed_root / "commands",
        "managed-name",
        "same physical file",
    )
    user_commands = user_root / "commands"
    user_commands.mkdir(parents=True)
    try:
        (user_commands / "user-name.md").hardlink_to(original)
    except OSError:
        import pytest

        pytest.skip("hard links are unavailable in this environment")

    commands = get_file_command_catalog(None)

    assert [entry["name"] for entry in commands] == ["managed-name"]


def test_file_commands_fall_back_to_main_repo_from_worktree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    worktree_git_dir = main / ".git" / "worktrees" / "feature"
    worktree_git_dir.mkdir(parents=True)
    worktree.mkdir()
    (worktree / ".git").write_text(
        f"gitdir: {worktree_git_dir}\n",
        encoding="utf-8",
    )
    (worktree_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree_git_dir / "gitdir").write_text(
        str(worktree / ".git") + "\n",
        encoding="utf-8",
    )
    command = _write_file_command(
        main / ".minicode" / "commands",
        "fallback",
        "main repo fallback",
    )
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "user-minicode"))
    monkeypatch.setattr(
        command_catalog,
        "_get_managed_minicode_dir",
        lambda: tmp_path / "managed",
    )

    commands = get_file_command_catalog(worktree)

    assert len(commands) == 1
    assert commands[0]["source"] == "project"
    assert commands[0]["source_path"] == str(command)
