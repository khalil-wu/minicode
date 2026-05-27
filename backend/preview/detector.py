"""Detect running dev servers by probing common ports on localhost."""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, asdict
from typing import Any

DEFAULT_PREVIEW_PORTS: tuple[tuple[int, str, str], ...] = (
    (3000, "Next.js / React", "next"),
    (3001, "Dev Server", "generic"),
    (4200, "Angular", "angular"),
    (4321, "Astro", "astro"),
    (5000, "Flask / Generic", "flask"),
    (5173, "Vite", "vite"),
    (5174, "Vite (alt)", "vite"),
    (8000, "Django / FastAPI", "django"),
    (8080, "Webpack / Generic", "webpack"),
    (8888, "Jupyter", "jupyter"),
    (9000, "PHP / Generic", "generic"),
)


@dataclass(frozen=True)
class PreviewServer:
    port: int
    url: str
    name: str
    framework: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_port_open(host: str, port: int, timeout: float = 0.15) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


async def detect_dev_servers(
    host: str = "127.0.0.1",
    ports: tuple[tuple[int, str, str], ...] = DEFAULT_PREVIEW_PORTS,
    timeout: float = 0.15,
) -> list[PreviewServer]:
    """Probe common dev server ports concurrently. Returns servers that responded."""
    loop = asyncio.get_running_loop()

    async def probe(port: int, name: str, framework: str) -> PreviewServer | None:
        ok = await loop.run_in_executor(None, _is_port_open, host, port, timeout)
        if not ok:
            return None
        return PreviewServer(
            port=port,
            url=f"http://{host}:{port}",
            name=name,
            framework=framework,
        )

    results = await asyncio.gather(*(probe(p, n, f) for p, n, f in ports))
    return [server for server in results if server is not None]
