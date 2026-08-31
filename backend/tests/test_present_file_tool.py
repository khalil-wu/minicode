from __future__ import annotations

import asyncio

from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.agent_artifact_tools import PresentFileTool


def test_present_file_validates_and_registers_known_folder_deliverable(tmp_path, monkeypatch) -> None:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    document = desktop / "test.docx"
    document.write_bytes(b"word-package")
    monkeypatch.setenv("MINICODE_DESKTOP_DIR", str(desktop))
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path / "workspace")

    result = asyncio.run(PresentFileTool().execute({"file_path": str(document)}, context=context))

    assert result.is_error is False
    assert result.output_files == [{
        "path": str(document.resolve()),
        "name": "test.docx",
        "size": len(b"word-package"),
        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "is_image": False,
    }]
    assert "[test.docx](<" in result.content


def test_present_file_accepts_a_self_contained_html_page(tmp_path, monkeypatch) -> None:
    # "Make me an animated web page" delivers exactly one .html file. Blocking
    # it as source code stranded the deliverable the user asked for, while the
    # same allowlist already passed .md/.json/.xml.
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    page = desktop / "pelican-bike.html"
    page.write_text("<!DOCTYPE html><title>bike</title>", encoding="utf-8")
    monkeypatch.setenv("MINICODE_DESKTOP_DIR", str(desktop))
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path / "workspace")

    result = asyncio.run(
        PresentFileTool().execute({"file_path": str(page), "label": "鹈鹕骑车网页"}, context=context),
    )

    assert result.is_error is False
    assert result.output_files is not None
    assert result.output_files[0]["path"] == str(page.resolve())
    assert result.output_files[0]["name"] == "鹈鹕骑车网页"
    assert result.output_files[0]["mime_type"] == "text/html"
    assert result.output_files[0]["is_image"] is False


def test_present_file_rejects_scripts_and_unapproved_external_paths(tmp_path, monkeypatch) -> None:
    desktop = tmp_path / "Desktop"
    outside = tmp_path / "private"
    desktop.mkdir()
    outside.mkdir()
    monkeypatch.setenv("MINICODE_DESKTOP_DIR", str(desktop))
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path / "workspace")
    script = desktop / "helper.py"
    script.write_text("print('x')", encoding="utf-8")
    external = outside / "report.docx"
    external.write_bytes(b"doc")

    script_result = asyncio.run(PresentFileTool().execute({"file_path": str(script)}, context=context))
    external_result = asyncio.run(PresentFileTool().execute({"file_path": str(external)}, context=context))

    assert script_result.is_error is True
    assert script_result.status == "blocked"
    assert external_result.is_error is True
    assert external_result.status == "blocked"
