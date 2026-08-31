import asyncio
import tempfile
from pathlib import Path

from backend.artifact.store import ArtifactStore
from backend.agent.tool_execution import changed_file_event_payload
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.file_tools import (
    EditFileTool,
    WriteFileTool,
)
from backend.tools.file_tools_common import content_hash
from backend.tools.fuzzy_search_tool import FuzzySearchTool
from backend.tools.notebook_tool import NotebookEditTool
from backend.tools.read_file import ReadFileTool
from backend.workspace.service import WorkspaceService
from backend.tools.registry import ToolRegistry


def test_write_file_rejects_non_string_path_and_content_before_execution():
    tool = WriteFileTool()

    assert "file_path must be a workspace file path string" in tool.validate_input({
        "file_path": 123,
        "content": "ok",
    })
    assert "content must be a string containing the complete file contents" in tool.validate_input({
        "file_path": "requirements.txt",
        "content": ["torch"],
    })


def test_edit_file_rejects_non_string_edit_fields_before_execution():
    tool = EditFileTool()

    assert "old_string must be a string containing the exact text to replace" in tool.validate_input({
        "file_path": "app.py",
        "old_string": ["old"],
        "new_string": "new",
    })
    assert "new_string must be a string containing the replacement text" in tool.validate_input({
        "file_path": "app.py",
        "old_string": "old",
        "new_string": {"value": "new"},
    })


def test_changed_file_event_carries_workspace_root():
    root = Path("C:/workspace-root")
    context = ToolExecutionContext(
        permission=PermissionContext(mode="auto"),
        workspace_root=root,
    )
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    call = ToolCallEvent(id="write-1", name="write_file", arguments={"file_path": "index.html"})

    payload = changed_file_event_payload(call, context, registry)

    assert payload is not None
    assert payload["path"] == "index.html"
    assert payload["workspace_root"] == str(root)


def test_write_file_preserves_exact_utf8_bytes_and_cleans_temp_file():
    async def _go():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tool = WriteFileTool()
            ctx = ToolExecutionContext(
                permission=PermissionContext(mode="auto"),
                workspace_root=root,
            )
            content = "line 1\nline 2\n"

            result = await tool.execute({
                "file_path": "nested/out.txt",
                "content": content,
            }, ctx)

            output = root / "nested" / "out.txt"
            assert not result.is_error
            assert output.read_bytes() == content.encode("utf-8")
            assert list(output.parent.glob(".out.txt.*.tmp")) == []

    asyncio.run(_go())


def test_write_file_keeps_model_result_compact_but_writes_full_content():
    async def _go():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "large.txt"
            old_content = "\n".join(f"old {i}" for i in range(5000)) + "\n"
            new_content = "\n".join(f"new {i}" for i in range(5000)) + "\n"
            output.write_text(old_content, encoding="utf-8")

            tool = WriteFileTool()
            ctx = ToolExecutionContext(
                permission=PermissionContext(mode="auto"),
                workspace_root=root,
            )

            result = await tool.execute({
                "file_path": "large.txt",
                "content": new_content,
                "expected_hash": content_hash(old_content),
            }, ctx)

            assert not result.is_error
            assert output.read_text(encoding="utf-8") == new_content
            assert "Diff stats:" in result.content
            assert "--- a/large.txt" not in result.content
            assert "+++ b/large.txt" not in result.content
            assert "new 4999" not in result.content

    asyncio.run(_go())


def test_edit_file_keeps_model_result_compact_but_writes_full_content():
    async def _go():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "app.py"
            old_content = "def greet():\n    return 'hi'\n"
            new_content = "def greet():\n    return 'hello'\n"
            output.write_text(old_content, encoding="utf-8")

            tool = EditFileTool()
            ctx = ToolExecutionContext(
                permission=PermissionContext(mode="auto"),
                workspace_root=root,
            )

            result = await tool.execute({
                "file_path": "app.py",
                "old_string": "return 'hi'",
                "new_string": "return 'hello'",
                "expected_hash": content_hash(old_content),
            }, ctx)

            assert not result.is_error
            assert output.read_text(encoding="utf-8") == new_content
            assert "Diff stats:" in result.content
            assert "--- a/app.py" not in result.content
            assert "+++ b/app.py" not in result.content
            assert "+    return 'hello'" not in result.content

    asyncio.run(_go())


def test_edit_file_mismatch_returns_current_excerpt_and_hash():
    async def _go():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "app.py"
            current = "def greet():\n    return 'hello'\n"
            output.write_text(current, encoding="utf-8")
            tool = EditFileTool()
            ctx = ToolExecutionContext(
                permission=PermissionContext(mode="auto"),
                workspace_root=root,
            )

            result = await tool.execute(
                {
                    "file_path": "app.py",
                    "old_string": "return 'hi'",
                    "new_string": "return 'welcome'",
                    "expected_hash": content_hash(current),
                },
                ctx,
            )

            assert result.is_error
            assert "old_string was not found" in result.content
            assert "Closest current file excerpt" in result.content
            assert "return 'hello'" in result.content
            assert content_hash(current) in result.content

    asyncio.run(_go())


def test_edit_file_rejects_noop_replacements_without_touching_the_file():
    async def _go():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "app.py"
            original = "value = 1\n"
            output.write_text(original, encoding="utf-8")
            before_bytes = output.read_bytes()

            result = await EditFileTool().execute(
                {
                    "file_path": "app.py",
                    "old_string": "value = 1",
                    "new_string": "value = 1",
                    "expected_hash": content_hash(original),
                },
                ToolExecutionContext(
                    permission=PermissionContext(mode="auto"),
                    workspace_root=root,
                ),
            )

            assert result.is_error
            assert "No changes to make" in result.content
            assert output.read_bytes() == before_bytes

    asyncio.run(_go())


def test_edit_file_quote_fallback_preserves_curly_quote_style_for_replace_all():
    async def _go():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "notes.md"
            original = "He said “old” and then “old”.\n"
            output.write_text(original, encoding="utf-8")

            result = await EditFileTool().execute(
                {
                    "file_path": "notes.md",
                    "old_string": '"old"',
                    "new_string": '"new"',
                    "replace_all": True,
                    "expected_hash": content_hash(original),
                },
                ToolExecutionContext(
                    permission=PermissionContext(mode="auto"),
                    workspace_root=root,
                ),
            )

            assert result.is_error is False
            assert output.read_text(encoding="utf-8") == "He said “new” and then “new”.\n"

    asyncio.run(_go())


def test_edit_file_matches_bom_prefixed_utf8_and_preserves_bom():
    async def _go():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "bom.txt"
            original = "\ufefffirst line\nsecond line\n"
            output.write_text(original, encoding="utf-8")

            result = await EditFileTool().execute(
                {
                    "file_path": "bom.txt",
                    "old_string": "first line",
                    "new_string": "updated line",
                    "expected_hash": content_hash(original),
                },
                ToolExecutionContext(
                    permission=PermissionContext(mode="auto"),
                    workspace_root=root,
                ),
            )

            assert result.is_error is False
            assert output.read_bytes().startswith(b"\xef\xbb\xbf")
            assert output.read_text(encoding="utf-8") == "\ufeffupdated line\nsecond line\n"

    asyncio.run(_go())


def test_new_file_write_invalidates_fuzzy_file_tree_cache():
    async def _go():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "existing.py").write_text("pass\n", encoding="utf-8")
            context = ToolExecutionContext(
                permission=PermissionContext(mode="auto"),
                workspace_root=root,
            )
            fuzzy = FuzzySearchTool(root)

            before = await fuzzy.execute({"query": "fresh"}, context)
            assert "未找到" in before.content

            written = await WriteFileTool().execute(
                {"file_path": "fresh_module.py", "content": "pass\n"},
                context,
            )
            assert written.is_error is False

            after = await fuzzy.execute({"query": "fresh"}, context)
            assert "fresh_module.py" in after.content

    asyncio.run(_go())


def test_notebook_edit_invalidates_shared_read_file_cache():
    async def _go():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            notebook = root / "demo.ipynb"
            notebook.write_text(
                '{"cells":[{"id":"cell-1","cell_type":"code","metadata":{},"source":["old\\n"],"outputs":[],"execution_count":1}],"metadata":{},"nbformat":4,"nbformat_minor":5}',
                encoding="utf-8",
            )
            context = ToolExecutionContext(
                permission=PermissionContext(mode="auto"),
                workspace_root=root,
                metadata={"_read_file_hashes": {}},
            )
            reader = ReadFileTool(ArtifactStore(storage_dir=root / "artifacts"))

            first = await reader.execute(
                {"file_path": "demo.ipynb"},
                context,
            )
            assert "old" in first.content

            edited = await NotebookEditTool().execute(
                {
                    "notebook_path": "demo.ipynb",
                    "cell_id": "cell-1",
                    "new_source": "new\\n",
                    "edit_mode": "replace",
                },
                context,
            )
            assert edited.is_error is False

            second = await reader.execute(
                {"file_path": "demo.ipynb"},
                context,
            )
            assert "new" in second.content
            assert '"old\\n"' not in second.content

    asyncio.run(_go())


def test_workspace_service_write_invalidates_shared_read_file_cache():
    async def _go():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "shared.txt"
            target.write_text("before\n", encoding="utf-8")
            context = ToolExecutionContext(
                permission=PermissionContext(mode="auto"),
                workspace_root=root,
                metadata={"_read_file_hashes": {}},
            )
            reader = ReadFileTool(ArtifactStore(storage_dir=root / "artifacts"))
            first = await reader.execute({"file_path": "shared.txt"}, context)
            assert "before" in first.content

            service = WorkspaceService(lambda: root)
            response = service.compare_and_write_file(
                "shared.txt",
                service.content_hash(target.read_bytes().decode("utf-8")),
                "after\n",
            )
            assert response.content == "after\n"

            second = await reader.execute({"file_path": "shared.txt"}, context)
            assert "after" in second.content
            assert "before" not in second.content

    asyncio.run(_go())


def test_write_file_truncates_large_diff_progress_events():
    async def _go():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events: list[tuple[str, dict]] = []

            async def emit(event_type: str, payload: dict) -> None:
                events.append((event_type, payload))

            tool = WriteFileTool()
            ctx = ToolExecutionContext(
                permission=PermissionContext(mode="auto"),
                workspace_root=root,
                emit_event=emit,
                metadata={"_current_tool_call_id": "tc-large-write"},
            )
            new_content = "\n".join(f"line {i}" for i in range(12000)) + "\n"

            result = await tool.execute({
                "file_path": "new-large.txt",
                "content": new_content,
            }, ctx)

            assert not result.is_error
            assert events
            assert events[-1][0] == "turn.diff.updated"
            final_diff = events[-1][1]["diff"]
            assert final_diff
            assert "new file mode" in final_diff
            assert "line 0" in final_diff

    asyncio.run(_go())


def test_list_files_hides_denylisted_entries_like_its_search_siblings():
    """A directory listing is a read of the workspace surface.

    grep_files/glob_files already refuse whatever read_file refuses. list_files
    enumerated names directly, so .env, secrets/api.txt and sub/key.pem were
    disclosed by name in every mode.
    """
    from backend.config import PermissionSettings
    from backend.permissions.checker import PermissionChecker
    from backend.tools.list_files import ListFilesTool
    from backend.tools.search_tools import GlobFilesTool

    async def _go():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sub").mkdir()
            (root / "secrets").mkdir()
            (root / ".env").write_text("SECRET=1", encoding="utf-8")
            (root / "secrets" / "api.txt").write_text("k", encoding="utf-8")
            (root / "sub" / "key.pem").write_text("k", encoding="utf-8")
            (root / "sub" / "normal.txt").write_text("hello", encoding="utf-8")
            checker = PermissionChecker(PermissionSettings(), root)
            ctx = ToolExecutionContext(
                permission=PermissionContext(mode="confirm", workspace_root=root),
                workspace_root=root,
                permission_checker=checker,
            )

            listed = await ListFilesTool().execute({"path": ".", "recursive": True}, ctx)
            globbed = await GlobFilesTool().execute({"pattern": "**/*"}, ctx)

            assert ".env" not in listed.content
            assert "key.pem" not in listed.content
            assert "api.txt" not in listed.content
            assert "normal.txt" in listed.content
            for name in (".env", "key.pem", "api.txt"):
                assert name not in globbed.content

            # The bypass-immune floor still applies in bypass mode.
            bypass = ToolExecutionContext(
                permission=PermissionContext(mode="bypass", workspace_root=root),
                workspace_root=root,
                permission_checker=checker,
            )
            assert ".env" not in (
                await ListFilesTool().execute({"path": "."}, bypass)
            ).content

    asyncio.run(_go())
