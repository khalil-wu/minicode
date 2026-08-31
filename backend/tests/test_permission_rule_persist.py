"""add_permission_content_rule: persists a Tool(content) rule to settings.json."""

from __future__ import annotations

import importlib
from typing import Any

import backend.config as config_mod
from backend.services.permission_content_service import add_permission_content_rule as add_rule_command


def test_add_content_allow_rule_appends_and_dedups(monkeypatch):
    stored: dict[str, Any] = {"permissions": {"content_allow_rules": ["run_command(git status)"]}}
    written: list[dict[str, Any]] = []

    monkeypatch.setattr(config_mod, "_load_settings_json", lambda: stored)
    monkeypatch.setattr(config_mod, "_write_settings_json", lambda data: written.append(data))

    result = config_mod.add_permission_content_rule("run_command(npm run:*)")
    assert "run_command(npm run:*)" in result
    assert "run_command(git status)" in result

    # Writing again with the same rule is a no-op (dedup).
    written.clear()
    again = config_mod.add_permission_content_rule("run_command(npm run:*)")
    assert again.count("run_command(npm run:*)") == 1
    assert written == []  # nothing persisted when no change


def test_add_content_deny_rule_writes_deny_list(monkeypatch):
    stored: dict[str, Any] = {"permissions": {}}
    written: list[dict[str, Any]] = []

    monkeypatch.setattr(config_mod, "_load_settings_json", lambda: stored)
    monkeypatch.setattr(config_mod, "_write_settings_json", lambda data: written.append(data))

    result = config_mod.add_permission_content_rule("run_command(rm -rf:*)", deny=True)
    assert result == ["run_command(rm -rf:*)"]
    assert written[0]["permissions"]["content_deny_rules"] == ["run_command(rm -rf:*)"]
    # allow list untouched
    assert written[0]["permissions"].get("content_allow_rules") is None


def test_add_blank_rule_is_noop(monkeypatch):
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(config_mod, "_load_settings_json", lambda: {"permissions": {}})
    monkeypatch.setattr(config_mod, "_write_settings_json", lambda data: written.append(data))

    assert config_mod.add_permission_content_rule("   ") == []
    assert written == []


def test_content_rule_command_requires_explicit_global_scope(monkeypatch):
    called = False

    def save_rule(_rule: str, *, deny: bool = False):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(config_mod, "add_permission_content_rule", save_rule)

    rejected = add_rule_command("run_command(git:*)", scope="workspace")

    assert rejected.outcome.level == "warning"
    assert rejected.outcome.data == {"scope": "workspace"}
    assert rejected.should_emit_config_change is False
    assert called is False


def test_content_rule_command_reports_global_scope(monkeypatch):
    monkeypatch.setattr(
        config_mod,
        "add_permission_content_rule",
        lambda rule, *, deny=False: [rule],
    )

    saved = add_rule_command("run_command(git:*)", scope="global")

    assert saved.outcome.level == "success"
    assert saved.outcome.data["scope"] == "global"
    assert saved.should_emit_config_change is True
