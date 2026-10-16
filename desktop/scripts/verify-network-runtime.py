"""Verify the packaged Source network and explicit Windows readiness contracts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
from backend.network.native import NativeNetworkRuntime, SOURCE_COMMIT

runtime = NativeNetworkRuntime()
manifest = json.loads((runtime.directory / "runtime.json").read_text(encoding="utf-8"))
for name, expected in manifest["files"].items():
    actual = hashlib.sha256((runtime.directory / name).read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError(f"Packaged network runtime file differs: {name}")
identity = runtime.invoke_sync("identity", {})
if identity != {"identity": "minicode-network", "protocol": 1, "sourceCommit": SOURCE_COMMIT}:
    raise RuntimeError("Packaged network runtime has a different Source identity")
if sys.platform == "win32":
    native = json.loads((root / "desktop/windows-sandbox-runtime/runtime.json").read_text(encoding="utf-8"))
    if native.get("managed_network_ready_protocol") != 1:
        raise RuntimeError("The Windows sandbox runtime does not support explicit managed network setup")
elif sys.platform == "linux":
    if manifest.get("linux_sandbox_routing_protocol") != 1:
        raise RuntimeError("The network runtime does not contain the pinned Linux sandbox helper")
    subprocess.run([str(runtime.binary), "--sandbox-policy-cwd", "/", "--help"],
        check=True, stdout=subprocess.DEVNULL)
print("Pinned network runtime and explicit sandbox readiness verified")
