from __future__ import annotations

import json

import pytest

from backend.managed_settings import ManagedSettingsResult, load_minicode_managed_file_settings, load_minicode_managed_settings
from backend.runtime_env import ShellEnvironmentPolicy, ShellEnvironmentPolicyError, shell_subprocess_env, mcp_subprocess_env


def test_explicit_empty_remote_policy_does_not_load_a_lower_precedence_source(monkeypatch):
    def lower_source():
        pytest.fail("Explicit remote policy already owns this selection")

    monkeypatch.setattr("backend.managed_settings._load_admin_platform_settings", lower_source)
    selected = load_minicode_managed_settings(remote_settings={})
    assert selected.source_kind == "remote"
    assert selected.present
    assert dict(selected.settings) == {}
    assert not selected.invalid


@pytest.mark.parametrize("field", ["httpProxyPort", "socksProxyPort"])
@pytest.mark.parametrize("port", [float("nan"), float("inf"), float("-inf"), 10**500, 1.5, True])
def test_invalid_managed_ports_produce_source_errors_without_overflow_or_fallback(tmp_path, monkeypatch, field, port):
    (tmp_path / "managed-settings.json").write_text(json.dumps({"sandbox": {"network": {field: port}}}), encoding="utf-8")
    monkeypatch.setattr("backend.managed_settings._load_admin_platform_settings", lambda: ManagedSettingsResult({}))
    monkeypatch.setattr("backend.managed_settings._load_windows_registry_settings", lambda *_: pytest.fail("Invalid present managed files must retain precedence"))
    selected = load_minicode_managed_settings(tmp_path)
    assert selected.present and selected.invalid
    assert selected.source_kind == "managed_files"
    assert str(tmp_path / "managed-settings.json") in selected.source_location
    assert field in selected.validation_errors[0]
    assert dict(selected.settings) == {}


@pytest.mark.parametrize("port", [1, 65535, 8080.0])
def test_supported_managed_ports_keep_the_existing_numeric_contract(tmp_path, port):
    payload = {"sandbox": {"network": {"httpProxyPort": port}}}
    (tmp_path / "managed-settings.json").write_text(json.dumps(payload), encoding="utf-8")
    selected = load_minicode_managed_file_settings(tmp_path)
    assert not selected.invalid
    assert dict(selected.settings) == payload


def test_environment_policy_parses_once_and_owns_its_configured_values(monkeypatch):
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "synthetic-runtime-token")
    original = {"set": {"AUDIT_VALUE": "accepted", "MINICODE_RUNTIME_TOKEN": "synthetic-config-token"}}
    policy = ShellEnvironmentPolicy.from_mapping(original)
    original["set"]["AUDIT_VALUE"] = "later caller edit"
    env = shell_subprocess_env(policy)
    assert env["AUDIT_VALUE"] == "accepted"
    assert "MINICODE_RUNTIME_TOKEN" not in env
    assert policy.ignore_default_excludes is True


@pytest.mark.parametrize("construct", [lambda value: ShellEnvironmentPolicy(set_values=value), lambda value: ShellEnvironmentPolicy.from_mapping({"set": value})])
def test_environment_values_are_rejected_at_their_construct_or_parse_boundary(construct):
    with pytest.raises(ShellEnvironmentPolicyError):
        construct({"AUDIT_VALUE": "embedded\x00value"})


def test_mcp_explicit_env_does_not_inherit_host_runtime_tokens(monkeypatch):
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "synthetic-runtime-token")
    monkeypatch.setenv("AUDIT_INHERITED", "requested")
    env = mcp_subprocess_env({"AUDIT_EXPLICIT": "literal"}, inherited_names=("AUDIT_INHERITED", "MINICODE_RUNTIME_TOKEN"))
    assert env["AUDIT_EXPLICIT"] == "literal"
    assert env["AUDIT_INHERITED"] == "requested"
    assert "MINICODE_RUNTIME_TOKEN" not in env


def test_present_dropin_path_of_the_wrong_type_reports_its_source_and_blocks_lower_policy(tmp_path, monkeypatch):
    (tmp_path / "managed-settings.d").write_text("regular file", encoding="utf-8")
    monkeypatch.setattr("backend.managed_settings._load_admin_platform_settings", lambda: ManagedSettingsResult({}))
    monkeypatch.setattr("backend.managed_settings._load_windows_registry_settings", lambda *_: pytest.fail("Present invalid managed path must retain precedence"))
    selected = load_minicode_managed_settings(tmp_path)
    assert selected.present and selected.invalid
    assert selected.source_kind == "managed_files"
    assert str(tmp_path / "managed-settings.d") in selected.source_location


def test_unreadable_managed_file_uses_the_existing_read_error_path_without_a_pre_stat(tmp_path, monkeypatch):
    from pathlib import Path

    original_read = Path.read_text

    def read_boundary(path, *args, **kwargs):
        if path == tmp_path / "managed-settings.json":
            raise PermissionError("controlled read failure")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_boundary)
    selected = load_minicode_managed_file_settings(tmp_path)
    assert selected.present and selected.invalid
    assert "controlled read failure" in selected.validation_errors[0]
