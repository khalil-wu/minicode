from __future__ import annotations

import asyncio
import json
from pathlib import Path, PureWindowsPath

import pytest

from backend.config_requirements import (
    ConfigRequirementsError,
    RequirementSource,
    RequirementViolation,
    RequirementsLayerEntry,
    compose_requirements,
    default_requirements_path,
    load_requirements_toml,
)
from backend.config import PermissionSettings
from backend.feature_flags import load_feature_flags
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.services.conversation_permission_service import plan_permission_mode_update
from backend.services.feature_flag_settings_service import (
    FeatureFlagSettingsError,
    update_feature_flag_settings,
)


def test_requirements_compose_tables_and_union_deny_read_high_precedence_first() -> None:
    system_source = RequirementSource("system_requirements_toml", location="system.toml")
    enterprise_source = RequirementSource(
        "enterprise_managed", source_id="bundle-1", name="Enterprise"
    )
    requirements = compose_requirements(
        [
            RequirementsLayerEntry(
                system_source,
                {
                    "allowed_sandbox_modes": ["read-only", "workspace-write"],
                    "features": {"global_search": True, "sdk_query": True},
                    "permissions": {"filesystem": {"deny_read": ["secrets/**"]}},
                },
            ),
            RequirementsLayerEntry(
                enterprise_source,
                {
                    "features": {"global_search": False},
                    "permissions": {"filesystem": {"deny_read": ["private/**"]}},
                },
            ),
        ]
    )

    assert requirements.feature_requirements == {
        "global_search": False,
        "sdk_query": True,
    }
    assert requirements.filesystem_deny_read == ("private/**", "secrets/**")
    assert requirements.source_for("features").kind == "composite"


def test_default_requirements_path_is_owned_by_minicode(monkeypatch) -> None:
    monkeypatch.delenv("MINICODE_REQUIREMENTS_FILE", raising=False)
    monkeypatch.setattr("backend.config_requirements.sys.platform", "win32")
    monkeypatch.setenv("ProgramData", r"C:\PolicyRoot")

    # Compare Windows path semantics explicitly: CI executes this branch on
    # Linux while simulating ``sys.platform == "win32"``.
    assert PureWindowsPath(default_requirements_path()) == PureWindowsPath(
        r"C:\PolicyRoot\MiniCode\requirements.toml"
    )


def test_requirements_strictly_reject_empty_or_unsafe_sandbox_allowlist(tmp_path: Path) -> None:
    empty = tmp_path / "empty.toml"
    empty.write_text("allowed_approval_policies = []\n", encoding="utf-8")
    layer = load_requirements_toml(empty, required=True)
    assert layer is not None
    with pytest.raises(ConfigRequirementsError, match="must not be empty"):
        compose_requirements([layer])

    unsafe = tmp_path / "unsafe.toml"
    unsafe.write_text("allowed_sandbox_modes = ['workspace-write']\n", encoding="utf-8")
    layer = load_requirements_toml(unsafe, required=True)
    assert layer is not None
    with pytest.raises(ConfigRequirementsError, match="read-only"):
        compose_requirements([layer])


def test_managed_features_override_settings_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINICODE_FEATURE_GLOBAL_SEARCH", "true")
    flags = load_feature_flags(
        {"feature_flags": {"global_search": True}},
        managed_requirements={"global_search": False},
    )
    assert flags.enabled("global_search") is False


def test_feature_mutation_is_rejected_before_writing_managed_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requirements = tmp_path / "requirements.toml"
    requirements.write_text(
        "allowed_sandbox_modes = ['read-only', 'workspace-write']\n"
        "[features]\n"
        "global_search = false\n",
        encoding="utf-8",
    )
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"feature_flags": {"global_search": False}}), encoding="utf-8")
    monkeypatch.setenv("MINICODE_REQUIREMENTS_FILE", str(requirements))
    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", settings_file)

    with pytest.raises(FeatureFlagSettingsError, match="Managed requirement"):
        asyncio.run(
            update_feature_flag_settings(
                {"global_search": True},
                settings_file=settings_file,
                config_change_hook=lambda **_kwargs: _noop(),
            )
        )
    assert json.loads(settings_file.read_text(encoding="utf-8"))["feature_flags"]["global_search"] is False


async def _noop() -> None:
    return None


def test_managed_requirements_reject_bypass_in_permission_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requirements = tmp_path / "requirements.toml"
    requirements.write_text(
        "allowed_approval_policies = ['on-request']\n"
        "allowed_sandbox_modes = ['read-only', 'workspace-write']\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINICODE_REQUIREMENTS_FILE", str(requirements))
    plan = plan_permission_mode_update({"mode": "bypass"})
    assert plan.error_event is not None
    assert "Managed requirement" in str(plan.error_event.data.get("message"))


def test_never_approval_policy_turns_prompts_into_policy_denials() -> None:
    checker = PermissionChecker(PermissionSettings())
    decision = checker.evaluate(
        "run_command",
        {"command": "git status"},
        context=PermissionContext(
            mode="confirm",
            approval_policy="never",
            requirements_source="requirements.toml",
        ),
    )
    assert decision.decision == "deny"
    assert decision.approval_policy == "deny"
    assert decision.matched_rule_source == "managed_requirements"


def test_feature_requirement_violation_reports_source() -> None:
    source = RequirementSource("system_requirements_toml", location="C:/policy/requirements.toml")
    requirements = compose_requirements(
        [RequirementsLayerEntry(source, {"features": {"global_search": False}})]
    )
    with pytest.raises(RequirementViolation, match="C:/policy/requirements.toml"):
        requirements.ensure_feature_value("global_search", True)


@pytest.mark.parametrize("key", ["bad@market@extra", "bad@", "bad/name", "../bad"])
def test_requirements_reject_malformed_enabled_plugin_identity(
    key: str,
) -> None:
    source = RequirementSource("system_requirements_toml", location="requirements.toml")
    with pytest.raises(ConfigRequirementsError, match="invalid plugin id"):
        compose_requirements(
            [RequirementsLayerEntry(source, {"enabled_plugins": {key: True}})]
        )


def test_requirements_reject_empty_plugin_version_constraint() -> None:
    source = RequirementSource("system_requirements_toml", location="requirements.toml")
    with pytest.raises(ConfigRequirementsError, match="boolean or string array"):
        compose_requirements(
            [RequirementsLayerEntry(source, {"enabled_plugins": {"demo": [""]}})]
        )
