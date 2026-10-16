"""Build command contracts, including simulated CI manifests without native execution."""
import ast
import json
from pathlib import Path
import shutil

import pytest


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_TARGET = "x86_64-pc-windows-msvc"
LINUX_TARGET = "x86_64-unknown-linux-gnu"
RESOURCE_FAMILIES = ("permissions/resources/execpolicy", "network/resources")


@pytest.fixture
def native_commands():
    source = ROOT / "setup.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    definitions = ast.Module(
        body=[node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef))],
        type_ignores=[],
    )
    namespace = {"__file__": str(source)}
    exec(compile(definitions, str(source), "exec"), namespace)
    return namespace


def stage_known_manifest_samples(directory, target):
    for resources in RESOURCE_FAMILIES:
        source = ROOT / "backend" / resources / WINDOWS_TARGET
        runtime = directory / "backend" / resources / target
        shutil.copytree(source, runtime)
        manifest_path = runtime / "runtime.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Linux models a downloaded artifact manifest using known bytes. This
        # tests host selection and integrity; it does not execute a Linux binary.
        manifest["target"] = target
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("host,target", [("win-amd64", WINDOWS_TARGET), ("linux-x86_64", LINUX_TARGET)])
def test_native_build_validates_both_manifests_and_tags_the_actual_host(native_commands, tmp_path, monkeypatch, host, target):
    stage_known_manifest_samples(tmp_path, target)
    native_commands["__file__"] = str(tmp_path / "setup.py")
    monkeypatch.setattr(native_commands["sysconfig"], "get_platform", lambda: host)
    copied = []
    monkeypatch.setattr(native_commands["build_py"], "run", lambda command: copied.append(command))
    distribution = native_commands["NativeDistribution"]()
    command = native_commands["NativeBuild"](distribution)
    command.run()

    assert copied == [command]
    assert distribution.has_ext_modules()
    assert native_commands["NativeWheel"](distribution).get_tag() == ("py3", "none", host.replace("-", "_"))


@pytest.mark.parametrize("resources", RESOURCE_FAMILIES)
@pytest.mark.parametrize("violation", ["wrong_target", "changed_resource"])
def test_native_build_rejects_a_wrong_target_or_changed_file_before_copying(native_commands, tmp_path, monkeypatch, resources, violation):
    stage_known_manifest_samples(tmp_path, LINUX_TARGET)
    native_commands["__file__"] = str(tmp_path / "setup.py")
    monkeypatch.setattr(native_commands["sysconfig"], "get_platform", lambda: "linux-x86_64")
    copied = []
    monkeypatch.setattr(native_commands["build_py"], "run", lambda command: copied.append(command))
    runtime = tmp_path / "backend" / resources / LINUX_TARGET
    manifest_path = runtime / "runtime.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if violation == "wrong_target":
        manifest["target"] = WINDOWS_TARGET
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        message = "belongs to a different platform"
    else:
        resource = next(name for name in manifest["files"] if name.startswith("LICENSE"))
        (runtime / resource).write_bytes(b"modified resource")
        message = "Staged native resource changed"

    with pytest.raises(RuntimeError, match=message):
        native_commands["NativeBuild"](native_commands["NativeDistribution"]()).run()
    assert copied == []


@pytest.mark.parametrize("host", ["macosx-14.0-arm64", "linux-aarch64"])
def test_native_build_does_not_advertise_unprovided_platforms(native_commands, monkeypatch, host):
    monkeypatch.setattr(native_commands["sysconfig"], "get_platform", lambda: host)
    with pytest.raises(KeyError, match=host.replace("-", "_")):
        native_commands["NativeBuild"](native_commands["NativeDistribution"]()).run()
