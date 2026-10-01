"""Focused owned-root verification, not test-source audit coverage."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from backend.memory.file_memory import FileMemory
from backend.memory.local_backend import LocalMemoryBackend
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.ast_tools import FindReferencesTool, GoToDefinitionTool
from backend.tools.memory_tools import MemoryAddAdHocNoteTool
from backend.tools.registry import ToolRegistry


@pytest.mark.parametrize("extension", ["py", "pyi"])
def test_python_definition_and_reference_routing(tmp_path: Path, extension: str) -> None:
    source = tmp_path / f"types.{extension}"
    source.write_text("class StubThing: ...\nitem: StubThing\n", encoding="utf-8")
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path)

    async def scenario() -> None:
        definition = await GoToDefinitionTool().execute({"name": "StubThing"}, context)
        assert not definition.is_error
        assert f"types.{extension}:1" in definition.content
        references = await FindReferencesTool().execute(
            {"name": "StubThing", "include_definitions": False}, context,
        )
        assert not references.is_error
        assert f"types.{extension}:1" not in references.content
        assert f"types.{extension}:2" in references.content

    asyncio.run(scenario())


@pytest.mark.parametrize("name,expected_line", [("xml", 1), ("ElementTree", None), ("tree", 2)])
def test_dotted_python_import_binding(
    tmp_path: Path, name: str, expected_line: int | None,
) -> None:
    source = tmp_path / "bindings.py"
    source.write_text(
        "import xml.etree.ElementTree\nimport xml.etree.ElementTree as tree\n",
        encoding="utf-8",
    )
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path)
    result = asyncio.run(GoToDefinitionTool().execute({"name": name}, context))
    assert not result.is_error
    if expected_line is None:
        assert "未找到" in result.content
    else:
        assert f"bindings.py:{expected_line}" in result.content


def test_cancelled_memory_worker_remains_owned_until_actual_write_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        memory = FileMemory(tmp_path / "memories")
        tool = MemoryAddAdHocNoteTool(memory)
        registry = ToolRegistry()
        registry.register(tool)
        context = ToolExecutionContext(
            permission=PermissionContext(), conversation_id="finite-memory-owner",
            tool_call_id="finite-memory-call",
            cancel_event=asyncio.Event(),
        )
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        actual_add = LocalMemoryBackend.add_ad_hoc_note

        def blocked_write(backend, **kwargs):
            entered.set()
            try:
                if not release.wait(3):
                    raise RuntimeError("finite memory worker barrier timed out")
                return actual_add(backend, **kwargs)
            finally:
                finished.set()

        monkeypatch.setattr(LocalMemoryBackend, "add_ad_hoc_note", blocked_write)
        monkeypatch.setattr("backend.tools.registry.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.02)
        filename = "2026-09-30T18-00-00-finite-cancel.md"
        target = memory.memory_dir / "extensions" / "ad_hoc" / "notes" / filename
        task = asyncio.create_task(registry.execute(
            tool.name, {"filename": filename, "note": "FINITE OWNED MEMORY NOTE"}, context=context,
        ))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            context.cancel_event.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not finished.is_set()
            assert not target.exists()
            owned_task, = context.pending_cleanup_tasks
            assert context.cleanup_tasks_by_call[context.tool_call_id] is owned_task
            assert any(receipt["pending"] == 1 for receipt in context.cleanup_receipts.values())
            # A second cancel cannot abandon the underlying blocking writer.
            owned_task.cancel()
            await asyncio.sleep(0)
            assert not owned_task.done()
            assert owned_task in context.pending_cleanup_tasks
        finally:
            release.set()
            await asyncio.gather(task, *context.pending_cleanup_tasks, return_exceptions=True)
            assert await asyncio.to_thread(finished.wait, 3)
        assert target.read_text(encoding="utf-8") == "FINITE OWNED MEMORY NOTE"
        assert context.pending_cleanup_tasks == set()

    asyncio.run(scenario())


def test_memory_normal_completion_returns_after_actual_append(tmp_path: Path) -> None:
    memory = FileMemory(tmp_path / "memories")
    filename = "2026-09-30T18-00-00-finite-normal.md"
    result = asyncio.run(MemoryAddAdHocNoteTool(memory).execute(
        {"filename": filename, "note": "FINITE NORMAL MEMORY NOTE"},
    ))
    assert not result.is_error
    assert result.content == "{}"
    target = memory.memory_dir / "extensions" / "ad_hoc" / "notes" / filename
    assert target.read_text(encoding="utf-8") == "FINITE NORMAL MEMORY NOTE"
