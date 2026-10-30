"""Cold-check the installed Linux wheel from an isolated interpreter and cwd."""
from __future__ import annotations

import argparse
import asyncio
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
    import httpx
    from backend.public_asset_root import PUBLIC_ASSET_ROOT
    from backend.version import __version__

    installed = Path(sysconfig.get_path("platlib")).resolve()
    backend_root = Path(backend.__file__).resolve().parent
    assert backend_root == installed / "backend", backend_root
    assert PUBLIC_ASSET_ROOT == backend_root / "public", PUBLIC_ASSET_ROOT
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
        required_resources = {
            "backend/public/apps/menu-settings.svg", "backend/public/pets/codex.webp", "backend/public/pets/animations.json",
            "backend/public/theme-boot.js", "backend/plugins/bundled/.minicode-plugin/marketplace.json",
            "backend/plugins/bundled/computer-use/.minicode-plugin/plugin.json",
            "backend/plugins/bundled/computer-use/skills/computer-use/SKILL.md",
        }
        assert required_resources <= names, required_resources - names
        metadata, = (name for name in names if name.endswith(".dist-info/WHEEL"))
        assert "Tag: py3-none-linux_x86_64" in package.read(metadata).decode("utf-8")

    async def check_public_routes() -> dict:
        assets = {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=backend.main.app), base_url="http://wheel") as client:
            for relative, content_type in (("apps/menu-settings.svg", "image/svg+xml"), ("pets/codex.webp", "image/webp"),
                                            ("pets/animations.json", "application/json"), ("theme-boot.js", "application/javascript")):
                response = await client.get(f"/{relative}")
                assert response.status_code == 200, (relative, response.status_code)
                assert response.headers["content-type"].startswith(content_type), (relative, response.headers)
                assert response.content == (PUBLIC_ASSET_ROOT / relative).read_bytes(), relative
                assets[relative] = hashlib.sha256(response.content).hexdigest()
            assert (await client.get("/apps/missing.svg")).status_code == 404
        return assets

    public_assets = asyncio.run(check_public_routes())

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
               "public_asset_root": str(PUBLIC_ASSET_ROOT), "public_assets": public_assets,
               "wheel_members": len(rows), "installed_backend_members_verified": backend_files,
               "native": {name: json.loads((reports / name / "identity.json").read_text(encoding="utf-8")) for name, _ in runtimes}}
    (reports / "wheel-identity.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
