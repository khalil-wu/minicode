"""Protocol-boundary validation for concurrent navigation and cancellation."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.lsp import client as lsp


class _Writer:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.written = asyncio.Event()
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise BrokenPipeError("peer closed")
        header, body = data.split(b"\r\n\r\n", 1)
        assert header == f"Content-Length: {len(body)}".encode()
        self.messages.append(json.loads(body))
        self.written.set()

    async def drain(self) -> None:
        await asyncio.sleep(0)


def _client(tmp_path: Path) -> tuple[lsp.LSPClient, _Writer]:
    writer = _Writer()
    client = lsp.LSPClient("unused", [], str(tmp_path))
    client._stdin = writer
    client._process = SimpleNamespace(returncode=None)
    return client, writer


def test_parallel_navigation_opens_once_and_updates_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        client, writer = _client(tmp_path)
        source = tmp_path / "source.py"
        source.write_text("value = 1\n", encoding="utf-8")
        await asyncio.gather(*(client._ensure_file_open(str(source)) for _ in range(3)))
        source.write_text("value = 2\n", encoding="utf-8")
        await asyncio.gather(*(client._ensure_file_open(str(source)) for _ in range(3)))
        await client.close_file(str(source))
        assert [message["method"] for message in writer.messages] == [
            "textDocument/didOpen", "textDocument/didChange", "textDocument/didClose",
        ]
        changed = writer.messages[1]["params"]
        assert changed["textDocument"]["version"] == 2
        assert changed["contentChanges"] == [{"text": "value = 2\n"}]
        assert client._opened_files == {}
    asyncio.run(scenario())


@pytest.mark.parametrize("peer_closed", [False, True])
def test_request_cancellation_notifies_peer_and_preserves_cancel(
    tmp_path: Path, peer_closed: bool,
) -> None:
    async def scenario() -> None:
        client, writer = _client(tmp_path)
        task = asyncio.create_task(client._send_request("textDocument/hover", {}))
        await writer.written.wait()
        writer.closed = peer_closed
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client._pending == {}
        if not peer_closed:
            assert writer.messages[1] == {
                "jsonrpc": "2.0", "method": "$/cancelRequest",
                "params": {"id": writer.messages[0]["id"]},
            }
    asyncio.run(scenario())


def test_timed_out_request_notifies_peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wait_for = asyncio.wait_for

    async def short_timeout(future, *, timeout):
        return await wait_for(future, timeout=0.001)

    monkeypatch.setattr(lsp.asyncio, "wait_for", short_timeout)

    async def scenario() -> None:
        client, writer = _client(tmp_path)
        with pytest.raises(RuntimeError, match="LSP request timed out"):
            await client._send_request("textDocument/references", {})
        assert client._pending == {}
        assert writer.messages[1]["method"] == "$/cancelRequest"
        assert writer.messages[1]["params"]["id"] == writer.messages[0]["id"]
    asyncio.run(scenario())


def test_network_file_uri_preserves_its_authority() -> None:
    expected = "//server/share/source file.py"
    if os.name == "nt":
        expected = expected.replace("/", "\\")
    assert lsp._uri_to_path("file://server/share/source%20file.py") == expected
