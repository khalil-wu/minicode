from __future__ import annotations

import json
import tomllib

import pytest

from backend.mcp.manager import MCPServerConfig, MCPServerManager
from backend.mcp.policy import (
    MCPPolicyError,
    load_mcp_policy,
    mcp_policy_from_requirements,
)


def _load_requirements_policy(path):
    return mcp_policy_from_requirements(
        tomllib.loads(path.read_text(encoding="utf-8")),
        source=str(path),
    )


def _stdio(name: str, command: str, *args: str, source: str = "user") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        command=command,
        args=list(args),
        transport="stdio",
        source=source,
        auto_start=False,
    )


def _remote(
    name: str,
    url: str,
    *,
    transport: str = "http",
    source: str = "user",
) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport=transport,
        url=url,
        source=source,
        auto_start=False,
    )


def test_access_policy_exact_command_wildcard_url_and_deny_precedence(tmp_path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "managed-settings.json").write_text(
        json.dumps(
            {
                "allowed_mcp_servers": [
                    {"command": ["node", "server.js", "--safe"]},
                    {"url": "https://*.example.com/mcp/*"},
                ],
                "denied_mcp_servers": [
                    {"name": "blocked-name"},
                    {"url": "https://bad.example.com/*"},
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = load_mcp_policy(
        managed_settings_dir=managed,
    )

    assert policy.allows(_stdio("ok", "node", "server.js", "--safe")) is True
    assert policy.allows(_stdio("ok", "node", "--safe", "server.js")) is False
    assert policy.allows(_stdio("blocked-name", "node", "server.js", "--safe")) is False
    assert policy.allows(_remote("api", "https://api.example.com/mcp/v1")) is True
    assert policy.allows(_remote("bad", "https://bad.example.com/mcp/v1")) is False
    assert policy.allows(_remote("sse", "https://api.example.com/mcp/sse", transport="sse")) is True
    assert policy.allows(_remote("ws", "wss://api.example.com/mcp/ws", transport="ws")) is False


def test_access_allowlist_transport_classes_override_name_entries(tmp_path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "managed-settings.json").write_text(
        json.dumps(
            {
                "allowed_mcp_servers": [
                    {"name": "named-stdio"},
                    {"name": "named-remote"},
                    {"command": ["approved", "arg"]},
                    {"url": "https://approved.example/*"},
                ]
            }
        ),
        encoding="utf-8",
    )
    policy = load_mcp_policy(
        managed_settings_dir=managed,
    )

    assert policy.allows(_stdio("named-stdio", "different")) is False
    assert policy.allows(_stdio("other", "approved", "arg")) is True
    assert policy.allows(_remote("named-remote", "https://different.example/mcp")) is False
    assert policy.allows(_remote("other", "https://approved.example/mcp")) is True


def test_access_empty_allowlist_blocks_all_and_undefined_allows_all(tmp_path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    user = tmp_path / "user"
    user.mkdir()
    (managed / "managed-settings.json").write_text(
        '{"allowed_mcp_servers": []}',
        encoding="utf-8",
    )
    blocked = load_mcp_policy(
        managed_settings_dir=managed,
    )
    assert blocked.allows(_stdio("docs", "node")) is False

    (managed / "managed-settings.json").write_text("{}", encoding="utf-8")
    unrestricted = load_mcp_policy(
        managed_settings_dir=managed,
    )
    assert unrestricted.allows(_stdio("docs", "node")) is True


def test_access_managed_only_uses_allowlist_and_denies_from_same_policy(tmp_path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "managed-settings.json").write_text(
        json.dumps(
            {
                "allow_managed_mcp_servers_only": True,
                "allowed_mcp_servers": [
                    {"command": ["admin-cli"]},
                    {"command": ["admin-cli", "blocked"]},
                ],
                "denied_mcp_servers": [
                    {"name": "user-denied"},
                    {"command": ["admin-cli", "blocked"]},
                ],
            }
        ),
        encoding="utf-8",
    )

    policy = load_mcp_policy(
        managed_settings_dir=managed,
    )

    assert policy.allows(_stdio("user", "user-cli")) is False
    assert policy.allows(_stdio("admin", "admin-cli")) is True
    assert policy.allows(_stdio("user-denied", "admin-cli")) is False
    assert policy.allows(_stdio("local-denied", "admin-cli", "blocked")) is False


def test_access_managed_dropins_merge_alphabetically_and_lock_mcp(tmp_path) -> None:
    managed = tmp_path / "managed"
    dropins = managed / "managed-settings.d"
    dropins.mkdir(parents=True)
    (managed / "managed-settings.json").write_text(
        json.dumps({"allowed_mcp_servers": [{"name": "base"}]}),
        encoding="utf-8",
    )
    (dropins / "10-security.json").write_text(
        json.dumps(
            {
                "allowed_mcp_servers": [{"name": "first"}],
                "strict_plugin_only_customization": False,
            }
        ),
        encoding="utf-8",
    )
    (dropins / "20-lock.json").write_text(
        json.dumps(
            {
                "allowed_mcp_servers": [{"name": "second"}],
                "strict_plugin_only_customization": ["mcp"],
            }
        ),
        encoding="utf-8",
    )

    policy = load_mcp_policy(
        managed_settings_dir=managed,
    )

    assert policy.strict_plugin_only is True
    assert policy.allows(_stdio("base", "base")) is True
    assert policy.allows(_stdio("first", "first")) is True
    assert policy.allows(_stdio("second", "second")) is True


def test_invalid_minicode_policy_source_is_rejected(tmp_path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "managed-settings.json").write_text(
        json.dumps({"allowed_mcp_servers": [{"command": []}]}),
        encoding="utf-8",
    )

    with pytest.raises(MCPPolicyError, match="non-empty string array"):
        load_mcp_policy(managed_settings_dir=managed)


def test_identity_requirements_match_legacy_and_structured_identities(tmp_path) -> None:
    path = tmp_path / "requirements.toml"
    path.write_text(
        r'''
[mcp_servers.legacy.identity]
command = "legacy-cli"

[mcp_servers.proxy.identity.command]
executable = "company-cli"
args = [
  { match = "exact", value = "mcp" },
  { match = "prefix", value = "https://internal.example/" },
  { match = "regex", expression = "[a-z]+-[0-9]+" },
]

[mcp_servers.remote.identity]
url = { match = "prefix", value = "https://mcp.example/" }
''',
        encoding="utf-8",
    )
    policy = _load_requirements_policy(path)

    assert policy.disabled_reason(_stdio("legacy", "legacy-cli", "any", "args")) is None
    assert policy.disabled_reason(
        _stdio("proxy", "company-cli", "mcp", "https://internal.example/v1", "tenant-42")
    ) is None
    assert policy.disabled_reason(
        _stdio("proxy", "company-cli", "mcp", "https://internal.example/v1", "tenant-42", "extra")
    ) is not None
    assert policy.disabled_reason(
        _stdio("proxy", "company-cli", "mcp", "https://internal.example/v1", "x-tenant-42")
    ) is not None
    assert policy.disabled_reason(_remote("remote", "https://mcp.example/v1")) is None
    assert policy.disabled_reason(
        _remote("remote", "https://mcp.example/v1", transport="sse")
    ) is None
    assert policy.disabled_reason(
        _remote("remote", "https://mcp.example/v1", transport="ws")
    ) is None
    assert policy.disabled_reason(_stdio("unlisted", "legacy-cli")) is not None


def test_identity_empty_requirements_block_all_but_unset_allows_all(tmp_path) -> None:
    path = tmp_path / "requirements.toml"
    path.write_text("[mcp_servers]\n", encoding="utf-8")
    blocked = _load_requirements_policy(path)
    assert blocked.disabled_reason(_stdio("docs", "node")) is not None

    path.write_text('allowed_sandbox_modes = ["read-only"]\n', encoding="utf-8")
    unrestricted = _load_requirements_policy(path)
    assert unrestricted.disabled_reason(_stdio("docs", "node")) is None


def test_identity_plugin_requirements_match_plugin_and_original_server_name(tmp_path) -> None:
    path = tmp_path / "requirements.toml"
    path.write_text(
        r'''
[plugins."sample@test".mcp_servers.docs.identity]
command = "docs-cli"
''',
        encoding="utf-8",
    )
    policy = _load_requirements_policy(path)

    matching = _stdio(
        "plugin:sample@test:docs",
        "docs-cli",
        source="plugin:sample@test",
    )
    wrong_server = _stdio(
        "plugin:sample@test:other",
        "docs-cli",
        source="plugin:sample@test",
    )
    wrong_plugin = _stdio(
        "plugin:other@test:docs",
        "docs-cli",
        source="plugin:other@test",
    )
    assert policy.disabled_reason(matching) is None
    assert policy.disabled_reason(wrong_server) is not None
    assert policy.disabled_reason(wrong_plugin) is not None


def test_identity_invalid_matcher_rejects_the_admin_requirements_file(tmp_path) -> None:
    path = tmp_path / "requirements.toml"
    path.write_text(
        r'''
[mcp_servers.docs.identity]
url = { match = "regex", expression = "(" }
''',
        encoding="utf-8",
    )

    with pytest.raises(MCPPolicyError, match="invalid regex"):
        _load_requirements_policy(path)


def test_manager_enterprise_config_is_exclusive_and_policy_filtered(monkeypatch, tmp_path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "managed-mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "enterprise": {"transport": "stdio", "command": "enterprise-cli"},
                    "blocked": {"transport": "stdio", "command": "blocked-cli"},
                }
            }
        ),
        encoding="utf-8",
    )
    (managed / "managed-settings.json").write_text(
        json.dumps({"allowed_mcp_servers": [{"command": ["enterprise-cli"]}]}),
        encoding="utf-8",
    )
    local = tmp_path / ".mcp.json"
    local.write_text(
        json.dumps({"servers": {"local": {"transport": "stdio", "command": "local-cli"}}}),
        encoding="utf-8",
    )
    manager = MCPServerManager(
        config_path=local,
        managed_settings_dir=managed,
        
        
    )
    monkeypatch.setattr(manager, "_load_plugin_configs", lambda **_kwargs: [])
    monkeypatch.setattr(manager, "_load_project_configs", lambda: [])

    configs = manager.load_config()

    assert [config.name for config in configs] == ["enterprise", "blocked"]
    assert configs[0].source == "enterprise"
    assert configs[0].enabled is True
    assert configs[1].enabled is False
    assert "policy" in configs[1].disabled_reason.lower()


def test_manager_strict_plugin_only_keeps_plugins_and_drops_user_sources(monkeypatch, tmp_path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "managed-settings.json").write_text(
        json.dumps({"strict_plugin_only_customization": ["mcp"]}),
        encoding="utf-8",
    )
    local = tmp_path / ".mcp.json"
    local.write_text(
        json.dumps({"servers": {"local": {"transport": "stdio", "command": "local-cli"}}}),
        encoding="utf-8",
    )
    manager = MCPServerManager(
        config_path=local,
        managed_settings_dir=managed,
        
        
    )
    monkeypatch.setattr(
        manager,
        "_load_plugin_configs",
        lambda **_kwargs: [_stdio("plugin:sample:docs", "plugin-cli", source="plugin:sample")],
    )
    monkeypatch.setattr(manager, "_load_project_configs", lambda: [])

    configs = manager.load_config()

    assert [config.name for config in configs] == ["plugin:sample:docs"]


def test_manager_retains_policy_blocked_servers_but_marks_them_non_connectable(
    monkeypatch,
    tmp_path,
) -> None:
    requirements = tmp_path / "requirements.toml"
    requirements.write_text(
        r'''
[mcp_servers.allowed.identity]
command = "allowed-cli"
''',
        encoding="utf-8",
    )
    local = tmp_path / ".mcp.json"
    local.write_text(
        json.dumps(
            {
                "servers": {
                    "allowed": {"transport": "stdio", "command": "allowed-cli", "auto_start": False},
                    "blocked": {"transport": "stdio", "command": "blocked-cli", "auto_start": False},
                }
            }
        ),
        encoding="utf-8",
    )
    manager = MCPServerManager(
        config_path=local,
        managed_settings_dir=tmp_path / "managed",
        
        requirements_path=requirements,
    )
    monkeypatch.setattr(manager, "_load_plugin_configs", lambda **_kwargs: [])
    monkeypatch.setattr(manager, "_load_project_configs", lambda: [])

    configs = {config.name: config for config in manager.load_config()}

    assert configs["allowed"].enabled is True
    assert configs["allowed"].disabled_reason == ""
    assert configs["blocked"].enabled is False
    assert "requirements.toml" in configs["blocked"].disabled_reason
