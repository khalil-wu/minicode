from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools import search_support, search_tools
from backend.tools.ast_tools import FindReferencesTool, GoToDefinitionTool
from backend.tools.fuzzy_search_tool import FuzzySearchTool
from backend.tools.git_tools import GitDiffTool


def context(root: Path, *, deny: list[str], allow: list[str] | None = None) -> ToolExecutionContext:
    constraints = {"denylist": deny}
    if allow is not None:
        constraints["allowlist"] = allow
    permission = PermissionContext(workspace_root=root, filesystem_constraints=constraints)
    checker = PermissionChecker(PermissionSettings(path_denylist=deny), root)
    return ToolExecutionContext(permission=permission, workspace_root=root, permission_checker=checker)


@pytest.fixture(params=[False, True], ids=["python", "ripgrep"])
def backend(request, monkeypatch):
    if request.param and not shutil.which("rg"):
        pytest.skip("ripgrep is not installed")
    monkeypatch.setattr(search_tools, "_HAS_RIPGREP", request.param)
    return request.param


@pytest.mark.parametrize("directory", [".", "src"])
@pytest.mark.parametrize("policy", ["deny", "allowlist", "negation", "case"])
def test_search_authorizes_concrete_paths_before_reading_and_paginating(tmp_path: Path, monkeypatch, backend, directory, policy):
    import backend.permissions.checker as checker_module

    public = tmp_path / "src/public/allowed.py"
    denied = tmp_path / "src/private/hidden.py"
    exception = tmp_path / "src/private/exception.py"
    for path in (public, denied, exception):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"NEEDLE {path.name}\n", encoding="utf-8")
    deny = ["src/private/**"]
    allow = None
    if policy == "allowlist":
        deny, allow = [], ["src/public"]
    elif policy == "negation":
        deny.append("!src/private/exception.py")
    elif policy == "case":
        monkeypatch.setattr(checker_module, "_FILESYSTEM_IS_CASE_INSENSITIVE", True)
        deny = ["SRC/PRIVATE/**"]
    owner = context(tmp_path, deny=deny, allow=allow)
    allowed = [path for path in (public, denied, exception) if owner.permission_checker.is_path_allowed(str(path), context=owner.permission)]
    assert public in allowed and denied not in allowed
    file_open = Path.open
    opened = []

    def tracked_open(path, *args, **kwargs):
        if path in (public, denied, exception):
            assert path in allowed, f"Unauthorized file was opened: {path}"
            opened.append(path)
        return file_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    spawn = search_support.spawn_exec
    content_search_paths = []

    async def tracked_spawn(*args, **kwargs):
        if args[0] == "rg" and "--files" not in args:
            paths = [Path(kwargs["cwd"]) / value for value in args[args.index("--") + 1:]]
            assert paths and all(path in allowed for path in paths)
            content_search_paths.extend(paths)
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(search_support, "spawn_exec", tracked_spawn)
    grep = asyncio.run(search_tools.GrepFilesTool().execute({"pattern": "NEEDLE", "path": directory, "glob": "**/*.py", "output_mode": "content"}, owner))
    glob = asyncio.run(search_tools.GlobFilesTool().execute({"pattern": "**/*.py", "path": directory, "head_limit": 0}, owner))
    assert not grep.is_error and not glob.is_error
    for path in (public, denied, exception):
        assert (path.name in grep.content) is (path in allowed)
        assert (path.name in glob.content) is (path in allowed)
    assert (content_search_paths if backend else opened)


@pytest.mark.parametrize("filters", [
    {"glob": "public/*.py"},
    {"glob": "*.{py,vue}"},
    {"glob": "!*.txt"},
    {"file_extensions": ["py"]},
])
def test_authorized_grep_keeps_query_filters(tmp_path: Path, backend, filters):
    for name in ("public/source.py", "private/hidden.py", "unrelated.txt"):
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("NEEDLE\n", encoding="utf-8")
    result = asyncio.run(search_tools.GrepFilesTool().execute(
        {"pattern": "NEEDLE", **filters}, context(tmp_path, deny=["private/**"]),
    ))
    assert not result.is_error
    assert "source.py" in result.content
    assert "hidden.py" not in result.content and "unrelated.txt" not in result.content


def test_authorized_ripgrep_keeps_native_types_and_ignore_rules(tmp_path: Path, monkeypatch):
    if not shutil.which("rg"):
        pytest.skip("ripgrep is not installed")
    monkeypatch.setattr(search_tools, "_HAS_RIPGREP", True)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    for name in ("visible.vue", "ignored.vue", "ignored/hidden.vue", "other.txt"):
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.vue\nignored/\n", encoding="utf-8")
    owner = context(tmp_path, deny=[])
    default = asyncio.run(search_tools.GrepFilesTool().execute({"pattern": "NEEDLE", "type": "vue"}, owner))
    explicit = asyncio.run(search_tools.GrepFilesTool().execute({"pattern": "NEEDLE", "glob": "*.vue"}, owner))
    assert not default.is_error and not explicit.is_error
    assert "visible.vue" in default.content and "ignored.vue" not in default.content
    assert "other.txt" not in default.content and "hidden.vue" not in default.content
    assert "ignored.vue" in explicit.content and "hidden.vue" not in explicit.content


@pytest.mark.parametrize("tool,args", [
    (search_tools.GrepFilesTool, {"pattern": "NEEDLE"}),
    (search_tools.GlobFilesTool, {"pattern": "*.py"}),
    (FuzzySearchTool, {"query": "source"}),
    (GitDiffTool, {}),
])
def test_projectless_search_does_not_borrow_constructor_workspace(tmp_path: Path, tool, args):
    owner = ToolExecutionContext(permission=PermissionContext(workspace_root=None), workspace_root=None)
    result = asyncio.run(tool(tmp_path).execute(args, owner))
    assert result.is_error and "open workspace" in result.content


def test_compiled_permission_rules_preserve_current_policy_and_order(tmp_path: Path):
    owner = context(tmp_path, deny=["private/**", "!private/exception.py"])
    checker = owner.permission_checker
    assert checker.is_path_allowed("private/exception.py", context=owner.permission)
    owner.permission.filesystem_constraints["denylist"].reverse()
    assert not checker.is_path_allowed("private/exception.py", context=owner.permission)
    owner.permission.filesystem_constraints["denylist"] = []
    assert checker.is_path_allowed("private/exception.py", context=owner.permission)


def test_prepared_permission_resolves_root_once_and_each_candidate_afresh(tmp_path: Path, monkeypatch):
    owner = context(tmp_path, deny=["private/**"])
    resolve = Path.resolve
    alias = tmp_path / "alias.py"
    targets = iter([tmp_path / "public.py", tmp_path / "private/hidden.py"])
    root_resolutions = []

    def tracked_resolve(path, *args, **kwargs):
        if path == tmp_path:
            root_resolutions.append(path)
        if path == alias:
            return next(targets)
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", tracked_resolve)
    allowed = owner.permission_checker.prepare_path_check(context=owner.permission)
    assert allowed(str(alias))
    assert not allowed(str(alias))
    assert root_resolutions == [tmp_path]


def test_explicit_denied_grep_file_never_reaches_ripgrep(tmp_path: Path, monkeypatch):
    target = tmp_path / "private.txt"
    target.write_text("NEEDLE", encoding="utf-8")
    owner = context(tmp_path, deny=["private.txt"])
    monkeypatch.setattr(search_tools, "_HAS_RIPGREP", True)

    async def unexpected(*args, **kwargs):
        raise AssertionError("Denied explicit file reached subprocess")

    monkeypatch.setattr(search_support, "spawn_exec", unexpected)
    result = asyncio.run(search_tools.GrepFilesTool().execute({"pattern": "NEEDLE", "path": "private.txt"}, owner))
    assert result.is_error
    assert "permission denied" in result.content


@pytest.mark.parametrize("tool", [GoToDefinitionTool, FindReferencesTool])
def test_ast_search_uses_context_directory_and_never_reads_denied_sources(tmp_path: Path, monkeypatch, tool):
    workspace = tmp_path / "workspace"
    visible = workspace / "src/visible.py"
    denied = workspace / "src/private/hidden.py"
    noisy = workspace / "src/node_modules/noisy.py"
    for file in (visible, denied, noisy):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("def target_symbol():\n    return target_symbol\n", encoding="utf-8")
    wrong = tmp_path / "src"
    wrong.mkdir()
    (wrong / "wrong.py").write_text("def target_symbol(): pass\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    owner = context(workspace, deny=["src/private/**"])
    read_text = Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert path not in (denied, noisy)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    result = asyncio.run(tool().execute({"name": "target_symbol", "directory": "src"}, owner))
    assert not result.is_error
    assert "visible.py" in result.content
    assert "wrong.py" not in result.content
    assert "hidden.py" not in result.content
    assert "noisy.py" not in result.content


def test_fuzzy_search_filters_before_top_k_and_does_not_cache_permission_decisions(tmp_path: Path):
    for name in ("private/item.py", "public/LongItemHelper.py"):
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("fixture", encoding="utf-8")
    tool = FuzzySearchTool(tmp_path)
    limited = asyncio.run(tool.execute({"query": "item", "max_results": 1}, context(tmp_path, deny=["private/**"])))
    assert "LongItemHelper.py" in limited.content and "private" not in limited.content
    other = asyncio.run(tool.execute({"query": "item", "max_results": 1}, context(tmp_path, deny=["public/**"])))
    assert "private" in other.content and "LongItemHelper.py" not in other.content


@pytest.mark.parametrize("focused", [None, "private", "private/隐秘.txt"])
def test_git_diff_uses_real_nul_delimited_names_for_permission_checks(tmp_path: Path, focused):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    for name in ("private/隐秘.txt", "public.txt"):
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    for name in ("private/隐秘.txt", "public.txt"):
        (tmp_path / name).write_text("SECRET_MARKER\n" if name.startswith("private") else "PUBLIC_MARKER\n", encoding="utf-8")
    args = {"file_path": focused} if focused else {}
    result = asyncio.run(GitDiffTool().execute(args, context(tmp_path, deny=["private/**"])))
    assert "SECRET_MARKER" not in result.content
    if focused is None:
        assert not result.is_error and "PUBLIC_MARKER" in result.content


def test_git_diff_checks_the_denied_source_of_a_rename(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    source = tmp_path / "private/old.txt"
    source.parent.mkdir()
    shared = "".join(f"shared source line {index}\n" for index in range(40))
    source.write_text(shared + "SECRET_SOURCE\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    destination = tmp_path / "public.txt"
    destination.write_text(shared + "PUBLIC_MARKER\n", encoding="utf-8")
    source.unlink()
    subprocess.run(["git", "-C", str(tmp_path), "add", "--intent-to-add", "public.txt"], check=True, capture_output=True)
    raw = subprocess.run(["git", "-C", str(tmp_path), "diff"], check=True, capture_output=True).stdout
    assert b"rename from private/old.txt" in raw and b"SECRET_SOURCE" in raw
    result = asyncio.run(GitDiffTool().execute({}, context(tmp_path, deny=["private/**"])))
    assert not result.is_error
    assert "SECRET_SOURCE" not in result.content and "PUBLIC_MARKER" in result.content


def test_ripgrep_enumeration_and_batches_share_transport_and_time_budgets(tmp_path: Path, monkeypatch):
    limits = []
    timeouts = []
    now = 100.0
    monkeypatch.setattr(search_support, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(search_support, "RIPGREP_TRANSPORT_LIMIT_BYTES", 100)
    monkeypatch.setattr(search_support, "_ripgrep_path_batches", lambda cmd, paths, deadline, root, allowed: ([str(path)] for path in paths))

    async def spawn(*args, **kwargs):
        return SimpleNamespace(args=args, returncode=0)

    async def communicate(proc, *, timeout, stdout_limit_bytes, stderr_limit_bytes):
        nonlocal now
        limits.append(stdout_limit_bytes)
        timeouts.append(timeout)
        assert stdout_limit_bytes == stderr_limit_bytes
        now += 0.5
        output = b"a.py\0b.py\0" if "--files" in proc.args else b"x" * 70
        if len(output) > stdout_limit_bytes:
            raise search_support.SubprocessOutputLimitError(
                stream_name="stdout", limit_bytes=stdout_limit_bytes, captured=output[:stdout_limit_bytes],
            )
        return output, b""

    monkeypatch.setattr(search_support, "spawn_exec", spawn)
    monkeypatch.setattr(search_support, "communicate_bounded", communicate)
    output, is_error = asyncio.run(search_support._grep_with_ripgrep("NEEDLE", tmp_path, is_allowed=lambda path: True))
    assert is_error and "transport limit" in output
    assert limits == [100, 90, 20]
    assert timeouts[0] > timeouts[1] > timeouts[2]


def test_ripgrep_candidate_deadline_applies_when_all_paths_are_denied(tmp_path: Path, monkeypatch):
    ticks = iter([1.0, 2.0])
    monkeypatch.setattr(search_support.time, "monotonic", lambda: next(ticks))
    paths = iter([tmp_path / "first.py", tmp_path / "second.py"])
    batches = search_support._ripgrep_path_batches(["rg"], paths, 1.5, tmp_path, lambda path: False)
    with pytest.raises(search_support.SearchResourceLimitError, match="candidate selection"):
        next(batches)


def test_ripgrep_authorized_batches_respect_windows_argv_and_global_pagination(tmp_path: Path, monkeypatch):
    if not shutil.which("rg"):
        pytest.skip("ripgrep is not installed")
    paths = []
    for index in range(40):
        path = tmp_path / f"long_source_filename_{index:03d}.py"
        path.write_text("NEEDLE\n", encoding="utf-8")
        os.utime(path, ns=(1_000_000_000 + index * 1_000_000, 1_000_000_000 + index * 1_000_000))
        paths.append(path)
    monkeypatch.setattr(search_tools, "_HAS_RIPGREP", True)
    monkeypatch.setattr(search_support, "RIPGREP_ARGV_LIMIT", 1100)
    spawn = search_support.spawn_exec
    calls = []

    async def checked_spawn(*args, **kwargs):
        if "--files" not in args:
            calls.append(args)
        assert len(subprocess.list2cmdline(args).encode("utf-16-le")) // 2 <= 1100
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(search_support, "spawn_exec", checked_spawn)
    result = asyncio.run(search_tools.GrepFilesTool().execute({"pattern": "NEEDLE", "head_limit": 2, "offset": 3}, context(tmp_path, deny=[])))
    assert not result.is_error
    assert len(calls) > 1
    assert paths[-4].name in result.content and paths[-5].name in result.content
    assert sum(path.name in result.content for path in paths) == 2
