"""Reuse syntax only; physical changes, policy and returned ownership stay live."""
import os
from unittest.mock import Mock

import pytest

from backend.skills.frontmatter import _parse_yaml_contents, parse_skill_frontmatter, parse_skill_yaml
from backend.skills.loader import SkillLoader


def test_identical_yaml_is_reused_without_sharing_mutable_metadata(monkeypatch):
    import backend.skills.frontmatter as parser

    _parse_yaml_contents.cache_clear()
    parse = Mock(wraps=parser.yaml.safe_load)
    monkeypatch.setattr(parser.yaml, "safe_load", parse)
    source = "---\ndescription: Reusable syntax\nmetadata:\n  nested: [original]\n---\nFirst body."
    first = parse_skill_frontmatter(source, "first-owner")
    first["metadata"]["nested"].append("changed by first caller")
    second = parse_skill_frontmatter(source.replace("First body.", "A changed body."), "second-owner")
    assert first["name"] == "first-owner" and second["name"] == "second-owner"
    assert second["metadata"]["nested"] == ["original"] and parse.call_count == 1
    changed = parse_skill_frontmatter(source.replace("Reusable syntax", "Modified syntax"), "second-owner")
    assert changed["description"] == "Modified syntax" and parse.call_count == 2


def test_same_size_same_timestamp_edit_and_asset_removal_are_visible_on_new_discovery(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    directory = root / "owned"
    (directory / "agents").mkdir(parents=True)
    skill = directory / "SKILL.md"
    skill.write_text("---\nname: owned\ndescription: Old summary\n---\nBody", encoding="utf-8")
    metadata = directory / "agents/openai.yaml"
    metadata.write_text("interface:\n  icon_small: icon.svg\n  icon_large: icon.svg\npolicy:\n  allow_implicit_invocation: true\n", encoding="utf-8")
    asset = directory / "icon.svg"
    asset.write_text("<svg/>", encoding="utf-8")
    loader = SkillLoader(tmp_path)
    monkeypatch.setattr(loader, "_search_dirs", lambda: [("user", root)])
    first = loader.discover()[0]
    stamp = skill.stat()
    skill.write_text(skill.read_text(encoding="utf-8").replace("Old summary", "New summary"), encoding="utf-8")
    os.utime(skill, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    asset.unlink()
    second = loader.discover()[0]
    assert first.description == "Old summary" and first.icon
    assert second.description == "New summary" and not second.icon and not second.icon_large
    metadata.write_text("policy:\n  allow_implicit_invocation: false\n", encoding="utf-8")
    assert not loader.discover()[0].allow_implicit_invocation
    assert first.allow_implicit_invocation
    skill.write_text("---\nname: owned\n---\nInvalid replacement", encoding="utf-8")
    assert loader.discover() == []


def test_optional_policy_yaml_does_not_share_normalized_product_lists():
    source = "policy:\n  products: [CODEX]\n"
    first = parse_skill_yaml(source)
    first["policy"]["products"][0] = "codex"
    assert parse_skill_yaml(source)["policy"]["products"] == ["CODEX"]
    with pytest.raises(ValueError):
        parse_skill_frontmatter("---\ndescription: [broken\n---\n", "bad")
