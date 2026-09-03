from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx

from backend.mcp.client import MCPClient
from backend.mcp.oauth import create_loopback_callback
from backend.secret_redaction import redact_secrets
from backend.services import llm_provider_helpers, llm_provider_service
from backend.ws.client_command_log import (
    ClientCommandDedupStore,
    cleanup_stale_client_command_logs,
)
from backend.ws.event_log import (
    WebSocketReplayEventStore,
    cleanup_stale_replay_logs,
)


def _anthropic_request(*, base_url: str, model: str = "claude-test") -> SimpleNamespace:
    section = SimpleNamespace(
        api_key="",
        base_url=base_url,
        model=model,
        model_metadata={},
        manual_models=[model],
    )
    return SimpleNamespace(provider="anthropic", anthropic=section)


def test_anthropic_refresh_and_check_resolve_key_for_incoming_base_url(monkeypatch) -> None:
    current = {
        "api_key": "old-endpoint-key",
        "base_url": "https://old.example/v1",
        "model": "old-model",
        "model_metadata": {},
    }
    resolved: list[tuple[str, str]] = []
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(llm_provider_service, "get_anthropic_settings", lambda: current)
    monkeypatch.setattr(
        llm_provider_service,
        "resolve_provider_api_key_for_base_url",
        lambda provider, base_url: resolved.append((provider, base_url)) or "scoped-new-key",
    )

    async def fetch_models(
        base_url: str,
        api_key: str,
        *,
        proxy_mode: str = "direct",
        headers: dict[str, str] | None = None,
        auth_header: bool = True,
    ) -> list[str]:
        calls.append(("models", base_url, api_key))
        return []

    async def check_generation(
        base_url: str,
        api_key: str,
        model: str,
        *,
        proxy_mode: str = "direct",
        headers: dict[str, str] | None = None,
        auth_header: bool = True,
    ) -> None:
        calls.append(("generation", base_url, api_key, model))

    refresh = asyncio.run(
        llm_provider_service.refresh_llm_models(
            _anthropic_request(base_url="https://new.example/v1"),
            fetch_anthropic_models=fetch_models,
        )
    )
    check = asyncio.run(
        llm_provider_service.check_llm_connection(
            _anthropic_request(base_url="https://new.example/v1"),
            fetch_anthropic_models=fetch_models,
            check_anthropic_generation=check_generation,
        )
    )

    assert refresh["source"] == "manual"
    assert check["ok"] is True
    assert resolved == [
        ("anthropic", "https://new.example/v1"),
        ("anthropic", "https://new.example/v1"),
    ]
    assert calls[0] == ("models", "https://new.example/v1", "scoped-new-key")
    assert calls[1] == ("models", "https://new.example/v1", "scoped-new-key")
    assert calls[2] == (
        "generation",
        "https://new.example/v1",
        "scoped-new-key",
        "claude-test",
    )


def test_provider_error_messages_use_codex_secret_redaction() -> None:
    response = httpx.Response(
        401,
        text='{"error":"invalid_api_key", "token":"abcdefghijklmno123"}',
        request=httpx.Request("GET", "https://provider.example/v1/models"),
    )
    error = httpx.HTTPStatusError("provider rejected request", request=response.request, response=response)

    message = llm_provider_helpers._http_error_message(error)

    assert "abcdefghijklmno123" not in message
    assert '"token":"[REDACTED_SECRET]"' in message
    assert redact_secrets("Authorization: Bearer abcdefghijklmnop") == (
        "Authorization: Bearer [REDACTED_SECRET]"
    )


def test_mcp_pagination_has_codex_catalog_and_cursor_limits() -> None:
    client = object.__new__(MCPClient)
    client.server_name = "bounded-server"

    async def too_many_items(_method, _params):
        return {"tools": [{"name": "tool"}] * 2049}

    client._request = too_many_items
    try:
        asyncio.run(client._paged("tools/list", "tools"))
    except ConnectionError as exc:
        assert "catalog limit" in str(exc)
    else:
        raise AssertionError("MCP catalog limit was not enforced")

    async def oversized_cursor(_method, _params):
        return {"tools": [], "nextCursor": "x" * (64 * 1024 + 1)}

    client._request = oversized_cursor
    try:
        asyncio.run(client._paged("tools/list", "tools"))
    except ConnectionError as exc:
        assert "pagination cursor" in str(exc)
    else:
        raise AssertionError("MCP cursor limit was not enforced")


def test_oauth_callback_is_bound_to_server_specific_route() -> None:
    async def scenario() -> None:
        callback = await create_loopback_callback(
            interactive=False,
            server_url="https://mcp.example.test/mcp",
        )
        try:
            port = callback.server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /callback?code=wrong&state=wrong HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            invalid_response = await reader.read()
            writer.close()
            await writer.wait_closed()
            assert b"400 Bad Request" in invalid_response
            assert not callback.future.done()

            callback_path = Path(callback.redirect_uri).name
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            request = (
                f"GET /callback/{callback_path}?code=good&state=verified HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            writer.write(request)
            await writer.drain()
            valid_response = await reader.read()
            writer.close()
            await writer.wait_closed()
            assert b"200 OK" in valid_response
            assert await callback.callback() == ("good", "verified")
        finally:
            await callback.close()

    asyncio.run(scenario())


def test_replay_and_client_command_logs_compact_and_expire(tmp_path: Path) -> None:
    replay_root = tmp_path / "replay"
    replay = WebSocketReplayEventStore(session_id="renderer", root_dir=replay_root)
    replay.append({"conversation_id": "keep", "type": "state"})
    replay.append({"conversation_id": "delete", "type": "state"})
    assert replay.delete_for_conversation("delete") == 1
    assert replay.load(limit=10) == [{"conversation_id": "keep", "type": "state"}]
    stale_path = replay_root / "stale.jsonl"
    stale_path.write_text(json.dumps({"conversation_id": "old"}) + "\n", encoding="utf-8")
    os.utime(stale_path, (80, 80))
    assert cleanup_stale_replay_logs(replay_root, now=100, max_age_seconds=10) == 1

    command_root = tmp_path / "commands"
    commands = ClientCommandDedupStore(session_id="renderer", root_dir=command_root)
    commands.append("one")
    commands.append("one")
    commands.append("two")
    assert commands.load_ids(limit=2) == ["one", "two"]
    assert len(commands.path.read_text(encoding="utf-8").splitlines()) == 2
    old_path = command_root / "old.jsonl"
    old_path.write_text('{"client_command_id":"old"}\n', encoding="utf-8")
    os.utime(old_path, (80, 80))
    assert cleanup_stale_client_command_logs(command_root, now=100, max_age_seconds=10) == 1


def test_client_command_log_preserves_corrupt_lines_and_recovers_partial_id(tmp_path: Path) -> None:
    store = ClientCommandDedupStore(session_id="renderer-corrupt", root_dir=tmp_path)
    store.append("complete")
    store.path.write_text(
        store.path.read_text(encoding="utf-8")
        + '{"client_command_id":"truncated"\n',
        encoding="utf-8",
    )
    before = store.path.read_text(encoding="utf-8")

    assert store.load_ids(limit=10) == ["complete", "truncated"]
    assert store.path.read_text(encoding="utf-8") == before
    assert store.last_load_error == {
        "path": str(store.path),
        "reason": "malformed_json",
        "line": "2",
    }
