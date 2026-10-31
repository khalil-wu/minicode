"""Verify the real ELF and Source protocol at the Linux artifact boundary."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


SOURCE_COMMITS = {
    "minicode-execpolicy": "822e58cc3d666166c7446c5b1ea2e52f5d09594c",
    "minicode-network": "970111f6cb62cc02fe36a82ea7d71b37a2889c97",
}
MAX_GLIBC = (2, 31)


def verify(runtime: Path, reports: Path) -> dict:
    reports.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((runtime / "runtime.json").read_text(encoding="utf-8"))
    engine = manifest["identity"]
    assert engine in SOURCE_COMMITS, engine
    source_commit = SOURCE_COMMITS[engine]
    assert manifest["target"] == "x86_64-unknown-linux-gnu", manifest["target"]
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((runtime / name).read_bytes()).hexdigest() == expected, name
    binary = runtime / engine
    with binary.open("rb") as executable:
        header = executable.read(20)
    assert header[:6] == b"\x7fELF\x02\x01" and int.from_bytes(header[18:20], "little") == 62, binary
    assert os.access(binary, os.X_OK), f"Native executable mode was lost: {binary}"

    def command(name: str, argv: list[str], payload: str | None = None) -> str:
        result = subprocess.run(argv, input=payload, text=True, encoding="utf-8", capture_output=True,
                                env={**os.environ, "LC_ALL": "C"})
        (reports / f"{name}.stdout.txt").write_text(result.stdout, encoding="utf-8")
        (reports / f"{name}.stderr.txt").write_text(result.stderr, encoding="utf-8")
        if result.returncode:
            sys.stderr.write(result.stdout + result.stderr)
        result.check_returncode()
        return result.stdout

    versions = command("elf-versions", ["readelf", "--version-info", str(binary)])
    required = sorted(set(re.findall(r"Name: (GLIBC_[^\s]+)", versions)))
    assert required, f"No GLIBC version requirements found in {binary}"
    for version in required:
        assert re.fullmatch(r"GLIBC_[0-9.]+", version), f"Unsupported GLIBC ABI: {version}"
        assert tuple(map(int, version[6:].split("."))) <= MAX_GLIBC, f"{binary.name} requires {version}; baseline is GLIBC_2.31"
    headers = command("elf-program-headers", ["readelf", "--program-headers", str(binary)])
    dynamic = command("elf-dynamic", ["readelf", "--dynamic", str(binary)])
    libraries = command("ldd", ["ldd", str(binary)])
    if engine == "minicode-execpolicy":
        identity = json.loads(command("identity", [str(binary)], '{"operation":"identity"}'))
        expected = {"ok": True, "identity": engine, "source_commit": source_commit, "starlark": "0.14.2", "protocol": 1}
        assert manifest["source_commit"] == source_commit
    else:
        identity = json.loads(command("identity", [str(binary)], '{"id":1,"method":"identity","params":{}}\n'))
        expected = {"id": 1, "result": {"identity": engine, "protocol": 1, "sourceCommit": source_commit}}
        assert manifest["sourceCommit"] == source_commit and manifest["linux_sandbox_routing_protocol"] == 1
        command("linux-helper", [str(binary), "--sandbox-policy-cwd", "/", "--help"])
    assert identity == expected, identity
    result = {"runtime": str(runtime), "source_commit": source_commit, "target": manifest["target"],
              "binary_sha256": manifest["files"][engine], "identity": identity,
              "maximum_required_glibc": max(required, key=lambda item: tuple(map(int, item[6:].split(".")))),
              "baseline_glibc": "2.31", "interpreter": re.search(r"Requesting program interpreter: ([^\]]+)", headers).group(1),
              "needed_libraries": re.findall(r"Shared library: \[([^\]]+)\]", dynamic), "ldd": libraries}
    (reports / "identity.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-directory", type=Path, required=True)
    parser.add_argument("--report-directory", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.runtime_directory.resolve(), args.report_directory.resolve()), indent=2))


if __name__ == "__main__":
    main()
