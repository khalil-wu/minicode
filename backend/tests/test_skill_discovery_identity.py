"""Canonical identities belong to one discovery, never a later filesystem view."""
from __future__ import annotations

from collections import Counter
import os
from pathlib import Path

from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.skills.loader import SkillLoader


def skill(root, name, description="Original instructions"):
    path = root / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\nFull original body.\n", encoding="utf-8")
    return path


def configured(loader, rules):
    loader._config_stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("session_flags"),
        {"skills": {"config": rules}}),))


def test_one_scan_reuses_each_skill_identity_across_filters_and_indexes(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    paths = [skill(root / f"owned-{index}", f"owned-{index}") for index in range(64)]
    loader = SkillLoader(tmp_path)
    configured(loader, [])
    monkeypatch.setattr(loader, "_search_dirs", lambda: [("user", root)])
    resolve = loader._path_key
    calls = Counter()

    def counted(path):
        calls[Path(path)] += 1
        return resolve(path)

    monkeypatch.setattr(loader, "_path_key", counted)
    observed = loader.discover()
    assert {item.source_path for item in observed} == set(paths)
    assert calls == Counter({root: 1, **{path: 1 for path in paths}})
    assert all(enabled for _meta, enabled in loader.list_settings_metas())
    assert set(loader._path_cache) == {resolve(path) for path in paths}
    # The next physical scan must still read edits and remove deleted files.
    skill(paths[0].parent, "owned-0", "Edited instructions")
    paths[1].unlink()
    refreshed = loader.discover()
    assert len(refreshed) == 63
    assert loader.get_meta("owned-0").description == "Edited instructions"
    assert loader.get_meta("owned-1") is None
    assert next(meta for meta in observed if meta.name == "owned-0").description == "Original instructions"


def test_ordered_name_path_and_role_rules_keep_exact_homonymous_skill_owner(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    first, second = skill(root / "first", "shared"), skill(root / "second", "shared")
    loader = SkillLoader(tmp_path)
    monkeypatch.setattr(loader, "_search_dirs", lambda: [("user", root)])
    configured(loader, [{"name": "shared", "enabled": False}, {"path": str(first), "enabled": True}])
    assert [meta.source_path for meta in loader.discover()] == [first]
    assert {meta.source_path: enabled for meta, enabled in loader.list_settings_metas()} == {first: True, second: False}
    assert loader.get_meta_by_path(second) is None
    configured(loader, [{"name": "shared", "enabled": False}, {"path": str(second), "enabled": True}])
    assert [meta.source_path for meta in loader.discover()] == [second]
    loader.restrict_role({"config": [{"path": str(second.parent), "enabled": False}]}, {})
    assert loader.discover() == []
    assert [(meta.source_path, enabled) for meta, enabled in loader.list_settings_metas()] == [(first, False)]


def test_same_target_alias_deduplicates_but_retarget_is_observed_on_next_scan(tmp_path, monkeypatch):
    first = skill(tmp_path / "first", "shared", "First target")
    second = skill(tmp_path / "second", "shared", "Second target")
    alias = tmp_path / "linked-skill"

    def link(target):
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(alias))
        else:
            alias.symlink_to(target, target_is_directory=True)

    def unlink():
        alias.rmdir() if os.name == "nt" else alias.unlink()

    link(first.parent)
    loader = SkillLoader(tmp_path)
    configured(loader, [])
    monkeypatch.setattr(loader, "_search_dirs", lambda: [("workspace", alias), ("user", first.parent)])
    try:
        original = loader.discover()
        assert [(meta.source_path, meta.source_level) for meta in original] == [(alias / "SKILL.md", "workspace")]
        unlink()
        link(second.parent)
        current = loader.discover()
        assert [(meta.description, meta.source_level) for meta in current] == [("Second target", "workspace"), ("First target", "user")]
        assert loader.get_meta_by_path(first).description == "First target"
        assert loader.get_meta_by_path(second).description == "Second target"
        assert original[0].description == "First target"
    finally:
        unlink()
