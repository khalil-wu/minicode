import pytest

from backend.workspace.context import WorkspaceContext


@pytest.mark.asyncio
async def test_workspace_index_prunes_environment_data_and_model_dirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
    for dirname in [".conda", "data", "checkpoints", "node_modules", ".ipynb_checkpoints"]:
        folder = tmp_path / dirname
        folder.mkdir()
        (folder / "ignored.py").write_text("print('skip')", encoding="utf-8")
    (tmp_path / "model.pt").write_text("binary-ish", encoding="utf-8")

    ctx = WorkspaceContext(tmp_path)
    metadata = await ctx.initialize()

    assert metadata.file_count == 1
    assert [path.replace("\\", "/") for path in sorted(ctx.file_index.keys())] == ["src/app.py"]
    assert not ctx.index_truncated


@pytest.mark.asyncio
async def test_workspace_index_stops_at_configured_cap(tmp_path):
    for index in range(5):
        (tmp_path / f"file-{index}.txt").write_text(str(index), encoding="utf-8")

    ctx = WorkspaceContext(tmp_path, max_index_files=3)
    metadata = await ctx.initialize()

    assert metadata.file_count == 3
    assert ctx.index_truncated
    summary = ctx.get_project_summary()
    assert "已截断到前 3 个文件" in summary


@pytest.mark.asyncio
async def test_workspace_index_uses_gitignore_wildmatch_semantics(tmp_path):
    (tmp_path / ".gitignore").write_text(
        "\n".join([
            "*.log",
            "!important.log",
            "/root-only.txt",
            "cache/",
            "generated/*",
            "!generated/keep.txt",
            "docs/**/*.tmp",
        ]),
        encoding="utf-8",
    )
    files = {
        "app.log": "ignored glob",
        "important.log": "negation keeps this",
        "root-only.txt": "root anchored ignore",
        "nested/root-only.txt": "nested path is not root anchored",
        "cache/ignored.py": "directory rule",
        "nested/cache/ignored.py": "directory rule at any depth",
        "generated/drop.py": "ignored child",
        "generated/keep.txt": "negated child",
        "docs/top.tmp": "recursive glob",
        "docs/nested/deep.tmp": "recursive glob",
        "src/keep.tmp": "outside recursive glob",
    }
    for relative_path, content in files.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    ctx = WorkspaceContext(tmp_path)
    await ctx.initialize()

    indexed = {path.replace("\\", "/") for path in ctx.file_index}
    assert indexed == {
        ".gitignore",
        "important.log",
        "nested/root-only.txt",
        "generated/keep.txt",
        "src/keep.tmp",
    }


@pytest.mark.asyncio
async def test_workspace_reports_only_minicode_project_instructions(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("external instructions", encoding="utf-8")
    context = WorkspaceContext(tmp_path)
    await context.initialize()
    assert context.to_dict()["has_project_instructions"] is False

    rules = tmp_path / ".minicode" / "rules"
    rules.mkdir(parents=True)
    (rules / "python.md").write_text("MiniCode instructions", encoding="utf-8")
    context = WorkspaceContext(tmp_path)
    await context.initialize()
    assert context.to_dict()["has_project_instructions"] is True
