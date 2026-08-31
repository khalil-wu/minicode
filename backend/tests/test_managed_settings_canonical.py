from __future__ import annotations

from backend.managed_settings import (
    _managed_settings_validation_error,
    normalize_minicode_policy_requirements,
)


def test_managed_policy_uses_only_minicode_snake_case_fields() -> None:
    settings = {
        "allow_managed_hooks_only": True,
        "allow_managed_mcp_servers_only": True,
        "disable_all_hooks": False,
        "allowed_http_hook_urls": ["https://hooks.example.test/*"],
        "http_hook_allowed_env_vars": ["MINICODE_PROJECT_DIR"],
    }
    assert normalize_minicode_policy_requirements(settings) == settings
    assert _managed_settings_validation_error(settings) == ""


def test_external_camel_case_policy_fields_are_rejected() -> None:
    assert _managed_settings_validation_error(
        {"allowManagedHooksOnly": True}
    )
    assert _managed_settings_validation_error(
        {"allowedHttpHookUrls": ["https://hooks.example.test/*"]}
    )
