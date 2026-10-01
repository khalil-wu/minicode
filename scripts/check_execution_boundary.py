"""Exercise real sandbox I/O, networking, exit codes and process cleanup.

Run with a prepared native Windows sandbox or a built container backend. No
model, credentials, or simulated tool results are used. Evidence stays under --out.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.sandbox.policy import (
    FileSystemAccessMode,
    FileSystemPath,
    FileSystemSandboxEntry,
    FileSystemSandboxPolicy,
    FileSystemSpecialPath,
    PermissionProfile,
    SandboxPolicy,
)
from backend.sandbox.runner import SandboxRunner
from backend.config import DATA_ROOT


async def check(output: Path) -> dict:
    workspace, sibling = output / "workspace", output / "sibling"
    workspace.mkdir(parents=True, exist_ok=True)
    sibling.mkdir(exist_ok=True)
    (workspace / ".git").mkdir(exist_ok=True)
    (sibling / "owned.txt").write_text("sibling-owned", encoding="utf-8")
    (workspace / "test_example.py").write_text(
        "from decimal import Decimal\ndef test_amount():\n    assert Decimal('0.1') * 3 == Decimal('0.3')\n",
        encoding="utf-8",
    )
    profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
        FileSystemSandboxEntry(
            FileSystemPath.special(FileSystemSpecialPath.ROOT),
            FileSystemAccessMode.READ,
        ),
        FileSystemSandboxEntry(FileSystemPath.path(workspace), FileSystemAccessMode.WRITE),
        FileSystemSandboxEntry(FileSystemPath.path(sibling), FileSystemAccessMode.READ),
        # Pytest's FD capture needs temporary files. This is an explicit
        # execution capability, not an unsandboxed fallback or an implicit
        # writable host-root grant.
        FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.TMPDIR), FileSystemAccessMode.WRITE),
    ]))
    policy = SandboxPolicy(workspace_root=workspace, permission_profile=profile, timeout=30)
    report = {"capability": asdict(SandboxRunner(policy).capability(cwd=workspace)), "checks": {}}
    native_temp_root = DATA_ROOT / "sandbox-native-temp"

    def native_temp_names() -> set[str]:
        return {path.name for path in native_temp_root.iterdir()} if native_temp_root.exists() else set()

    async def command(name: str, text: str):
        result = await SandboxRunner(policy).run(text, cwd=workspace)
        report[name] = asdict(result)
        return result

    result = await command("working_directory", 'python -c "import os; print(os.getcwd()); open(\'inside.txt\',\'w\').write(\'inside\')"')
    report["checks"]["inside_write"] = result.exit_code == 0 and (workspace / "inside.txt").read_text() == "inside"
    report["checks"]["cwd"] = result.stdout.strip().replace("\\", "/") in {"/workspace", workspace.as_posix()}
    result = await command("exit_code", 'python -c "raise SystemExit(7)"')
    report["checks"]["exit_code"] = result.exit_code == 7
    target = str(sibling / "owned.txt")
    result = await command("read_sibling", f'python -c "print(open(r\'{target}\').read())"')
    report["checks"]["declared_read"] = result.exit_code == 0 and "sibling-owned" in result.stdout
    result = await command("write_sibling", f'python -c "open(r\'{target}\',\'w\').write(\'escape\')"')
    report["checks"]["sibling_write_denied"] = result.exit_code != 0 and (sibling / "owned.txt").read_text() == "sibling-owned"
    result = await command("write_git_metadata", 'python -c "open(\'.git/probe.txt\',\'w\').write(\'escape\')"')
    report["checks"]["protected_metadata_write_denied"] = (
        result.exit_code != 0 and not (workspace / ".git" / "probe.txt").exists()
    )
    result = await command("direct_network", 'python -c "import socket; s=socket.socket(); s.settimeout(1); print(s.connect_ex((\'1.1.1.1\',443)))"')
    report["checks"]["direct_network_denied"] = result.exit_code == 0 and result.stdout.strip().isdigit() and int(result.stdout.strip()) != 0
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        previous_proxy = os.environ.get("HTTP_PROXY")
        os.environ["HTTP_PROXY"] = f"http://127.0.0.1:{port}"
        try:
            result = await command(
                "loopback_network",
                f'python -c "import socket; s=socket.socket(); s.settimeout(1); '
                f"print(s.connect_ex(('127.0.0.1',{port})))\"",
            )
        finally:
            if previous_proxy is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = previous_proxy
    report["checks"]["loopback_network_denied"] = (
        result.exit_code == 0
        and result.stdout.strip().isdigit()
        and int(result.stdout.strip()) != 0
    )
    result = await command("pytest", "python -m pytest -q -p no:cacheprovider test_example.py")
    # Pytest can suppress its terminal summary when the sandbox account has no
    # color/TTY capability. Exit 0 plus the completed progress line is the
    # authoritative result in both modes.
    report["checks"]["pytest"] = result.exit_code == 0 and "[100%]" in result.stdout

    owner_only_temp_command = (
        'python -c "import pathlib,tempfile,time; '
        "p=pathlib.Path(tempfile.gettempdir())/'owner-only'; "
        "p.mkdir(mode=0o700,exist_ok=True); "
        "(p/'data.txt').write_text('owned'); print('READY',flush=True); time.sleep(30)\""
    )
    before_native_temp = native_temp_names()
    timeout_policy = replace(policy, timeout=8)
    result = await SandboxRunner(timeout_policy).run(owner_only_temp_command, cwd=workspace)
    report["timeout"] = asdict(result)
    report["checks"]["timeout_cleanup"] = (
        result.timed_out
        and "READY" in result.stdout
        and not result.cleanup_pending
        and native_temp_names() == before_native_temp
    )
    cancel = asyncio.Event()
    ready = asyncio.Event()

    async def on_output(piece: str, _stream: str = "stdout") -> None:
        if "READY" in piece:
            ready.set()

    runner = SandboxRunner(policy)
    running = asyncio.create_task(runner.run(
        owner_only_temp_command,
        cwd=workspace,
        cancel_event=cancel,
        stream_callback=on_output,
    ))
    try:
        await asyncio.wait_for(ready.wait(), timeout=20)
    except asyncio.TimeoutError:
        pass
    cancel.set()
    result = await running
    report["cancel"] = asdict(result)
    report["checks"]["cancel_cleanup"] = (
        ready.is_set()
        and result.cancelled
        and not result.cleanup_pending
        and native_temp_names() == before_native_temp
    )
    report["passed"] = all(report["checks"].values())
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(check(args.out.resolve()))
    print(json.dumps({"capability": report["capability"], "checks": report["checks"], "passed": report["passed"]}, indent=2))
    raise SystemExit(0 if report["passed"] else 1)
