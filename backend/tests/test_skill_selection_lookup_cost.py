import os

import pytest

from backend.config_layers import ConfigLayerStack
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    roots = {name: tmp_path / name for name in ("workspace", "user", "plugin")}
    paths = {}

    def skill(source, name, *, invocable=True):
        path = roots[source] / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: Private selection fixture.\nuser-invocable: {str(invocable).lower()}\n---\nComplete instructions.\n", encoding="utf-8")
        paths[source, name] = path

    skill("workspace", "shared")
    skill("workspace", "only")
    skill("workspace", "same")
    skill("workspace", "no-invoke", invocable=False)
    skill("user", "shared")
    skill("user", "name_alias")
    skill("plugin", "shared")
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda self: [
        ("workspace", roots["workspace"]), ("user", roots["user"]), ("plugin:demo", roots["plugin"]),
    ])
    loader = SkillLoader()
    loader._config_stack = ConfigLayerStack(())
    manager = SkillManager(loader)
    manager.discover()
    return manager, paths


def test_plain_and_name_only_selection_do_not_normalize_unmentioned_paths(catalog, monkeypatch):
    manager, paths = catalog

    def unnecessary(source_path):
        raise AssertionError(f"Unmentioned filesystem identity was requested: {source_path}")

    monkeypatch.setattr(SkillManager, "_skill_key", staticmethod(unnecessary))
    assert manager.detect("Complete the local task") == []
    selected = manager.detect("Use $only and $name-alias and $demo:shared")
    assert [item.name for item in selected] == ["only", "name_alias", "demo:shared"]
    assert [item.source_path for item in selected] == [str(paths["workspace", "only"]),
        str(paths["user", "name_alias"]), str(paths["plugin", "shared"])]
    assert manager.detect("Use $shared") == []
    assert manager.detect("Use [$shared](app://example) and [$shared](mcp://example) and [$shared](plugin://example)") == []


def test_structured_and_linked_paths_keep_exact_homonymous_owner_and_order(catalog):
    manager, paths = catalog
    user = paths["user", "shared"]
    workspace = paths["workspace", "shared"]
    selected = manager.detect(f"Use [$shared]({workspace}) and $shared and [$shared]({workspace})",
        selected_skills=[{"name": "shared", "path": str(user)}])
    assert [item.source_path for item in selected] == [str(user), str(workspace)]
    assert [item.name for item in selected] == ["shared", "shared"]


def test_invalid_structured_owner_keeps_plain_name_blocked(catalog, tmp_path):
    manager, _ = catalog
    missing = tmp_path / "missing/SKILL.md"
    selected = manager.detect("Use $only", selected_skills=[{"name": "only", "path": str(missing)}])
    assert [(item.name, item.source_path) for item in selected] == [("only", str(missing))]
    assert manager.load_skill_payload("only", source_path=selected[0].source_path) is None


def test_role_and_explicit_invocation_policy_still_restrict_selection(catalog):
    manager, paths = catalog
    restricted = manager.snapshot(role_config={"config": [{"name": "only", "enabled": False}]}, config_stack=ConfigLayerStack(()))
    marker = object()
    restricted.attach_executor_catalog(marker)
    assert restricted.detect("Use $only") == []
    assert [item.name for item in manager.detect("Use $only")] == ["only"]
    assert restricted.snapshot().executor_catalog is marker
    assert manager.detect("Use $no-invoke") == []
    selected = manager.detect("Perform the local task", selected_skills=[{"name": "no-invoke", "path": str(paths["workspace", "no-invoke"])}])
    assert len(selected) == 1 and selected[0].unavailable_reason


def test_relative_and_windows_case_paths_keep_existing_exact_resolution(catalog, tmp_path, monkeypatch):
    manager, paths = catalog
    path = paths["workspace", "shared"]
    monkeypatch.chdir(tmp_path)
    relative = path.relative_to(tmp_path)
    assert [item.source_path for item in manager.detect(f"Use [$shared]({relative})")] == [str(path)]
    if os.name == "nt":
        assert [item.source_path for item in manager.detect(f"Use [$shared]({str(path).upper()})")] == [str(path)]


@pytest.mark.parametrize("scheme", ["app", "mcp", "plugin"])
@pytest.mark.parametrize("template", ["{uri}", "[@same]({uri})", "[$same]({uri})"], ids=["bare-uri", "at-link", "skill-link"])
def test_resource_uri_does_not_invoke_a_same_named_skill(catalog, scheme, template):
    manager, _ = catalog
    message = template.format(uri=f"{scheme}://same")
    assert manager.detect(message, selected_skills=[]) == []


@pytest.mark.parametrize("message", ["$same", "/same", "Use $same", "Use /same"])
def test_independent_skill_sigils_still_select_the_current_skill(catalog, message):
    manager, paths = catalog
    selected = manager.detect(message)
    assert [(item.name, item.source_path) for item in selected] == [("same", str(paths["workspace", "same"]))]


@pytest.mark.parametrize("prefix", ["", "skill://"], ids=["discovery-path", "skill-uri"])
def test_plugin_reference_preserves_exact_linked_skill_selection(catalog, prefix):
    manager, paths = catalog
    path = paths["user", "shared"]
    message = f"[@same](plugin://same@local) [$shared]({prefix}{path.as_posix()})"
    selected = manager.detect(message)
    assert [(item.name, item.source_path) for item in selected] == [("shared", str(path))]


@pytest.mark.parametrize("plugin_reference", ["[@same](plugin://same)", "[@same](plugin://same@local)"])
def test_deleted_skill_selection_is_not_reactivated_by_the_remaining_plugin(catalog, plugin_reference):
    manager, paths = catalog
    path = paths["workspace", "same"]
    selected = manager.detect(f"[$same]({path.as_posix()}) {plugin_reference}",
        selected_skills=[{"name": "same", "path": str(path)}])
    assert [(item.name, item.source_path) for item in selected] == [("same", str(path))]
    assert manager.detect(plugin_reference, selected_skills=[]) == []
