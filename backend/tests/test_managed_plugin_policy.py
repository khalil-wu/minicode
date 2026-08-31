from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import pytest

import backend.plugins.store as plugin_store_module
import backend.services.plugin_settings_service as plugin_service
from backend.config import load_config_layer_stack
from backend.managed_settings import load_minicode_managed_file_settings
from backend.plugins.manager import MarketplaceRegistry
from backend.services.plugin_settings_service import (
    ManagedPluginPolicy,
    PluginSettingsError,
    import_plugin_from_path,
    import_plugin_package,
)


def _plugin_source(
    root: Path,
    name: str = "security",
    *,
    version: str = "",
) -> Path:
    source = root / "sources" / name
    manifest = source / ".minicode-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    payload = {"name": name}
    if version:
        payload["version"] = version
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    (source / "marker.txt").write_text("original", encoding="utf-8")
    return source


def _policy(state: object) -> ManagedPluginPolicy:
    return ManagedPluginPolicy(
        enabled_plugins={"security@official": state},
        strict_known_marketplaces=None,
        blocked_marketplaces=(),
        marketplace_requirements={},
    )


def _patch_plugin_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    install_root = tmp_path / "installed"
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(plugin_service, "plugin_install_root", lambda: install_root)
    monkeypatch.setattr(
        plugin_service,
        "_load_settings_json",
        lambda: json.loads(settings_file.read_text(encoding="utf-8")),
    )
    monkeypatch.setattr(
        plugin_service,
        "_write_settings_json",
        lambda payload: settings_file.write_text(json.dumps(payload), encoding="utf-8"),
    )

    class NoopStore:
        def materialize(self, *args: object, **kwargs: object) -> object:
            raise OSError("test store disabled")

    monkeypatch.setattr(plugin_store_module, "PluginStore", NoopStore)
    return settings_file


async def _allow_config_change(**_kwargs: object) -> None:
    return None


@pytest.mark.parametrize("state", [True, False])
def test_managed_identity_rejects_first_local_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: bool,
) -> None:
    settings_file = _patch_plugin_install(monkeypatch, tmp_path)
    source = _plugin_source(tmp_path)

    with pytest.raises(PluginSettingsError, match="managed plugin enablement policy"):
        asyncio.run(
            import_plugin_from_path(
                source,
                marketplace="official",
                settings_file=settings_file,
                config_change_hook=_allow_config_change,
                _policy=_policy(state),
            )
        )

    assert not (tmp_path / "installed").exists()


def test_managed_identity_rejects_overwrite_without_changing_existing_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_file = _patch_plugin_install(monkeypatch, tmp_path)
    source = _plugin_source(tmp_path)
    asyncio.run(
        import_plugin_from_path(
            source,
            marketplace="official",
            settings_file=settings_file,
            config_change_hook=_allow_config_change,
            _policy=ManagedPluginPolicy({}, None, (), {}),
        )
    )
    installed_marker = tmp_path / "installed" / "security-official" / "marker.txt"
    assert installed_marker.read_text(encoding="utf-8") == "original"

    (source / "marker.txt").write_text("malicious replacement", encoding="utf-8")
    with pytest.raises(PluginSettingsError, match="managed plugin enablement policy"):
        asyncio.run(
            import_plugin_from_path(
                source,
                overwrite=True,
                marketplace="official",
                settings_file=settings_file,
                config_change_hook=_allow_config_change,
                _policy=_policy(True),
            )
        )
    assert installed_marker.read_text(encoding="utf-8") == "original"


def test_trusted_marketplace_provenance_allows_managed_true_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_file = _patch_plugin_install(monkeypatch, tmp_path)
    source = _plugin_source(tmp_path)
    result = asyncio.run(
        import_plugin_from_path(
            source,
            marketplace="official",
            settings_file=settings_file,
            config_change_hook=_allow_config_change,
            _policy=_policy(True),
            _trusted_marketplace=True,
            _marketplace_source_descriptor={
                "source": "directory",
                "path": str(source.resolve()),
            },
        )
    )
    assert result["imported"]["id"] == "security@official"


def test_trusted_marketplace_constraint_uses_directory_manifest_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_file = _patch_plugin_install(monkeypatch, tmp_path)
    source = _plugin_source(tmp_path, version="1.2.0")
    provenance = {"source": "directory", "path": str(source.resolve())}

    result = asyncio.run(
        import_plugin_from_path(
            source,
            marketplace="official",
            settings_file=settings_file,
            config_change_hook=_allow_config_change,
            _policy=_policy([">=1.0.0", "<2.0.0"]),
            _trusted_marketplace=True,
            _marketplace_source_descriptor=provenance,
        )
    )

    assert result["imported"]["id"] == "security@official"


def test_trusted_marketplace_constraint_uses_package_manifest_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_file = _patch_plugin_install(monkeypatch, tmp_path)
    source = _plugin_source(tmp_path, version="1.2.0")
    package = tmp_path / "security.zip"
    with zipfile.ZipFile(package, "w") as archive:
        for path in source.rglob("*"):
            if path.is_file():
                archive.write(path, Path("bundle") / path.relative_to(source))
    provenance = {"source": "file", "path": str(package.resolve())}

    result = asyncio.run(
        import_plugin_package(
            package,
            marketplace="official",
            settings_file=settings_file,
            config_change_hook=_allow_config_change,
            _policy=_policy([">=1.0.0", "<2.0.0"]),
            _trusted_marketplace=True,
            _marketplace_source_descriptor=provenance,
        )
    )

    assert result["imported"]["id"] == "security@official"


def test_trusted_marketplace_constraint_rejects_manifest_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_file = _patch_plugin_install(monkeypatch, tmp_path)
    source = _plugin_source(tmp_path, version="2.1.0")

    with pytest.raises(PluginSettingsError, match="disabled by managed"):
        asyncio.run(
            import_plugin_from_path(
                source,
                marketplace="official",
                settings_file=settings_file,
                config_change_hook=_allow_config_change,
                _policy=_policy([">=1.0.0", "<2.0.0"]),
                _trusted_marketplace=True,
                _marketplace_source_descriptor={
                    "source": "directory",
                    "path": str(source.resolve()),
                },
            )
        )


def test_trusted_marketplace_install_requires_registered_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_file = _patch_plugin_install(monkeypatch, tmp_path)
    source = _plugin_source(tmp_path)
    with pytest.raises(PluginSettingsError, match="source provenance"):
        asyncio.run(
            import_plugin_from_path(
                source,
                marketplace="official",
                settings_file=settings_file,
                config_change_hook=_allow_config_change,
                _policy=_policy(True),
                _trusted_marketplace=True,
            )
        )


def test_policy_none_loads_current_managed_policy_and_cannot_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_file = _patch_plugin_install(monkeypatch, tmp_path)
    source = _plugin_source(tmp_path)
    monkeypatch.setattr(plugin_service, "_plugin_policy_from_stack", lambda *_args: _policy(False))

    with pytest.raises(PluginSettingsError, match="managed plugin enablement policy"):
        asyncio.run(
            import_plugin_from_path(
                source,
                marketplace="official",
                settings_file=settings_file,
                config_change_hook=_allow_config_change,
            )
        )


def test_marketplace_source_policy_matches_github_and_raw_url_spellings() -> None:
    policy = ManagedPluginPolicy(
        {},
        ({"source": "github", "repo": "acme/security"},),
        (),
        {},
    )
    policy.assert_source_allowed({"source": "url", "url": "https://github.com/acme/security"})

    blocked = ManagedPluginPolicy(
        {},
        None,
        ({"source": "github", "repo": "acme/security"},),
        {},
    )
    with pytest.raises(PluginSettingsError, match="blocked"):
        blocked.assert_source_allowed(
            {"source": "url", "url": "https://github.com/acme/security"}
        )


def test_invalid_active_marketplace_rule_fails_closed_even_when_later_rule_matches(
    tmp_path: Path,
) -> None:
    policy = ManagedPluginPolicy(
        {},
        (
            {"source": "hostPattern", "hostPattern": "["},
            {"source": "directory", "path": str(tmp_path.resolve())},
        ),
        (),
        {},
    )
    with pytest.raises(PluginSettingsError, match="policy is invalid") as error:
        policy.assert_source_allowed(
            {"source": "directory", "path": str(tmp_path.resolve())}
        )
    assert error.value.status_code == 503


def test_marketplace_list_does_not_swallow_unexpected_policy_errors(
    tmp_path: Path,
) -> None:
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    registry.add(
        "demo",
        {"source": "directory", "path": str(tmp_path.resolve())},
        policy=ManagedPluginPolicy({}, None, (), {}),
    )

    class ExplodingPolicy:
        def assert_policy_valid(self) -> None:
            return None

        def assert_source_allowed(self, _source: object) -> None:
            raise RuntimeError("unexpected policy failure")

    with pytest.raises(RuntimeError, match="unexpected policy failure"):
        registry.list(policy=ExplodingPolicy())


def test_marketplace_cache_path_cannot_replace_original_policy_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_file = _patch_plugin_install(monkeypatch, tmp_path)
    source = _plugin_source(tmp_path)
    cache = tmp_path / "cache" / "security"
    cache.mkdir(parents=True)
    (cache / ".minicode-plugin").mkdir()
    (cache / ".minicode-plugin" / "plugin.json").write_text(
        json.dumps({"name": "security"}), encoding="utf-8"
    )
    policy = ManagedPluginPolicy(
        {},
        ({"source": "directory", "path": str(source.resolve())},),
        (),
        {},
    )
    with pytest.raises(PluginSettingsError, match="strict_known_marketplaces"):
        asyncio.run(
            import_plugin_from_path(
                cache,
                marketplace="official",
                settings_file=settings_file,
                config_change_hook=_allow_config_change,
                _policy=policy,
                _trusted_marketplace=True,
                _marketplace_source_descriptor={
                    "source": "directory",
                    "path": str(cache.resolve()),
                },
            )
        )


def test_invalid_managed_file_keeps_source_location_and_blocks_lower_layers(
    tmp_path: Path,
) -> None:
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    managed_file = managed_dir / "managed-settings.json"
    managed_file.write_text(json.dumps({"enabled_plugins": []}), encoding="utf-8")

    result = load_minicode_managed_file_settings(managed_dir)
    assert result.present is True
    assert result.invalid is True
    assert result.source_location == str(managed_file)

    stack = load_config_layer_stack(
        managed_settings_result=result,
        cwd=tmp_path,
    )
    assert stack.managed_policy_errors
    policy = plugin_service._plugin_policy_from_stack(stack)
    with pytest.raises(PluginSettingsError, match="policy is invalid"):
        policy.assert_policy_valid()


def test_user_plugin_enablement_persists_only_canonical_minicode_schema() -> None:
    settings = {
        "plugins": {
            "demo@official": {"enabled": False, "capabilities": ["skills"]},
        },
        "enabledPlugins": {"foreign@official": True},
    }

    plugin_service._persist_user_plugin_enablement(
        settings,
        "demo@official",
        True,
    )

    assert settings == {
        "plugins": {
            "demo@official": {
                "enabled": True,
                "capabilities": ["skills"],
            }
        }
    }


def test_user_plugin_enablement_clear_preserves_other_plugin_entries() -> None:
    settings = {
        "plugins": {
            "demo@official": {"enabled": True},
            "other@official": {"enabled": False},
        }
    }

    plugin_service._clear_user_plugin_enablement(settings, "demo@official")

    assert settings == {
        "plugins": {
            "other@official": {"enabled": False},
        }
    }


def test_foreign_plugin_enablement_dialects_are_not_runtime_inputs() -> None:
    states, explicit, invalid = plugin_service._collect_configured_plugin_states(
        {
            "enabledPlugins": {"foreign@official": True},
            "enabled_plugins": {"managed-leak@official": True},
            "plugins": {
                "canonical@official": {"enabled": True},
                "disabled": ["canonical@official"],
            },
        },
        None,
    )

    assert states == {"canonical@official": True}
    assert explicit is True
    assert invalid == {}
