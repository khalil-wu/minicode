"""Response compression scoped to the capability directory."""
from __future__ import annotations

from starlette.middleware.gzip import GZipMiddleware

STATUS_PATH = "/api/status"


class StatusGzipMiddleware(GZipMiddleware):
    """Compress only ``/api/status``.

    That route carries the full tool catalogue (about 200 KB of JSON that
    compresses roughly tenfold). Streaming and media routes keep their exact
    bytes and Range semantics, so compression stays scoped to this one path.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and str(scope.get("path") or "").rstrip("/") == STATUS_PATH:
            await super().__call__(scope, receive, send)
            return
        await self.app(scope, receive, send)
