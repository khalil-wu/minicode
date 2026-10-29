from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from fastapi import FastAPI, HTTPException, Response

from backend.api import routes_skills
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.services import skills_api_service
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager


@pytest.fixture
def skills_settings(tmp_path, monkeypatch):
    user, bundled, plugins = (tmp_path / scope for scope in ("user", "bundled", "plugins"))
    paths = {}
    for root, name in ((user, "one"), (user, "two"), (bundled, "builtin"), (plugins, "plugin-owned")):
        target = root / name / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text(f"---\nname: {name}\ndescription: {name} workflow\n---\nReal instructions", encoding="utf-8")
        paths[name] = target
    settings = {"unrelated": {"draft_setting": "preserved"}, "skills": {"config": [{"path": str(paths["two"]), "enabled": False}]}}
    extra = []
    def stack(**_kwargs):
        return ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "settings.json")), deepcopy(settings)), *extra))
    monkeypatch.setattr("backend.config.load_config_layer_stack", stack)
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda _self: [("user", user), ("builtin", bundled), ("plugin", plugins)])
    monkeypatch.setattr(skills_api_service, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr("backend.skills.marketplace.USER_SKILLS_DIR", user)
    writes = []
    def save(mutator):
        mutator(settings)
        writes.append(deepcopy(settings))
    monkeypatch.setattr(skills_api_service, "_update_settings_json", save)
    async def hook(**_kwargs):
        return None
    monkeypatch.setattr(skills_api_service, "run_config_change_hook", hook)
    manager = SkillManager(SkillLoader())
    monkeypatch.setattr(routes_skills._state, "bootstrap", SimpleNamespace(skill_manager=manager))
    monkeypatch.setattr(routes_skills._state, "invalidate_status_cache", lambda: None)
    return SimpleNamespace(manager=manager, paths=paths, settings=settings, writes=writes, extra=extra)


@pytest.mark.asyncio
async def test_user_settings_inventory_keeps_disabled_skills_out_of_the_executable_catalog(skills_settings):
    fixture = skills_settings
    response = Response()
    payload = await routes_skills.get_skill_settings_api(response)
    assert response.headers["Cache-Control"] == "no-store"
    assert {skill["name"]: skill["enabled"] for skill in payload["skills"]} == {"one": True, "two": False}
    assert {skill["uninstall_name"] for skill in payload["skills"]} == {"one", "two"}
    assert {skill["name"] for skill in payload["available_skills"]} == {"one", "builtin", "plugin-owned"}
    assert fixture.manager.snapshot().get_meta_by_path(fixture.paths["two"]) is None


def test_uninstall_identity_uses_owned_directory_and_excludes_other_user_roots(skills_settings, tmp_path, monkeypatch):
    fixture = skills_settings
    fixture.paths["one"].write_text("---\nname: shared\ndescription: Local workflow\n---\nLocal instructions", encoding="utf-8")
    external = tmp_path / "external-skills" / "one" / "SKILL.md"
    external.parent.mkdir(parents=True)
    external.write_text("---\nname: shared\ndescription: External workflow\n---\nExternal instructions", encoding="utf-8")
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda _self: [("user", fixture.paths["one"].parent.parent),
        ("user", external.parent.parent), ("plugin", fixture.paths["plugin-owned"].parent.parent)])
    payload = skills_api_service.skills_settings_payload(skill_manager=fixture.manager)
    by_path = {Path(skill["path"]): skill for skill in payload["skills"]}
    assert by_path[fixture.paths["one"]]["name"] == "shared"
    assert by_path[fixture.paths["one"]]["uninstall_name"] == "one"
    assert by_path[external]["name"] == "shared"
    assert by_path[external]["uninstall_name"] is None
    assert "uninstall_name" not in payload["plugin_skills"][0]
    assert external.exists() and fixture.paths["one"].exists()


@pytest.mark.asyncio
async def test_toggle_persists_path_rules_and_the_next_turn_sees_them_without_mutating_inflight_snapshot(skills_settings):
    fixture = skills_settings
    captured = fixture.manager.snapshot()
    request = routes_skills.SkillEnabledRequest(path=str(fixture.paths["one"]), enabled=False)
    result = await routes_skills.update_skill_settings_api(request, Response())
    assert {skill["name"] for skill in result["skills"]} == {"one", "two"}
    assert not any(skill["name"] == "one" for skill in result["available_skills"])
    assert fixture.manager.snapshot().load_skill_payload("one") is None
    assert captured.load_skill_payload("one")["content"].endswith("Real instructions")
    assert fixture.settings["unrelated"] == {"draft_setting": "preserved"}
    enabled = await routes_skills.update_skill_settings_api(
        routes_skills.SkillEnabledRequest(path=str(fixture.paths["one"]), enabled=True), Response())
    assert next(skill for skill in enabled["skills"] if skill["name"] == "one")["enabled"] is True
    assert fixture.manager.snapshot().load_skill_payload("one")["content"].endswith("Real instructions")
    rules = fixture.settings["skills"]["config"]
    assert len([rule for rule in rules if rule["path"] == str(fixture.paths["one"].resolve())]) == 1
    assert next(rule for rule in rules if rule["path"] == str(fixture.paths["two"]))["enabled"] is False


@pytest.mark.asyncio
async def test_settings_cannot_modify_a_builtin_or_undiscovered_path(skills_settings):
    for target in (skills_settings.paths["builtin"], Path("C:/unrelated/SKILL.md")):
        with pytest.raises(HTTPException) as error:
            await routes_skills.update_skill_settings_api(routes_skills.SkillEnabledRequest(path=str(target), enabled=False), Response())
        assert error.value.status_code == 400
    assert skills_settings.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name,field", [("one", "skills"), ("plugin-owned", "plugin_skills")])
async def test_effective_workspace_override_preserves_a_successful_user_write_and_reports_actual_state(skills_settings, name, field):
    fixture = skills_settings
    fixture.extra.append(ConfigLayer(ConfigLayerSource("session_flags"), {"skills": {"config": [{"name": name, "enabled": False}]}}))
    result = await routes_skills.update_skill_settings_api(
        routes_skills.SkillEnabledRequest(path=str(fixture.paths[name]), enabled=True), Response())
    assert len(fixture.writes) == 1
    assert next(rule for rule in fixture.writes[0]["skills"]["config"] if rule["path"] == str(fixture.paths[name].resolve()))["enabled"] is True
    assert next(skill for skill in result[field] if skill["name"] == name)["enabled"] is False
    assert not any(skill["name"] == name for skill in result["available_skills"])
    assert fixture.manager.snapshot().get_meta_by_path(fixture.paths[name]) is None
    fixture.extra.clear()
    inherited = await routes_skills.get_skill_settings_api(Response())
    assert next(skill for skill in inherited[field] if skill["name"] == name)["enabled"] is True
    assert any(skill["name"] == name for skill in inherited["available_skills"])


@pytest.mark.asyncio
async def test_http_commit_receipt_is_successful_while_each_workspace_keeps_its_effective_policy(skills_settings, tmp_path, monkeypatch):
    fixture = skills_settings
    root_a, root_b = tmp_path / "workspace-a", tmp_path / "workspace-b"
    root_a.mkdir()
    root_b.mkdir()
    override = ConfigLayer(ConfigLayerSource("session_flags"), {"skills": {"config": [{"name": "one", "enabled": False}]}})

    def stack(*, cwd=None, **_kwargs):
        layers = [ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "settings.json")), deepcopy(fixture.settings))]
        if cwd is not None and Path(cwd) == root_a:
            layers.append(override)
        return ConfigLayerStack(tuple(layers))

    monkeypatch.setattr("backend.config.load_config_layer_stack", stack)
    app = FastAPI()
    app.include_router(routes_skills.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        saved = await client.put("/api/skills/settings", json={"path": str(fixture.paths["one"]), "enabled": True, "workspace_root": str(root_a)})
        assert saved.status_code == 200
        assert saved.headers["Cache-Control"] == "no-store"
        assert next(skill for skill in saved.json()["skills"] if skill["name"] == "one")["enabled"] is False
        assert len(fixture.writes) == 1
        assert next(rule for rule in fixture.writes[0]["skills"]["config"] if rule["path"] == str(fixture.paths["one"].resolve()))["enabled"] is True
        current_b = await client.get("/api/skills/settings", params={"workspace_root": str(root_b)})
        assert current_b.status_code == 200 and current_b.json()["workspace_root"] == str(root_b)
        assert next(skill for skill in current_b.json()["skills"] if skill["name"] == "one")["enabled"] is True
        assert any(skill["name"] == "one" for skill in current_b.json()["available_skills"])


@pytest.mark.asyncio
async def test_configuration_hook_veto_keeps_the_skill_and_other_settings_unchanged(skills_settings, monkeypatch):
    from backend.hooks.runtime import ConfigChangeHookBlocked
    before = deepcopy(skills_settings.settings)
    async def blocked(**_kwargs):
        raise ConfigChangeHookBlocked("Explicit hook veto")
    monkeypatch.setattr(skills_api_service, "run_config_change_hook", blocked)
    with pytest.raises(HTTPException) as error:
        await routes_skills.update_skill_settings_api(routes_skills.SkillEnabledRequest(path=str(skills_settings.paths["one"]), enabled=False), Response())
    assert "Explicit hook veto" in error.value.detail
    assert skills_settings.settings == before
    assert skills_settings.writes == []


@pytest.mark.asyncio
async def test_disabled_user_skill_icon_stays_available_without_admitting_its_instructions(skills_settings):
    fixture = skills_settings
    icon = fixture.paths["two"].parent / "icon.svg"
    icon.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    metadata = icon.parent / "agents/openai.yaml"
    metadata.parent.mkdir()
    metadata.write_text('interface:\n  icon_small: "./icon.svg"\n', encoding="utf-8")
    snapshot = fixture.manager.snapshot()
    assert snapshot.resolve_asset(fixture.paths["two"], "small") == icon
    assert snapshot.load_skill_payload("two") is None


@pytest.mark.asyncio
async def test_plugin_skill_disable_keeps_details_and_reenable_while_removing_execution_roots(skills_settings):
    fixture = skills_settings
    path = fixture.paths["plugin-owned"]
    captured = fixture.manager.snapshot()
    result = await routes_skills.update_skill_settings_api(
        routes_skills.SkillEnabledRequest(path=str(path), enabled=False), Response())
    assert {skill["name"] for skill in result["skills"]} == {"one", "two"}
    assert [(skill["name"], skill["enabled"]) for skill in result["plugin_skills"]] == [("plugin-owned", False)]
    snapshot = fixture.manager.snapshot()
    assert snapshot.get_meta_by_path(path) is None
    assert snapshot.load_skill_payload("plugin-owned", path) is None
    assert path.parent.resolve() not in snapshot.readable_roots()
    detail = await routes_skills.get_skill_detail_api(Response(), skill_path=str(path))
    assert detail["enabled"] is False
    assert detail["content"] == "Real instructions"
    assert detail["raw_content"] == path.read_bytes().decode("utf-8-sig")
    assert captured.load_skill_payload("plugin-owned", path) is not None
    enabled = await routes_skills.update_skill_settings_api(
        routes_skills.SkillEnabledRequest(path=str(path), enabled=True), Response())
    assert enabled["plugin_skills"][0]["enabled"] is True
    assert fixture.manager.snapshot().load_skill_payload("plugin-owned", path) is not None


@pytest.mark.asyncio
async def test_catalog_detail_reads_complete_body_without_prompt_limit_or_frontmatter(skills_settings, monkeypatch):
    fixture = skills_settings
    path = fixture.paths["plugin-owned"]
    body = "# Plugin workflow\n\n" + "Long actual instructions.\n" * 500 + "\nFinal instruction."
    path.write_text("---\nname: plugin-owned\ndescription: Workflow\n---\n" + body, encoding="utf-8", newline="")
    original_search = SkillLoader._search_dirs
    def search(loader):
        loader._direct_skill_roots.add(loader._path_key(path.parent.parent))
        return original_search(loader)
    monkeypatch.setattr(SkillLoader, "_search_dirs", search)
    response = Response()
    detail = await routes_skills.get_skill_detail_api(response, skill_path=str(path))
    assert response.headers["Cache-Control"] == "no-store"
    assert detail["content"] == body
    assert len(detail["content"].encode("utf-8")) > 8_000
    assert detail["raw_content"] == path.read_bytes().decode("utf-8-sig")
    prompt = fixture.manager.snapshot().load_skill_payload("plugin-owned", path)
    assert len(prompt["content"].encode("utf-8")) == 8_000
    assert "Final instruction." not in prompt["content"]


@pytest.mark.asyncio
async def test_detail_rejects_existing_files_outside_registered_catalog(skills_settings, tmp_path):
    path = tmp_path / "unregistered" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("---\nname: unregistered\ndescription: Private\n---\nUnregistered content", encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        await routes_skills.get_skill_detail_api(Response(), skill_path=str(path))
    assert error.value.status_code == 404
    assert "Unregistered content" not in error.value.detail


@pytest.mark.asyncio
async def test_detail_keeps_original_markdown_frontmatter_and_line_endings_for_copy(skills_settings):
    path = skills_settings.paths["one"]
    original = '---\r\nname: one\r\ndescription: "Original description"\r\ncustom: preserve-me\r\n---\r\n# Original title\r\n\r\nOriginal body.\r\n'
    path.write_bytes(original.encode("utf-8"))
    detail = await routes_skills.get_skill_detail_api(Response(), skill_path=str(path))
    assert detail["raw_content"] == original
    assert detail["content"] == "# Original title\r\n\r\nOriginal body."
