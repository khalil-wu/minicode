from __future__ import annotations

import asyncio
import base64
import io
import zipfile
from types import SimpleNamespace

from backend.agent.context import ContextBuilder
from backend.agent.attachment_policy import build_attachment_input_plan
from backend.agent.state import AgentState
from backend.config import LLMSettings
from backend.attachments.store import AttachmentStore
from backend.artifact.store import ArtifactStore
from backend.documents.service import ingest_uploaded_document
from backend.services.chat_api_service import (
    AttachmentUploadContext,
    ChatApiServiceError,
    upload_document_payload,
)
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMMessage
from backend.llm.capabilities import ProviderCapabilities
from backend.tools.agent_tools import ReadArtifactTool
from backend.llm.openai_adapter import OpenAIAdapter
import pytest


def test_ingest_python_file_creates_code_attachment() -> None:
    result = ingest_uploaded_document(
        file_name="script.py",
        raw_content=b"print('hello')\n",
        artifact_store=ArtifactStore(),
    )

    assert result.attachment.kind == "code"
    assert result.attachment.media_type == "text/x-python"
    assert "print('hello')" in result.full_text


def test_ingest_markdown_file_creates_document_attachment() -> None:
    result = ingest_uploaded_document(
        file_name="notes.md",
        raw_content="# Title\n\nbody text\n".encode("utf-8"),
        artifact_store=ArtifactStore(),
    )

    assert result.attachment.kind == "document"
    assert result.attachment.media_type == "text/markdown"
    assert result.attachment.summary == "Markdown document"


@pytest.mark.parametrize("file_name", [".gitconfig", ".bashrc", ".mcp.json"])
def test_dangerous_config_files_cannot_be_uploaded(file_name: str, tmp_path) -> None:
    with pytest.raises(ValueError, match="Sensitive files"):
        ingest_uploaded_document(
            file_name=file_name,
            raw_content=b"secret",
            artifact_store=ArtifactStore(storage_dir=tmp_path),
        )


@pytest.mark.parametrize("file_name", [".env", "id_rsa", "secret.pem"])
def test_credential_files_are_uploadable(file_name: str, tmp_path) -> None:
    # MiniCode has no credential-file hard-refuse list;
    # only dangerous config paths are blocked.
    result = ingest_uploaded_document(
        file_name=file_name,
        raw_content=b"secret",
        artifact_store=ArtifactStore(storage_dir=tmp_path),
    )
    assert result.file_name == file_name


def test_env_example_remains_uploadable(tmp_path) -> None:
    result = ingest_uploaded_document(
        file_name=".env.example",
        raw_content=b"KEY=placeholder\n",
        artifact_store=ArtifactStore(storage_dir=tmp_path),
    )
    assert result.file_name == ".env.example"


def test_ingest_zip_archive_summarizes_entries_and_indexes_text_members() -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("src/main.py", "print('zip ok')\n")
        zf.writestr("README.md", "# Archive\n\nhello zip\n")

    result = ingest_uploaded_document(
        file_name="bundle.zip",
        raw_content=archive.getvalue(),
        artifact_store=ArtifactStore(),
    )

    assert result.attachment.kind == "archive"
    assert result.attachment.media_type == "application/zip"
    assert "src/main.py" in result.full_text
    assert "hello zip" in result.full_text


def test_ingest_pptx_extracts_slide_text_without_optional_dependencies() -> None:
    presentation = io.BytesIO()
    with zipfile.ZipFile(presentation, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
              <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
            </Types>""",
        )
        zf.writestr(
            "ppt/slides/slide1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
                   xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
              <p:cSld>
                <p:spTree>
                  <p:sp>
                    <p:txBody>
                      <a:p>
                        <a:r><a:t>Hello slide</a:t></a:r>
                      </a:p>
                    </p:txBody>
                  </p:sp>
                </p:spTree>
              </p:cSld>
            </p:sld>""",
        )

    result = ingest_uploaded_document(
        file_name="deck.pptx",
        raw_content=presentation.getvalue(),
        artifact_store=ArtifactStore(),
    )

    assert result.attachment.kind == "presentation"
    assert result.attachment.media_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    assert "Hello slide" in result.full_text


def test_binary_document_cleanup_retries_windows_file_lock(monkeypatch) -> None:
    attempts = 0

    def flaky_remove(path: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(32, "file is in use")

    monkeypatch.setattr("backend.documents.service.os.remove", flaky_remove)
    monkeypatch.setattr(
        "backend.documents.service._parse_pdf",
        lambda path: {"title": "Paper", "full_text": "Extracted text", "format": "pdf", "pages": 1},
    )

    result = ingest_uploaded_document(
        file_name="paper.pdf",
        raw_content=b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n",
        artifact_store=ArtifactStore(),
    )

    assert result.attachment.media_type == "application/pdf"
    assert attempts == 2


def test_ingest_xlsx_extracts_sheet_cells_into_readable_text() -> None:
    workbook = io.BytesIO()
    with zipfile.ZipFile(workbook, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
              <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
              <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
              <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
            </Types>""",
        )
        zf.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets>
                <sheet name="Sheet1" sheetId="1" r:id="rId1"/>
              </sheets>
            </workbook>""",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
            </Relationships>""",
        )
        zf.writestr(
            "xl/sharedStrings.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <si><t>Field</t></si>
              <si><t>Description</t></si>
              <si><t>user_id</t></si>
              <si><t>Primary key</t></si>
            </sst>""",
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>
                <row r="1">
                  <c r="A1" t="s"><v>0</v></c>
                  <c r="B1" t="s"><v>1</v></c>
                </row>
                <row r="2">
                  <c r="A2" t="s"><v>2</v></c>
                  <c r="B2" t="s"><v>3</v></c>
                </row>
              </sheetData>
            </worksheet>""",
        )

    result = ingest_uploaded_document(
        file_name="dictionary.xlsx",
        raw_content=workbook.getvalue(),
        artifact_store=ArtifactStore(),
    )

    assert result.attachment.kind == "document"
    assert result.attachment.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "Sheet1" in result.full_text
    assert "Field | Description" in result.full_text
    assert "user_id | Primary key" in result.full_text


def test_ingest_image_keeps_native_multimodal_data() -> None:

    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01"
        b"\x0b\xe7\x02\x9d"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    result = ingest_uploaded_document(
        file_name="cat.png",
        raw_content=png_bytes,
        artifact_store=ArtifactStore(),
    )

    assert result.attachment.kind == "image"
    assert result.attachment.media_type == "image/png"
    assert result.attachment.data == base64.b64encode(png_bytes).decode("ascii")
    assert "native multimodal" in result.full_text


def test_ingested_image_attachment_reaches_context_as_multimodal_input() -> None:
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01"
        b"\x0b\xe7\x02\x9d"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    result = ingest_uploaded_document(
        file_name="screen.png",
        raw_content=png_bytes,
        artifact_store=ArtifactStore(),
    )
    state = AgentState(user_message="what is in this image?")
    state.attachments = [result.attachment.to_dict()]

    messages = asyncio.run(ContextBuilder().build(state.user_message, state))
    user_message = messages[-1]

    assert user_message.role == "user"
    assert user_message.images == [
        {
            "media_type": "image/png",
            "data": base64.b64encode(png_bytes).decode("ascii"),
        }
    ]
    assert user_message.documents == []


def test_image_attachment_uses_metadata_fallback_for_non_vision_model() -> None:
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01"
        b"\x0b\xe7\x02\x9d"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    result = ingest_uploaded_document(
        file_name="screen.png",
        raw_content=png_bytes,
        artifact_store=ArtifactStore(),
    )
    state = AgentState(user_message="what is in this image?")
    state.attachments = [result.attachment.to_dict()]
    llm = SimpleNamespace(
        capabilities=ProviderCapabilities(
            provider="custom",
            model="text-only-model",
            wire_api="chat",
            vision=False,
            confidence="configured",
        )
    )

    messages = asyncio.run(ContextBuilder(llm=llm).build(state.user_message, state))
    user_message = messages[-1]

    assert user_message.images == []
    assert "does not support native image input" in user_message.content
    assert "switch to a vision-capable model" in user_message.content


def test_image_attachment_fallback_omits_empty_artifact_reference_for_non_vision_model() -> None:
    llm = SimpleNamespace(
        capabilities=ProviderCapabilities(
            provider="custom",
            model="text-only-model",
            wire_api="chat",
            vision=False,
            confidence="configured",
        )
    )

    plan = build_attachment_input_plan(
        [
            {
                "kind": "image",
                "file_name": "screen.png",
                "media_type": "image/png",
                "data": "iVBORw0KGgo=",
                "artifact_id": "",
            }
        ],
        llm=llm,
    )

    hints = "\n".join(plan.text_hints)
    assert plan.images == []
    assert "read_artifact('')" not in hints
    assert "the attachment metadata" in hints


def test_gateway_hostname_and_model_name_do_not_suppress_unknown_image_support() -> None:
    for base_url, model in (
        ("https://api.deepseek.com/v1", "deepseek-v4-pro"),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ):
        llm = OpenAIAdapter(
            settings=LLMSettings(
                api_key="test",
                provider="custom",
                base_url=base_url,
                model=model,
                wire_api="chat",
            )
        )
        plan = build_attachment_input_plan(
            [{
                "kind": "image",
                "file_name": "screen.png",
                "media_type": "image/png",
                "data": "iVBORw0KGgo=",
                "artifact_id": "",
            }],
            llm=llm,
        )

        assert plan.images == [{"media_type": "image/png", "data": "iVBORw0KGgo="}]
        assert plan.text_hints == []


def test_ingested_pdf_attachment_reaches_context_as_native_document(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.documents.service._parse_pdf",
        lambda path: {"title": "Paper", "full_text": "Extracted text", "format": "pdf", "pages": 1},
    )
    pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"

    result = ingest_uploaded_document(
        file_name="paper.pdf",
        raw_content=pdf_bytes,
        artifact_store=ArtifactStore(),
    )
    state = AgentState(user_message="summarize this pdf")
    state.attachments = [result.attachment.to_dict()]

    messages = asyncio.run(ContextBuilder().build(state.user_message, state))
    user_message = messages[-1]

    assert result.attachment.media_type == "application/pdf"
    assert result.attachment.data == base64.b64encode(pdf_bytes).decode("ascii")
    assert user_message.documents == [
        {
            "media_type": "application/pdf",
            "data": base64.b64encode(pdf_bytes).decode("ascii"),
            "file_name": "paper.pdf",
        }
    ]
    assert "extracted text is available via read_artifact" in user_message.content


def test_pdf_reference_recovers_native_body_from_attachment_store(tmp_path) -> None:
    native_data = base64.b64encode(b"%PDF-1.4\nreference-only\n%%EOF").decode("ascii")
    attachment_store = AttachmentStore(tmp_path / "attachments")
    attachment_store.save(
        artifact_id="artifact_pdf_ref",
        content="Extracted PDF text",
        metadata={"attachment": {"file_name": "paper.pdf"}},
        native_data=native_data,
    )

    plan = build_attachment_input_plan(
        [{
            "kind": "document",
            "file_name": "paper.pdf",
            "media_type": "application/pdf",
            "artifact_id": "artifact_pdf_ref",
            "size_bytes": 31,
        }],
        attachment_store=attachment_store,
    )

    assert plan.documents == [{
        "media_type": "application/pdf",
        "data": native_data,
        "file_name": "paper.pdf",
    }]


def test_native_media_limits_match_minicode_provider_envelope() -> None:
    oversized_image = base64.b64encode(b"i" * (3_932_160 + 1)).decode("ascii")
    oversized_pdf = base64.b64encode(b"p" * (20 * 1024 * 1024 + 1)).decode("ascii")

    plan = build_attachment_input_plan([
        {
            "kind": "image",
            "file_name": "too-large.png",
            "media_type": "image/png",
            "data": oversized_image,
            "artifact_id": "artifact-image",
        },
        {
            "kind": "document",
            "file_name": "too-large.pdf",
            "media_type": "application/pdf",
            "data": oversized_pdf,
            "artifact_id": "artifact-pdf",
        },
    ])

    assert plan.images == []
    assert plan.documents == []
    hints = "\n".join(plan.text_hints)
    assert "too-large.png: native image input skipped because the file is too large" in hints
    assert "too-large.pdf: native PDF input skipped because it exceeds the safe request limit" in hints


def test_native_media_count_is_capped_at_provider_limit() -> None:
    attachments = [
        {
            "kind": "image",
            "file_name": f"image-{index}.png",
            "media_type": "image/png",
            "data": "aQ==",
            "artifact_id": f"artifact-{index}",
        }
        for index in range(101)
    ]

    plan = build_attachment_input_plan(attachments)

    assert len(plan.images) == 100
    assert plan.images[-1]["data"] == "aQ=="
    assert any("image-100.png" in hint and "provider maximum of 100 media items" in hint for hint in plan.text_hints)


def test_native_pdf_page_count_is_capped_at_provider_limit() -> None:
    plan = build_attachment_input_plan([{
        "kind": "document",
        "file_name": "long.pdf",
        "media_type": "application/pdf",
        "data": base64.b64encode(b"%PDF long").decode("ascii"),
        "artifact_id": "artifact-long-pdf",
        "page_count": 101,
    }])

    assert plan.documents == []
    assert any("101 pages" in hint and "provider maximum of 100" in hint for hint in plan.text_hints)


def test_pdf_parse_error_is_not_exposed_as_document_text(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.documents.service._parse_pdf",
        lambda path: {"title": "Broken", "full_text": "错误: PDF 解析失败。缺少 pymupdf", "format": "pdf", "pages": 0},
    )
    pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"

    result = ingest_uploaded_document(
        file_name="broken.pdf",
        raw_content=pdf_bytes,
        artifact_store=ArtifactStore(),
    )
    state = AgentState(user_message="summarize this pdf")
    state.attachments = [result.attachment.to_dict()]

    messages = asyncio.run(ContextBuilder().build(state.user_message, state))
    user_message = messages[-1]

    assert "indexed_chunks" not in result.attachment.to_dict()
    assert result.attachment.parse_error.startswith("错误:")
    assert "text extraction failed" in user_message.content
    assert "instead of inferring from the title" in user_message.content


def test_pdf_parse_error_blocks_title_only_summary_when_native_pdf_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.documents.service._parse_pdf",
        lambda path: {"title": "Broken", "full_text": "错误: PDF 解析失败。缺少 pymupdf", "format": "pdf", "pages": 0},
    )
    pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"

    result = ingest_uploaded_document(
        file_name="broken.pdf",
        raw_content=pdf_bytes,
        artifact_store=ArtifactStore(),
    )
    state = AgentState(user_message="summarize this pdf")
    state.attachments = [result.attachment.to_dict()]
    llm = OpenAIAdapter(
        settings=LLMSettings(
            api_key="test",
            base_url="http://example.test/v1",
            model="test-model",
            wire_api="chat",
        )
    )

    messages = asyncio.run(ContextBuilder(llm=llm).build(state.user_message, state))
    user_message = messages[-1]

    assert user_message.documents == []
    assert "PDF/text extraction failed" in user_message.content
    assert "Do not summarize or interpret the document body from the title alone" in user_message.content


def test_pdf_attachment_uses_text_fallback_for_openai_chat_wire_api(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.documents.service._parse_pdf",
        lambda path: {"title": "Paper", "full_text": "Extracted text", "format": "pdf", "pages": 1},
    )
    pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"

    result = ingest_uploaded_document(
        file_name="paper.pdf",
        raw_content=pdf_bytes,
        artifact_store=ArtifactStore(),
    )
    state = AgentState(user_message="summarize this pdf")
    state.attachments = [result.attachment.to_dict()]
    llm = OpenAIAdapter(
        settings=LLMSettings(
            api_key="test",
            base_url="http://example.test/v1",
            model="test-model",
            wire_api="chat",
        )
    )

    messages = asyncio.run(ContextBuilder(llm=llm).build(state.user_message, state))
    user_message = messages[-1]

    assert user_message.documents == []
    assert "active API format does not accept native PDF input" in user_message.content
    assert "read_artifact" in user_message.content


def test_non_pdf_document_attachment_uses_parsed_artifact_not_native_payload() -> None:
    result = ingest_uploaded_document(
        file_name="notes.md",
        raw_content="# Title\n\nbody text\n".encode("utf-8"),
        artifact_store=ArtifactStore(),
    )
    state = AgentState(user_message="summarize this")
    state.attachments = [result.attachment.to_dict()]

    messages = asyncio.run(ContextBuilder().build(state.user_message, state))
    user_message = messages[-1]

    assert user_message.images == []
    assert user_message.documents == []
    assert "read_artifact" in user_message.content


def test_read_artifact_accepts_uploaded_document_doc_id(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.documents.service._parse_pdf",
        lambda path: {"title": "Paper", "full_text": "Extracted PDF body", "format": "pdf", "pages": 1},
    )
    pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
    artifact_store = ArtifactStore()

    result = ingest_uploaded_document(
        file_name="paper.pdf",
        raw_content=pdf_bytes,
        artifact_store=artifact_store,
    )
    attachment_store = AttachmentStore()
    attachment_store.save(
        artifact_id=result.artifact_id,
        content=result.full_text,
        metadata={"attachment": result.attachment.to_dict()},
    )

    tool = ReadArtifactTool(ArtifactStore(), attachment_store=attachment_store)
    response = asyncio.run(tool.execute({"artifact_id": result.doc_id}))

    assert not response.is_error
    assert response.content == "Extracted PDF body"


def test_uploaded_document_artifact_is_owner_scoped(tmp_path) -> None:
    artifact_store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = ingest_uploaded_document(
        file_name="notes.md",
        raw_content=b"# Notes\n\nprivate body",
        artifact_store=artifact_store,
        conversation_id="conv-owner",
        workspace_root=workspace,
    )

    assert artifact_store.get(
        result.artifact_id,
        conversation_id="conv-owner",
        workspace_root=workspace,
    ) == "# Notes\n\nprivate body"
    assert artifact_store.get(result.artifact_id, conversation_id="conv-other") is None


def test_attachment_native_payload_requires_matching_owner_scope(tmp_path) -> None:
    store = AttachmentStore(tmp_path / "attachments")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    native = base64.b64encode(b"private image bytes").decode("ascii")
    store.save(
        artifact_id="scoped-image",
        content="",
        native_data=native,
        metadata={
            "conversation_id": "conv-owner",
            "workspace_root": str(workspace),
            "attachment": {
                "kind": "image",
                "file_name": "private.png",
                "media_type": "image/png",
            },
        },
    )

    assert store.get_payload("scoped-image") is None
    assert store.get_native_data("scoped-image", conversation_id="conv-other") is None
    assert store.get_preview("scoped-image", conversation_id="conv-owner") is None
    assert store.get_native_data(
        "scoped-image",
        conversation_id="conv-owner",
        workspace_root=str(workspace),
    ) == native


def test_attachment_plan_uses_server_payload_and_actual_native_size(tmp_path) -> None:
    store = AttachmentStore(tmp_path / "attachments")
    native = base64.b64encode(b"server-owned-bytes").decode("ascii")
    store.save(
        artifact_id="server-image",
        content="",
        native_data=native,
        metadata={
            "conversation_id": "conv-owner",
            "attachment": {
                "kind": "image",
                "file_name": "server.png",
                "media_type": "image/png",
                "size_bytes": len(b"server-owned-bytes"),
            },
        },
    )

    plan = build_attachment_input_plan(
        [
            {
                "artifact_id": "server-image",
                "kind": "image",
                "file_name": "spoofed.png",
                "media_type": "image/jpeg",
                "size_bytes": 1,
                "data": base64.b64encode(b"client-spoof").decode("ascii"),
            }
        ],
        attachment_store=store,
        conversation_id="conv-owner",
    )

    assert plan.images == [{"media_type": "image/png", "data": native}]


def test_attachment_metadata_limit_is_reported_as_413(monkeypatch, tmp_path) -> None:
    artifact_store = ArtifactStore(storage_dir=tmp_path / "artifacts")

    class RejectingAttachmentStore:
        def save(self, **_kwargs):
            raise ValueError("Attachment metadata exceeds the 256 KB limit.")

    context = AttachmentUploadContext(
        session_id="session-upload-limit",
        conversation_id="conversation-upload-limit",
        conversation=object(),
        workspace_root=tmp_path,
        artifact_store=artifact_store,
        attachment_store=RejectingAttachmentStore(),
    )

    with pytest.raises(ChatApiServiceError) as exc_info:
        upload_document_payload(
            context=context,
            file_name="notes.txt",
            raw_content=b"hello",
        )

    assert exc_info.value.status_code == 413

def test_openai_responses_input_includes_images() -> None:
    adapter = OpenAIAdapter.__new__(OpenAIAdapter)
    payload = adapter._build_responses_input(
        [
            LLMMessage(
                role="user",
                content="describe this",
                images=[{"media_type": "image/png", "data": "abc123"}],
            )
        ]
    )

    assert payload == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "describe this"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,abc123",
                    "detail": "auto",
                },
            ],
        }
    ]


def test_openai_responses_input_includes_pdf_documents() -> None:
    adapter = OpenAIAdapter.__new__(OpenAIAdapter)
    payload = adapter._build_responses_input(
        [
            LLMMessage(
                role="user",
                content="summarize",
                documents=[{"media_type": "application/pdf", "data": "pdf123", "file_name": "paper.pdf"}],
            )
        ]
    )

    assert payload == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "summarize"},
                {
                    "type": "input_file",
                    "filename": "paper.pdf",
                    "file_data": "data:application/pdf;base64,pdf123",
                },
            ],
        }
    ]


def test_anthropic_input_includes_pdf_documents() -> None:
    adapter = AnthropicAdapter(api_key="test")
    _, messages = adapter._convert_messages(
        [
            LLMMessage(
                role="user",
                content="summarize",
                documents=[{"media_type": "application/pdf", "data": "pdf123", "file_name": "paper.pdf"}],
            )
        ]
    )

    assert messages[0]["content"] == [
        {"type": "text", "text": "summarize"},
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": "pdf123",
            },
        },
    ]
