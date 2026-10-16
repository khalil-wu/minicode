"""Actual MCP SDK consumption through the original executor capabilities."""
from __future__ import annotations

import base64
from datetime import timedelta
import json
import os
import sys
from types import SimpleNamespace

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import pytest

from backend.environments import EnvironmentManager
from backend.mcp.executor_http import executor_mcp_http_client_factory
from backend.mcp.executor_stdio import executor_stdio_client
from backend.mcp.environment_runtime import remote_mcp_environment_policy
from backend.skills.environment_catalog import (
    ExecutorCapabilityRoot, ExecutorRootDiscovery, ExecutorSkillCatalog, discover_executor_roots,
)
from backend.tests.test_environments_http import _config, _delta, _environment, _reply


@pytest.mark.asyncio
@pytest.mark.parametrize("bearer_env", [None, "EXECUTOR_TOKEN"])
async def test_mcp_sdk_http_preserves_protocol_cookie_session_and_original_executor(tmp_path, bearer_env):
    methods = []

    async def handler(socket, message):
        params = message["params"]
        if params["method"] == "GET":
            await _reply(socket, message, status=405)
            await _delta(socket, params["requestId"], 1, done=True)
            return
        if params["method"] == "DELETE":
            await _reply(socket, message, status=204)
            await _delta(socket, params["requestId"], 1, done=True)
            return
        body = json.loads(base64.b64decode(params["bodyBase64"]))
        methods.append(body["method"])
        if "id" not in body:
            await _reply(socket, message, status=202)
            await _delta(socket, params["requestId"], 1, done=True)
            return
        if body["method"] == "initialize":
            result = {"protocolVersion": body["params"]["protocolVersion"], "capabilities": {"tools": {}},
                "serverInfo": {"name": "actual-executor-mcp", "version": "1"}}
            headers = [{"name": "mcp-session-id", "value": "original-session"},
                {"name": "set-cookie", "value": "owner=original; Path=/"}]
        elif body["method"] == "tools/list":
            result = {"tools": [{"name": "echo", "description": "Actual executor echo", "inputSchema": {
                "type": "object", "properties": {"text": {"type": "string"}}}}]}
            headers = []
        else:
            assert body["method"] == "tools/call"
            header_map = {header["name"].lower(): header for header in params["headers"]}
            assert header_map["mcp-session-id"]["value"] == "original-session"
            assert header_map["cookie"]["value"] == "owner=original"
            assert header_map["x-owner"]["value"] == "captured-sdk"
            if bearer_env is not None:
                assert header_map["authorization"] == {"name": "authorization", "value": "Bearer ", "valueEnvVar": bearer_env}
            result = {"content": [{"type": "text", "text": body["params"]["arguments"]["text"]}]}
            headers = []
        encoded = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}, ensure_ascii=False).encode()
        await _delta(socket, params["requestId"], 1, encoded[:7])
        await _reply(socket, message, headers=[{"name": "content-type", "value": "application/json"}, *headers])
        await _delta(socket, params["requestId"], 2, encoded[7:], done=True)

    async with _environment(tmp_path, handler) as (manager, original, requests):
        lease = manager.borrow("remote")
        factory = executor_mcp_http_client_factory(lease, bearer_token_env_var=bearer_env)
        try:
            async with streamablehttp_client("https://executor.invalid/mcp", headers={"x-owner": "captured-sdk"},
                httpx_client_factory=factory) as streams:
                async with ClientSession(streams[0], streams[1], read_timeout_seconds=timedelta(seconds=5)) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "actual-executor-mcp"
                    manager.upsert({"id": "remote", "program": sys.executable, "args": ["-c", "pass"]})
                    assert manager.get_environment("remote") is not original
                    assert (await session.list_tools()).tools[0].name == "echo"
                    result = await session.call_tool("echo", {"text": "实际中文 😀"})
                    assert result.content[0].text == "实际中文 😀"
            assert not original.client.http_streams and original.client.http_queued_bytes == 0
        finally:
            await lease.aclose()
        assert original.closed
        assert {"initialize", "notifications/initialized", "tools/list", "tools/call"} <= set(methods)
        assert not any(message["method"] == "http/cancel" for message in requests)


MCP_STDIO_SERVER = '''import json,sys
while True:
    line=sys.stdin.buffer.readline()
    if not line: break
    message=json.loads(line)
    if "id" not in message: continue
    method=message["method"]
    if method=="initialize":
        result={"protocolVersion":message["params"]["protocolVersion"],"capabilities":{"tools":{}},"serverInfo":{"name":"source-stdio-sdk","version":"1"}}
    elif method=="tools/list":
        result={"tools":[{"name":"finish","inputSchema":{"type":"object","properties":{}}}]}
    else:
        result={"content":[{"type":"text","text":"真实尾行中文 😀"}]}
        sys.stderr.buffer.write(b"stderr-is-not-protocol\\n"); sys.stderr.buffer.flush()
    body=json.dumps({"jsonrpc":"2.0","id":message["id"],"result":result},ensure_ascii=False).encode()
    if method=="tools/call":
        sys.stdout.buffer.write(body);sys.stdout.buffer.flush();break
    sys.stdout.buffer.write(body+b"\\r\\n");sys.stdout.buffer.flush()
'''


def _native_manager(tmp_path):
    home = tmp_path / "source-home"
    home.mkdir()
    _config(tmp_path, {"program": os.environ["MINICODE_SOURCE_EXECUTOR"],
        "args": ["exec-server", "--listen", "stdio", "--concurrent-requests", "8"],
        "cwd": str(tmp_path), "env": {"CODEX_HOME": str(home)}})
    return EnvironmentManager(tmp_path)


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("MINICODE_SOURCE_EXECUTOR"), reason="requires explicit owned Source executor")
async def test_source_executor_stdio_drives_real_mcp_sdk_and_final_unterminated_unicode(tmp_path):
    server = tmp_path / "mcp_server.py"
    server.write_text(MCP_STDIO_SERVER, encoding="utf-8")
    manager = _native_manager(tmp_path)
    lease = manager.borrow("remote")
    try:
        async with executor_stdio_client(lease, [sys.executable, str(server)], tmp_path.as_uri(), env={},
            env_policy=remote_mcp_environment_policy(())) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=10)) as session:
                assert (await session.initialize()).serverInfo.name == "source-stdio-sdk"
                assert (await session.list_tools()).tools[0].name == "finish"
                result = await session.call_tool("finish", {})
                assert result.content[0].text == "真实尾行中文 😀"
        assert not lease.environment.client.processes
    finally:
        await lease.aclose()
        receipts = await manager.close()
    assert receipts[0][-1].exit_code == 0 and not receipts[0][-1].forced


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("MINICODE_SOURCE_EXECUTOR"), reason="requires explicit owned Source executor")
async def test_source_executor_stdio_delivers_original_parse_error_to_sdk_instead_of_silently_dropping_frame(tmp_path, caplog):
    peer = tmp_path / "malformed_mcp.py"
    peer.write_bytes(b'import sys\nsys.stdout.buffer.write(b\'broken-json\\n{"jsonrpc":"2.0","method":"notifications/owned"}\\n\');sys.stdout.buffer.flush()\n')
    manager = _native_manager(tmp_path)
    lease = manager.borrow("remote")
    try:
        async with executor_stdio_client(lease, [sys.executable, str(peer)], tmp_path.as_uri(), env={},
            env_policy=remote_mcp_environment_policy(())) as streams:
            error = await streams[0].receive()
            message = await streams[0].receive()
            assert isinstance(error, ValueError)
            assert message.message.root.method == "notifications/owned"
            assert "Failed to parse remote MCP server message" in caplog.text
    finally:
        await lease.aclose()
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("high_level", [False, True])
@pytest.mark.skipif(not os.environ.get("MINICODE_SOURCE_EXECUTOR"), reason="requires explicit owned Source executor")
async def test_source_executor_skill_discovery_reads_files_in_original_environment(tmp_path, high_level):
    root_dir = tmp_path / "selected-skills"
    skill_dir = root_dir / "exact"
    (skill_dir / "agents").mkdir(parents=True)
    original = "---\nname: exact\ndescription: Executor original Skill\n---\n完整原指令 😀\n"
    (skill_dir / "SKILL.md").write_bytes(original.encode("utf-8"))
    (skill_dir / "agents/openai.yaml").write_text("policy:\n  allow_implicit_invocation: false\n", encoding="utf-8")
    (skill_dir / "reference.txt").write_text("原环境引用", encoding="utf-8")
    manager = _native_manager(tmp_path)
    lease = manager.borrow("remote")
    try:
        info = await lease.environment.info()
        root = ExecutorCapabilityRoot("selected", "remote", root_dir.as_uri(), lease.environment, None,
            info.get("platformOs"), high_level, False)
        discoveries = await discover_executor_roots((root,))
        catalog = ExecutorSkillCatalog(discoveries)
        assert not catalog.warnings
        assert len(catalog.entries) == 1
        entry = catalog.entries[0]
        assert entry.name == "exact" and entry.environment_id == "remote" and not entry.allow_implicit_invocation
        assert catalog.list_page()["skills"] == []
        catalog.validate_required_skills([SimpleNamespace(environment_id="remote", skill_names=("exact",))])
        assert (await catalog.read(entry.package))[2] == original
        assert (await catalog.read(entry.package, entry.package + "/reference.txt"))[2] == "原环境引用"
        assert not lease.environment.client.files
    finally:
        await lease.aclose()
        receipts = await manager.close()
    assert receipts[0][-1].exit_code == 0 and not receipts[0][-1].forced


def _catalog(*, enabled=True, environment_id="remote", name="exact", body="完整 😀\n" * 40):
    root = ExecutorCapabilityRoot("selected", environment_id, "file:///executor/skills", None, None, "linux", True, False)
    document = "---\nname: " + name + "\ndescription: Actual catalog\n---\n" + body
    discovery = ExecutorRootDiscovery(root, {"id": root.id, "path": root.path,
        "skills": [{"instructions": {"path": "file:///executor/skills/exact/SKILL.md", "contents": document}}]})
    return ExecutorSkillCatalog((discovery,), disabled_paths={environment_id: {"file:///executor/skills/exact/SKILL.md"}} if not enabled else {}), document


@pytest.mark.parametrize("environment_id,name,enabled", [("other", "exact", True), ("remote", "EXACT", True), ("remote", "exact", False)])
def test_required_skill_gate_uses_exact_enabled_environment_source(environment_id, name, enabled):
    catalog, _ = _catalog(environment_id=environment_id, name=name, enabled=enabled)
    with pytest.raises(ValueError, match='Required skill "exact" from environment "remote" is unavailable'):
        catalog.validate_required_skills([SimpleNamespace(environment_id="remote", skill_names=("exact",))])


@pytest.mark.asyncio
async def test_executor_skill_pages_keep_complete_utf8_original_and_forbid_resource_escape():
    catalog, document = _catalog()
    entry = catalog.entries[0]
    cursor, parts = None, []
    while True:
        page = await catalog.read_page(entry.package, cursor=cursor, response_byte_budget=260)
        parts.append(page["contents"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert "".join(parts) == document
    with pytest.raises(ValueError, match="does not match its package"):
        await catalog.read(entry.package, entry.package + "/../outside")
    with pytest.raises(ValueError, match="unavailable"):
        await catalog.read("skill://other/executor/skills/exact")
