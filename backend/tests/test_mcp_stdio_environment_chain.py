from __future__ import annotations

import sys
import json

import pytest

from backend.mcp.client import MCPClient


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_real_stdio_server_receives_only_its_configured_pythonpath(tmp_path, configured):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    module_dir = tmp_path / "configured-modules"
    module_dir.mkdir()
    (module_dir / "fixture_mcp_marker.py").write_text("value = 42\n", encoding="utf-8")
    server = workspace / "server.py"
    server.write_text('''
import importlib.util
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP
server = FastMCP("environment fixture")
@server.tool()
def inspect_environment() -> dict:
    return {"pythonpath": os.environ.get("PYTHONPATH"), "cwd": str(Path.cwd()),
            "marker_found": importlib.util.find_spec("fixture_mcp_marker") is not None}
if __name__ == "__main__":
    server.run(transport="stdio")
''', encoding="utf-8")
    explicit_env = {"PYTHONPATH": str(module_dir)} if configured else {}
    client = MCPClient("environment-fixture", command=sys.executable, args=[str(server)],
        cwd=str(workspace), workspace_root=workspace, env=explicit_env, startup_timeout=20)
    try:
        await client.connect()
        result = await client.call_tool("inspect_environment", {})
        assert not result.is_error
        actual = result.structured_content or json.loads(result.text)
        assert actual["cwd"] == str(workspace)
        assert actual["pythonpath"] == (str(module_dir) if configured else None)
        assert actual["marker_found"] is configured
    finally:
        await client.close()
