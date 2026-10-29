import httpx
import pytest

from backend.main import app


@pytest.mark.asyncio
async def test_frontend_public_assets_are_served_by_backend():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        icon = await client.get("/apps/menu-settings.svg")
        pet = await client.get("/pets/codex.webp")
        missing = await client.get("/apps/missing.svg")
    assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/svg+xml")
    assert pet.status_code == 200 and pet.headers["content-type"].startswith("image/webp")
    assert missing.status_code == 404
