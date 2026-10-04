import hashlib
import os
from pathlib import Path
import subprocess

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.workspace.api import create_workspace_router
from backend.workspace.service import WorkspaceService


def _write(root: Path, path: str, content: str | bytes) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return target


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(create_workspace_router())
    return app


def test_project_index_api_returns_complete_cross_directory_sources_and_read_file_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda root: root == tmp_path)
    source = "export const 标题 = '原始快照';\r\n"
    _write(tmp_path, "src/中文名字.ts", source)
    _write(tmp_path, "tests/app.test.tsx", "export const test = <div />;\n")
    _write(tmp_path, "shared/globals.d.ts", "declare const shared: string;\n")
    _write(tmp_path, "tsconfig.json", '{ // JSONC is preserved\n"extends": "./configs/tsconfig.base.json"\n}\n')
    _write(tmp_path, "configs/tsconfig.base.json", '{"compilerOptions":{"strict":true}}')
    _write(tmp_path, "packages/client/jsconfig.json", '{"compilerOptions":{"checkJs":true}}')
    _write(tmp_path, "packages/client/package.json", '{"name":"client","type":"module"}')
    for suffix in ["js", "jsx", "mjs", "cjs", "mts", "cts"]:
        _write(tmp_path, f"packages/client/module.{suffix}", "export {}\n")
    for index in range(125):
        _write(tmp_path, f"packages/shared/nested/module-{index:03}.ts", f"export const value{index} = {index};\n")

    with TestClient(_app()) as client:
        response = client.get("/api/workspace/project-index", params={"workspace_root": str(tmp_path)})
    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace_root"] == str(tmp_path.resolve())
    assert payload["complete"] is True
    assert payload["issues"] == []
    indexed = {file["path"]: file for file in payload["files"]}
    assert len(indexed) == 138
    assert indexed["src/中文名字.ts"]["content"] == source
    assert indexed["src/中文名字.ts"]["content_hash"] == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert indexed["src/中文名字.ts"] == {
        **WorkspaceService(lambda: tmp_path).read_file("src/中文名字.ts").model_dump(), "kind": "source",
    }
    assert indexed["shared/globals.d.ts"]["kind"] == "declaration"
    assert indexed["configs/tsconfig.base.json"]["kind"] == "config"
    assert indexed["packages/client/jsconfig.json"]["kind"] == "config"
    assert indexed["packages/client/package.json"]["kind"] == "package"
    assert indexed["packages/shared/nested/module-124.ts"]["kind"] == "source"


def test_dependency_pass_reads_real_declarations_and_metadata_without_dependency_implementations(tmp_path):
    expected = {
        "node_modules/library/dist/index.d.ts": "declaration",
        "node_modules/library/build/module.d.mts": "declaration",
        "node_modules/library/module.d.cts": "declaration",
        "node_modules/library/package.json": "package",
        "node_modules/@tsconfig/base/tsconfig.json": "config",
        "frontend/node_modules/.pnpm/nested@1/node_modules/nested/dist/index.d.ts": "declaration",
        "frontend/node_modules/nested/jsconfig.json": "config",
    }
    for path in expected:
        _write(tmp_path, path, "{}" if path.endswith(".json") else "export interface Shared {}\n")
    _write(tmp_path, "node_modules/library/implementation.js", b"\xff\xfe\x00")
    _write(tmp_path, "node_modules/library/image.png", b"\xff\xfe\x00")
    _write(tmp_path, "frontend/node_modules/nested/implementation.ts", "export const implementation = 1")
    _write(tmp_path, ".gitignore", "node_modules/\n")

    index = WorkspaceService(lambda: tmp_path).project_index()
    assert index.complete is True
    assert {file.path: file.kind for file in index.files} == expected


def test_sources_follow_nested_gitignore_hidden_directory_and_protected_state_rules(tmp_path, monkeypatch):
    _write(tmp_path, ".gitignore", "generated/\n**/*.skip.ts\n!keep.skip.ts\n")
    _write(tmp_path, "src/.gitignore", "private.ts\n")
    _write(tmp_path, "src/app.ts", "export {}")
    _write(tmp_path, "keep.skip.ts", "export {}")
    _write(tmp_path, ".hidden/visible.ts", "export {}")
    for path in [
        "generated/ignored.ts", "nested/blocked.skip.ts", "src/private.ts",
        ".git/ignored.ts", ".vscode/ignored.ts", ".idea/ignored.ts", ".minicode/ignored.ts",
        "dist/output.ts", "build/output.ts", "state/storage.ts", "state/node_modules/private/types.d.ts",
    ]:
        _write(tmp_path, path, b"\xff\xfe")
    monkeypatch.setattr("backend.security.sensitive_files.application_state_roots", lambda: (tmp_path / "state",))

    index = WorkspaceService(lambda: tmp_path).project_index()
    assert index.complete is True
    assert {file.path for file in index.files} == {"src/app.ts", "keep.skip.ts", ".hidden/visible.ts"}


@pytest.mark.parametrize("link_name", ["external-link", "node_modules"])
def test_project_index_does_not_cross_directory_links_or_dependency_junctions(tmp_path, link_name):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    _write(root, "inside.ts", "export {}")
    _write(outside, "outside.ts", "export const outside = true")
    _write(outside, "outside.d.ts", "declare const outside: true")
    link = root / link_name
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)

    index = WorkspaceService(lambda: root).project_index()
    assert index.complete is True
    assert [file.path for file in index.files] == ["inside.ts"]


def test_project_index_reports_failed_file_reads_without_claiming_complete_data(tmp_path):
    _write(tmp_path, "valid.ts", "export {}")
    _write(tmp_path, "invalid.ts", b"\xff\xfe")
    _write(tmp_path, "oversized.ts", b"x" * 65)

    index = WorkspaceService(lambda: tmp_path, max_file_bytes=64).project_index()
    assert index.complete is False
    assert [file.path for file in index.files] == ["valid.ts"]
    assert [(issue.path, issue.status_code) for issue in index.issues] == [("invalid.ts", 400), ("oversized.ts", 413)]
    assert "UTF-8" in index.issues[0].message
    assert "too large" in index.issues[1].message


def test_project_index_marks_a_file_removed_during_read_as_missing_then_refreshes(tmp_path, monkeypatch):
    _write(tmp_path, "removed.ts", "export {}")
    service = WorkspaceService(lambda: tmp_path)
    read_file = service.read_indexed_file

    def remove_before_read(path, **kwargs):
        path.unlink()
        return read_file(path, **kwargs)

    monkeypatch.setattr(service, "read_indexed_file", remove_before_read)
    first = service.project_index()
    assert first.complete is False
    assert [(issue.path, issue.status_code) for issue in first.issues] == [("removed.ts", 404)]
    second = service.project_index()
    assert second.complete is True
    assert second.files == []


def test_source_only_refresh_updates_additions_deletions_configs_and_content_without_scanning_dependencies(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: True)
    _write(tmp_path, "src/old.ts", "export const old = true")
    _write(tmp_path, "src/changed.js", "export const changed = 1")
    _write(tmp_path, "src/project.d.ts", "declare const local: true")
    _write(tmp_path, "tsconfig.json", '{"compilerOptions":{"strict":true}}')
    _write(tmp_path, "node_modules/library/index.d.ts", "export interface Remote {}")
    scandir = os.scandir

    def recorded(path):
        assert "node_modules" not in Path(path).parts
        return scandir(path)

    monkeypatch.setattr(os, "scandir", recorded)
    with TestClient(_app()) as client:
        params = {"workspace_root": str(tmp_path), "include_dependencies": "false"}
        first = client.get("/api/workspace/project-index", params=params).json()
        (tmp_path / "src/old.ts").unlink()
        _write(tmp_path, "src/new.tsx", "export const newFile = <div />")
        _write(tmp_path, "src/changed.js", "export const changed = 2")
        _write(tmp_path, "tsconfig.json", '{"compilerOptions":{"strict":false}}')
        second = client.get("/api/workspace/project-index", params=params).json()
    assert first["complete"] is True
    assert second["complete"] is True
    assert {file["path"] for file in first["files"]} == {"src/old.ts", "src/changed.js", "src/project.d.ts", "tsconfig.json"}
    indexed = {file["path"]: file for file in second["files"]}
    assert set(indexed) == {"src/new.tsx", "src/changed.js", "src/project.d.ts", "tsconfig.json"}
    assert indexed["src/changed.js"]["content"] == "export const changed = 2"
    assert indexed["tsconfig.json"]["content"] == '{"compilerOptions":{"strict":false}}'
    assert indexed["src/project.d.ts"]["kind"] == "declaration"


def test_project_index_api_requires_the_same_explicit_trusted_workspace_as_file_reads(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: False)
    with TestClient(_app()) as client:
        assert client.get("/api/workspace/project-index").status_code == 422
        denied = client.get("/api/workspace/project-index", params={"workspace_root": str(tmp_path)})
        assert denied.status_code == 403
        missing = client.get("/api/workspace/project-index", params={"workspace_root": str(tmp_path / "missing")})
        assert missing.status_code == 409


def test_project_index_directory_failures_are_explicit_api_errors(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: True)
    scandir = os.scandir

    def denied(path):
        if Path(path) == tmp_path:
            raise PermissionError("fixture directory read denied")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", denied)
    with TestClient(_app()) as client:
        response = client.get("/api/workspace/project-index", params={"workspace_root": str(tmp_path)})
    assert response.status_code == 500
    assert "fixture directory read denied" in response.json()["detail"]


@pytest.mark.parametrize("content", ["", "\ufeffexport const 值 = 1;\r\n", "export const value = '中文';\n"])
def test_parallel_index_snapshot_matches_ordinary_read_file_exactly(tmp_path, content):
    target = _write(tmp_path, "src/snapshot.ts", content)
    os.utime(target, (1_700_000_000, 1_700_000_000))
    service = WorkspaceService(lambda: tmp_path)
    expected = service.read_file("src/snapshot.ts").model_dump()
    index = service.project_index(include_dependencies=False)
    assert index.complete is True
    assert len(index.files) == 1
    actual = index.files[0].model_dump()
    assert actual.pop("kind") == "source"
    assert actual == expected


@pytest.mark.parametrize("target_kind,status_code", [("outside", 400), ("protected_inside", 403)])
def test_indexed_reader_rechecks_alias_real_target_before_reading_content(tmp_path, monkeypatch, target_kind, status_code):
    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "outside" if target_kind == "outside" else root / "application-storage"
    secret = _write(target, "secret.ts", "export const secret = 'not a project source';")
    alias = root / "alias"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(target)], check=True, capture_output=True)
    else:
        alias.symlink_to(target, target_is_directory=True)
    protected_roots = (target.resolve(),) if target_kind == "protected_inside" else ()
    monkeypatch.setattr("backend.security.sensitive_files.application_state_roots", lambda: protected_roots)
    original_open = Path.open

    def never_open_secret(path, mode="r", *args, **kwargs):
        assert path != secret or mode != "rb", "The real target must be rejected before reading bytes"
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", never_open_secret)
    service = WorkspaceService(lambda: root)
    with pytest.raises(HTTPException) as ordinary_failure:
        service.read_file("alias/secret.ts")
    with pytest.raises(HTTPException) as indexed_failure:
        # This also covers a previously scanned path becoming an alias before
        # its parallel reader runs: traversal evidence cannot skip this check.
        service.read_indexed_file(alias / "secret.ts", root=root.resolve(), application_roots=protected_roots)
    assert ordinary_failure.value.status_code == status_code
    assert indexed_failure.value.status_code == status_code


def test_index_reuses_application_roots_only_within_one_request_and_refreshes_them_next_time(tmp_path, monkeypatch):
    _write(tmp_path, "first-state/types.ts", "export const first = 1;")
    _write(tmp_path, "second-state/types.ts", "export const second = 2;")
    roots = [tmp_path / "first-state"]
    requests = []

    def application_roots():
        requests.append(tuple(roots))
        return tuple(roots)

    monkeypatch.setattr("backend.security.sensitive_files.application_state_roots", application_roots)
    service = WorkspaceService(lambda: tmp_path)
    first = service.project_index(include_dependencies=False)
    assert first.complete is True
    assert [file.path for file in first.files] == ["second-state/types.ts"]
    roots[:] = [tmp_path / "second-state"]
    second = service.project_index(include_dependencies=False)
    assert second.complete is True
    assert [file.path for file in second.files] == ["first-state/types.ts"]
    assert len(requests) == 2
