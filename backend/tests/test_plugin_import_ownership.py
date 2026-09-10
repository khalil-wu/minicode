from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import pytest

import backend.config as config
import backend.services.plugin_settings_service as plugin_service
from backend.plugins.layout import plugin_install_root
from backend.plugins.policy import ManagedPluginPolicy
from backend.plugins.store import PluginStore


def _package(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir()
    package = directory / "plugin.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            ".minicode-plugin/plugin.json",
            json.dumps({"name": name, "version": "1.0.0"}),
        )
        archive.writestr("marker.txt", name)
    return package


@pytest.mark.parametrize("first_outcome", ["complete", "reject", "cancel"])
def test_overlapping_package_imports_own_staging_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_outcome: str
) -> None:
    monkeypatch.setattr(config, "STATE_ROOT", tmp_path / "state")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(plugin_service, "SETTINGS_FILE", settings_file)
    packages = [_package(tmp_path, name) for name in ("alpha-fixture", "beta-fixture")]
    policy = ManagedPluginPolicy(
        enabled_plugins={},
        strict_known_marketplaces=None,
        blocked_marketplaces=(),
        marketplace_requirements={},
    )

    async def scenario() -> None:
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release_first = asyncio.Event()
        release_second = asyncio.Event()

        async def first_hook(**kwargs):
            first_started.set()
            await release_first.wait()
            if first_outcome == "reject":
                raise RuntimeError("fixture hook rejected")

        async def second_hook(**kwargs):
            second_started.set()
            await release_second.wait()

        first = asyncio.create_task(plugin_service.import_plugin_from_path(
            packages[0],
            settings_file=settings_file,
            config_change_hook=first_hook,
            _policy=policy,
        ))
        tasks = [first]
        try:
            async with asyncio.timeout(10):
                await first_started.wait()
                second = asyncio.create_task(plugin_service.import_plugin_from_path(
                    packages[1],
                    settings_file=settings_file,
                    config_change_hook=second_hook,
                    _policy=policy,
                ))
                tasks.append(second)
                await second_started.wait()
                staging = plugin_install_root() / ".package-imports"
                assert len(list(staging.iterdir())) == 2

                if first_outcome == "cancel":
                    first.cancel()
                else:
                    release_first.set()
                first_result = (await asyncio.gather(first, return_exceptions=True))[0]
                if first_outcome == "complete":
                    assert first_result["imported"]["name"] == "alpha-fixture"
                    first_store = Path(first_result["imported"]["store"]["path"])
                    assert (first_store / "marker.txt").read_text() == "alpha-fixture"
                elif first_outcome == "reject":
                    assert isinstance(first_result, RuntimeError)
                else:
                    assert isinstance(first_result, asyncio.CancelledError)

                remaining = list(staging.iterdir())
                assert len(remaining) == 1
                assert (remaining[0] / "marker.txt").read_text() == "beta-fixture"
                release_second.set()
                second_result = await second
                assert second_result["imported"]["name"] == "beta-fixture"
                second_store = Path(second_result["imported"]["store"]["path"])
                assert (second_store / "marker.txt").read_text() == "beta-fixture"
                assert list(staging.iterdir()) == []
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        expected_names = ["beta-fixture"]
        if first_outcome == "complete":
            expected_names.append("alpha-fixture")
        else:
            assert not PluginStore().version_path("local", "alpha-fixture", "1.0.0").exists()
        for name in expected_names:
            destination = PluginStore().version_path("local", name, "1.0.0")
            manifest = json.loads((destination / ".minicode-plugin" / "plugin.json").read_text())
            assert manifest["name"] == name
            assert (destination / "marker.txt").read_text() == name

    asyncio.run(scenario())
