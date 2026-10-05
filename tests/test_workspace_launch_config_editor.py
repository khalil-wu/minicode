import json

import pytest
from fastapi import HTTPException

from backend.preview.launcher import load_preview_launch_configs
from backend.security.sensitive_files import is_protected_write_path
from backend.workspace.service import WorkspaceService


LAUNCH_PATH = ".minicode/launch.json"


def _launch_content(command="npm run dev"):
    return json.dumps({
        "configurations": [{
            "name": "web",
            "command": command,
            "cwd": ".",
            "port": 3000,
        }],
    }) + "\n"


@pytest.mark.parametrize("directory_exists", [False, True])
def test_editor_creates_launch_config_then_preview_reads_it(tmp_path, directory_exists):
    if directory_exists:
        (tmp_path / ".minicode").mkdir()
    service = WorkspaceService(lambda: tmp_path)

    with pytest.raises(HTTPException) as missing:
        service.read_file(LAUNCH_PATH)
    assert missing.value.status_code == 404

    content = _launch_content()
    with pytest.raises(HTTPException) as stale:
        service.compare_and_write_file(LAUNCH_PATH, service.content_hash(content), content)
    assert stale.value.status_code == 409
    assert not (tmp_path / LAUNCH_PATH).exists()

    saved = service.compare_and_write_file(LAUNCH_PATH, "", content)
    snapshot = service.read_file(LAUNCH_PATH)
    configs = load_preview_launch_configs(tmp_path)

    assert snapshot.content == saved.content == content
    assert snapshot.content_hash == saved.content_hash == service.content_hash(content)
    assert snapshot.path == LAUNCH_PATH
    assert snapshot.language_hint == "json"
    assert (tmp_path / LAUNCH_PATH).read_text(encoding="utf-8") == content
    assert len(configs) == 1
    assert configs[0].source == LAUNCH_PATH
    assert configs[0].command == "npm run dev"
    assert configs[0].cwd == str(tmp_path.resolve())


def test_editor_reads_and_saves_existing_launch_config_with_hash_conflicts(tmp_path):
    target = tmp_path / LAUNCH_PATH
    target.parent.mkdir()
    target.write_text(_launch_content(), encoding="utf-8")
    service = WorkspaceService(lambda: tmp_path)
    original = service.read_file(LAUNCH_PATH)
    changed = _launch_content("npm run preview")
    target.write_text(changed, encoding="utf-8")

    with pytest.raises(HTTPException) as stale:
        service.compare_and_write_file(LAUNCH_PATH, original.content_hash, original.content)
    assert stale.value.status_code == 409
    assert target.read_text(encoding="utf-8") == changed

    current = service.read_file(LAUNCH_PATH)
    saved = service.compare_and_write_file(LAUNCH_PATH, current.content_hash, original.content)

    assert saved.content == original.content
    assert load_preview_launch_configs(tmp_path)[0].command == "npm run dev"


@pytest.mark.parametrize("path", [
    ".minicode/rules.md",
    ".minicode/nested/launch.json",
    "nested/.minicode/launch.json",
    ".mcp.json",
    ".git/config",
    ".vscode/launch.json",
])
def test_editor_launch_config_exception_keeps_other_protected_paths_blocked(tmp_path, path):
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("keep\n", encoding="utf-8")
    service = WorkspaceService(lambda: tmp_path)

    with pytest.raises(HTTPException) as read:
        service.read_file(path)
    with pytest.raises(HTTPException) as write:
        service.compare_and_write_file(path, service.content_hash("keep\n"), "changed\n")

    assert read.value.status_code == write.value.status_code == 403
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_launch_config_exception_does_not_widen_other_workspace_operations(tmp_path):
    target = tmp_path / LAUNCH_PATH
    target.parent.mkdir()
    target.write_text(_launch_content(), encoding="utf-8")
    service = WorkspaceService(lambda: tmp_path)

    assert is_protected_write_path(target)
    with pytest.raises(HTTPException) as create:
        service.write_file(LAUNCH_PATH, "changed\n")
    with pytest.raises(HTTPException) as preview:
        service.preview_file(LAUNCH_PATH)
    with pytest.raises(HTTPException) as raw:
        service.raw_file_response(LAUNCH_PATH)
    with pytest.raises(HTTPException) as indexed:
        service.read_indexed_file(target, root=tmp_path.resolve(), application_roots=())

    assert create.value.status_code == preview.value.status_code == raw.value.status_code == indexed.value.status_code == 403
    assert target.read_text(encoding="utf-8") == _launch_content()


@pytest.mark.parametrize("redirect", ["file", "directory"])
@pytest.mark.parametrize("protected_target", [False, True])
def test_editor_rejects_redirected_launch_config_symlinks(tmp_path, redirect, protected_target):
    if redirect == "file":
        destination = tmp_path / (".mcp.json" if protected_target else "config.json")
        destination.write_text("keep\n", encoding="utf-8")
        link = tmp_path / LAUNCH_PATH
        link.parent.mkdir()
        directory_link = False
    else:
        directory = tmp_path / (".vscode" if protected_target else "config")
        directory.mkdir()
        destination = directory / "launch.json"
        destination.write_text("keep\n", encoding="utf-8")
        link = tmp_path / ".minicode"
        directory_link = True
    try:
        link.symlink_to(destination.parent if directory_link else destination, target_is_directory=directory_link)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    service = WorkspaceService(lambda: tmp_path)

    with pytest.raises(HTTPException) as read:
        service.read_file(LAUNCH_PATH)
    with pytest.raises(HTTPException) as write:
        service.compare_and_write_file(LAUNCH_PATH, service.content_hash("keep\n"), "changed\n")

    assert read.value.status_code == write.value.status_code == 403
    assert destination.read_text(encoding="utf-8") == "keep\n"


def test_editor_launch_config_exception_keeps_application_state_protected(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.config.DATA_ROOT", tmp_path)
    target = tmp_path / LAUNCH_PATH
    target.parent.mkdir()
    target.write_text(_launch_content(), encoding="utf-8")
    service = WorkspaceService(lambda: tmp_path)

    with pytest.raises(HTTPException) as read:
        service.read_file(LAUNCH_PATH)
    with pytest.raises(HTTPException) as write:
        service.compare_and_write_file(LAUNCH_PATH, service.content_hash(_launch_content()), "changed\n")

    assert read.value.status_code == write.value.status_code == 403
    assert target.read_text(encoding="utf-8") == _launch_content()
