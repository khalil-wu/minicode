from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.loop_session import populate_prompt_context
from backend.agent.state import AgentState
from backend.commands.slash_commands import build_slash_command_handler
from backend.conversations.public_projection import project_public_transcript_message
from backend.permissions.context import PermissionContext
from backend.services.code_review_service import code_review_options, prepare_code_review
from backend.ws.utils import normalize_user_input_metadata


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Review test")
    git(tmp_path, "config", "user.email", "review@example.invalid")
    (tmp_path / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    return tmp_path


def test_uncommitted_target_covers_staged_unstaged_untracked_and_preserves_repository(repo: Path) -> None:
    (repo / "staged.py").write_text("staged = True\n", encoding="utf-8")
    git(repo, "add", "staged.py")
    (repo / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "未提交.py").write_text("new = True\n", encoding="utf-8")
    (repo / " leading file.py").write_text("leading = True\n", encoding="utf-8")
    before = git(repo, "status", "--porcelain")
    prepared = prepare_code_review(repo, "uncommitted", request_message="请审阅未提交的更改")
    assert all(path in prepared["diff"] for path in ("staged.py", "tracked.py", "未提交.py", " leading file.py"))
    assert prepared["scope"]["gitRoot"] == str(repo)
    assert prepared["prompt"].startswith("## Code review guidelines:\n# Review Guidelines\n")
    assert "Do not return JSON, XML, a findings object" in prepared["prompt"]
    assert "::code-comment" in prepared["prompt"] and "Repository Rule Attribution" in prepared["prompt"]
    assert "staged, unstaged, and untracked files" in prepared["prompt"]
    assert prepared["prompt"].endswith("## My request:\n请审阅未提交的更改")
    assert git(repo, "status", "--porcelain") == before


def test_base_branch_resolves_real_merge_base_and_cold_read_keeps_it_pinned(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "feature")
    (repo / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    git(repo, "commit", "-am", "change")
    prepared = prepare_code_review(repo, "base-branch", "main")
    assert prepared["scope"]["mergeBaseSha"] == base
    assert f"git diff {base}" in prepared["prompt"]
    assert "+value = 2" in prepared["diff"]
    assert code_review_options(repo)["branches"][0] == "main"
    git(repo, "branch", "-D", "main")
    assert prepare_code_review(repo, "base-branch", "main", base)["diff"] == prepared["diff"]
    with pytest.raises(subprocess.CalledProcessError):
        prepare_code_review(repo, "base-branch", "missing")


def test_builtin_slash_prepares_same_full_prompt_and_real_scope(repo: Path) -> None:
    emitted = []
    async def emit(command, message, **kwargs):
        emitted.append((command, kwargs["data"]))
    ws = SimpleNamespace(active_conversation_id="owner", conversation_repo=SimpleNamespace(get_conversation_summary=lambda _: object()),
                         session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda _: repo), emit_command_result=emit)
    handler = build_slash_command_handler({"command": "review", "source": "builtin", "type": "template"})
    handled, prompt = asyncio.run(handler(ws, "", []))
    assert not handled and prompt == prepare_code_review(repo, "uncommitted")["prompt"]
    scope = emitted[0][1]["code_review"]
    normalized = normalize_user_input_metadata({"display_content": "Please review my uncommitted changes", "code_review": scope})
    projected = project_public_transcript_message({"id": "u-review", "role": "user", "content": prompt, **normalized})
    assert projected["code_review"] == scope
    assert projected["display_content"] == "Please review my uncommitted changes"
    assert "Review Guidelines" in projected["content"]


@pytest.mark.parametrize("stored_mode", ["build", "plan", "review", "explore"])
def test_review_turn_uses_default_collaboration_without_changing_parent_permissions(repo: Path, stored_mode: str) -> None:
    permission = PermissionContext(mode="bypass", workspace_root=repo)
    metadata = {"agent_mode": stored_mode, "code_review": prepare_code_review(repo, "uncommitted")["scope"]}
    state = AgentState(user_message="Review")
    populate_prompt_context(state=state, metadata=metadata, workspace_root=repo, permission_context=permission)
    assert state.prompt_context["collaboration_mode"] == "default"
    assert state.prompt_context["agent_mode"] == "build"
    assert permission.mode == "bypass" and metadata["agent_mode"] == stored_mode
