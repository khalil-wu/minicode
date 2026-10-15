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


@pytest.mark.parametrize("model,required", [
    ("protected-model", True), ("provider_1/protected-model", True),
    ("protected-model-preview", False), ("openai-codex/protected-model-preview", False),
    ("protected-modelish", False), ("/protected-model", False),
    ("bad.provider/protected-model", False), ("provider/nested/protected-model", False),
])
def test_source_auto_review_model_requirement_uses_actual_toml_and_exact_supported_aliases(tmp_path, model, required):
    path = tmp_path / "requirements.toml"
    path.write_text('[auto_review]\nrequired_on_models = ["protected-model"]\n', encoding="utf-8")
    requirements = compose_requirements([load_requirements_toml(path)])
    assert requirements.auto_review_required_for_model(model) is required
    assert requirements.can_select_user_reviewer() is True


def test_source_auto_review_model_union_cannot_remove_lower_layer_requirements_and_keeps_higher_ignore_rules(tmp_path):
    lower = tmp_path / "system.toml"
    higher = tmp_path / "managed.toml"
    lower.write_text('[auto_review]\nrequired_on_models = ["alpha", "beta"]\nignore_rules = ["lower-model"]\n', encoding="utf-8")
    higher.write_text('allowed_approvals_reviewers = ["auto_review"]\n[auto_review]\nrequired_on_models = ["beta", "gamma"]\nignore_rules = []\n', encoding="utf-8")
    requirements = compose_requirements([load_requirements_toml(lower), load_requirements_toml(higher)])
    assert set(requirements.value_for("auto_review")["required_on_models"]) == {"alpha", "beta", "gamma"}
    assert requirements.value_for("auto_review")["ignore_rules"] == []
    assert requirements.auto_review_required_for_model("source/alpha") is True
    assert requirements.can_select_user_reviewer() is False
    assert requirements.source_for("auto_review").kind == "composite"


@pytest.mark.parametrize("slug", ["", " protected-model", "protected-model ", "provider/protected-model"])
def test_source_managed_protected_slugs_reject_namespaces_and_whitespace_in_actual_file(tmp_path, slug):
    path = tmp_path / "requirements.toml"
    path.write_text(f'[auto_review]\nrequired_on_models = [{json.dumps(slug)}]\n', encoding="utf-8")
    with pytest.raises(ConfigRequirementsError, match="non-empty model slugs"):
        load_requirements_toml(path)


def test_source_unset_higher_auto_review_fields_keep_lower_models_and_ignore_rules():
    lower = RequirementsLayerEntry(RequirementSource("system_requirements_toml", location="lower.toml"),
        {"auto_review": {"required_on_models": ["alpha"], "ignore_rules": ["ignore-alpha"]}})
    higher = RequirementsLayerEntry(RequirementSource("enterprise_managed", name="actual requirement layer"),
        {"auto_review": {"required_on_models": None, "ignore_rules": None}})
    requirements = compose_requirements([lower, higher])
    assert requirements.value_for("auto_review") == {"required_on_models": ["alpha"], "ignore_rules": ["ignore-alpha"]}


@pytest.mark.parametrize("field,value", [
    ("allowed_domains", "example.com"),
    ("denied_domains", {"internal.example": True}),
    ("allow_unix_sockets", "/tmp/socket"),
])
def test_malformed_legacy_network_policy_is_rejected(field: str, value: object) -> None:
    source = RequirementSource("legacy_managed_config", location="managed.json")
    with pytest.raises(ConfigRequirementsError, match=f"network.{field}"):
        compose_requirements([
            RequirementsLayerEntry(source, {"network": {field: value}}),
        ])


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


def test_explicit_external_sandbox_obeys_managed_modes_without_changing_ui_mode_derivation():
    requirements = compose_requirements([])
    assert requirements.sandbox_mode_for_permission_mode("auto", sandbox_mode="external-sandbox") == "external-sandbox"
    assert requirements.sandbox_mode_for_permission_mode("plan", sandbox_mode="workspace-write") == "read-only"

    source = RequirementSource("system_requirements_toml", location="system.toml")
    restricted = compose_requirements([RequirementsLayerEntry(source, {
        "allowed_sandbox_modes": ["read-only", "workspace-write"],
    })])
    _, violation = restricted.resolve_permission_mode("auto", sandbox_mode="external-sandbox")
    assert violation is not None
    assert violation.field == "sandbox_mode"
    assert violation.candidate == "external-sandbox"

    external = compose_requirements([RequirementsLayerEntry(source, {
        "allowed_sandbox_modes": ["read-only", "external-sandbox"],
    })])
    assert external.resolve_permission_mode("auto", sandbox_mode="external-sandbox") == ("auto", None)


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
    assert decision.scope["requirements_source"] == "requirements.toml"


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


@pytest.mark.parametrize("allowed,expected", [
    (["read-only"], ["plan"]),
    (["read-only", "workspace-write"], ["confirm", "auto", "plan"]),
    (["read-only", "workspace-write", "danger-full-access"], ["confirm", "auto", "plan", "bypass"]),
])
def test_composer_permission_range_is_the_real_managed_sandbox_range(monkeypatch, allowed, expected):
    from types import SimpleNamespace
    from backend.ws.handler import WebSocketSession

    requirements = compose_requirements([RequirementsLayerEntry(
        RequirementSource("system_requirements_toml", location="requirements.toml"), {"allowed_sandbox_modes": allowed})])
    session = SimpleNamespace(permission_context=PermissionContext(mode="plan", source="fixture",
        approval_policy="on-request", sandbox_mode="read-only", requirements_source="requirements.toml", workspace_scope="project"),
        config=SimpleNamespace(permissions=PermissionSettings(), config_layer_stack=SimpleNamespace(requirements=requirements)),
        session_lifecycle=SimpleNamespace(sandbox_capability_payload={"probe_status": "ready"}))
    assert WebSocketSession._runtime_permission_payload(session)["available_modes"] == expected
