"""Build MiniCode's network bridge against one exact, isolated Codex Source tree."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile


ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "desktop/native-network"
SOURCE_COMMIT = "970111f6cb62cc02fe36a82ea7d71b37a2889c97"


def original_image_source(history: bytes) -> bytes:
    """Keep the private Source estimator's two exact ranges as a narrow include."""
    ranges = (
        (b"const ORIGINAL_IMAGE_PATCH_SIZE:", b"\nfn estimate_response_item_model_visible_bytes"),
        (b"fn parse_base64_image_data_url(", b"\n/// Inline image estimate"),
    )
    sections = []
    for first, after in ranges:
        start = history.index(first)
        end = history.index(after, start)
        sections.append(history[start:end])
    return b"\n".join(sections)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv, *, cwd, environment, capture=False, payload=None):
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    if capture:
        result = subprocess.run(argv, cwd=cwd, env=environment, input=payload,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
        if result.returncode:
            sys.stderr.buffer.write(result.stderr)
            sys.stderr.buffer.flush()
            result.check_returncode()
        return result
    process = subprocess.Popen(argv, cwd=cwd, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, creationflags=flags)
    for line in process.stdout:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
    code = process.wait()
    if code:
        raise subprocess.CalledProcessError(code, argv)
    return subprocess.CompletedProcess(argv, code)


def prepare_source_tree(checkout: Path, build: Path, environment) -> Path:
    source = build / "source"
    identity_path = build / "source-identity.json"
    if not identity_path.exists():
        build.mkdir(parents=True, exist_ok=True)
        archive = build / "source.tar"
        run(["git", "-c", "core.autocrlf=false", "archive", "--format=tar", "--output", str(archive), SOURCE_COMMIT],
            cwd=checkout, environment=environment)
        source.mkdir(exist_ok=True)
        with tarfile.open(archive) as contents:
            contents.extractall(source, filter="data")
        identity_path.write_text(json.dumps({"sourceCommit": SOURCE_COMMIT}), encoding="utf-8")
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    if identity != {"sourceCommit": SOURCE_COMMIT}:
        raise RuntimeError("Network build directory contains a different Source revision")
    return source


def prepare_bridge(project: Path) -> tuple[Path, ...]:
    (project / "src").mkdir(parents=True, exist_ok=True)
    inputs = []
    for path in (BRIDGE / "Cargo.toml", *(BRIDGE / "src").glob("*.rs")):
        target = project / path.relative_to(BRIDGE)
        contents = path.read_bytes()
        if sys.platform == "linux" and path.name == "Cargo.toml":
            contents += b'\n[target.\'cfg(target_os = "linux")\'.dependencies]\ncodex-linux-sandbox = { path = "../linux-sandbox" }\n\n[[bin]]\nname = "minicode-network"\npath = "src/linux_entry.rs"\n'
        if not target.exists() or target.read_bytes() != contents:
            target.write_bytes(contents)
        inputs.append(target)
    if sys.platform == "linux":
        target = project / "src/linux_entry.rs"
        shutil.copyfile(BRIDGE / "linux/entry.rs", target)
        inputs.append(target)
    return tuple(inputs)


def prepare_linux_read_metadata_adapter(checkout: Path, workspace: Path, environment) -> dict:
    relative = "linux-sandbox/src/bwrap.rs"
    original = run(["git", "show", f"{SOURCE_COMMIT}:codex-rs/{relative}"],
        cwd=checkout, environment=environment, capture=True).stdout
    imported = b"use codex_protocol::permissions::is_protected_metadata_name;"
    included = b'include!("minicode_read_metadata_names.rs");'
    if original.count(imported) != 1:
        raise RuntimeError("The pinned bwrap READ metadata import is not its single expected source range")
    adapted = original.replace(imported, included, 1)
    helper_source = BRIDGE / "linux/read_metadata_names.rs"
    helper_relative = "linux-sandbox/src/minicode_read_metadata_names.rs"
    helper = helper_source.read_bytes()
    for target, contents in ((workspace / relative, adapted), (workspace / helper_relative, helper)):
        if not target.exists() or target.read_bytes() != contents:
            target.write_bytes(contents)
    return {relative: {
        "upstream": f"{SOURCE_COMMIT}:codex-rs/{relative}",
        "upstream_sha256": hashlib.sha256(original).hexdigest(),
        "adapted_sha256": hashlib.sha256(adapted).hexdigest(),
        "replacements": [{"from": imported.decode(), "to": included.decode(), "count": 1}],
        "scope": "Existing READ mount target classification only; protocol permissions and DENY paths unchanged",
        "helper": {"source": "desktop/native-network/linux/read_metadata_names.rs", "staged": helper_relative,
            "sha256": hashlib.sha256(helper).hexdigest()},
    }}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cargo", type=Path, required=True)
    parser.add_argument("--source-checkout", type=Path, required=True)
    parser.add_argument("--build-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    cargo, checkout = args.cargo.resolve(), args.source_checkout.resolve()
    script_hash = digest(Path(__file__).resolve())
    build, output = args.build_directory.resolve(), args.output_directory.resolve()
    environment = {**os.environ, "PATH": str(cargo.parent) + os.pathsep + os.environ["PATH"]}
    compiler = run([str(cargo.parent / ("rustc.exe" if sys.platform == "win32" else "rustc")), "-Vv"],
        cwd=ROOT, environment=environment, capture=True).stdout.decode("utf-8")
    if not compiler.startswith("rustc 1.99.1 "):
        raise RuntimeError("The Source network runtime requires Rust 1.99.1")
    source = prepare_source_tree(checkout, build, environment)
    workspace = source / "codex-rs"
    project = workspace / "minicode-network"
    bridge_inputs = prepare_bridge(project)
    source_adaptations = (prepare_linux_read_metadata_adapter(checkout, workspace, environment)
        if sys.platform == "linux" else {})
    manifest = workspace / "Cargo.toml"
    text = manifest.read_text(encoding="utf-8")
    if '    "minicode-network",' not in text:
        manifest.write_text(text.replace("members = [", 'members = [\n    "minicode-network",', 1), encoding="utf-8")
    lock = workspace / "Cargo.lock"
    lock_text = lock.read_text(encoding="utf-8")
    dependencies = ("anyhow", "base64 0.22.1", "codex-config", "codex-execpolicy", "codex-file-system", "codex-network-proxy", "codex-protocol",
            "codex-utils-audio", "codex-utils-cache", "codex-utils-path-uri", "codex-utils-string", "codex-windows-sandbox", "image",
            "serde", "serde_json", "tokio", "tracing", "tracing-subscriber")
    if sys.platform == "linux":
        dependencies = tuple(sorted((*dependencies, "codex-linux-sandbox")))
    lock_text = re.sub(r'\n\[\[package\]\]\nname = "minicode-network"\n.*?(?=\n\[\[package\]\]|\Z)', '', lock_text, flags=re.S)
    lock_text += '\n[[package]]\nname = "minicode-network"\nversion = "0.1.0"\ndependencies = [\n'
    lock_text += "".join(f' "{name}",\n' for name in dependencies) + "]\n"
    if lock.read_text(encoding="utf-8") != lock_text:
        lock.write_text(lock_text, encoding="utf-8")
    pinned_files = ("core/src/config/network_proxy_spec.rs", "core/src/network_policy_decision.rs",
        "file-system/src/lib.rs", "file-system/src/exec_permission_profile_serde.rs",
        "utils/audio/src/lib.rs", "utils/audio/Cargo.toml", "utils/image/Cargo.toml",
        "utils/cache/src/lib.rs", "utils/string/src/truncate.rs",
        "core/src/context_manager/history.rs")
    if sys.platform == "linux":
        pinned_files += ("linux-sandbox/src/linux_run_main.rs", "linux-sandbox/src/proxy_routing.rs",
            "linux-sandbox/src/proxy_lifecycle.rs", "linux-sandbox/src/landlock.rs", "protocol/src/permissions.rs")
    for relative in pinned_files:
        original = run(["git", "show", f"{SOURCE_COMMIT}:codex-rs/{relative}"],
            cwd=checkout, environment=environment, capture=True).stdout
        if (workspace / relative).read_bytes() != original:
            raise RuntimeError(f"{relative} differs from the pinned Source object")
    original_image = original_image_source((workspace / "core/src/context_manager/history.rs").read_bytes())
    if (project / "src/source_original_image.rs").read_bytes() != original_image:
        raise RuntimeError("The original-image estimator differs from its exact pinned Source ranges")
    run([str(cargo), "build", "--release", "--locked", "-p", "minicode-network", "-j", "2"],
        cwd=workspace, environment=environment)
    binary = workspace / "target/release" / ("minicode-network.exe" if sys.platform == "win32" else "minicode-network")
    verified = run([str(binary)], cwd=project, environment=environment, capture=True,
        payload=b'{"id":1,"method":"identity","params":{}}\n').stdout.decode("utf-8")
    message = json.loads(verified)
    if message != {"id": 1, "result": {"identity": "minicode-network", "protocol": 1, "sourceCommit": SOURCE_COMMIT}}:
        raise RuntimeError("The compiled network runtime has a different Source identity")
    if sys.platform == "linux":
        run([str(binary), "--sandbox-policy-cwd", "/", "--help"], cwd=project,
            environment=environment, capture=True)
    target = next(line.removeprefix("host: ") for line in compiler.splitlines() if line.startswith("host: "))
    metadata = json.loads(run([str(cargo), "metadata", "--locked", "--format-version", "1", "--filter-platform", target],
        cwd=workspace, environment=environment, capture=True).stdout)
    packages = {item["id"]: item for item in metadata["packages"]}
    nodes = {item["id"]: item for item in metadata["resolve"]["nodes"]}
    selected = {item["id"] for item in metadata["packages"] if item["name"] == "minicode-network"}
    pending = list(selected)
    while pending:
        for dependency in nodes[pending.pop()]["deps"]:
            if all(kind["kind"] == "dev" for kind in dependency["dep_kinds"]):
                continue
            if dependency["pkg"] not in selected:
                selected.add(dependency["pkg"])
                pending.append(dependency["pkg"])
    notices = []
    for package in sorted((packages[key] for key in selected), key=lambda item: (item["name"], item["version"])):
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
    license_file = source / "LICENSE"
    shutil.copyfile(license_file, output / "LICENSE.openai-codex")
    shutil.copyfile(source / "NOTICE", output / "NOTICE.openai-codex")
    (output / "THIRD_PARTY_NOTICES.txt").write_text("\n\n".join(notices) + "\n", encoding="utf-8", newline="\n")
    source_manifest = {"source": "https://github.com/openai/codex", "source_commit": SOURCE_COMMIT,
        "rust_toolchain": "1.99.1", "target": target,
        "source_files": {relative: digest(workspace / relative) for relative in pinned_files},
        "source_adaptations": source_adaptations,
        "source_derivations": {"src/source_original_image.rs": {
            "upstream": "core/src/context_manager/history.rs",
            "sha256": hashlib.sha256(original_image).hexdigest(),
            "ranges": [
                ["const ORIGINAL_IMAGE_PATCH_SIZE:", "fn estimate_response_item_model_visible_bytes"],
                ["fn parse_base64_image_data_url(", "/// Inline image estimate"],
            ],
        }},
        "cargo_lock_sha256": digest(lock),
        "build_script_sha256": script_hash,
        "bridge_files": {str(path.relative_to(project)).replace("\\", "/"): digest(path)
            for path in bridge_inputs}}
    (output / "source-manifest.json").write_text(json.dumps(source_manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    runtime = {**message["result"], "target": target, "files": {path.name: digest(path)
        for path in output.iterdir() if path.is_file() and path.name != "runtime.json"}}
    if sys.platform == "win32":
        runtime["managed_network_ready_protocol"] = 1
    if sys.platform == "linux":
        runtime["linux_sandbox_routing_protocol"] = 1
    (output / "runtime.json").write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"runtime": str(output), "identity": message["result"]}))


if __name__ == "__main__":
    main()
