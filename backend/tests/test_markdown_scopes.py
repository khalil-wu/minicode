from __future__ import annotations

from pathlib import Path

from backend.agent.markdown_scopes import (
    find_canonical_git_root,
    get_markdown_directories,
    get_minicode_config_home_dir,
    get_project_dirs_up_to_home,
)


def _make_worktree_layout(tmp_path: Path) -> tuple[Path, Path]:
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
    return main, worktree


def test_nested_repo_walk_widens_to_session_project_root(tmp_path: Path) -> None:
    session_root = tmp_path / "project"
    nested_repo = session_root / "vendor" / "dependency"
    cwd = nested_repo / "src"
    cwd.mkdir(parents=True)
    (session_root / ".git").mkdir()
    (nested_repo / ".git").mkdir()
    expected = [
        cwd / ".minicode" / "agents",
        nested_repo / ".minicode" / "agents",
        session_root / ".minicode" / "agents",
    ]
    for directory in expected:
        directory.mkdir(parents=True)

    assert get_project_dirs_up_to_home(
        "agents",
        cwd,
        session_project_root=session_root,
    ) == expected


def test_nested_repo_outside_session_stops_at_nearest_git_root(
    tmp_path: Path,
) -> None:
    session_root = tmp_path / "session"
    other_repo = tmp_path / "other"
    cwd = other_repo / "src"
    cwd.mkdir(parents=True)
    (session_root / ".git").mkdir(parents=True)
    (other_repo / ".git").mkdir()
    nearest = other_repo / ".minicode" / "commands"
    parent = tmp_path / ".minicode" / "commands"
    nearest.mkdir(parents=True)
    parent.mkdir(parents=True)

    assert get_project_dirs_up_to_home(
        "commands",
        cwd,
        session_project_root=session_root,
    ) == [nearest]


def test_worktree_falls_back_to_main_repo_only_when_local_scope_is_absent(
    tmp_path: Path,
) -> None:
    main, worktree = _make_worktree_layout(tmp_path)
    main_agents = main / ".minicode" / "agents"
    main_agents.mkdir(parents=True)

    assert find_canonical_git_root(worktree) == main
    scopes = get_markdown_directories(
        "agents",
        worktree,
        managed_root=tmp_path / "managed",
        session_project_root=worktree,
    )
    assert [scope.path for scope in scopes if scope.source == "project"] == [
        main_agents
    ]

    local_agents = worktree / ".minicode" / "agents"
    local_agents.mkdir(parents=True)
    scopes = get_markdown_directories(
        "agents",
        worktree,
        managed_root=tmp_path / "managed",
        session_project_root=worktree,
    )
    assert [scope.path for scope in scopes if scope.source == "project"] == [
        local_agents
    ]


def test_minicode_config_dir_controls_user_markdown_scope(
    monkeypatch,
    tmp_path: Path,
) -> None:
    configured = tmp_path / "custom-claude-home"
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(configured))

    assert get_minicode_config_home_dir() == configured
    scopes = get_markdown_directories(
        "commands",
        None,
        managed_root=tmp_path / "managed",
    )
    assert next(scope.path for scope in scopes if scope.source == "user") == (
        configured / "commands"
    )
