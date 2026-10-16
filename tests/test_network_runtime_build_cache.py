"""Source initialization must not mistake a restored Cargo target for Source."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest


@pytest.fixture
def builder():
    path = Path(__file__).resolve().parents[1] / "desktop/scripts/prepare-network-runtime.py"
    spec = importlib.util.spec_from_file_location("network_runtime_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("restored_target", [False, True])
def test_source_initialization_archives_the_pin_and_preserves_target_cache(builder, tmp_path, monkeypatch, restored_target):
    checkout, build = tmp_path / "checkout", tmp_path / "build"
    cached = build / "source/codex-rs/target/release/owned-cache"
    if restored_target:
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"keep this compilation cache")
    calls = []
    environment = {"OWNED_SOURCE_BUILD": "1"}

    def archive(argv, *, cwd, environment):
        assert cwd == checkout
        assert environment == {"OWNED_SOURCE_BUILD": "1"}
        assert argv == ["git", "-c", "core.autocrlf=false", "archive", "--format=tar", "--output",
            str(build / "source.tar"), builder.SOURCE_COMMIT]
        calls.append(argv)
        contents = b"[workspace]\nmembers = []\n"
        entry = tarfile.TarInfo("codex-rs/Cargo.toml")
        entry.size = len(contents)
        with tarfile.open(build / "source.tar", "w") as archive_file:
            archive_file.addfile(entry, io.BytesIO(contents))

    monkeypatch.setattr(builder, "run", archive)
    source = builder.prepare_source_tree(checkout, build, environment)
    assert (source / "codex-rs/Cargo.toml").read_bytes() == b"[workspace]\nmembers = []\n"
    assert json.loads((build / "source-identity.json").read_text()) == {"sourceCommit": builder.SOURCE_COMMIT}
    if restored_target:
        assert cached.read_bytes() == b"keep this compilation cache"
    assert builder.prepare_source_tree(checkout, build, environment) == source
    assert len(calls) == 1


def test_failed_archive_does_not_publish_a_source_identity(builder, tmp_path, monkeypatch):
    build = tmp_path / "build"
    cached = build / "source/codex-rs/target/owned-cache"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"retained target")
    failure = subprocess.CalledProcessError(128, ["git", "archive"])

    def fail_archive(*args, **kwargs):
        raise failure

    monkeypatch.setattr(builder, "run", fail_archive)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        builder.prepare_source_tree(tmp_path / "checkout", build, {})
    assert caught.value is failure
    assert not (build / "source-identity.json").exists()
    assert cached.read_bytes() == b"retained target"


def test_existing_different_source_identity_is_rejected_without_archiving(builder, tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir()
    identity = build / "source-identity.json"
    identity.write_text(json.dumps({"sourceCommit": "different-source-revision"}))

    def unexpected_archive(*args, **kwargs):
        raise AssertionError("An existing Source identity must be checked before any archive")

    monkeypatch.setattr(builder, "run", unexpected_archive)
    with pytest.raises(RuntimeError, match="different Source revision"):
        builder.prepare_source_tree(tmp_path / "checkout", build, {})
    assert json.loads(identity.read_text()) == {"sourceCommit": "different-source-revision"}
