from __future__ import annotations

import pytest

from backend.lsp.editor import editor_language_servers
from backend.workspace.editor_language import EditorDocument, EditorLanguageRequest, editor_language_request
from backend.workspace.service import WorkspaceService


@pytest.mark.asyncio
async def test_native_clangd_completes_directives_real_headers_members_and_functions_without_saving(tmp_path):
    disk = "// user disk content\n"
    (tmp_path / "main.cpp").write_text(disk, encoding="utf-8")
    (tmp_path / "project.hpp").write_text("int project_function();\n", encoding="utf-8")
    service = WorkspaceService(get_workspace_root=lambda: tmp_path)

    async def request(method, content, line=None, character=None, **extra):
        lines = content.split("\n")
        result = await editor_language_request(service, EditorLanguageRequest(
            method=method, documents=[EditorDocument(path="main.cpp", content=content)],
            line=len(lines) - 1 if line is None else line,
            character=len(lines[-1]) if character is None else character, **extra))
        return result["result"]

    def items(result):
        return result if isinstance(result, list) else result["items"]

    try:
        directives = items(await request("completion", "#"))
        assert any(item["textEdit"]["newText"].startswith("include <") for item in directives)
        assert any(item["textEdit"]["newText"].startswith("define ") for item in directives)
        headers = items(await request("completion", "#include <io"))
        assert any(item["label"].strip() == "iostream>" for item in headers)
        project_headers = items(await request("completion", '#include "proj'))
        assert any(item["label"].strip() == 'project.hpp"' for item in project_headers)
        code = "struct Helper { int member; int calculate(int count); };\nint main(){ Helper value; value."
        members = items(await request("completion", code))
        assert {item["label"].strip() for item in members} >= {"member", "calculate(int count)"}
        calculate = next(item for item in members if item["label"].strip().startswith("calculate("))
        assert calculate["insertTextFormat"] == 2 and "${1:" in calculate["textEdit"]["newText"]
        standard_methods = items(await request("completion", "#include <string>\nint main(){ std::string value; value."))
        assert any(item["label"].strip().startswith("append(") for item in standard_methods)
        signature = await request("signatureHelp", "int sum(int first, int second);\nint main(){ sum(")
        assert "sum" in signature["signatures"][0]["label"] and len(signature["signatures"][0]["parameters"]) == 2
        source = "int sum(int first, int second) { return first + second; }\nint main() { return sum(1, 2); }"
        definition = await request("definition", source, line=1, character=21)
        locations = definition if isinstance(definition, list) else [definition]
        assert any(location.get("uri", location.get("targetUri", "")).endswith("main.cpp") for location in locations)
        renamed = await request("rename", source, line=1, character=21, new_name="add")
        assert len(renamed["changes"][(tmp_path / "main.cpp").as_uri()]) == 2
        assert (tmp_path / "main.cpp").read_text(encoding="utf-8") == disk
    finally:
        await editor_language_servers.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "language"), [("example.txt", "cpp"), ("main.c", "c")])
async def test_explicit_cpp_mode_and_c_mode_use_real_compiler_completions(tmp_path, path, language):
    (tmp_path / path).write_text("// unchanged\n", encoding="utf-8")
    service = WorkspaceService(get_workspace_root=lambda: tmp_path)
    text = "struct Data { int count; };\nint main(){ struct Data value; value."
    try:
        result = await editor_language_request(service, EditorLanguageRequest(method="completion",
            documents=[EditorDocument(path=path, content=text, language=language)], line=1, character=len(text.split("\n")[-1])))
        values = result["result"] if isinstance(result["result"], list) else result["result"]["items"]
        assert "count" in {item["label"].strip() for item in values}
        assert (tmp_path / path).read_text(encoding="utf-8") == "// unchanged\n"
    finally:
        await editor_language_servers.shutdown()
