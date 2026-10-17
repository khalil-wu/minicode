"""Source comparisons and atomic UI actions through the real WS/executor chain."""
from __future__ import annotations

import base64

import pytest

from backend.tests.test_executor_editor_api import editor_server
from backend.tests.test_executor_tool_admission import executor_host
from backend.tests.test_executor_git_changes_ws import request, review_action, seed
from backend.tests.test_executor_git_review_ws import contents, git, item


@pytest.mark.asyncio
async def test_uncommitted_is_one_head_to_disk_version_not_staged_plus_unstaged(editor_server):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    for name in ("tracked.txt", "wild1.txt"):
        (root / name).write_bytes(b"index version\n")
    await git(server, "add", "tracked.txt", "wild1.txt")
    (root / "tracked.txt").write_bytes(b"working version\n")
    (root / "wild1.txt").write_bytes(b"before\n")
    (root / "new.txt").write_bytes(b"first\nsecond\n")
    uncommitted = await request(server, "diff.git_working_tree", source="uncommitted")
    assert uncommitted["source"] == "uncommitted" and uncommitted["status"] == "success"
    assert [row["path"] for row in uncommitted["files"]] == ["tracked.txt"]
    assert [row["path"] for row in uncommitted["untracked_files"]] == ["new.txt"]
    assert (uncommitted["total_additions"], uncommitted["total_deletions"]) == (3, 1)
    unstaged = await request(server, "diff.git_working_tree")
    assert unstaged["source"] == "unstaged"
    assert {row["path"] for row in unstaged["files"]} == {"tracked.txt", "wild1.txt"}
    staged = await request(server, "diff.git_staged")
    head_disk = await contents(server, uncommitted, item("tracked.txt", "old"), item("tracked.txt"))
    head_index = await contents(server, staged, item("tracked.txt", "old"), item("tracked.txt"))
    index_disk = await contents(server, unstaged, item("tracked.txt", "old"), item("tracked.txt"))
    assert [row["lines"] for row in head_disk["results"]] == [["before\n"], ["working version\n"]]
    assert [row["lines"] for row in head_index["results"]] == [["before\n"], ["index version\n"]]
    assert [row["lines"] for row in index_disk["results"]] == [["index version\n"], ["working version\n"]]
    assert uncommitted["files"][0]["revision"].startswith("uncommitted:M:")


@pytest.mark.asyncio
async def test_uncommitted_keeps_staged_rename_identity_after_complete_rewrite(editor_server):
    server = editor_server
    await seed(server)
    await git(server, "mv", "rename 元.txt", "rename 后.txt")
    (server.host.workspace / "rename 后.txt").write_bytes(b"complete new content\n")
    review = await request(server, "diff.git_working_tree", source="uncommitted")
    row, = review["files"]
    assert row["status"] == "R" and row["path"] == "rename 后.txt" and row["old_path"] == "rename 元.txt"
    assert (row["additions"], row["deletions"]) == (1, 1)
    assert "rename from rename 元.txt" in row["patch"] and "rename to rename 后.txt" in row["patch"]
    result = await contents(server, review, item(row["path"], "old"), item(row["path"]))
    assert [side["lines"] for side in result["results"]] == [["before\n"], ["complete new content\n"]]


@pytest.mark.asyncio
async def test_source_untracked_same_path_overrides_staged_deletion_metadata_once(editor_server):
    server = editor_server
    await seed(server)
    await git(server, "rm", "tracked.txt")
    (server.host.workspace / "tracked.txt").write_bytes(b"recreated untracked version\n")
    review = await request(server, "diff.git_working_tree", source="uncommitted")
    assert review["files"] == []
    row, = review["untracked_files"]
    assert row["path"] == "tracked.txt" and row["status"] == "A" and row["old_oid"] is None
    assert (review["total_additions"], review["total_deletions"]) == (1, 0)
    result = await contents(server, review, item("tracked.txt", "old"), item("tracked.txt"))
    assert result["results"][0]["source_kind"] == "absent"
    assert result["results"][1]["lines"] == ["recreated untracked version\n"]


@pytest.mark.asyncio
async def test_branch_commit_options_use_actual_merge_base_and_object_versions_with_separate_live_markdown(editor_server):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    base = await git(server, "rev-parse", "HEAD")
    await git(server, "branch", "source-base")
    (root / "tracked.txt").write_bytes(b"committed version\n")
    (root / "review.md").write_bytes(b"# Committed Markdown\n")
    (root / "image.png").write_bytes(b"\x89PNG\x00committed image")
    await git(server, "add", "tracked.txt", "review.md", "image.png")
    await git(server, "-c", "user.name=MiniCode", "-c", "user.email=test@example.invalid", "commit", "--no-gpg-sign", "-qm", "source commit")
    commit = await git(server, "rev-parse", "HEAD")
    (root / "tracked.txt").write_bytes(b"current working version\n")
    (root / "review.md").write_bytes(b"# Live Markdown\n")
    (root / "image.png").write_bytes(b"\x89PNG\x00current image must not replace committed blob")
    (root / "untracked.md").write_bytes(b"# Untracked\n")
    options = await request(server, "diff.git_review_options", expected="command.result", base_branch="source-base")
    assert options["level"] == "success", options
    assert options["data"]["merge_base_sha"] == base
    assert options["data"]["default_base_branch"] is None
    assert [entry["sha"] for entry in options["data"]["commits"]] == [commit]
    assert options["data"]["commits"][0]["subject"] == "source commit"
    unavailable = await request(server, "diff.git_working_tree", source="branch")
    assert unavailable["status"] == "expected" and unavailable["reason"] == "default_branch_unavailable"
    assert unavailable["files"] == unavailable["untracked_files"] == []
    invalid_base = await request(server, "diff.git_working_tree", source="branch", base_branch="missing-actual-base")
    assert invalid_base["status"] == "expected" and invalid_base["reason"] == "merge_base_unavailable"
    branch = await request(server, "diff.git_working_tree", source="branch", base_branch="source-base")
    assert branch["comparison"] == {"base_branch": "source-base", "merge_base_sha": base}
    assert "untracked.md" in branch["untracked"]
    committed = await request(server, "diff.git_working_tree", source="commit", commit_sha=commit)
    assert committed["comparison"] == {"commit_sha": commit, "parent_sha": base}
    assert committed["untracked"] == committed["untracked_files"] == []
    history = await contents(server, committed, item("tracked.txt", "old"), item("tracked.txt"), item("review.md"))
    assert [entry["lines"] for entry in history["results"]] == [["before\n"], ["committed version\n"], ["# Committed Markdown\n"]]
    assert all(entry["source_kind"] == "object" for entry in history["results"])
    image = await contents(server, committed, item("image.png", "new", "binary"))
    assert base64.b64decode(image["results"][0]["data_base64"]) == b"\x89PNG\x00committed image"
    assert image["results"][0]["source_kind"] == "object"
    branch_bytes = await contents(server, branch, item("tracked.txt", "old"), item("tracked.txt"))
    assert [entry["lines"] for entry in branch_bytes["results"]] == [["before\n"], ["current working version\n"]]
    (root / "review.md").write_bytes(b"# Changed current Markdown\n")
    preview = await contents(server, committed, item("review.md", "new", "markdown-preview"))
    assert preview["results"][0]["lines"] == ["# Changed current Markdown\n"]
    assert preview["results"][0]["source_kind"] == "disk" and "oid" not in preview["results"][0]
    strict = await contents(server, committed, item("review.md"))
    assert strict["results"][0]["lines"] == ["# Committed Markdown\n"] and strict["results"][0]["source_kind"] == "object"
    (root / "review.md").unlink()
    missing_preview = await contents(server, committed, item("review.md", "new", "markdown-preview"))
    assert missing_preview["results"][0]["error"]["type"] == "not-found"


@pytest.mark.asyncio
async def test_default_branch_uses_a_real_recorded_remote_head_instead_of_an_invented_local_main(editor_server):
    server = editor_server
    await seed(server)
    base = await git(server, "rev-parse", "HEAD")
    await git(server, "remote", "add", "source", server.host.workspace.as_posix())
    await git(server, "update-ref", "refs/remotes/source/release", base)
    await git(server, "symbolic-ref", "refs/remotes/source/HEAD", "refs/remotes/source/release")
    (server.host.workspace / "tracked.txt").write_bytes(b"real branch comparison\n")
    options = await request(server, "diff.git_review_options", expected="command.result")
    assert options["data"]["default_base_branch"] == "source/release"
    assert options["data"]["merge_base_sha"] == base and options["data"]["comparison_status"] == "success"
    assert "source/release" in options["data"]["branches"]
    review = await request(server, "diff.git_working_tree", source="branch")
    assert review["comparison"] == {"base_branch": "source/release", "merge_base_sha": base}
    assert review["status"] == "success" and "+real branch comparison" in review["files"][0]["patch"]


@pytest.mark.asyncio
async def test_source_remote_head_timeout_only_uses_verified_remote_tracking_candidates(editor_server, monkeypatch):
    from backend.diff import comparison

    server = editor_server
    await seed(server)
    base = await git(server, "rev-parse", "HEAD")
    await git(server, "remote", "add", "source", server.host.workspace.as_posix())
    await git(server, "update-ref", "refs/remotes/source/main", base)
    original = comparison._result
    timed_out = []
    async def source_timeout(root, args, service, **kwargs):
        if args == ["remote", "show", "source"]:
            timed_out.append(service)
            raise TimeoutError("Source remote HEAD query timed out")
        return await original(root, args, service, **kwargs)
    monkeypatch.setattr(comparison, "_result", source_timeout)
    found = await request(server, "diff.git_review_options", expected="command.result")
    assert found["level"] == "success" and found["data"]["default_base_branch"] == "source/main", found
    assert found["data"]["merge_base_sha"] == base
    await git(server, "update-ref", "-d", "refs/remotes/source/main")
    missing = await request(server, "diff.git_review_options", expected="command.result")
    assert missing["level"] == "success" and missing["data"]["default_base_branch"] is None, missing
    assert missing["data"]["comparison_status"] == "expected" and missing["data"]["reason"] == "default_branch_unavailable"
    assert len(timed_out) == 2
    assert not (server.view / ".git").exists()


@pytest.mark.asyncio
async def test_review_options_and_prepare_share_the_same_executor_and_real_source_base_resolver(editor_server):
    server = editor_server
    await seed(server)
    (server.host.workspace / "tracked.txt").write_bytes(b"executor review only\n")
    initial = await request(server, "review.options", expected="command.result")
    assert initial["level"] == "success", initial
    assert initial["data"]["defaultBaseBranch"] is None
    assert initial["data"]["comparisonStatus"] == "expected" and initial["data"]["reason"] == "default_branch_unavailable"
    assert initial["data"]["branches"] == []
    assert initial["data"]["environment_id"] == server.params["environment_id"]
    await git(server, "branch", "review-base")
    options = await request(server, "review.options", expected="command.result")
    assert options["data"]["branches"] == ["review-base"]
    prepared = await request(server, "review.prepare", expected="command.result", mode="base-branch", base_branch="review-base")
    assert prepared["level"] == "success", prepared
    payload = prepared["data"]
    assert payload["conversation_id"] == server.params["conversation_id"]
    assert payload["cwd_uri"] == server.host.workspace.as_uri()
    assert "+executor review only" in payload["diff"] and "controller must remain untouched" not in payload["diff"]
    assert payload["scope"]["baseBranch"] == "review-base"
    base = payload["scope"]["mergeBaseSha"]
    await git(server, "branch", "-D", "review-base")
    cold = await request(server, "review.prepare", expected="command.result", mode="base-branch", base_branch="review-base", merge_base_sha=base)
    assert cold["level"] == "success" and cold["data"]["diff"] == payload["diff"], cold
    uncommitted = await request(server, "review.prepare", expected="command.result", mode="uncommitted")
    assert uncommitted["level"] == "success" and "+executor review only" in uncommitted["data"]["diff"]
    assert not (server.view / ".git").exists()


@pytest.mark.asyncio
async def test_review_prepare_from_a_child_cwd_keeps_repository_relative_paths_on_the_same_executor(editor_server, monkeypatch):
    from backend.agent.environment_selection import TurnEnvironmentRequest, TurnEnvironmentSelection

    server = editor_server
    await seed(server)
    root = server.host.workspace
    child = root / "nested"
    child.mkdir()
    (child / "inside.txt").write_bytes(b"inside before\n")
    await git(server, "add", "nested/inside.txt")
    await git(server, "-c", "user.name=MiniCode", "-c", "user.email=test@example.invalid", "commit", "--no-gpg-sign", "-qm", "nested base")
    (child / "inside.txt").write_bytes(b"inside after\n")
    (root / "tracked.txt").write_bytes(b"repository root after\n")
    await server.host.owner.update_selections((TurnEnvironmentSelection.from_request(TurnEnvironmentRequest(
        server.params["environment_id"], child.as_uri(), (root.as_uri(),))),))
    original = server.host.environments.primary().environment.process.start
    starts = []
    async def recorded(argv, cwd, **kwargs):
        starts.append((argv, cwd))
        return await original(argv, cwd, **kwargs)
    monkeypatch.setattr(server.host.environments.primary().environment.process, "start", recorded)
    prepared = await request(server, "review.prepare", expected="command.result", mode="uncommitted",
        workspace_root=child.as_uri(), cwd_uri=child.as_uri())
    assert prepared["level"] == "success", prepared
    payload = prepared["data"]
    assert payload["scope"]["cwd"] in {str(child), child.as_uri(), child.as_posix()}
    assert payload["scope"]["gitRoot"] in {str(root), root.as_uri(), root.as_posix()}
    assert payload["cwd_uri"] == child.as_uri()
    assert "diff --git a/nested/inside.txt b/nested/inside.txt" in payload["diff"]
    assert "diff --git a/tracked.txt b/tracked.txt" in payload["diff"]
    assert all(cwd in {child.as_uri(), root.as_uri()} for _, cwd in starts)
    assert {cwd for argv, cwd in starts if "diff" in argv} == {root.as_uri()}
    assert not (server.view / ".git").exists()


@pytest.mark.asyncio
async def test_unborn_sources_and_atomic_unstage_do_not_require_a_head_commit(editor_server):
    server = editor_server
    root = server.host.workspace
    await git(server, "init", "-q")
    await git(server, "config", "core.autocrlf", "false")
    (root / "initial.txt").write_bytes(b"index before first commit\n")
    await git(server, "add", "initial.txt")
    (root / "initial.txt").write_bytes(b"disk before first commit\n")
    review = await request(server, "diff.git_working_tree", source="uncommitted")
    row, = review["files"]
    assert row["status"] == "A" and row["old_oid"] is None
    sides = await contents(server, review, item("initial.txt", "old"), item("initial.txt"))
    assert [entry["lines"] for entry in sides["results"]] == [[], ["disk before first commit\n"]]
    staged = await review_action(server, "diff.git_stage_file", path="initial.txt")
    assert staged["level"] == "success", staged
    unstaged = await review_action(server, "diff.git_unstage_all")
    assert unstaged["level"] == "success", unstaged
    assert await git(server, "ls-files") == ""
    assert (root / "initial.txt").read_bytes() == b"disk before first commit\n"


@pytest.mark.asyncio
async def test_ui_action_requires_the_displayed_scope_generation_and_never_accepts_stale_or_uncommitted(editor_server):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    (root / "tracked.txt").write_bytes(b"captured change\n")
    captured = await request(server, "diff.git_working_tree")
    (root / "tracked.txt").write_bytes(b"newer change\n")
    stale = await review_action(server, "diff.git_stage_file", path="tracked.txt", snapshot=captured)
    assert stale["level"] == "error" and stale["data"]["status"] == "stale-snapshot", stale
    missing = await request(server, "diff.git_stage_file", expected="command.result", path="tracked.txt", source="unstaged")
    assert missing["level"] == "error" and "snapshot_generation" in missing["message"]
    uncommitted = await request(server, "diff.git_working_tree", source="uncommitted")
    refused = await review_action(server, "diff.git_revert_file", source="uncommitted", snapshot=uncommitted,
        path="tracked.txt", confirmed=True)
    assert refused["level"] == "error" and "explicit staged or unstaged" in refused["message"]
    assert await git(server, "show", ":tracked.txt") == "before"
    assert (root / "tracked.txt").read_bytes() == b"newer change\n"


@pytest.mark.asyncio
async def test_atomic_all_uses_displayed_files_but_full_binary_patch_and_leaves_new_unshown_files(editor_server, monkeypatch):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    (root / "tracked.txt").write_bytes(b"  real change  \n")
    (root / "wild1.txt").write_bytes(b"  before  \n")
    (root / "shown.txt").write_bytes(b"shown untracked\n")
    captured = await request(server, "diff.git_working_tree", hide_whitespace=True)
    assert [row["path"] for row in captured["files"]] == ["tracked.txt"]
    (root / "late.txt").write_bytes(b"new file after displayed snapshot\n")
    original = server.host.environments.primary().environment.process.start
    writes = []
    async def recorded(argv, cwd, **kwargs):
        if "apply" in argv:
            writes.append(argv)
        assert not any(command in argv for command in ("add", "reset", "restore"))
        return await original(argv, cwd, **kwargs)
    monkeypatch.setattr(server.host.environments.primary().environment.process, "start", recorded)
    result = await review_action(server, "diff.git_stage_all", snapshot=captured)
    assert result["level"] == "success" and result["data"]["status"] == "success", result
    assert len(writes) == 1 and "--cached" in writes[0] and "--binary" in writes[0] and "--3way" not in writes[0]
    staged = await request(server, "diff.git_staged")
    assert {row["path"] for row in staged["files"]} == {"tracked.txt", "shown.txt"}
    staged_text = await contents(server, staged, item("tracked.txt"))
    assert staged_text["results"][0]["lines"] == ["  real change  \n"]
    unstaged = await review_action(server, "diff.git_unstage_all", snapshot=staged)
    assert unstaged["level"] == "success", unstaged
    to_revert = await request(server, "diff.git_working_tree", hide_whitespace=True)
    reverted = await review_action(server, "diff.git_revert_all", snapshot=to_revert, confirmed=True)
    assert reverted["level"] == "success" and reverted["data"]["source"] == "unstaged", reverted
    assert (root / "tracked.txt").read_bytes() == b"before\n"
    assert (root / "wild1.txt").read_bytes() == b"  before  \n"
    assert not (root / "shown.txt").exists() and not (root / "late.txt").exists()
    assert not list(root.glob(".minicode-undo-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("overlap", [False, True], ids=["both-targets-succeed", "cached-succeeds-worktree-refuses"])
async def test_staged_revert_uses_one_patch_for_two_real_targets_and_reports_partial_success(editor_server, monkeypatch, overlap):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    (root / "tracked.txt").write_bytes(b"staged version\n")
    staged_action = await review_action(server, "diff.git_stage_file", path="tracked.txt")
    assert staged_action["level"] == "success", staged_action
    if overlap:
        (root / "tracked.txt").write_bytes(b"overlapping newer worktree\n")
    staged = await request(server, "diff.git_staged")
    original = server.host.environments.primary().environment.process.start
    applies = []
    async def recorded(argv, cwd, **kwargs):
        if "apply" in argv:
            applies.append(argv)
        return await original(argv, cwd, **kwargs)
    monkeypatch.setattr(server.host.environments.primary().environment.process, "start", recorded)
    result = await review_action(server, "diff.git_revert_file", source="staged", snapshot=staged, path="tracked.txt", confirmed=True)
    assert result["data"]["status"] == ("partial-success" if overlap else "success"), result
    assert result["level"] == ("error" if overlap else "success")
    assert result["data"]["applied_targets"] == (["staged"] if overlap else ["staged", "unstaged"])
    assert await git(server, "show", ":tracked.txt") == "before"
    assert (root / "tracked.txt").read_bytes() == (b"overlapping newer worktree\n" if overlap else b"before\n")
    assert len(applies) == 2 and all("--reverse" in argv and "--binary" in argv and "--3way" not in argv for argv in applies)
    assert "--cached" in applies[0] and "--cached" not in applies[1] and applies[0][-1] == applies[1][-1]
    assert not list(root.glob(".minicode-undo-*"))
    retired = await contents(server, staged, item("tracked.txt"))
    assert retired["status"] == "stale-snapshot"


@pytest.mark.asyncio
async def test_atomic_binary_stage_and_history_preview_preserve_actual_head_index_and_disk_bytes(editor_server):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    before, after = b"\x89PNG\r\n\x00original", b"\x89PNG\r\n\x00changed"
    (root / "image.png").write_bytes(before)
    await git(server, "add", "image.png")
    await git(server, "-c", "user.name=MiniCode", "-c", "user.email=test@example.invalid", "commit", "--no-gpg-sign", "-qm", "binary base")
    (root / "image.png").write_bytes(after)
    source = await request(server, "diff.git_working_tree")
    assert source["files"][0]["is_binary"] is True
    result = await review_action(server, "diff.git_stage_file", snapshot=source, path="image.png")
    assert result["level"] == "success", result
    staged = await request(server, "diff.git_staged")
    sides = await contents(server, staged, item("image.png", "old", "binary"), item("image.png", "new", "binary"))
    assert [base64.b64decode(entry["data_base64"]) for entry in sides["results"]] == [before, after]
    reverted = await review_action(server, "diff.git_revert_file", source="staged", snapshot=staged, path="image.png", confirmed=True)
    assert reverted["level"] == "success", reverted
    assert (root / "image.png").read_bytes() == before
