import importlib.util
import json
from pathlib import Path


def test_notice_generation_uses_installed_versions_and_complete_deduplicated_license_text(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "desktop/scripts/collect-third-party-notices.py"
    spec = importlib.util.spec_from_file_location("fixture_notice_collector", script)
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    modules = tmp_path / "frontend/node_modules"
    modules.mkdir(parents=True)
    packages = {}
    for name, version in [("first", "1.0.0"), ("second", "2.0.0")]:
        package = modules / name
        package.mkdir()
        (package / "package.json").write_text(json.dumps({"name": name, "version": version, "license": "MIT"}), encoding="utf8")
        (package / "LICENSE").write_text("Original complete fixture license text.", encoding="utf8")
        packages["node_modules/" + name] = {"version": version}
    packages["node_modules/uninstalled-other-platform"] = {"optional": True, "os": ["android"]}
    packages["node_modules/build-only"] = {"dev": True}
    (modules / ".package-lock.json").write_text(json.dumps({"packages": packages}), encoding="utf8")
    monkeypatch.setattr(collector.subprocess, "check_output", lambda *_args, **_kwargs: "")
    text, count = collector.collect_notices(tmp_path)
    assert count == 2
    assert "first 1.0.0" in text and "second 2.0.0" in text
    assert text.count("Original complete fixture license text.") == 1
    assert "see the complete notice under first 1.0.0" in text
    assert "uninstalled-other-platform" not in text and "build-only" not in text
