import httpx
import mimetypes
import pytest

from backend.main import app


@pytest.mark.asyncio
async def test_frontend_public_assets_are_served_by_backend(monkeypatch):
    mimetypes.init()
    monkeypatch.setitem(mimetypes.types_map, ".svg", "image/svg")
    monkeypatch.setitem(mimetypes.types_map, ".webp", "application/octet-stream")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        icon = await client.get("/apps/menu-settings.svg")
        pet = await client.get("/pets/codex.webp")
        theme = await client.get("/theme-boot.js")
        missing = await client.get("/apps/missing.svg")
        unchanged = await client.get("/apps/menu-settings.svg", headers={"If-None-Match": icon.headers["etag"]})
        partial = await client.get("/apps/menu-settings.svg", headers={"Range": "bytes=0-9"})
    assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/svg+xml")
    assert pet.status_code == 200 and pet.headers["content-type"].startswith("image/webp")
    assert theme.status_code == 200 and theme.headers["content-type"].startswith("application/javascript")
    assert missing.status_code == 404
    assert unchanged.status_code == 304
    assert partial.status_code == 206 and partial.content == icon.content[:10]
    assert partial.headers["content-type"] == "image/svg+xml"
