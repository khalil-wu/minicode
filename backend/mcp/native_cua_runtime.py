"""Build the self-contained MiniCode CUA server for the receiving executor."""
from __future__ import annotations

import base64
from dataclasses import replace
import gzip
import json
import ntpath
from pathlib import Path
import sys

from backend.mcp.node_repl import _SERVER

ROOT = Path(__file__).parent
PROVIDER = "minicode-win32"


def is_native_cua(config):
    return config.env.get("MINICODE_CUA_PROVIDER") == PROVIDER


def build_native_bootstrap(*, python, app_access, native_source=None):
    source = native_source if native_source is not None else (ROOT / "native_cua_windows.py").read_text(encoding="utf-8")
    compressed = base64.b64encode(gzip.compress(source.encode(), mtime=0)).decode()
    python_source = "import base64,gzip;exec(gzip.decompress(base64.b64decode('" + compressed + "')))"
    options = {"python": python, "pythonSource": python_source, "appAccess": app_access}
    sources = {"api": (ROOT / "native_cua_api.mjs").read_text(encoding="utf-8"),
        "bridge": (ROOT / "native_cua_bridge.mjs").read_text(encoding="utf-8"), "node": _SERVER}
    # Compress actual source together once. Nesting base64 module URLs before
    # compression inflates the captured Windows execution wrapper past 32 KiB.
    module = ("const sources=" + json.dumps(sources, separators=(",", ":")) + ";\n"
        "const asModule=s=>'data:text/javascript;base64,'+Buffer.from(s).toString('base64');\n"
        "const options=" + json.dumps(options, separators=(",", ":")) + ";\n"
        "options.apiModule=asModule(sources.api);\n"
        "const {createNativeCuaExtension}=await import(asModule(sources.bridge));\n"
        "globalThis.__minicodeNodeReplExtension=({send})=>createNativeCuaExtension({...options,send});\n"
        "await import(asModule(sources.node));")
    payload = base64.b64encode(gzip.compress(module.encode(), mtime=0)).decode()
    return ("await import('data:text/javascript;base64,' + "
        "(await import('node:zlib')).gunzipSync(Buffer.from('" + payload
        + "', 'base64')).toString('base64'))")


def native_cua_config(selected, attachment, app_access):
    if selected.env["CUA_REPL_ENABLED_SURFACES"] != "computer":
        raise ValueError("MiniCode native CUA supplies only the computer surface")
    if attachment.peer_info["platformOs"] != "windows":
        raise ValueError("MiniCode native CUA requires a Windows receiving environment; no macOS/Linux adapter is bundled")
    if attachment.is_remote:
        python = selected.env["MINICODE_CUA_PYTHON"]
        if not ntpath.isabs(python):
            raise ValueError("MINICODE_CUA_PYTHON must be the absolute Python path on the receiving Windows host")
    else:
        python = sys.executable
    bootstrap = build_native_bootstrap(python=python, app_access=app_access)
    env = {**selected.env, **({"ELECTRON_RUN_AS_NODE": "1"} if not attachment.is_remote else {})}
    return replace(selected, args=["--expose-internals", "--input-type=module", "--eval", bootstrap], env=env)


class NativeCuaTurnControl:
    def __init__(self, client):
        self.client = client

    async def request(self, method, params):
        name = {"closeActiveTurn": "__minicode_close_cua_turn", "closeThread": "__minicode_close_cua_thread"}[method]
        result = await self.client._request("tools/call", {"name": name, "arguments": params})
        if result.get("isError"):
            raise ConnectionError("Native CUA close failed: " + str(result["content"]))
        return result["structuredContent"]["closed"]
