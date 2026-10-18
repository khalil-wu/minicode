"""Build and stage the pinned native exec-policy bridge with its provenance.

The build reads the checked-in vendor tree, never a temporary Codex checkout.
Use an explicit output directory while another desktop verification is frozen.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


REPOSITORY = Path(__file__).resolve().parents[2]
PROJECT = REPOSITORY / "desktop/native-execpolicy"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: list[str], *, environment: dict[str, str], capture: bool = False, payload: bytes | None = None):
    return subprocess.run(command, cwd=PROJECT, env=environment, check=True,
        input=payload, stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cargo", type=Path, required=True, help="Actual pinned toolchain cargo binary")
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    cargo = args.cargo.resolve()
    output = args.output_directory.resolve()
    environment = {**os.environ, "PATH": str(cargo.parent) + os.pathsep + os.environ["PATH"]}
    source_manifest_path = PROJECT / "source-manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    for entry in source_manifest["files"]:
        if sha256(PROJECT / entry["vendored"]) != entry["sha256"]:
            raise RuntimeError(f"Vendored Source changed: {entry['vendored']}")
    suffix = ".exe" if sys.platform == "win32" else ""
    rustc = cargo.parent / ("rustc" + suffix)
    compiler = run([str(rustc), "-Vv"], environment=environment, capture=True).stdout.decode("utf-8")
    if not compiler.startswith("rustc " + source_manifest["rust_toolchain"] + " "):
        raise RuntimeError("The exec-policy runtime requires the pinned Source Rust toolchain")
    target = next(line.removeprefix("host: ") for line in compiler.splitlines() if line.startswith("host: "))
    run([str(cargo), "build", "--release", "--locked", "--offline", "-p", "minicode-execpolicy", "-j", "2"],
        environment=environment)
    binary = PROJECT / "target/release" / ("minicode-execpolicy" + suffix)
    identity = json.loads(run([str(binary)], environment=environment, capture=True,
        payload=b'{"operation":"identity"}').stdout)
    if identity != {"ok": True, "identity": "minicode-execpolicy", "source_commit": source_manifest["commit"],
                    "starlark": source_manifest["starlark"], "protocol": 1}:
        raise RuntimeError("The compiled exec-policy binary returned a different Source identity")
    metadata = json.loads(run([str(cargo), "metadata", "--locked", "--offline", "--format-version", "1"],
        environment=environment, capture=True).stdout)
    notices = []
    for package in sorted(metadata["packages"], key=lambda item: (item["name"], item["version"])):
        package_root = Path(package["manifest_path"]).parent
        candidates = {path for pattern in ("LICENSE*", "LICENCE*", "COPYING*")
            for path in package_root.glob(pattern) if path.is_file()}
        if package.get("license_file"):
            candidates.add(package_root / package["license_file"])
        contents = [f"{package['name']} {package['version']} — {package.get('license') or 'license file'}"]
        for path in sorted(candidates):
            contents.extend([path.name, path.read_text(encoding="utf-8")])
        notices.append("\n\n".join(contents))
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, output / binary.name)
    for name in ("LICENSE.openai-codex", "NOTICE.openai-codex", "source-manifest.json"):
        shutil.copyfile(PROJECT / name, output / name)
    (output / "THIRD_PARTY_NOTICES.txt").write_text("\n\n".join(notices) + "\n", encoding="utf-8")
    source_inputs = [PROJECT / name for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "source-manifest.json", ".cargo/config.toml")]
    source_inputs.extend(sorted((PROJECT / "src").glob("*.rs")))
    source_inputs.extend([PROJECT / "vendor/source-shell/Cargo.toml", PROJECT / "vendor/source-shell/src/lib.rs",
        PROJECT / "vendor/source-shell/src/source_test_environment.rs"])
    source_inputs.append(Path(__file__).resolve())
    fingerprint = hashlib.sha256(json.dumps({os.path.relpath(path, PROJECT).replace("\\", "/"): sha256(path)
        for path in source_inputs}, sort_keys=True).encode("utf-8")).hexdigest()
    manifest = {**identity, "target": target, "source": source_manifest["source"],
        "rust_toolchain": source_manifest["rust_toolchain"], "input_sha256": fingerprint,
        "cargo_lock_sha256": sha256(PROJECT / "Cargo.lock"),
        "files": {path.name: sha256(path) for path in sorted(output.iterdir()) if path.is_file() and path.name != "runtime.json"}}
    (output / "runtime.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"runtime": str(output), "identity": identity, "target": target,
        "binary_sha256": sha256(output / binary.name)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
