"""Run the actual staged native engine outside the checkout with no Rust PATH."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-directory", type=Path, required=True)
    parser.add_argument("--cold-directory", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--dumpbin", type=Path)
    args = parser.parse_args()
    runtime = args.runtime_directory.resolve()
    cold = args.cold_directory.resolve()
    manifest = json.loads((runtime / "runtime.json").read_text(encoding="utf-8"))
    cold.mkdir(parents=True, exist_ok=True)
    for name, expected in manifest["files"].items():
        assert sha256(runtime / name) == expected, name
        shutil.copyfile(runtime / name, cold / name)
    shutil.copyfile(runtime / "runtime.json", cold / "runtime.json")
    binary = cold / ("minicode-execpolicy.exe" if sys.platform == "win32" else "minicode-execpolicy")
    environment = dict(os.environ)
    environment.pop("CARGO_HOME", None)
    environment.pop("RUSTUP_HOME", None)
    environment["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32") if sys.platform == "win32" else "/usr/bin:/bin"
    cases = []

    def invoke(name: str, payload: dict, expected_code: int = 0) -> dict:
        result = subprocess.run([str(binary)], cwd=cold, env=environment,
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        assert result.returncode == expected_code, (name, result.returncode, result.stderr.decode("utf-8"))
        value = json.loads(result.stdout)
        cases.append({"name": name, "exit_code": result.returncode, "result": value})
        return value

    identity = invoke("cold identity", {"operation": "identity"})
    assert identity == {"ok": True, "identity": "minicode-execpolicy", "source_commit": manifest["source_commit"],
        "starlark": "0.14.2", "protocol": 1}
    source = {"path": "cold-user.rules", "contents": "def approve(verb):\n    prefix_rule(pattern=[\"git\", verb], decision=\"allow\")\nfor verb in [\"status\", \"diff\"]:\n    approve(verb)\n"}
    inspected = invoke("real Starlark after relocation", {"operation": "inspect", "sources": [source],
        "ignore_allow_prefix_rules": False})
    assert inspected["policy"]["allowed_prefixes"] == [["git", "diff"], ["git", "status"]]
    requirement = {"operation": "command_requirement", "sources": [source], "environment_sources": [],
        "ignore_allow_prefix_rules": False, "command": ["bash", "-lc", "git status && git diff --stat"],
        "configured_shell": "bash", "shell_mode": "normal", "approval_policy": "untrusted", "prefix_rule": None,
        "permission_profile": {"managed": False, "file_system": {"kind": "unrestricted", "full_disk_write": True}},
        "windows_sandbox_level": "disabled", "sandbox_permissions": "use_default"}
    allowed = invoke("real shell decomposition and explicit bypass", requirement)
    assert allowed["commands"] == [["git", "status"], ["git", "diff", "--stat"]]
    assert allowed["requirement"]["kind"] == "skip" and allowed["requirement"]["bypass_sandbox"] is True
    requirement["ignore_allow_prefix_rules"] = True
    ignored = invoke("admitted-model Allow filtering", requirement)
    assert ignored["requirement"]["kind"] == "needs_approval"
    assert ignored["requirement"]["proposed_execpolicy_amendment"] is None
    assert ignored["prompt_policy"]["allowed_prefixes"] == []
    requirement["ignore_allow_prefix_rules"] = False
    requirement["sources"] = []
    requirement["approval_policy"] = "on-request"
    safe = invoke("ordinary safe Skip", requirement)
    assert safe["requirement"]["kind"] == "skip" and safe["requirement"]["bypass_sandbox"] is False
    requirement["command"] = ["bash", "-lc", "rm -rf scratch"]
    requirement["approval_policy"] = "never"
    forbidden = invoke("Source dangerous command Never decision", requirement)
    assert forbidden["requirement"]["kind"] == "forbidden"
    assert "rm -f style commands are not permitted" in forbidden["requirement"]["reason"]
    invalid = invoke("original file and line parse failure", {"operation": "inspect",
        "sources": [{"path": "bad-user.rules", "contents": "# preserved source line\nprefix_rule(pattern=[\"git\"], match=[[\"hg\"]])\n"}],
        "ignore_allow_prefix_rules": False}, 1)
    assert invalid["error"]["location"]["path"] == "bad-user.rules"
    assert invalid["error"]["location"]["start"]["line"] == 2
    state = cold / "state"
    state.mkdir(exist_ok=True)
    saved = state / "rules/default.rules"
    prefix = ["example", 'quoted"token', "路径\\text"]
    invoke("original durable amendment writer", {"operation": "append_allow_prefix", "path": str(saved), "prefix": prefix})
    restored = invoke("cold parse of actually persisted amendment", {"operation": "inspect",
        "sources": [{"path": str(saved), "contents": saved.read_text(encoding="utf-8")}], "ignore_allow_prefix_rules": False})
    assert restored["policy"]["allowed_prefixes"] == [prefix]
    dependencies = None
    if args.dumpbin:
        result = subprocess.run([str(args.dumpbin.resolve()), "/dependents", str(binary)], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        output = result.stdout.decode("utf-8", errors="replace")
        dependencies = [line.strip() for line in output.splitlines() if line.strip().lower().endswith(".dll")]
        assert not any("vcruntime" in name.lower() or "msvcp" in name.lower() for name in dependencies), dependencies
    report = {"runtime": str(runtime), "cold_directory": str(cold), "source_commit": manifest["source_commit"],
        "target": manifest["target"], "binary_sha256": sha256(binary), "manifest_hashes_match": True,
        "rust_path_present": False, "runtime_dependencies": dependencies, "cases": cases,
        "passed": len(cases), "failed": 0, "scope": "actual relocated release binary only; not wheel or desktop consumer"}
    args.report.resolve().write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": len(cases), "report": str(args.report.resolve()), "binary_sha256": report["binary_sha256"]}))


if __name__ == "__main__":
    main()
