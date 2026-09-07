from __future__ import annotations

import json
import sys


def send(payload: dict) -> None:
    print(json.dumps({"jsonrpc": "2.0", **payload}), flush=True)


def catalog(request_id: int, name: str, request_count: int) -> None:
    send({
        "id": request_id,
        "result": {
            "tools": [{
                "name": name,
                "description": f"catalog request {request_count}",
                "inputSchema": {"type": "object"},
            }],
        },
    })


def main() -> None:
    request_count = 0
    pending: tuple[int, str, int] | None = None
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        if method == "initialize":
            send({
                "id": message["id"],
                "result": {
                    "protocolVersion": message["params"]["protocolVersion"],
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "catalog-fixture", "version": "1.0"},
                },
            })
        elif method == "tools/list":
            request_count += 1
            if pending is not None:
                send({
                    "id": message["id"],
                    "error": {"code": -32603, "message": "concurrent catalog requests"},
                })
            elif request_count in (1, 2):
                name = "startup_stale" if request_count == 1 else "refresh_stale"
                pending = (message["id"], name, request_count)
                send({"method": "notifications/tools/list_changed"})
            elif request_count == 3:
                send({
                    "id": message["id"],
                    "error": {"code": -32603, "message": "temporary catalog error"},
                })
                send({"method": "notifications/tools/list_changed"})
            else:
                catalog(message["id"], "fresh_tool", request_count)
        elif method == "ping":
            if pending is not None:
                catalog(*pending)
                pending = None
            send({"id": message["id"], "result": {}})


if __name__ == "__main__":
    main()
