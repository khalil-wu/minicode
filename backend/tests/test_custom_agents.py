"""Custom agent definitions: frontmatter loader + TaskTool integration."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import backend.agents.loader as agents_loader
import backend.tools.agent_tools as agent_tools
import backend.tools.subagent_support as subagent_support
from backend.services.tool_registry_factory import build_tool_registry as _build_tool_registry
from backend.artifact.store import ArtifactStore
from backend.tools.agent_tools import TaskTool


def _write_agent(dirpath: Path, filename: str, body: str) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / filename).write_text(body, encoding="utf-8")


def test_discover_parses_frontmatter_and_body(monkeypatch, tmp_path):
    project_agents = tmp_path / ".mini-code" / "agents"
    _write_agent(
        project_agents,
        "reviewer.md",
        "---\n"
        "name: reviewer\n"
        "description: Adversarial code reviewer\n"
        "model: inherit\n"
        "tools: [read_file, grep_files]\n"
        "---\n"
        "You are a senior code reviewer. Find bugs and risky changes. "
        "Return CONCISE findings with file:line.\n",
    )
    monkeypatch.setattr(agents_loader, "_agent_search_dirs", lambda root=None: [project_agents])

    agents = agents_loader.discover_agents()
    assert "reviewer" in agents
    reviewer = agents["reviewer"]
    assert reviewer.description == "Adversarial code reviewer"
    assert "senior code reviewer" in reviewer.prompt
    assert reviewer.tools == ["read_file", "grep_files"]


def test_get_custom_agent_returns_none_for_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(agents_loader, "_agent_search_dirs", lambda root=None: [tmp_path / "agents"])
    assert agents_loader.get_custom_agent("does-not-exist") is None


def test_project_agent_overrides_user_global(monkeypatch, tmp_path):
    project = tmp_path / "project" / "agents"
    user = tmp_path / "user" / "agents"
    _write_agent(project, "lint.md", "---\nname: lint\ndescription: project version\n---\nproject body\n")
    _write_agent(user, "lint.md", "---\nname: lint\ndescription: user version\n---\nuser body\n")
    # Project dir listed first → wins.
    monkeypatch.setattr(agents_loader, "_agent_search_dirs", lambda root=None: [project, user])

    agents = agents_loader.discover_agents()
    assert agents["lint"].description == "project version"


def test_empty_file_is_skipped(monkeypatch, tmp_path):
    d = tmp_path / "agents"
    _write_agent(d, "blank.md", "")
    monkeypatch.setattr(agents_loader, "_agent_search_dirs", lambda root=None: [d])
    assert agents_loader.discover_agents() == {}


def test_discover_recurses_through_nested_agent_directories(
    monkeypatch,
    tmp_path,
):
    agents_dir = tmp_path / "agents"
    _write_agent(
        agents_dir / "review",
        "security.md",
        "---\nname: security-review\ndescription: nested agent\n---\nReview security.\n",
    )
    monkeypatch.setattr(
        agents_loader,
        "_agent_search_dirs",
        lambda root=None: [agents_dir],
    )

    discovered = agents_loader.discover_agents()

    assert discovered["security-review"].description == "nested agent"
    assert discovered["security-review"].source_path == (
        agents_dir / "review" / "security.md"
    )


def test_discover_deduplicates_hard_linked_agent_files(
    monkeypatch,
    tmp_path,
):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    _write_agent(
        first_dir,
        "first.md",
        "---\nname: first\ndescription: first source\n---\nRun once.\n",
    )
    second_dir.mkdir()
    try:
        (second_dir / "second.md").hardlink_to(first_dir / "first.md")
    except OSError:
        import pytest

        pytest.skip("hard links are unavailable in this environment")
    monkeypatch.setattr(
        agents_loader,
        "_agent_search_dirs",
        lambda root=None: [first_dir, second_dir],
    )

    definitions = agents_loader.discover_agent_definitions()

    assert [definition.name for definition in definitions] == ["first"]


def test_worktree_fallback_agent_keeps_project_source_identity(
    monkeypatch,
    tmp_path,
):
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    worktree_git_dir = main / ".git" / "worktrees" / "feature"
    worktree_git_dir.mkdir(parents=True)
    worktree.mkdir()
    (worktree / ".git").write_text(
        f"gitdir: {worktree_git_dir}\n",
        encoding="utf-8",
    )
    (worktree_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree_git_dir / "gitdir").write_text(
        str(worktree / ".git") + "\n",
        encoding="utf-8",
    )
    main_agent = main / ".minicode" / "agents" / "fallback.md"
    _write_agent(
        main_agent.parent,
        main_agent.name,
        "---\nname: fallback\ndescription: main repo fallback\n---\nRun.\n",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "user-claude"))
    monkeypatch.setattr(
        agents_loader,
        "_get_managed_minicode_dir",
        lambda: tmp_path / "managed",
    )
    monkeypatch.setattr(
        agents_loader,
        "get_explicit_active_workspace_root",
        lambda: worktree,
    )

    definitions = agents_loader.discover_agent_definitions(worktree)

    assert len(definitions) == 1
    assert definitions[0].source == "project"
    assert definitions[0].source_path == main_agent


def test_plugin_only_agent_policy_keeps_managed_filesystem_scope(
    monkeypatch,
    tmp_path,
):
    managed = tmp_path / "managed"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        agents_loader,
        "_get_managed_minicode_dir",
        lambda: managed,
    )
    monkeypatch.setattr(
        agents_loader,
        "_agents_restricted_to_plugins",
        lambda _root: True,
    )

    assert agents_loader._agent_search_dirs(project) == [
        managed / "agents"
    ]


def test_task_tool_schema_exposes_discovered_custom_agent(monkeypatch):
    monkeypatch.setattr(
        subagent_support,
        "discover_agents",
        lambda: {"reviewer": object(), "docs-writer": object()},
    )

    schema = TaskTool(artifact_store=object()).get_schema()
    agent_type_schema = schema.parameters["properties"]["agent_type"]
    parallel_agent_type_schema = (
        schema.parameters["properties"]["parallel_tasks"]["items"]["properties"]["agent_type"]
    )

    assert agent_type_schema["enum"][:3] == [
        "general-purpose",
        "explore",
        "plan",
    ]
    assert "reviewer" in agent_type_schema["enum"]
    assert "docs-writer" in parallel_agent_type_schema["enum"]
    assert "Available types:" in agent_type_schema["description"]


def test_default_registry_exposes_async_task_and_plan_exit_tools():
    registry = _build_tool_registry(ArtifactStore(), llm_provider=lambda: object())
    assert registry.get_tool("task") is not None
    assert registry.get_tool("task_stop") is not None
    assert registry.get_tool("task_status") is not None
    assert registry.get_tool("exit_plan_mode") is not None
