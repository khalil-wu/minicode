import asyncio
import logging
import os
import sys


logger = logging.getLogger(__name__)
DEFAULT_WS_MAX_SIZE_BYTES = 1024 * 1024
# The desktop client owns liveness with an application-level ping/pong and
# reconnect/replay loop. Keep Uvicorn's transport ping disabled by default so
# there is one timeout authority; deployments may opt in explicitly.
DEFAULT_WS_PING_INTERVAL_SECONDS: float | None = None


def _should_enable_reload() -> bool:
    return not sys.platform.startswith("win")


def _resolve_backend_host() -> str:
    requested = os.environ.get("MINICODE_BACKEND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    token = os.environ.get("MINICODE_RUNTIME_TOKEN", "").strip()
    if not token and requested not in {"127.0.0.1", "localhost", "::1"}:
        logger.warning(
            "MINICODE_RUNTIME_TOKEN is not set; binding backend to 127.0.0.1 instead of %s",
            requested,
        )
        return "127.0.0.1"
    return requested


def _resolve_ws_ping_interval() -> float | None:
    raw = os.environ.get("MINICODE_WS_PING_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_WS_PING_INTERVAL_SECONDS
    if raw.lower() in {"0", "none", "off", "disabled", "false"}:
        return None
    return float(raw)


if __name__ == "__main__":
    # Windows 上必须使用 ProactorEventLoop 才能支持 asyncio.create_subprocess_exec
    # 必须在 uvicorn.run() 之前设置，否则 uvicorn 可能用 SelectorEventLoop
    if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    import uvicorn

    host = _resolve_backend_host()
    port = int(os.environ.get("MINICODE_BACKEND_PORT", "8000"))
    ws_max_size = int(os.environ.get("MINICODE_WS_MAX_SIZE_BYTES", str(DEFAULT_WS_MAX_SIZE_BYTES)))
    ws_ping_interval = _resolve_ws_ping_interval()

    uvicorn.run(
        "backend.main:app",
        host=host,
        port=port,
        reload=_should_enable_reload(),
        ws_max_size=ws_max_size,
        ws_ping_interval=ws_ping_interval,
        access_log=False,
    )
