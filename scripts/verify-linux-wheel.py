"""Cold-check the installed Linux wheel from an isolated interpreter and cwd."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import zipfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-directory", type=Path, required=True)
    parser.add_argument("--report-directory", type=Path, required=True)
    args = parser.parse_args()
    reports = args.report_directory.resolve()
    reports.mkdir(parents=True, exist_ok=True)
    wheel, = args.wheel_directory.glob("minicode-*-py3-none-linux_x86_64.whl")
    assert sys.flags.isolated, "Run this check with the installed venv's python -I"
    os.environ["MINICODE_STATE_ROOT"] = str(reports / "cold-state")
    import backend
    import backend.main
    from backend.version import __version__

    installed = Path(sysconfig.get_path("platlib")).resolve()
    backend_root = Path(backend.__file__).resolve().parent
    assert backend_root == installed / "backend", backend_root
    assert not Path.cwd().is_relative_to(Path(__file__).resolve().parents[1]), "Cold cwd must be outside the checkout"
    with zipfile.ZipFile(wheel) as package:
        names = set(package.namelist())
        record, = (name for name in names if name.endswith(".dist-info/RECORD"))
        rows = list(csv.reader(io.StringIO(package.read(record).decode("utf-8"))))
        assert {row[0] for row in rows} == names
        backend_files = 0
        for name, hash_field, size in rows:
            contents = package.read(name)
            if hash_field:
                algorithm, digest = hash_field.split("=", 1)
                assert algorithm == "sha256"
                assert base64.urlsafe_b64encode(hashlib.sha256(contents).digest()).decode().rstrip("=") == digest, name
                assert len(contents) == int(size), name
            else:
                assert name == record, name
            if name.startswith("backend/"):
                assert (installed / name).read_bytes() == contents, name
                backend_files += 1
        assert not any("x86_64-pc-windows-msvc" in name for name in names)
        metadata, = (name for name in names if name.endswith(".dist-info/WHEEL"))
        assert "Tag: py3-none-linux_x86_64" in package.read(metadata).decode("utf-8")

    for name, command in (
        ("pip-check", [sys.executable, "-m", "pip", "check"]),
        ("pip-freeze", [sys.executable, "-m", "pip", "freeze"]),
        ("cli-version", [str(Path(sys.executable).parent / "minicode-host"), "--version"]),
    ):
        with (reports / f"{name}.txt").open("w", encoding="utf-8") as output:
            subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=True)
    assert (reports / "cli-version.txt").read_text(encoding="utf-8").strip() == f"MiniCode {__version__} desktop-host/1"
    verifier = Path(__file__).with_name("verify-linux-native-runtime.py")
    runtimes = (
        ("execpolicy", backend_root / "permissions/resources/execpolicy/x86_64-unknown-linux-gnu"),
        ("network", backend_root / "network/resources/x86_64-unknown-linux-gnu"),
    )
    for name, directory in runtimes:
        subprocess.run([sys.executable, "-I", str(verifier), "--runtime-directory", str(directory),
                        "--report-directory", str(reports / name)], check=True)
    receipt = {"wheel": wheel.name, "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
               "python": sys.executable, "python_version": sys.version, "backend_file": backend.__file__,
               "cold_cwd": str(Path.cwd()), "cold_path": os.environ["PATH"], "isolated_interpreter": True,
               "routes": len(backend.main.app.routes),
               "wheel_members": len(rows), "installed_backend_members_verified": backend_files,
               "native": {name: json.loads((reports / name / "identity.json").read_text(encoding="utf-8")) for name, _ in runtimes}}
    (reports / "wheel-identity.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
