from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

import backend.plugins.materializer as materializer_module
from backend.plugins.materializer import (
    MaterializationError,
    MaterializedSource,
    MarketplaceSource,
    materialize_source,
    parse_marketplace_source,
    recover_materialization_artifacts,
)


def _marketplace_archive(tmp_path: Path) -> Path:
    archive = tmp_path / "marketplace.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(
            "bundle/.agents/plugins/marketplace.json",
            json.dumps({"name": "demo-market", "plugins": []}),
        )
    return archive


def _replacement_archive(path: Path, marker: str) -> Path:
    with zipfile.ZipFile(path, "w") as handle:
        handle.writestr(
            "bundle/.agents/plugins/marketplace.json",
            json.dumps({"name": "demo-market", "plugins": []}),
        )
        handle.writestr("bundle/marker.txt", marker)
    return path


def test_source_parser_matches_codex_git_and_archive_forms() -> None:
    assert parse_marketplace_source("owner/repo@main").ref == "main"
    assert parse_marketplace_source("owner/repo@main").locator.endswith("owner/repo.git")
    assert parse_marketplace_source("https://example.test/catalog.zip").kind == "url"
    assert parse_marketplace_source("npm:@scope/tool@1.2.3").kind == "npm"
    codex_url = parse_marketplace_source(
        {
            "source": "url",
            "url": "https://github.com/example/plugins",
            "path": "plugins/review",
            "ref": "main",
        }
    )
    assert codex_url.kind == "git"
    assert codex_url.subpath == "plugins/review"
    assert parse_marketplace_source(codex_url.to_dict()).subpath == "plugins/review"
    with pytest.raises(MaterializationError):
        parse_marketplace_source("file:///etc/passwd")


@pytest.mark.parametrize(
    "source",
    [
        {"source": "git", "url": "https://example.test/repo.git", "ref": "--upload-pack=evil"},
        {"source": "git", "url": "--upload-pack=evil"},
        {"source": "npm", "package": "--registry=https://evil.test/pkg"},
        {"source": "npm", "package": "@scope/pkg", "version": "--ignore-scripts=false"},
    ],
)
def test_source_parser_rejects_option_injection_components(source) -> None:
    with pytest.raises(MaterializationError, match="must not start"):
        parse_marketplace_source(source)


@pytest.mark.parametrize(
    "locator",
    [
        "http://127.0.0.1/repo.git",
        "https://169.254.169.254/repo.git",
        "ssh://git@10.0.0.1/repo.git",
        "git@localhost:owner/repo.git",
    ],
)
def test_git_materialization_rejects_literal_private_hosts(
    tmp_path: Path,
    locator: str,
) -> None:
    called = False

    def runner(_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("git must not run for a rejected host")

    with pytest.raises(MaterializationError, match="local host|private address"):
        materialize_source(
            MarketplaceSource("git", locator),
            tmp_path / "materialized",
            command_runner=runner,
        )
    assert called is False


@pytest.mark.parametrize(
    "locator",
    [
        "https://user:secret@example.test/repo.git",
        "http://token@example.test/repo.git",
        "ssh://git:secret@example.test/repo.git",
        "https://example.test/repo.git#main",
    ],
)
def test_git_materialization_rejects_embedded_secrets_and_fragments(
    tmp_path: Path,
    locator: str,
) -> None:
    with pytest.raises(MaterializationError, match="credentials|password|fragment"):
        materialize_source(
            MarketplaceSource("git", locator),
            tmp_path / "materialized",
            command_runner=lambda *_args, **_kwargs: None,
        )


def test_git_clone_uses_validated_branch_and_terminates_locator_options(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    class Result:
        returncode = 0
        stderr = ""

    def runner(args, **_kwargs):
        command = [str(value) for value in args]
        commands.append(command)
        if command[:2] == ["git", "clone"]:
            destination = Path(command[-1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "marketplace.json").write_text("{}", encoding="utf-8")
        return Result()

    result = materialize_source(
        MarketplaceSource(
            "git",
            "https://example.test/owner/repo.git",
            ref="feature/safe-ref",
        ),
        tmp_path / "materialized",
        command_runner=runner,
    )

    assert result.path == tmp_path / "materialized"
    clone = next(command for command in commands if "clone" in command)
    branch_index = clone.index("--branch")
    terminator_index = clone.index("--", clone.index("clone") + 1)

    assert clone[branch_index + 1] == "feature/safe-ref"
    assert clone[clone.index("--") + 1] == "https://example.test/owner/repo.git"
    assert terminator_index > branch_index
    assert not any("fetch" in command for command in commands)


def test_legitimate_ssh_and_github_sources_remain_supported() -> None:
    github = parse_marketplace_source("owner/repo@main")
    ssh = parse_marketplace_source("git@github.com:owner/repo.git#release")
    ssh_url = parse_marketplace_source("ssh://git@github.com/owner/repo.git#release")

    assert github.locator == "https://github.com/owner/repo.git"
    assert github.ref == "main"
    assert ssh.locator == "git@github.com:owner/repo.git"
    assert ssh.ref == "release"
    assert ssh_url.locator == "ssh://git@github.com/owner/repo.git"
    assert ssh_url.ref == "release"


def test_archive_materialization_is_atomic_and_writes_provenance(tmp_path: Path) -> None:
    archive = _marketplace_archive(tmp_path)
    destination = tmp_path / "materialized"
    result = materialize_source(
        {"source": "file", "path": str(archive)},
        destination,
    )
    assert result.path == destination
    assert (destination / ".agents/plugins/marketplace.json").is_file()
    payload = json.loads(
        (destination / ".marketplace-source.json").read_text(encoding="utf-8")
    )
    assert payload["source"]["source"] == "file"


def test_archive_path_traversal_fails_closed(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../../escape.txt", "no")
    with pytest.raises(MaterializationError, match="escapes"):
        materialize_source(
            {"source": "file", "path": str(archive)},
            tmp_path / "out",
        )


def test_injected_url_transport_can_be_tested_without_network(tmp_path: Path) -> None:
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as handle:
        handle.writestr(
            "marketplace.json",
            json.dumps({"name": "remote", "plugins": []}),
        )
    archive_bytes.seek(0)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size=-1):
            return archive_bytes.read(size)

    result = materialize_source(
        {"source": "url", "url": "https://example.test/catalog.zip"},
        tmp_path / "remote",
        urlopen=lambda *_args, **_kwargs: Response(),
    )
    assert (result.path / "marketplace.json").is_file()


def test_url_content_length_limit_rejects_before_reading_body(tmp_path: Path) -> None:
    class Response:
        headers = {
            "Content-Length": str(
                materializer_module.MAX_MARKETPLACE_ARCHIVE_BYTES + 1
            )
        }
        read_called = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            self.read_called = True
            return b""

    response = Response()
    with pytest.raises(MaterializationError, match="exceeds"):
        materialize_source(
            {"source": "url", "url": "https://example.test/catalog.zip"},
            tmp_path / "remote",
            urlopen=lambda *_args, **_kwargs: response,
        )

    assert response.read_called is False


def test_tar_symlink_member_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tgz"
    with tarfile.open(archive, "w:gz") as handle:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        handle.addfile(info)
    with pytest.raises(MaterializationError, match="regular file"):
        materialize_source(
            {"source": "file", "path": str(archive)},
            tmp_path / "out",
        )


def test_staged_validation_failure_preserves_active_destination(tmp_path: Path) -> None:
    destination = tmp_path / "materialized"
    destination.mkdir()
    (destination / "marker.txt").write_text("known-good", encoding="utf-8")
    archive = _replacement_archive(tmp_path / "replacement.zip", "unvalidated")

    def reject_staged(root: Path) -> None:
        assert (root / "marker.txt").read_text(encoding="utf-8") == "unvalidated"
        raise MaterializationError("staged marketplace is invalid")

    with pytest.raises(MaterializationError, match="staged marketplace is invalid"):
        materialize_source(
            {"source": "file", "path": str(archive)},
            destination,
            validate=reject_staged,
        )

    assert (destination / "marker.txt").read_text(encoding="utf-8") == "known-good"
    assert not list(tmp_path.glob(".marketplace-stage-*"))
    assert not list(tmp_path.glob(".materialized.*.activate"))
    assert not list(tmp_path.glob(".materialized.*.backup"))


def test_activation_callback_failure_restores_previous_destination(tmp_path: Path) -> None:
    destination = tmp_path / "materialized"
    destination.mkdir()
    (destination / "marker.txt").write_text("known-good", encoding="utf-8")
    archive = _replacement_archive(tmp_path / "replacement.zip", "new-snapshot")

    def reject_commit(materialized: MaterializedSource) -> None:
        assert materialized.path == destination
        assert (destination / "marker.txt").read_text(encoding="utf-8") == "new-snapshot"
        raise OSError("registry commit failed")

    with pytest.raises(OSError, match="registry commit failed"):
        materialize_source(
            {"source": "file", "path": str(archive)},
            destination,
            after_activate=reject_commit,
        )

    assert (destination / "marker.txt").read_text(encoding="utf-8") == "known-good"
    assert not (destination / ".marketplace-source.json").exists()
    assert not list(tmp_path.glob(".marketplace-stage-*"))
    assert not list(tmp_path.glob(".materialized.*.activate"))
    assert not list(tmp_path.glob(".materialized.*.backup"))


def test_provenance_failure_never_replaces_active_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "materialized"
    destination.mkdir()
    (destination / "marker.txt").write_text("known-good", encoding="utf-8")
    archive = _replacement_archive(tmp_path / "replacement.zip", "new-snapshot")

    def fail_provenance(_destination: Path, _materialized: MaterializedSource) -> None:
        raise OSError("simulated provenance failure")

    monkeypatch.setattr(materializer_module, "_write_provenance", fail_provenance)
    with pytest.raises(OSError, match="simulated provenance failure"):
        materialize_source(
            {"source": "file", "path": str(archive)},
            destination,
        )

    assert (destination / "marker.txt").read_text(encoding="utf-8") == "known-good"
    assert not list(tmp_path.glob(".materialized.*.activate"))
    assert not list(tmp_path.glob(".materialized.*.backup"))


def test_activation_rename_failure_restores_previous_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "materialized"
    destination.mkdir()
    (destination / "marker.txt").write_text("known-good", encoding="utf-8")
    archive = _replacement_archive(tmp_path / "replacement.zip", "new-snapshot")
    original_replace = Path.replace

    def fail_new_root_replace(path: Path, target: Path) -> Path:
        if path.name.endswith(".activate") and Path(target) == destination:
            raise OSError("simulated activation rename failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_new_root_replace)
    with pytest.raises(OSError, match="simulated activation rename failure"):
        materialize_source(
            {"source": "file", "path": str(archive)},
            destination,
        )

    assert (destination / "marker.txt").read_text(encoding="utf-8") == "known-good"
    assert not list(tmp_path.glob(".materialized.*.activate"))
    assert not list(tmp_path.glob(".materialized.*.backup"))


def test_interrupted_activation_restores_backup_and_removes_siblings(tmp_path: Path) -> None:
    destination = tmp_path / "materialized"
    backup = tmp_path / ".materialized.0123456789abcdef0123456789abcdef.backup"
    activate = tmp_path / ".materialized.fedcba9876543210fedcba9876543210.activate"
    backup.mkdir()
    activate.mkdir()
    (backup / "marker.txt").write_text("known-good", encoding="utf-8")

    result = recover_materialization_artifacts(destination)

    assert result["restored"] == 1
    assert (destination / "marker.txt").read_text(encoding="utf-8") == "known-good"
    assert not backup.exists()
    assert not activate.exists()


def test_completed_activation_discards_stale_activation_siblings(tmp_path: Path) -> None:
    destination = tmp_path / "materialized"
    backup = tmp_path / ".materialized.0123456789abcdef0123456789abcdef.backup"
    activate = tmp_path / ".materialized.fedcba9876543210fedcba9876543210.activate"
    destination.mkdir()
    backup.mkdir()
    activate.mkdir()
    (destination / "marker.txt").write_text("new", encoding="utf-8")

    result = recover_materialization_artifacts(destination)

    assert result["restored"] == 0
    assert result["removed"] == 2
    assert (destination / "marker.txt").read_text(encoding="utf-8") == "new"
    assert not backup.exists()
    assert not activate.exists()
