import asyncio
import re
from pathlib import Path

import backend.tools.search_tools as search_tools
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.file_tools import EditFileTool, ListFilesTool, WriteFileTool
from backend.tools.file_tools_common import clear_list_files_cache, content_hash
from backend.tools.search_tools import GlobFilesTool, GrepFilesTool
from backend.tools.search_support import clear_search_caches


def _workspace_context(path: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        permission=PermissionContext(mode="confirm", source="test"),
        workspace_root=str(path),
    )


def test_glob_files_repeated_search_reflects_live_workspace(tmp_path) -> None:
    clear_search_caches()
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")
    tool = GlobFilesTool()
    args = {"pattern": "**/*.py", "directory": str(tmp_path)}
    context = _workspace_context(tmp_path)
    first = asyncio.run(tool.execute(args, context=context))
    (tmp_path / "b.py").write_text("print('b')\n", encoding="utf-8")
    second = asyncio.run(tool.execute(args, context=context))

    assert first.is_error is False
    assert second.is_error is False
    assert "a.py" in first.content
    assert "b.py" not in first.content
    assert "a.py" in second.content
    assert "b.py" in second.content


def test_grep_files_repeated_search_reflects_live_content(tmp_path) -> None:
    clear_search_caches()
    main = tmp_path / "main.py"
    main.write_text("def helper():\n    return 1\n", encoding="utf-8")
    tool = GrepFilesTool()
    args = {
        "pattern": "run_agent_loop",
        "directory": str(tmp_path),
        "file_extensions": [".py"],
    }
    context = _workspace_context(tmp_path)
    first = asyncio.run(tool.execute(args, context=context))
    main.write_text("def run_agent_loop():\n    pass\n", encoding="utf-8")
    second = asyncio.run(tool.execute(args, context=context))

    assert first.is_error is False
    assert second.is_error is False
    assert "main.py" not in first.content
    assert "main.py" in second.content


def test_grep_files_default_returns_matching_file_names(tmp_path) -> None:
    clear_search_caches()
    (tmp_path / "compiler.py").write_text(
        "class SQLCompiler:\n    ordering_parts = True\n",
        encoding="utf-8",
    )

    result = asyncio.run(
        GrepFilesTool().execute(
            {"pattern": "ordering_parts", "directory": "."},
            context=_workspace_context(tmp_path),
        )
    )

    assert result.is_error is False
    assert "模式: files_with_matches" in result.content
    assert "compiler.py" in result.content
    assert "ordering_parts = True" not in result.content


def test_grep_files_rejects_absolute_path_without_workspace_context(tmp_path) -> None:
    clear_search_caches()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("MINICODE_SECRET=1\n", encoding="utf-8")

    result = asyncio.run(
        GrepFilesTool().execute({
            "pattern": "MINICODE_SECRET",
            "directory": str(outside),
        })
    )

    assert result.is_error is True
    assert "outside" in result.content.lower()


def test_grep_files_skips_symlink_targets_outside_workspace(tmp_path) -> None:
    clear_search_caches()
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "app.py").write_text("SAFE_VALUE = 1\n", encoding="utf-8")
    secret = outside / "secret.py"
    secret.write_text("LEAK_ME = 'secret'\n", encoding="utf-8")
    link = workspace / "linked_secret.py"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        return

    result = asyncio.run(
        GrepFilesTool().execute(
            {"pattern": "LEAK_ME", "directory": "."},
            context=_workspace_context(workspace),
        )
    )

    assert result.is_error is False
    assert "linked_secret.py" not in result.content
    assert "secret" not in result.content


def test_glob_files_skips_symlink_targets_outside_workspace(tmp_path) -> None:
    clear_search_caches()
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "app.py").write_text("SAFE_VALUE = 1\n", encoding="utf-8")
    secret = outside / "secret.py"
    secret.write_text("LEAK_ME = 'secret'\n", encoding="utf-8")
    link = workspace / "linked_secret.py"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        return

    result = asyncio.run(
        GlobFilesTool().execute(
            {"pattern": "**/*.py", "directory": "."},
            context=_workspace_context(workspace),
        )
    )

    assert result.is_error is False
    assert "app.py" in result.content
    assert "linked_secret.py" not in result.content


def test_glob_files_returns_oldest_matches_first_and_accepts_path_alias(tmp_path) -> None:
    clear_search_caches()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    older = workspace / "older.py"
    newer = workspace / "newer.py"
    older.write_text("print('old')\n", encoding="utf-8")
    newer.write_text("print('new')\n", encoding="utf-8")
    older_time = older.stat().st_mtime - 20
    newer_time = newer.stat().st_mtime + 20
    older.touch()
    newer.touch()
    import os

    os.utime(older, (older_time, older_time))
    os.utime(newer, (newer_time, newer_time))

    result = asyncio.run(
        GlobFilesTool().execute(
            {"pattern": "*.py", "path": "."},
            context=_workspace_context(workspace),
        )
    )

    assert result.is_error is False
    # cc glob.ts never reverses: oldest-first ordering.
    assert result.content.index("older.py") < result.content.index("newer.py")


def test_grep_files_supports_explicit_output_modes_and_type_filter(tmp_path) -> None:
    clear_search_caches()
    (tmp_path / "app.py").write_text("needle\nneedle\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("needle\n", encoding="utf-8")
    tool = GrepFilesTool()
    context = _workspace_context(tmp_path)

    files_result = asyncio.run(
        tool.execute(
            {
                "pattern": "needle",
                "path": ".",
                "type": "py",
                "output_mode": "files_with_matches",
            },
            context=context,
        )
    )
    count_result = asyncio.run(
        tool.execute(
            {
                "pattern": "needle",
                "directory": ".",
                "file_extensions": [".py"],
                "output_mode": "count",
            },
            context=context,
        )
    )

    assert files_result.is_error is False
    assert "app.py" in files_result.content
    assert "notes.md" not in files_result.content
    assert ":1:" not in files_result.content
    assert count_result.is_error is False
    assert "app.py:2" in count_result.content


def test_grep_files_supports_multiline_patterns(tmp_path) -> None:
    clear_search_caches()
    (tmp_path / "shape.py").write_text("class Shape:\n    width = 1\n", encoding="utf-8")

    result = asyncio.run(
        GrepFilesTool().execute(
            {
                "pattern": "class Shape:[\\s\\S]*width",
                "directory": ".",
                "file_extensions": [".py"],
                "multiline": True,
                "output_mode": "content",
            },
            context=_workspace_context(tmp_path),
        )
    )

    assert result.is_error is False
    assert "shape.py:1:" in result.content
    assert "class Shape" in result.content


def test_list_files_reuses_unchanged_dependency_snapshot(monkeypatch, tmp_path) -> None:
    clear_list_files_cache()
    (tmp_path / "alpha.txt").write_text("a", encoding="utf-8")
    (tmp_path / "beta.txt").write_text("b", encoding="utf-8")

    tool = ListFilesTool()

    original_iterdir = Path.iterdir
    iterdir_calls = 0

    def counting_iterdir(self: Path):
        nonlocal iterdir_calls
        iterdir_calls += 1
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    args = {"directory": str(tmp_path), "recursive": False}
    context = _workspace_context(tmp_path)
    first = asyncio.run(tool.execute(args, context=context))
    second = asyncio.run(tool.execute(args, context=context))

    assert first.is_error is False
    assert second.is_error is False
    assert first.content == second.content
    assert iterdir_calls == 1


def test_recursive_list_cache_invalidates_after_external_nested_change(tmp_path) -> None:
    clear_list_files_cache()
    nested = tmp_path / "src" / "nested"
    nested.mkdir(parents=True)
    (nested / "first.txt").write_text("first", encoding="utf-8")
    tool = ListFilesTool()
    context = _workspace_context(tmp_path)

    first = asyncio.run(tool.execute({"directory": ".", "recursive": True}, context=context))
    (nested / "second.txt").write_text("second", encoding="utf-8")
    second = asyncio.run(tool.execute({"directory": ".", "recursive": True}, context=context))

    assert first.is_error is False
    assert "src/nested/first.txt" in first.content
    assert "src/nested/second.txt" not in first.content
    assert second.is_error is False
    assert "src/nested/second.txt" in second.content


def test_list_files_skips_windows_reserved_device_names(tmp_path) -> None:
    clear_list_files_cache()
    (tmp_path / "normal.txt").write_text("ok", encoding="utf-8")
    (tmp_path / "nul").write_text("reserved", encoding="utf-8")

    result = asyncio.run(
        ListFilesTool().execute(
            {"directory": "."},
            context=ToolExecutionContext(
                permission=PermissionContext(mode="auto", source="test"),
                workspace_root=tmp_path,
            ),
        )
    )

    assert result.is_error is False
    assert "normal.txt" in result.content
    assert "nul" not in result.content


def test_write_file_refreshes_list_files_result(tmp_path) -> None:
    clear_list_files_cache()
    (tmp_path / "first.txt").write_text("first", encoding="utf-8")

    list_tool = ListFilesTool()
    write_tool = WriteFileTool()
    context = _workspace_context(tmp_path)

    first_list = asyncio.run(list_tool.execute({"directory": str(tmp_path), "recursive": False}, context=context))
    assert first_list.is_error is False
    assert "first.txt" in first_list.content

    write_result = asyncio.run(
        write_tool.execute(
            {"file_path": str(tmp_path / "second.txt"), "content": "second", "expected_hash": ""},
            context=context,
        )
    )
    assert write_result.is_error is False

    second_list = asyncio.run(list_tool.execute({"directory": str(tmp_path), "recursive": False}, context=context))
    assert second_list.is_error is False
    assert "second.txt" in second_list.content


def test_write_file_rejects_stale_expected_hash(tmp_path) -> None:
    target = tmp_path / "app.py"
    target.write_text("before\n", encoding="utf-8")
    stale_hash = content_hash("before\n")
    target.write_text("external edit\n", encoding="utf-8")

    result = asyncio.run(
        WriteFileTool().execute({
            "file_path": str(target),
            "content": "agent edit\n",
            "expected_hash": stale_hash,
        }, context=_workspace_context(tmp_path))
    )

    assert result.is_error is True
    assert "actual_hash" in result.content
    assert target.read_text(encoding="utf-8") == "external edit\n"


def test_write_and_edit_file_refuse_dangerous_files(tmp_path) -> None:
    # MiniCode's DANGEROUS_FILES hard-refuse edits to
    # shell/git config; it has no credential-file list, so .env is not blocked.
    gitconfig = tmp_path / ".gitconfig"
    gitconfig.write_text("[user]\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("TOKEN=old\n", encoding="utf-8")
    context = _workspace_context(tmp_path)

    blocked = asyncio.run(
        WriteFileTool().execute(
            {"file_path": ".gitconfig", "content": "[user]\nname=x\n", "expected_hash": content_hash("[user]\n")},
            context=context,
        )
    )
    env_write = asyncio.run(
        WriteFileTool().execute(
            {"file_path": ".env", "content": "TOKEN=new\n", "expected_hash": content_hash("TOKEN=old\n")},
            context=context,
        )
    )

    assert blocked.is_error is True
    assert "protected path" in blocked.content
    assert gitconfig.read_text(encoding="utf-8") == "[user]\n"
    assert env_write.is_error is False
    assert env_file.read_text(encoding="utf-8") == "TOKEN=new\n"


def test_write_and_edit_file_refuse_protected_control_paths(tmp_path) -> None:
    git_hook = tmp_path / ".git" / "hooks" / "pre-commit"
    mcp_config = tmp_path / ".mcp.json"
    settings = tmp_path / "settings.json"
    git_hook.parent.mkdir(parents=True)
    git_hook.write_text("old hook\n", encoding="utf-8")
    mcp_config.write_text("{}\n", encoding="utf-8")
    settings.write_text("{}\n", encoding="utf-8")
    context = _workspace_context(tmp_path)

    write_git = asyncio.run(
        WriteFileTool().execute(
            {
                "file_path": ".git/hooks/pre-commit",
                "content": "new hook\n",
                "expected_hash": content_hash("old hook\n"),
            },
            context=context,
        )
    )
    edit_mcp = asyncio.run(
        EditFileTool().execute(
            {
                "file_path": ".mcp.json",
                "old_string": "{}",
                "new_string": '{"mcpServers": {}}',
                "expected_hash": content_hash("{}\n"),
            },
            context=context,
        )
    )
    # MiniCode does not hard-refuse a bare settings.json.
    write_settings = asyncio.run(
        WriteFileTool().execute(
            {
                "file_path": "settings.json",
                "content": '{"permissions": {}}\n',
                "expected_hash": content_hash("{}\n"),
            },
            context=context,
        )
    )

    assert write_git.is_error is True
    assert edit_mcp.is_error is True
    assert write_settings.is_error is False
    assert "protected path" in write_git.content
    assert "protected path" in edit_mcp.content
    assert git_hook.read_text(encoding="utf-8") == "old hook\n"
    assert mcp_config.read_text(encoding="utf-8") == "{}\n"
    assert settings.read_text(encoding="utf-8") == '{"permissions": {}}\n'


def test_edit_file_requires_expected_hash_for_existing_file(tmp_path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")

    missing_hash = asyncio.run(
        EditFileTool().execute({
            "file_path": str(target),
            "old_string": "1",
            "new_string": "2",
        }, context=_workspace_context(tmp_path))
    )
    fresh_hash = content_hash("value = 1\n")
    edited = asyncio.run(
        EditFileTool().execute({
            "file_path": str(target),
            "old_string": "1",
            "new_string": "2",
            "expected_hash": fresh_hash,
        }, context=_workspace_context(tmp_path))
    )

    assert missing_hash.is_error is True
    assert "expected_hash is required" in missing_hash.content
    assert edited.is_error is False
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_stdlib_regex_fallback_rejects_uncancellable_backtracking_pattern(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "sample.txt").write_text("a" * 128 + "!", encoding="utf-8")
    monkeypatch.setattr(search_tools, "_HAS_RIPGREP", False)
    monkeypatch.setattr(search_tools, "_safe_regex", re)
    tool = search_tools.GrepFilesTool(workspace_root=tmp_path)

    result = asyncio.run(
        tool.execute(
            {
                "pattern": "(a|aa)+$",
                "path": str(tmp_path),
                "file_extensions": [".txt"],
            }
        )
    )

    assert result.is_error is True
    assert "无法安全执行" in result.content
