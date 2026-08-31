from __future__ import annotations

import asyncio

from backend.tools import image_generation_tool


def test_generate_image_tool_preserves_the_active_profile_proxy_mode(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Adapter:
        def __init__(self, settings) -> None:
            captured["settings"] = settings

        async def generate_images(self, _prompt: str, **_kwargs):
            return [("aW1hZ2U=", "image/png")]

        async def aclose(self) -> None:
            captured["closed"] = True

    tool = image_generation_tool.GenerateImageTool()
    monkeypatch.setattr(image_generation_tool, "OpenAIAdapter", _Adapter)
    monkeypatch.setattr(
        tool,
        "_settings_for_context",
        lambda _context=None: {
            "enabled": True,
            "provider": "custom",
            "api_key": "test-key",
            "base_url": "https://images.example/v1",
            "model": "image-model",
            "proxy_mode": "direct",
            "default_headers": (("X-Tenant", "acme"),),
            "auth_header": True,
            "size": "1024x1024",
            "quality": "",
        },
    )

    result = asyncio.run(tool.execute({"prompt": "Draw a cat"}))

    settings = captured["settings"]
    assert settings.proxy_mode == "direct"
    assert settings.default_headers == (("X-Tenant", "acme"),)
    assert settings.auth_header is True
    assert result.is_error is False
    assert result.images == [{"data": "aW1hZ2U=", "media_type": "image/png"}]
    assert captured["closed"] is True
