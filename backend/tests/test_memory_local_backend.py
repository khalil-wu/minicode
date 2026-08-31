from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from backend.agent.stream_sanitizer import ThinkingStreamSanitizer, scrub_thinking_tags
from backend.memory.citations import parse_memory_citation
from backend.memory.file_memory import FileMemory
from backend.memory.local_backend import LocalMemoryBackend, MemoryBackendError
from backend.memory.prompts import build_memory_read_prompt
from backend.tools.memory_tools import (
    MemoryAddAdHocNoteTool,
    MemoryListTool,
    MemoryReadTool,
    MemorySearchTool,
)


def test_local_backend_matches_codex_read_list_search_and_note_contract(tmp_path: Path) -> None:
    root = tmp_path / "memories"
    root.mkdir()
    (root / "MEMORY.md").write_text("alpha\nbeta\n", encoding="utf-8")
    backend = LocalMemoryBackend(root)

    assert backend.read(path="MEMORY.md", line_offset=3)["content"] == ""
    assert backend.read(path="MEMORY.md", max_lines=1) == {
        "path": "MEMORY.md",
        "start_line_number": 1,
        "content": "alpha\n",
        "truncated": True,
    }
    assert backend.list()["entries"] == [
        {"path": "MEMORY.md", "entry_type": "file"}
    ]
    result = backend.search(
        queries=["alpha", "beta"],
        match_mode={"type": "all_within_lines", "line_count": 2},
    )
    assert result["matches"][0]["matched_queries"] == ["alpha", "beta"]

    filename = "2026-08-10T12-00-00-user-update.md"
    assert backend.add_ad_hoc_note(filename=filename, note="remember this") == {}
    assert (root / "extensions" / "ad_hoc" / "notes" / filename).read_text(
        encoding="utf-8"
    ) == "remember this"
    with pytest.raises(MemoryBackendError, match="already exists"):
        backend.add_ad_hoc_note(filename=filename, note="overwrite")
    with pytest.raises(MemoryBackendError, match="must stay within"):
        backend.resolve("../outside")


def test_dedicated_memory_tools_replace_full_file_overwrite_protocol(tmp_path: Path) -> None:
    memory = FileMemory(tmp_path / "memories")
    (memory.memory_dir / "MEMORY.md").write_text("needle\n", encoding="utf-8")
    tools = [
        MemoryAddAdHocNoteTool(memory),
        MemoryListTool(memory),
        MemoryReadTool(memory),
        MemorySearchTool(memory),
    ]
    assert [tool.name for tool in tools] == [
        "memory_add_ad_hoc_note",
        "memory_list",
        "memory_read",
        "memory_search",
    ]
    read_result = asyncio.run(tools[2].execute({"path": "MEMORY.md"}))
    assert json.loads(read_result.content)["content"] == "needle\n"
    note_result = asyncio.run(
        tools[0].execute(
            {
                "filename": "2026-08-10T12-00-00-note.md",
                "note": "new note",
            }
        )
    )
    assert json.loads(note_result.content) == {}


def test_ad_hoc_note_creation_is_create_only_under_concurrency(tmp_path: Path) -> None:
    backend = LocalMemoryBackend(tmp_path / "memories")
    filename = "2026-08-10T12-00-00-concurrent.md"
    barrier = threading.Barrier(8)
    successes: list[str] = []
    failures: list[str] = []
    results_lock = threading.Lock()

    def add_note(index: int) -> None:
        barrier.wait()
        try:
            backend.add_ad_hoc_note(filename=filename, note=f"note-{index}")
        except MemoryBackendError:
            with results_lock:
                failures.append(filename)
        else:
            with results_lock:
                successes.append(filename)

    threads = [threading.Thread(target=add_note, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(successes) == 1
    assert len(failures) == 7
    assert (tmp_path / "memories" / "extensions" / "ad_hoc" / "notes" / filename).is_file()


def test_memory_citation_is_hidden_across_stream_chunks_and_parsed() -> None:
    sanitizer = ThinkingStreamSanitizer()
    chunks = [
            "before <minicode-memory-",
            "citation><citation_entries>\nMEMORY.md:1-2|note=[summary]\n</citation_entries>",
            "<rollout_ids>\na\na\nb\n</rollout_ids></minicode-memory-citation> after",
    ]
    visible = "".join(sanitizer.feed(chunk) for chunk in chunks)
    assert visible == "before  after"
    assert scrub_thinking_tags("x<minicode-memory-citation>hidden</minicode-memory-citation>y") == "xy"
    assert parse_memory_citation(sanitizer.citations) == {
        "entries": [
            {
                "path": "MEMORY.md",
                "line_start": 1,
                "line_end": 2,
                "note": "summary",
            }
        ],
        "rollout_ids": ["a", "b"],
    }


def test_memory_read_prompt_uses_only_minicode_memory_contract(tmp_path: Path) -> None:
    prompt = build_memory_read_prompt(tmp_path / "memories", "v1\n## Task\nUse the index")

    assert "MiniCode Memory" in prompt
    assert "background context" in prompt
    assert "untrusted reference material" in prompt
    assert "codex" not in prompt.lower()
    assert "session_meta.payload.id" not in prompt
