"""The captured agent turn which owns embedded-browser control and downloads."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
from backend.tools.browser_support import BROWSER_OPERATION_SECONDS


class EmbeddedBrowserExecution:
    def __init__(self, turn_id: str):
        self.control_id = "browser_control_" + uuid4().hex
        self.turn_id = turn_id
        self.scopes = {}
        self.pending = set()
        self.receipts = {}

    def bind(self, endpoint: str, headers: dict, owner: dict) -> None:
        key = (endpoint, owner["conversation_id"], owner["window_instance_id"], owner["window_binding_token"], headers["authorization"])
        self.scopes[key] = (endpoint, headers, owner)

    async def run(self, operation):
        task = asyncio.current_task()
        self.pending.add(task)
        try:
            return await operation
        finally:
            self.pending.discard(task)

    def retain(self, task: asyncio.Task, operation_id: str, receipt: dict) -> None:
        self.receipts[operation_id] = receipt
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def finish_after(self, consumers: tuple[asyncio.Task, ...]) -> None:
        results = []
        while pending := {task for task in (*consumers, *self.pending) if not task.done()}:
            results.extend(await asyncio.gather(*(asyncio.shield(task) for task in pending), return_exceptions=True))
        unconfirmed = [operation_id for operation_id, receipt in self.receipts.items() if receipt["pending"]]
        if unconfirmed:
            raise RuntimeError("Original browser cleanup is not confirmed: " + ", ".join(unconfirmed))
        for endpoint, headers, owner in self.scopes.values():
            async with httpx.AsyncClient(timeout=BROWSER_OPERATION_SECONDS, follow_redirects=False, trust_env=False) as client:
                response = await client.post(f"{endpoint.rstrip('/')}/v1/session-control", headers=headers,
                    json={**owner, "action": "release", "agent_control_id": self.control_id, "agent_turn_id": self.turn_id})
                response.raise_for_status()
                result = response.json()
                if result["ok"] is not True:
                    raise RuntimeError(result["error"])
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("Owned browser operations did not settle successfully", errors)
