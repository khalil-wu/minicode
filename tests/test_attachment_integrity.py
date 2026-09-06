from __future__ import annotations

import asyncio
import base64
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

from backend.agent.attachment_policy import build_attachment_input_plan
from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.documents.parsers import PDFPageLimitError
from backend.documents.service import ingest_uploaded_document, parse_document_preview
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.artifact_service import read_artifact_content
from backend.services.chat_api_service import AttachmentUploadContext, upload_document_payload
from backend.tools.agent_artifact_tools import ReadArtifactTool


def _word_bytes() -> bytes:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("BEFORE_TABLE")
    cell = document.add_table(rows=1, cols=1).cell(0, 0)
    cell.text = "TABLE_CONTENT"
    cell.add_table(rows=1, cols=1).cell(0, 0).text = "NESTED_TABLE_CONTENT"
    document.add_paragraph("AFTER_TABLE")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _workbook_bytes(target: str) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("xl/workbook.xml", '''
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets><sheet name="Budget" sheetId="1" r:id="rId1"/></sheets>
            </workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", f'''
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Target="{target}"/>
            </Relationships>''')
        archive.writestr("xl/worksheets/sheet1.xml", '''
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>
                <row r="1"><c r="A1" t="inlineStr"><is><t>Name</t></is></c>
                  <c r="B1" t="inlineStr"><is><t>Quantity</t></is></c>
                  <c r="C1" t="inlineStr"><is><t>Price</t></is></c></row>
                <row r="2"><c r="A2" t="inlineStr"><is><t>Alice</t></is></c>
                  <c r="C2"><v>42</v></c></row>
                <row r="4"><c r="C4"><f>SUM(C2:C3)</f></c></row>
                <row r="205"><c r="AA205" t="inlineStr"><is><t>TAIL_CELL</t></is></c></row>
              </sheetData>
            </worksheet>''')
    return output.getvalue()


def _presentation_bytes(order: list[int], *, broken_slide: int = 0) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        slides = "".join(f'<p:sldId id="{255 + number}" r:id="slide{number}"/>' for number in order)
        archive.writestr("ppt/presentation.xml", f'''
            <p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <p:sldIdLst>{slides}</p:sldIdLst>
            </p:presentation>''')
        relationships = "".join(
            f'<Relationship Id="slide{number}" Target="/ppt/slides/slide{number}.xml"/>'
            for number in reversed(order)
        )
        archive.writestr("ppt/_rels/presentation.xml.rels", f'''
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              {relationships}
            </Relationships>''')
        for number in sorted(order):
            archive.writestr(f"ppt/slides/slide{number}.xml", "<broken" if number == broken_slide else f'''
                <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
                  <a:p><a:r><a:t>SLIDE_{number:02d}</a:t></a:r></a:p>
                </p:sld>''')
    return output.getvalue()


def _upload_context(tmp_path: Path, *, owner: str = "owner-a") -> AttachmentUploadContext:
    return AttachmentUploadContext(
        session_id="session-documents",
        conversation_id=owner,
        conversation=object(),
        workspace_root=tmp_path,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        attachment_store=AttachmentStore(tmp_path / "attachments"),
    )


def test_word_body_tables_and_nested_tables_keep_document_order() -> None:
    parsed = parse_document_preview("contract.docx", _word_bytes())

    text = parsed["full_text"]
    assert text.index("BEFORE_TABLE") < text.index("TABLE_CONTENT")
    assert text.index("TABLE_CONTENT") < text.index("NESTED_TABLE_CONTENT") < text.index("AFTER_TABLE")
    assert parsed["title"] == "contract"
    assert parsed["pages"] == 0  # Word pagination requires a layout engine.
    assert not parsed.get("parse_error")


@pytest.mark.parametrize("target", [
    "/xl/worksheets/sheet1.xml", "worksheets/sheet1.xml", "worksheets/../worksheets/sheet1.xml",
])
def test_workbook_relationships_keep_sparse_coordinates_and_tail_cells(target: str) -> None:
    parsed = parse_document_preview("budget.xlsx", _workbook_bytes(target))

    assert not parsed.get("parse_error")
    assert "A1: Name | B1: Quantity | C1: Price" in parsed["full_text"]
    assert "A2: Alice | C2: 42" in parsed["full_text"]
    assert "C4: =SUM(C2:C3)" in parsed["full_text"]
    assert "AA205: TAIL_CELL" in parsed["full_text"]


def test_missing_workbook_part_is_a_diagnostic_not_a_successful_empty_sheet() -> None:
    parsed = parse_document_preview("broken.xlsx", _workbook_bytes("worksheets/missing.xml"))

    assert "missing.xml" in parsed["parse_error"]
    assert "No extractable text" in parsed["full_text"]


def test_slides_follow_presentation_order_instead_of_zip_or_filename_order() -> None:
    order = [12, 2, 10, 1, 3, 4, 5, 6, 7, 8, 9, 11]
    parsed = parse_document_preview("deck.pptx", _presentation_bytes(order))

    positions = [parsed["full_text"].index(f"SLIDE_{number:02d}") for number in order]
    assert positions == sorted(positions)
    assert parsed["pages"] == 12
    assert not parsed.get("parse_error")


def test_partial_presentation_keeps_readable_slides_and_warns_the_model(tmp_path: Path) -> None:
    context = _upload_context(tmp_path)
    result = upload_document_payload(
        context=context, file_name="partial.pptx", raw_content=_presentation_bytes([2, 1], broken_slide=1),
    )
    attachment = result["attachment"]
    assert not attachment.get("parse_error")
    assert "slide 2" in attachment["parse_warning"]

    plan = build_attachment_input_plan(
        [attachment], attachment_store=context.attachment_store,
        conversation_id=context.conversation_id, workspace_root=str(tmp_path),
    )
    assert "SLIDE_02" in plan.inlined_texts[0]["content"]
    assert "slide 2" in "\n".join(plan.text_hints)
    assert plan.documents == []


def test_archive_truncation_is_reported_without_suppressing_readable_text() -> None:
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("long.txt", "start " + "x" * 7000)
        for number in range(12):
            archive.writestr(f"file-{number}.txt", f"entry {number}")

    parsed = parse_document_preview("bundle.zip", output.getvalue())

    assert "start " in parsed["full_text"]
    assert "6,000" in parsed["parse_warning"]
    assert "first 12 of 13" in parsed["parse_warning"]
    assert not parsed.get("parse_error")


@pytest.mark.parametrize("pages", [100, 101])
def test_compressed_pdf_page_limit_is_checked_before_text_extraction(monkeypatch, pages: int) -> None:
    pymupdf = pytest.importorskip("pymupdf")
    converter = pytest.importorskip("pymupdf4llm")
    with pymupdf.open() as document:
        for _ in range(pages):
            document.new_page()
        raw = document.tobytes(garbage=4, deflate=True, use_objstms=1)
    extract = Mock(return_value="Extracted PDF text")
    monkeypatch.setattr(converter, "to_markdown", extract)

    if pages == 101:
        with pytest.raises(PDFPageLimitError, match="101 pages; the limit is 100"):
            parse_document_preview("large.pdf", raw)
        extract.assert_not_called()
    else:
        parsed = parse_document_preview("allowed.pdf", raw)
        assert parsed["pages"] == 100
        extract.assert_called_once()


def test_textless_pdf_retains_real_page_count_and_original(tmp_path: Path) -> None:
    pymupdf = pytest.importorskip("pymupdf")
    with pymupdf.open() as document:
        document.new_page()
        document.new_page()
        raw = document.tobytes(garbage=4, deflate=True, use_objstms=1)

    result = ingest_uploaded_document(file_name="scanned.pdf", raw_content=raw,
                                      artifact_store=ArtifactStore(storage_dir=tmp_path))

    assert result.attachment.page_count == 2
    assert result.attachment.parse_error
    assert base64.b64decode(result.attachment.data) == raw


def test_encrypted_pdf_is_retained_with_an_explicit_diagnostic(tmp_path: Path) -> None:
    pymupdf = pytest.importorskip("pymupdf")
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Private PDF")
        raw = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="reader")

    result = ingest_uploaded_document(file_name="encrypted.pdf", raw_content=raw,
                                      artifact_store=ArtifactStore(storage_dir=tmp_path))

    assert "encrypted" in result.attachment.parse_error
    assert result.attachment.page_count == 0
    assert base64.b64decode(result.attachment.data) == raw


def test_missing_pdf_dependency_preserves_original_and_reports_unknown_pages(monkeypatch, tmp_path: Path) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "pymupdf", None)
    raw = b"%PDF-1.7\noriginal-body"
    result = ingest_uploaded_document(file_name="paper.pdf", raw_content=raw,
                                      artifact_store=ArtifactStore(storage_dir=tmp_path))

    assert "pymupdf" in result.attachment.parse_error
    assert result.attachment.page_count == 0
    assert base64.b64decode(result.attachment.data) == raw


@pytest.mark.parametrize("file_name, raw", [
    ("notes.txt", b"plain text"),
    ("page.html", b"<html><script>example()</script></html>"),
    ("broken.docx", b"invalid Word original"),
    ("broken.xlsx", b"invalid Excel original"),
    ("broken.pptx", b"invalid PowerPoint original"),
    ("broken.zip", b"invalid archive original"),
    ("data.bin", b"\x00\x01\x02\xffbinary"),
])
def test_original_bytes_survive_restart_without_entering_transport_metadata(tmp_path: Path, file_name: str, raw: bytes) -> None:
    context = _upload_context(tmp_path)
    result = upload_document_payload(context=context, file_name=file_name, raw_content=raw)
    assert "data" not in result["attachment"]

    restored = AttachmentStore(tmp_path / "attachments")
    payload = restored.find_payload(file_name, conversation_id="owner-a", workspace_root=str(tmp_path))
    assert payload is not None
    assert base64.b64decode(payload["native_data"]) == raw
    assert "data" not in payload["metadata"]["attachment"]
    assert restored.get_native_data(result["artifact_id"], conversation_id="owner-b", workspace_root=str(tmp_path)) is None


def test_same_name_aliases_survive_migration_overwrite_fork_and_delete(tmp_path: Path) -> None:
    directory = tmp_path / "attachments"
    store = AttachmentStore(directory)
    for artifact_id, owner in [("z_first", "owner-a"), ("a_second", "owner-b"), ("b_latest", "owner-a")]:
        store.save(artifact_id=artifact_id, content=artifact_id, metadata={
            "conversation_id": owner, "workspace_root": str(tmp_path),
            "attachment": {"file_name": "report.txt", "doc_id": f"doc_{artifact_id}"},
        })
    (directory / "index.json").write_text(json.dumps({"report.txt": "a_second"}), encoding="utf-8")
    store = AttachmentStore(directory)
    assert store.resolve_content("REPORT.TXT", conversation_id="owner-a", workspace_root=str(tmp_path))[0] == "b_latest"
    assert store.resolve_content("report.txt", conversation_id="owner-b", workspace_root=str(tmp_path))[0] == "a_second"
    assert store.find_payload("report.txt", conversation_id="owner-c", workspace_root=str(tmp_path)) is None

    store.save(artifact_id="b_latest", content="renamed", metadata={
        "conversation_id": "owner-a", "workspace_root": str(tmp_path),
        "attachment": {"file_name": "renamed.txt"},
    })
    assert store.resolve_content("report.txt", conversation_id="owner-a", workspace_root=str(tmp_path))[0] == "z_first"
    assert store.find_payload("doc_b_latest", conversation_id="owner-a", workspace_root=str(tmp_path)) is None
    assert store.share_for_conversation("owner-a", "fork", tmp_path) == 2
    assert store.delete_for_conversation("owner-a") == 0
    assert store.resolve_content("report.txt", conversation_id="fork", workspace_root=str(tmp_path))[0] == "z_first"
    assert store.delete_for_conversation("fork") == 2
    assert store.resolve_content("report.txt", conversation_id="owner-b", workspace_root=str(tmp_path))[0] == "a_second"


def test_concurrent_sessions_can_resolve_their_own_same_name_attachment(tmp_path: Path) -> None:
    def upload(number: int) -> None:
        AttachmentStore(tmp_path).save(artifact_id=f"artifact_{number}", content=f"content {number}", metadata={
            "conversation_id": f"owner-{number}",
            "attachment": {"file_name": "report.txt"},
        })

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(upload, range(16)))

    restored = AttachmentStore(tmp_path)
    for number in range(16):
        assert restored.resolve_content("report.txt", conversation_id=f"owner-{number}")[1] == f"content {number}"


def test_reparse_updates_model_and_preview_without_losing_fork_access(monkeypatch, tmp_path: Path) -> None:
    raw = _word_bytes()
    context = _upload_context(tmp_path)
    with monkeypatch.context() as unavailable:
        unavailable.setattr("backend.documents.service._parse_docx", Mock(side_effect=ImportError("parser unavailable")))
        uploaded = upload_document_payload(context=context, file_name="contract.docx", raw_content=raw)
    artifact_id = uploaded["artifact_id"]
    assert uploaded["attachment"]["parse_error"]
    store = context.attachment_store
    assert store.share_for_conversation("owner-a", "fork", tmp_path) == 1
    original_scopes = store.get_metadata(artifact_id, conversation_id="fork", workspace_root=str(tmp_path))["owner_scopes"]
    tool = ReadArtifactTool(context.artifact_store, attachment_store=store)
    tool_context = ToolExecutionContext(permission=PermissionContext(), conversation_id="fork", workspace_root=tmp_path)

    result = asyncio.run(tool.execute({"artifact_id": "contract.docx"}, context=tool_context))

    assert not result.is_error
    assert "NESTED_TABLE_CONTENT" in result.content
    assert artifact_id in result.display_summary
    restored = AttachmentStore(tmp_path / "attachments")
    metadata = restored.get_metadata(artifact_id, conversation_id="fork", workspace_root=str(tmp_path))
    assert metadata["owner_scopes"] == original_scopes
    assert metadata["attachment"]["parse_error"] == ""
    assert base64.b64decode(restored.get_native_data(artifact_id, conversation_id="fork", workspace_root=str(tmp_path))) == raw

    preview = read_artifact_content(context.artifact_store, restored, artifact_id,
                                    conversation_id="fork", workspace_root=str(tmp_path))
    assert "BEFORE_TABLE" in preview.preview
    assert "NESTED_TABLE_CONTENT" in preview.content
    assert preview.to_event().data["has_native"] is True
    assert preview.to_event().data["parse_error"] == ""
    plan = build_attachment_input_plan([uploaded["attachment"]], attachment_store=restored,
                                       conversation_id="fork", workspace_root=str(tmp_path))
    assert "NESTED_TABLE_CONTENT" in plan.inlined_texts[0]["content"]
    assert not plan.documents
    assert not plan.text_hints

    assert restored.delete_for_conversation("owner-a") == 0
    assert restored.get(artifact_id, conversation_id="fork", workspace_root=str(tmp_path)) == preview.content


def test_error_prefixed_user_text_is_content_not_a_parser_diagnostic(tmp_path: Path) -> None:
    context = _upload_context(tmp_path)
    text = "Error: this is a troubleshooting guide.\n错误: 这是原文。"
    uploaded = upload_document_payload(context=context, file_name="guide.txt", raw_content=text.encode())
    result = asyncio.run(ReadArtifactTool(context.artifact_store, attachment_store=context.attachment_store).execute(
        {"artifact_id": uploaded["artifact_id"]},
        context=ToolExecutionContext(permission=PermissionContext(), conversation_id="owner-a", workspace_root=tmp_path),
    ))

    assert result.content == text
    assert not uploaded["attachment"].get("parse_error")
