from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from backend.tools import search_support, search_tools
from backend.tools.search_tools import GlobFilesTool, GrepFilesTool


@pytest.fixture(params=[False, True], ids=["python", "ripgrep"])
def search_backend(request, monkeypatch):
    if request.param and not shutil.which("rg"):
        pytest.skip("ripgrep is not installed")
    monkeypatch.setattr(search_tools, "_HAS_RIPGREP", request.param)


@pytest.mark.parametrize("glob", ["with space.txt", "**/with space.txt", "*.{txt,md}"])
def test_grep_globs_preserve_spaces_and_support_root_files(tmp_path, search_backend, glob):
    (tmp_path / "with space.txt").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "excluded.py").write_text("NEEDLE\n", encoding="utf-8")
    result = asyncio.run(GrepFilesTool(tmp_path).execute({"pattern": "NEEDLE", "glob": glob}))
    assert not result.is_error
    assert "with space.txt" in result.content
    assert "excluded.py" not in result.content


@pytest.mark.parametrize("pattern", ["*.py", "**/*.py", "*.{py,ts}", "!*.txt"])
def test_glob_matches_root_and_nested_files_consistently(tmp_path, search_backend, pattern):
    (tmp_path / "src").mkdir()
    (tmp_path / "root.py").write_text("fixture", encoding="utf-8")
    (tmp_path / "src/nested.py").write_text("fixture", encoding="utf-8")
    (tmp_path / "excluded.txt").write_text("fixture", encoding="utf-8")
    result = asyncio.run(GlobFilesTool(tmp_path).execute({"pattern": pattern}))
    assert not result.is_error
    assert "root.py" in result.content
    assert "nested.py" in result.content
    assert "excluded.txt" not in result.content


def test_grep_include_glob_cannot_override_explicit_exclusions(tmp_path):
    if not shutil.which("rg"):
        pytest.skip("ripgrep is not installed")
    (tmp_path / "private").mkdir()
    (tmp_path / "private/secret.txt").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "public.txt").write_text("NEEDLE\n", encoding="utf-8")
    output, is_error = asyncio.run(search_support._grep_with_ripgrep(
        "NEEDLE", tmp_path, glob_pattern="**/*.txt", exclude_globs=["!**/private/**"], output_mode="files_with_matches",
    ))
    assert not is_error
    assert "public.txt" in output
    assert "secret.txt" not in output


def test_grep_glob_overrides_ignored_files_but_keeps_ignored_directories_pruned(tmp_path, search_backend):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored/hidden.txt").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "ignored-file.txt").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored/\nignored-file.txt\n", encoding="utf-8")
    tool = GrepFilesTool(tmp_path)
    default = asyncio.run(tool.execute({"pattern": "NEEDLE"}))
    explicit = asyncio.run(tool.execute({"pattern": "NEEDLE", "glob": "**/*.txt"}))
    assert not default.is_error and not explicit.is_error
    assert "hidden.txt" not in default.content
    assert "ignored-file.txt" not in default.content
    assert "hidden.txt" not in explicit.content
    assert "ignored-file.txt" in explicit.content


@pytest.mark.parametrize("output_mode", ["files_with_matches", "content", "count"])
def test_python_grep_reports_incomplete_search_for_overlong_input(tmp_path, monkeypatch, output_mode):
    monkeypatch.setattr(search_tools, "_HAS_RIPGREP", False)
    monkeypatch.setattr(search_support, "REGEX_MAX_LINE_CHARS", 64)
    (tmp_path / "long.txt").write_text("x" * 128 + "NEEDLE\n", encoding="utf-8")
    result = asyncio.run(GrepFilesTool(tmp_path).execute({"pattern": "NEEDLE", "output_mode": output_mode}))
    assert result.is_error
    assert "64-byte Python search input limit" in result.content
    assert "long.txt" in result.content


@pytest.mark.parametrize("tool", [GrepFilesTool, GlobFilesTool])
def test_python_search_reports_directory_read_failures(tmp_path, monkeypatch, tool):
    monkeypatch.setattr(search_tools, "_HAS_RIPGREP", False)
    scandir = os.scandir

    def denied(path):
        if Path(path) == tmp_path:
            raise PermissionError("fixture directory read denied")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", denied)
    result = asyncio.run(tool(tmp_path).execute({"pattern": "NEEDLE"}))
    assert result.is_error
    assert "fixture directory read denied" in result.content


def test_python_grep_reports_file_read_failures(tmp_path, monkeypatch):
    target = tmp_path / "locked.txt"
    target.write_text("NEEDLE\n", encoding="utf-8")
    monkeypatch.setattr(search_tools, "_HAS_RIPGREP", False)
    file_open = Path.open

    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError("fixture file read denied")
        return file_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    result = asyncio.run(GrepFilesTool(tmp_path).execute({"pattern": "NEEDLE"}))
    assert result.is_error
    assert "fixture file read denied" in result.content


def test_python_glob_prunes_ignored_directories_before_scanning(tmp_path, monkeypatch):
    (tmp_path / "node_modules/deep").mkdir(parents=True)
    (tmp_path / "visible.py").write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(search_tools, "_HAS_RIPGREP", False)
    visited = []
    scandir = os.scandir

    def counted(path):
        visited.append(Path(path))
        return scandir(path)

    monkeypatch.setattr(os, "scandir", counted)
    result = asyncio.run(GlobFilesTool(tmp_path).execute({"pattern": "**/*.py"}))
    assert not result.is_error
    assert "visible.py" in result.content
    assert visited == [tmp_path]


@pytest.mark.parametrize("tool,pattern", [(GrepFilesTool, "NEEDLE"), (GlobFilesTool, "*.txt")])
@pytest.mark.parametrize("head_limit,offset,count,has_more", [
    (0, 1, 2, False),
    (1, 2, 1, False),
    (1, 1, 1, True),
    (0, 3, 0, False),
])
def test_search_page_reports_only_remaining_results(
    tmp_path, search_backend, tool, pattern, head_limit, offset, count, has_more,
):
    names = ["first.txt", "second.txt", "third.txt"]
    for name in names:
        (tmp_path / name).write_text("NEEDLE\n", encoding="utf-8")
    result = asyncio.run(tool(tmp_path).execute({
        "pattern": pattern, "head_limit": head_limit, "offset": offset,
    }))
    assert not result.is_error
    assert sum(name in result.content for name in names) == count
    assert ("Use offset to fetch the next page" in result.content) is has_more
    if not has_more:
        assert "后续仍有匹配" not in result.content
        assert "more matches are available" not in result.content
