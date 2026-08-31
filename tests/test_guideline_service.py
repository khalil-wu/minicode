from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil
from uuid import uuid4

import backend.agent.instruction_discovery as instruction_discovery
from backend.agent.instruction_discovery import (
    load_matching_project_rules,
    load_project_guideline_bundle,
    load_project_guidelines,
)


@contextmanager
def _workspace_tmp_dir():
    root = Path.cwd() / f".guideline-test-{uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    # Tests exercise one isolated workspace. A local project marker prevents
    # the real repository's instructions from being inherited.
    (root / ".git").mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _project_blocks(bundle):
    """Ignore optional user-level instructions supplied by the test host."""
    return [block for block in bundle.blocks if block.source_kind != "user_instruction"]


def test_guideline_bundle_preserves_priority_order_and_provenance() -> None:
    with _workspace_tmp_dir() as temp_dir:
        workspace_dir = Path(temp_dir)
        minicode_dir = workspace_dir / ".minicode"
        minicode_dir.mkdir()
        (minicode_dir / "INSTRUCTIONS.md").write_text(
            "# root\nroot guidance", encoding="utf-8"
        )
        rules_dir = minicode_dir / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "10-style.md").write_text("style rule", encoding="utf-8")
        (rules_dir / "20-tests.md").write_text("test rule", encoding="utf-8")

        bundle = load_project_guideline_bundle(workspace_dir=workspace_dir)
        project_blocks = _project_blocks(bundle)

        assert [block.source_kind for block in project_blocks] == [
            "project_instruction",
            "project_rule",
            "project_rule",
        ]
        assert [block.path.name for block in project_blocks] == [
            "INSTRUCTIONS.md",
            "10-style.md",
            "20-tests.md",
        ]
        assert project_blocks[-1].priority > project_blocks[0].priority
        assert "Project Guidelines & Memory" in bundle.rendered_markdown
        assert "root guidance" in load_project_guidelines(workspace_dir)


def test_guideline_bundle_loads_project_instruction_and_rules() -> None:
    with _workspace_tmp_dir() as temp_dir:
        workspace_dir = Path(temp_dir)
        minicode_dir = workspace_dir / ".minicode"
        (minicode_dir / "rules").mkdir(parents=True)
        (minicode_dir / "INSTRUCTIONS.md").write_text("project instructions", encoding="utf-8")
        (minicode_dir / "rules" / "global.md").write_text("project rule", encoding="utf-8")

        bundle = load_project_guideline_bundle(workspace_dir=workspace_dir)
        project_blocks = _project_blocks(bundle)

        assert [block.path.name for block in project_blocks[:2]] == [
            "INSTRUCTIONS.md",
            "global.md",
        ]
        assert project_blocks[0].source_kind == "project_instruction"
        assert "project instructions" in bundle.rendered_markdown


def test_guideline_bundle_loads_nested_instructions_root_to_cwd() -> None:
    with _workspace_tmp_dir() as temp_dir:
        project_root = Path(temp_dir)
        (project_root / ".git").mkdir(exist_ok=True)
        nested = project_root / "packages" / "feature"
        nested.mkdir(parents=True)
        (project_root / ".minicode").mkdir()
        (nested / ".minicode").mkdir()
        (project_root / ".minicode" / "INSTRUCTIONS.md").write_text("root instructions", encoding="utf-8")
        (nested / ".minicode" / "INSTRUCTIONS.local.md").write_text("nested instructions", encoding="utf-8")

        project_blocks = _project_blocks(
            load_project_guideline_bundle(workspace_dir=nested)
        )

        assert [block.path for block in project_blocks] == [
            project_root / ".minicode" / "INSTRUCTIONS.md",
            nested / ".minicode" / "INSTRUCTIONS.local.md",
        ]


def test_instruction_walk_stops_at_project_root() -> None:
    with _workspace_tmp_dir() as temp_dir:
        parent = Path(temp_dir)
        project_root = parent / "repo"
        nested = project_root / "packages" / "feature"
        nested.mkdir(parents=True)
        (project_root / ".git").mkdir()
        (parent / ".minicode").mkdir()
        (project_root / ".minicode").mkdir()
        (parent / ".minicode" / "INSTRUCTIONS.md").write_text("outside instructions", encoding="utf-8")
        (project_root / ".minicode" / "INSTRUCTIONS.md").write_text("repo instructions", encoding="utf-8")

        project_blocks = _project_blocks(
            load_project_guideline_bundle(workspace_dir=nested)
        )
        paths = [block.path for block in project_blocks]

        assert parent / ".minicode" / "INSTRUCTIONS.md" not in paths
        assert project_root / ".minicode" / "INSTRUCTIONS.md" in paths


def test_minicode_managed_user_and_recursive_rules_follow_order(monkeypatch) -> None:
    with _workspace_tmp_dir() as temp_dir:
        root = Path(temp_dir)
        managed = root / "managed"
        user = root / "user" / ".minicode"
        workspace = root / "workspace"
        (managed / "rules" / "nested").mkdir(parents=True)
        (user / "rules" / "nested").mkdir(parents=True)
        workspace.mkdir()
        (managed / "INSTRUCTIONS.md").write_text("managed instructions", encoding="utf-8")
        (managed / "rules" / "nested" / "policy.md").write_text(
            "managed rule", encoding="utf-8"
        )
        (user / "INSTRUCTIONS.md").write_text("user instructions", encoding="utf-8")
        (user / "rules" / "nested" / "style.md").write_text(
            "user rule", encoding="utf-8"
        )
        (workspace / ".minicode").mkdir()
        (workspace / ".minicode" / "INSTRUCTIONS.md").write_text("project instructions", encoding="utf-8")
        monkeypatch.setattr(instruction_discovery, "_get_managed_minicode_dir", lambda: managed)
        monkeypatch.setattr(instruction_discovery, "get_minicode_config_home_dir", lambda: user)
        instruction_discovery.clear_guideline_cache()

        blocks = load_project_guideline_bundle(workspace_dir=workspace).blocks
        owned_blocks = [block for block in blocks if root in block.path.parents]
        kinds = [block.source_kind for block in owned_blocks]

        assert kinds[:4] == [
            "managed_instruction",
            "managed_rule",
            "user_instruction",
            "user_rule",
        ]
        assert kinds[-1] == "project_instruction"


def test_guideline_bundle_prefers_local_instruction_override() -> None:
    with _workspace_tmp_dir() as temp_dir:
        workspace_dir = Path(temp_dir)
        minicode_dir = workspace_dir / ".minicode"
        minicode_dir.mkdir()
        (minicode_dir / "INSTRUCTIONS.md").write_text("base instructions", encoding="utf-8")
        (minicode_dir / "INSTRUCTIONS.local.md").write_text(
            "override instructions", encoding="utf-8"
        )

        bundle = load_project_guideline_bundle(workspace_dir=workspace_dir)
        project_blocks = _project_blocks(bundle)

        assert [block.path.name for block in project_blocks] == ["INSTRUCTIONS.local.md"]
        assert "override instructions" in bundle.rendered_markdown
        assert "base instructions" not in bundle.rendered_markdown


def test_guideline_bundle_supports_additional_directories() -> None:
    with _workspace_tmp_dir() as temp_dir:
        workspace_dir = Path(temp_dir)
        extra_dir = workspace_dir / "packages" / "feature-a"
        extra_dir.mkdir(parents=True)
        (extra_dir / ".minicode").mkdir()
        (extra_dir / ".minicode" / "INSTRUCTIONS.md").write_text("feature guidance", encoding="utf-8")

        bundle = load_project_guideline_bundle(
            workspace_dir=workspace_dir,
            additional_directories=[extra_dir],
        )
        project_blocks = _project_blocks(bundle)

        assert len(project_blocks) == 1
        assert project_blocks[0].path == extra_dir / ".minicode" / "INSTRUCTIONS.md"
        assert project_blocks[0].scope == str(extra_dir)


def test_guideline_bundle_cache_invalidates_when_files_change() -> None:
    with _workspace_tmp_dir() as temp_dir:
        workspace_dir = Path(temp_dir)
        (workspace_dir / ".minicode").mkdir()
        guideline_path = workspace_dir / ".minicode" / "INSTRUCTIONS.md"
        guideline_path.write_text("first guidance", encoding="utf-8")

        first = load_project_guideline_bundle(workspace_dir=workspace_dir)
        guideline_path.write_text("updated guidance", encoding="utf-8")
        second = load_project_guideline_bundle(workspace_dir=workspace_dir)

        assert "first guidance" in first.rendered_markdown
        assert "updated guidance" in second.rendered_markdown
        assert first.rendered_markdown != second.rendered_markdown


def test_guideline_include_skips_non_text_files() -> None:
    with _workspace_tmp_dir() as workspace_dir:
        (workspace_dir / ".minicode").mkdir()
        (workspace_dir / ".minicode" / "INSTRUCTIONS.md").write_text(
            "root guidance\n@../payload.pdf",
            encoding="utf-8",
        )
        (workspace_dir / "payload.pdf").write_bytes(b"%PDF secret binary body")

        bundle = load_project_guideline_bundle(workspace_dir=workspace_dir)

        assert "root guidance" in bundle.rendered_markdown
        assert "secret binary body" not in bundle.rendered_markdown
        assert all(block.path.name != "payload.pdf" for block in bundle.blocks)


def test_guideline_hooks_receive_include_parent_and_compact_reason(
    monkeypatch,
) -> None:
    with _workspace_tmp_dir() as workspace_dir:
        (workspace_dir / ".minicode").mkdir()
        root = workspace_dir / ".minicode" / "INSTRUCTIONS.md"
        child = workspace_dir / ".minicode" / "child.md"
        root.write_text("root\n@child.md", encoding="utf-8")
        child.write_text("child", encoding="utf-8")
        calls: list[dict[str, str]] = []

        def capture(path, source_kind, **kwargs) -> None:
            calls.append({"path": str(path), "source_kind": source_kind, **kwargs})

        monkeypatch.setattr(instruction_discovery, "_schedule_instructions_loaded_hook", capture)
        instruction_discovery.clear_guideline_cache()
        load_project_guideline_bundle(
            workspace_dir=workspace_dir,
            load_reason="compact",
        )

        root_call = next(call for call in calls if call["path"] == str(root))
        child_call = next(call for call in calls if call["path"] == str(child))
        assert root_call["load_reason"] == "compact"
        assert child_call["load_reason"] == ""
        assert child_call["parent_file_path"] == str(root)


def test_conditional_rules_are_excluded_until_a_touched_path_matches() -> None:
    with _workspace_tmp_dir() as workspace_dir:
        rules_dir = workspace_dir / ".minicode" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "python.md").write_text(
            "---\npaths:\n  - src/**/*.py\n---\npython-only guidance",
            encoding="utf-8",
        )
        (rules_dir / "global.md").write_text("global guidance", encoding="utf-8")

        initial = load_project_guidelines(workspace_dir)
        matched = load_matching_project_rules(
            workspace_dir,
            [workspace_dir / "src" / "pkg" / "module.py"],
        )
        unmatched = load_matching_project_rules(
            workspace_dir,
            [workspace_dir / "frontend" / "main.ts"],
        )

        assert "global guidance" in initial
        assert "python-only guidance" not in initial
        assert "python-only guidance" in matched
        assert unmatched == ""
