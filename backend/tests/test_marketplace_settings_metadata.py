from __future__ import annotations

import json

import pytest
from fastapi import Response

from backend.api import routes_skills
from backend.plugins.manager import MarketplaceRegistry
from backend.plugins.materializer import MaterializationError


def test_registered_marketplace_preserves_manifest_display_name_through_refresh(tmp_path):
    source = tmp_path / "source"
    manifest = source / ".agents/plugins/marketplace.json"
    manifest.parent.mkdir(parents=True)
    def write(display_name):
        manifest.write_text(json.dumps({"name": "team", "interface": {"displayName": display_name}, "plugins": []}), encoding="utf-8")
    write("Team tools")
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    record, already = registry.add_source(str(source))
    assert not already
    assert record["interface"]["displayName"] == "Team tools"
    assert registry.list()[0]["materialized_path"]
    write("Updated team tools")
    refreshed = registry.refresh("team")
    assert refreshed["interface"]["displayName"] == "Updated team tools"
    assert registry.list()[0]["interface"]["displayName"] == "Updated team tools"


def test_malformed_marketplace_ui_metadata_is_reported_before_registration(tmp_path):
    manifest = tmp_path / "source/.agents/plugins/marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "team", "interface": {"displayName": ["invalid"]}, "plugins": []}), encoding="utf-8")
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    with pytest.raises(MaterializationError, match="interface.displayName"):
        registry.add_source(str(manifest.parents[2]))
    assert registry.list() == []


@pytest.mark.asyncio
async def test_removal_runtime_retry_reports_the_actual_refresh_result(monkeypatch):
    actual = {"ok": False, "errors": ["Owned MCP process did not stop"], "warnings": [], "sessions": []}
    async def refresh():
        return actual
    monkeypatch.setattr(routes_skills, "_refresh_plugin_runtime_state", refresh)
    response = Response()
    result = await routes_skills.refresh_plugin_runtime_api(response)
    assert result == {"runtime_refresh": actual}
    assert response.headers["Cache-Control"] == "no-store"
