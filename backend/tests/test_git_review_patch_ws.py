"""Copy-patch reads preserve the selected executor/source and real binary data."""
import pytest
import subprocess

from backend.diff.review_patch import review_patch

from backend.tests.test_executor_editor_api import editor_server
from backend.tests.test_executor_tool_admission import executor_host
from backend.tests.test_executor_git_changes_ws import assert_owner, request, seed
from backend.tests.test_executor_git_review_ws import git


@pytest.mark.asyncio
async def test_selected_patch_sources_are_complete_and_apply_to_their_real_baseline(editor_server):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    await git(server, "branch", "patch-base")
    (root / "tracked.txt").write_bytes(b"committed version\n")
    await git(server, "add", "tracked.txt")
    await git(server, "-c", "user.name=MiniCode", "-c", "user.email=test@example.invalid",
              "commit", "--no-gpg-sign", "-qm", "patch commit")
    commit = await git(server, "rev-parse", "HEAD")
    (root / "tracked.txt").write_bytes(b"staged version\n")
    await git(server, "add", "tracked.txt")
    (root / "tracked.txt").write_bytes(b"working version  \n")
    image = bytes(range(256)) * 4
    (root / "new image.bin").write_bytes(image)

    patches = {}
    for source in ("staged", "unstaged", "uncommitted", "branch", "commit"):
        result = await request(server, "diff.git_review_patch", expected="command.result", source=source,
                               base_branch="patch-base", commit_sha=commit, hide_whitespace=True)
        assert result["level"] == "success", result
        assert_owner(server, result)
        patches[source] = result["data"]["unified_diff"]
    assert "-committed version\n+staged version\n" in patches["staged"]
    assert "-staged version\n+working version  \n" in patches["unstaged"]
    assert "-committed version\n+working version  \n" in patches["uncommitted"]
    assert "-before\n+working version  \n" in patches["branch"]
    assert "-before\n+committed version\n" in patches["commit"]
    for source in ("unstaged", "uncommitted", "branch"):
        assert "GIT binary patch" in patches[source]
    assert "new image.bin" not in patches["staged"]
    assert "new image.bin" not in patches["commit"]

    # Use the exported patch rather than a display-filtered approximation.
    (root / "copied.patch").write_bytes(patches["branch"].encode())
    await git(server, "worktree", "add", "--detach", "apply-target", "patch-base")
    await git(server, "-C", "apply-target", "apply", "--3way", "../copied.patch")
    assert (root / "apply-target" / "tracked.txt").read_bytes() == b"working version  \n"
    assert (root / "apply-target" / "new image.bin").read_bytes() == image


@pytest.mark.asyncio
async def test_subdirectory_export_uses_repository_paths_for_tracked_and_untracked_files(tmp_path):
    def run(*args, cwd=tmp_path, patch=None):
        subprocess.run(["git", *args], cwd=cwd, input=patch, check=True, capture_output=True)

    run("init", "-q")
    run("config", "core.autocrlf", "false")
    folder = tmp_path / "nested files"
    folder.mkdir()
    (folder / "tracked.txt").write_bytes(b"before\n")
    run("add", ".")
    run("-c", "user.name=MiniCode", "-c", "user.email=test@example.invalid", "commit", "--no-gpg-sign", "-qm", "base")
    (folder / "tracked.txt").write_bytes(b"after\n")
    (folder / "new.bin").write_bytes(bytes(range(256)))
    patch = await review_patch(str(folder), "uncommitted")
    assert "a/nested files/tracked.txt" in patch
    assert "b/nested files/new.bin" in patch
    run("worktree", "add", "--detach", "patch-target", "HEAD")
    target = tmp_path / "patch-target"
    run("apply", "--3way", "-", cwd=target, patch=patch.encode())
    assert (target / "nested files" / "tracked.txt").read_bytes() == b"after\n"
    assert (target / "nested files" / "new.bin").read_bytes() == bytes(range(256))
