from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

import backend.skills.marketplace as marketplace
from backend.main import app
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.skills.marketplace import (
    import_local_skill,
    install_marketplace_skill,
    list_extensions_marketplace,
    registry_server_to_marketplace_mcp,
    remove_user_skill,
)


def _write_skill(root, name: str, description: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            "---\n\n"
            f"# {name}\n"
        ),
        encoding="utf-8",
    )


def test_skill_loader_discovers_minicode_project_and_user_locations(monkeypatch, tmp_path) -> None:
    project_skills = tmp_path / ".minicode" / "skills"
    user_home = tmp_path / "user-minicode"
    _write_skill(project_skills, "project-review", "project review description")
    _write_skill(user_home / "skills", "user-review", "user review description")

    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(user_home))
    loader = SkillLoader(project_root=tmp_path)
    loader.discover()

    project_skill = loader.get_meta("project-review")
    user_skill = loader.get_meta("user-review")
    assert project_skill is not None
    assert project_skill.source_level == "workspace"
    assert user_skill is not None
    assert user_skill.source_level == "user"


def test_skill_loader_discovers_managed_minicode_skills_before_user(
    monkeypatch,
    tmp_path,
) -> None:
    managed_root = tmp_path / "managed-minicode"
    user_home = tmp_path / "user-minicode"
    _write_skill(managed_root / "skills", "policy-review", "managed policy")
    _write_skill(user_home / "skills", "user-review", "user skill")

    monkeypatch.setattr(
        "backend.skills.loader._get_managed_minicode_dir",
        lambda: managed_root,
    )
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(user_home))
    loader = SkillLoader(project_root=tmp_path)
    loader.discover()

    policy = loader.get_meta("policy-review")
    assert policy is not None
    assert policy.source_level == "managed"
    assert loader.list_skill_names().index("policy-review") < loader.list_skill_names().index("user-review")


def test_skill_frontmatter_controls_invocation_semantics(
    monkeypatch,
    tmp_path,
) -> None:
    user_home = tmp_path / "user-minicode"
    skill_dir = user_home / "skills" / "release"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: release\n"
            "description: Ship the current release safely\n"
            "disable-model-invocation: true\n"
            "user-invocable: false\n"
            "---\n\n"
            "# Ship the current release safely\n"
        ),
        encoding="utf-8",
    )
    nested = user_home / "skills" / "group"
    _write_skill(nested, "nested", "nested MiniCode skill")

    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(user_home))
    loader = SkillLoader(project_root=tmp_path)
    loader.discover()

    skill = loader.get_meta("release")
    assert skill is not None
    assert skill.description == "Ship the current release safely"
    assert skill.allow_implicit_invocation is False
    assert skill.user_invocable is False
    assert loader.get_meta("nested") is not None
    assert SkillManager(loader).detect("$release /release") == []


def test_skill_without_frontmatter_is_rejected(monkeypatch, tmp_path) -> None:
    user_home = tmp_path / "user-minicode"
    skill_dir = user_home / "skills" / "body-only"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n# Review the current change\n\nFollow the repository review workflow.\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(user_home))
    loader = SkillLoader(project_root=tmp_path)
    loader.discover()

    assert loader.get_meta("body-only") is None


def test_skill_with_invalid_frontmatter_is_rejected(monkeypatch, tmp_path) -> None:
    user_home = tmp_path / "user-minicode"
    skill_dir = user_home / "skills" / "invalid-meta"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: [not valid\n---\n\n# Recover this workflow\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(user_home))
    loader = SkillLoader(project_root=tmp_path)
    loader.discover()

    assert loader.get_meta("invalid-meta") is None


def test_duplicate_skill_name_requires_explicit_source_path(monkeypatch, tmp_path) -> None:
    user_home = tmp_path / "user-minicode"
    _write_skill(user_home / "skills", "same-name", "user skill")
    _write_skill(tmp_path / ".minicode" / "skills", "same-name", "project skill")

    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(user_home))
    loader = SkillLoader(project_root=tmp_path)
    loader.discover()

    matches = loader.get_metas("same-name")
    assert len(matches) == 2
    assert loader.get_invocation_meta("same-name") is None
    detection = SkillManager(loader).detect("/same-name")
    assert detection == []


def test_marketplace_defaults_to_minicode_private_skill_dir() -> None:
    assert marketplace.USER_SKILLS_DIR == marketplace.get_minicode_config_home_dir() / "skills"


def test_marketplace_network_catalog_respects_explicit_offline_mode(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_ENABLE_NETWORK_MARKETPLACE", "0")
    payload = asyncio.run(list_extensions_marketplace(force_refresh=True))
    assert len(payload["skills"]) == 39
    assert payload["source_status"]["openai_skills"]["source"] == "bundled"
    assert payload["source_status"]["mcp_registry"]["source"] == "disabled"


def test_marketplace_public_catalog_is_available_without_hidden_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("MINICODE_ENABLE_NETWORK_MARKETPLACE", raising=False)
    def catalog_response(url):
        assert "registry.modelcontextprotocol.io" in url
        return {"servers": []}
    monkeypatch.setattr("backend.skills.marketplace._default_fetch_json", catalog_response)
    payload = asyncio.run(list_extensions_marketplace(force_refresh=True))
    assert "pdf" in {skill["name"] for skill in payload["skills"]}
    assert payload["source_status"]["openai_skills"]["source"] == "bundled"
    assert payload["source_status"]["openai_skills"]["ok"] is True


def test_bundled_skill_catalog_uses_declared_metadata_and_existing_assets() -> None:
    catalog = json.loads(marketplace.PUBLIC_SKILLS_CATALOG.read_text(encoding="utf-8"))
    assert catalog["revision"] == "49f948faa9258a0c61caceaf225e179651397431"
    assert len(catalog["skills"]) == 39
    pdf = next(skill for skill in catalog["skills"] if skill["name"] == "pdf")
    assert pdf["title"] == "PDF Skill" and pdf["shortDescription"] == "Create, edit, and review PDFs"
    assert pdf["description"].startswith("Use when tasks involve reading, creating, or reviewing PDF files")
    assets = Path(__file__).resolve().parents[1] / "frontend/public"
    assert all((assets / skill["iconAsset"]).is_file() for skill in catalog["skills"] if "iconAsset" in skill)


def test_public_skill_detail_serves_the_actual_bundled_instructions() -> None:
    from fastapi import FastAPI
    from backend.api.routes_skills import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    response = client.get("/api/skills/marketplace/pdf")
    assert response.status_code == 200 and "When to use" in response.json()["content"]
    assert response.json()["raw_content"].startswith('---\nname: "pdf"')
    assert client.get("/api/skills/marketplace/not-in-catalog").status_code == 404


def test_import_local_skill_copies_only_real_skill_tree(tmp_path) -> None:
    source = tmp_path / "source" / "review"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: local-review\ndescription: 本地审查\n---\n# Review\n",
        encoding="utf-8",
    )
    target = tmp_path / "minicode-skills"
    result = import_local_skill(source, target)
    assert result["skill"]["name"] == "local-review"
    assert (target / "local-review" / "SKILL.md").is_file()


def test_remove_user_skill_deletes_only_installed_user_skill(tmp_path) -> None:
    _write_skill(tmp_path, "user-review-workflow", "user-installed skill")

    result = remove_user_skill("user-review-workflow", skills_dir=tmp_path)

    assert result["removed"] is True
    assert result["skill"]["name"] == "user-review-workflow"
    assert not (tmp_path / "user-review-workflow").exists()


def test_remove_user_skill_rejects_missing_or_unsafe_name(tmp_path) -> None:
    try:
        remove_user_skill("../user-review-workflow", skills_dir=tmp_path)
    except ValueError as exc:
        assert "safe directory name" in str(exc)
    else:
        raise AssertionError("remove_user_skill should reject traversal names")

    try:
        remove_user_skill("missing-skill", skills_dir=tmp_path)
    except FileNotFoundError as exc:
        assert "is not installed" in str(exc)
    else:
        raise AssertionError("remove_user_skill should reject missing skills")


def test_skill_install_api_returns_only_verified_upstream_skill_content(monkeypatch, tmp_path) -> None:
    async def install_verified(skill_name: str, *, skill_manager=None) -> dict:
        assert skill_name == "gh-fix-ci"
        return {
            "installed": True,
            "skill": {"name": "gh-fix-ci", "source": "openai"},
            "path": str(tmp_path / "gh-fix-ci" / "SKILL.md"),
            "skills": [],
        }

    monkeypatch.setattr("backend.api.routes_skills.install_skill_from_marketplace", install_verified)

    with TestClient(app) as client:
        response = client.post("/api/skills/install", json={"skill_name": "gh-fix-ci"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["installed"] is True
    assert payload["skill"]["name"] == "gh-fix-ci"
    assert payload["skill"]["source"] == "openai"
    assert isinstance(payload["skills"], list)


def test_skill_remove_api_deletes_installed_skill_and_refreshes_skill_list(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.skills.marketplace.USER_SKILLS_DIR", tmp_path)
    _write_skill(tmp_path, "user-ci-workflow", "user-installed skill")

    with TestClient(app) as client:
        response = client.delete("/api/skills/user-ci-workflow")

    assert response.status_code == 200
    payload = response.json()
    assert payload["removed"] is True
    assert payload["skill"]["name"] == "user-ci-workflow"
    assert not (tmp_path / "user-ci-workflow").exists()
    assert isinstance(payload["skills"], list)


def test_extensions_marketplace_aggregates_openai_skills_and_mcp_registry() -> None:
    async def fake_json(url: str) -> dict:
        if "registry.modelcontextprotocol.io" in url:
            return {
                "servers": [
                    {
                        "server": {
                            "name": "example.com/docs",
                            "title": "Example Docs MCP",
                            "description": "Search project documentation.",
                            "version": "1.2.3",
                            "remotes": [{"type": "streamable-http", "url": "https://example.com/mcp"}],
                        },
                        "_meta": {
                            "io.modelcontextprotocol.registry/official": {
                                "status": "active",
                                "isLatest": True,
                            }
                        },
                    }
                ],
                "metadata": {"count": 1},
            }
        raise AssertionError(f"unexpected json url {url}")

    payload = asyncio.run(
        list_extensions_marketplace(
            installed_names={"pdf"},
            installed_mcp_names={"example-com-docs"},
            fetch_json=fake_json,
            force_refresh=True,
        )
    )

    assert payload["source_status"]["openai_skills"]["source"] == "bundled"
    assert payload["source_status"]["mcp_registry"]["source"] == "live"
    assert next(skill for skill in payload["skills"] if skill["name"] == "pdf")["installed"] is True
    assert next(skill for skill in payload["skills"] if skill["name"] == "gh-fix-ci")["installed"] is False
    registry_entry = next(item for item in payload["mcp"] if item["name"] == "example.com/docs")
    assert registry_entry["actionable"] is True
    assert registry_entry["provider_name"] == "example-com-docs"
    assert registry_entry["installed"] is True
    assert registry_entry["website_url"] == "https://example.com/mcp"
    assert registry_entry["config_snippet"]["server"]["transport"] == "http"
    assert registry_entry["config_snippet"]["server"]["url"] == "https://example.com/mcp"
    assert registry_entry["config_snippet"]["server"]["auto_start"] is False


def test_skill_catalog_does_not_wait_for_the_mcp_registry(monkeypatch) -> None:
    from backend.services.skills_api_service import skills_marketplace_payload

    async def hanging_registry(_url):
        await asyncio.sleep(10)
    monkeypatch.setattr(marketplace, "_default_fetch_json", hanging_registry)
    payload = asyncio.run(asyncio.wait_for(skills_marketplace_payload(skill_manager=None), timeout=1))
    assert payload["source_status"]["openai_skills"]["source"] == "bundled"
    assert any(skill["name"] == "pdf" for skill in payload["skills"])


def test_registry_package_only_server_is_preview_not_fake_action() -> None:
    entry = registry_server_to_marketplace_mcp(
        {
            "server": {
                "name": "example.com/package-only",
                "title": "Package Only",
                "description": "Requires manual package setup.",
                "packages": [{"registry_name": "npm", "name": "@example/mcp"}],
            }
        },
        installed_names=set(),
    )

    assert entry["actionable"] is False
    assert entry["config_snippet"] is None
    assert entry["setup_mode"] == "manual"


def test_marketplace_reports_named_timeout_without_fabricating_connectors() -> None:
    async def fake_json(url: str) -> dict | list[dict]:
        if "api.github.com" in url:
            return []
        raise TimeoutError()

    payload = asyncio.run(
        list_extensions_marketplace(
            fetch_json=fake_json,
            force_refresh=True,
        )
    )

    assert payload["source_status"]["mcp_registry"]["ok"] is False
    assert payload["source_status"]["mcp_registry"]["error"] == "TimeoutError"
    assert payload["source_status"]["mcp_registry"]["source"] == "unavailable"
    assert payload["mcp"] == []


def test_install_marketplace_skill_preserves_complete_bundle(monkeypatch, tmp_path) -> None:
    import zipfile
    from backend.plugins.materializer import materialize_source

    bundle = tmp_path / "skill.zip"
    files = {
        "SKILL.md": "---\nname: gh-fix-ci\ndescription: Fix CI failures.\n---\n# Fix CI\n",
        "scripts/check.py": "print('check')\n",
        "references/checks.md": "Reference content\n",
        "assets/icon.svg": "<svg/>\n",
        "agents/openai.yaml": "interface:\n  display_name: Fix CI\n",
    }
    with zipfile.ZipFile(bundle, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)

    def staged_bundle(source, destination, **kwargs):
        assert source == {"source": "github", "repo": "openai/skills", "sha": "49f948faa9258a0c61caceaf225e179651397431", "path": "skills/.curated/gh-fix-ci"}
        return materialize_source({"source": "file", "path": str(bundle)}, destination, **kwargs)

    monkeypatch.setattr("backend.skills.marketplace.materialize_source", staged_bundle)
    result = asyncio.run(install_marketplace_skill("gh-fix-ci", skills_dir=tmp_path))

    assert result["installed"] is True
    assert result["skill"]["source"] == "openai"
    assert (tmp_path / "gh-fix-ci" / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: gh-fix-ci")
    for name, content in files.items():
        assert (tmp_path / "gh-fix-ci" / name).read_text(encoding="utf-8") == content


def test_marketplace_skill_validation_precedes_activation(monkeypatch, tmp_path) -> None:
    import zipfile
    import pytest
    from backend.plugins.materializer import materialize_source

    bundle = tmp_path / "invalid.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("scripts/tool.py", "pass\n")
    monkeypatch.setattr("backend.skills.marketplace.materialize_source", lambda _source, destination, **kwargs: materialize_source({"source": "file", "path": str(bundle)}, destination, **kwargs))
    with pytest.raises(FileNotFoundError):
        asyncio.run(install_marketplace_skill("missing-manifest", skills_dir=tmp_path / "skills"))
    assert not (tmp_path / "skills" / "missing-manifest").exists()


def test_extensions_marketplace_api_exposes_unified_payload(monkeypatch) -> None:
    async def fake_marketplace(installed_names=None, **_kwargs):
        return {
            "skills": [],
            "mcp": [],
            "source_status": {
                "openai_skills": {"source": "fallback", "ok": True, "count": 0},
                "mcp_registry": {"source": "fallback", "ok": True, "count": 0},
            },
            "generated_at": "2026-04-26T00:00:00Z",
        }

    monkeypatch.setattr("backend.api.routes_skills.list_extensions_marketplace", fake_marketplace)

    with TestClient(app) as client:
        response = client.get("/api/extensions/marketplace")

    assert response.status_code == 200
    payload = response.json()
    assert payload["skills"] == []
    assert payload["mcp"] == []
    assert "source_status" in payload
