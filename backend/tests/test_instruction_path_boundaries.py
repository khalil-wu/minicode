from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.agent import instruction_discovery as instructions


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "api/nested").mkdir(parents=True)
    monkeypatch.setattr(instructions, "_get_managed_minicode_dir", lambda: tmp_path / "managed")
    monkeypatch.setattr(instructions, "get_minicode_config_home_dir", lambda: tmp_path / "user")
    instructions.clear_guideline_cache()
    yield root
    instructions.clear_guideline_cache()


def _rule(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\npaths: ["api/**"]\n---\n{body}\n', encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Windows filename identity")
def test_case_aliases_keep_instruction_scope_and_change_notifications(repository):
    _rule(repository / ".MINICODE/rules/api.md", "CASE_SCOPE_SENTINEL")
    content = instructions.load_matching_project_rules(
        repository / "api/nested", [repository / "api/service.py"],
    )
    assert "CASE_SCOPE_SENTINEL" in content
    agents = repository / "AGENTS.MD"
    agents.write_text("USER_GUIDANCE", encoding="utf-8")
    assert instructions.guideline_change_metadata(agents)["source_kind"] == "direct"


def test_imported_conditional_rule_retains_its_declared_project_scope(repository):
    _rule(repository / ".minicode/rules/api.md", "ROOT_SCOPE\n@../../docs/shared.md")
    _rule(repository / "docs/shared.md", "IMPORTED_SCOPE")
    content = instructions.load_matching_project_rules(
        repository / "api/nested", [repository / "api/service.py"],
    )
    assert "ROOT_SCOPE" in content and "IMPORTED_SCOPE" in content


def test_directory_glob_keeps_its_anchor(repository):
    _rule(repository / ".minicode/rules/api.md", "ANCHORED_RULE")
    assert "ANCHORED_RULE" in instructions.load_matching_project_rules(
        repository, [repository / "api/service.py"],
    )
    assert "ANCHORED_RULE" not in instructions.load_matching_project_rules(
        repository, [repository / "vendor/api/service.py"],
    )


def test_global_glob_with_exclusion_keeps_both_patterns(repository):
    rule = repository / ".minicode/rules/scoped.md"
    rule.parent.mkdir(parents=True)
    rule.write_text('---\npaths: ["**", "!vendor/**"]\n---\nEXCLUDED_SCOPE\n', encoding="utf-8")
    assert "EXCLUDED_SCOPE" in instructions.load_matching_project_rules(
        repository, [repository / "api/service.py"],
    )
    assert "EXCLUDED_SCOPE" not in instructions.load_matching_project_rules(
        repository, [repository / "vendor/service.py"],
    )


@pytest.mark.parametrize("metadata", ['paths: ["api/**"', "paths: 42", "false", "[]"])
def test_malformed_rule_scope_never_becomes_global_guidance(repository, metadata):
    rule = repository / ".minicode/rules/broken.md"
    rule.parent.mkdir(parents=True)
    rule.write_text(f"---\n{metadata}\n---\nOUT_OF_SCOPE_GUIDANCE\n@../../imported.md\n", encoding="utf-8")
    (repository / "imported.md").write_text("OUT_OF_SCOPE_IMPORT", encoding="utf-8")
    content = instructions.load_project_guidelines(repository)
    assert "invalid frontmatter" in content
    assert "OUT_OF_SCOPE_GUIDANCE" not in content
    assert "OUT_OF_SCOPE_IMPORT" not in content


def test_unclosed_scope_is_not_plain_global_markdown(repository):
    rule = repository / ".minicode/rules/broken.md"
    rule.parent.mkdir(parents=True)
    rule.write_text('---\npaths: ["api/**"]\nOUT_OF_SCOPE_GUIDANCE\n', encoding="utf-8")
    content = instructions.load_project_guidelines(repository)
    assert "unterminated YAML frontmatter" in content
    assert "OUT_OF_SCOPE_GUIDANCE" not in content


def test_empty_frontmatter_still_allows_an_unconditional_rule(repository):
    rule = repository / ".minicode/rules/global.md"
    rule.parent.mkdir(parents=True)
    rule.write_text("---\n---\nGLOBAL_GUIDANCE\n", encoding="utf-8")
    assert "GLOBAL_GUIDANCE" in instructions.load_project_guidelines(repository)


def test_outside_imports_are_rejected_before_filesystem_resolution(repository, monkeypatch):
    outside = repository.parent / "outside.md"
    outside.write_text("OUTSIDE_CONTENT", encoding="utf-8")
    (repository / "inside.md").write_text("INSIDE_CONTENT", encoding="utf-8")
    (repository / "AGENTS.md").write_text(
        "@inside.md\n@../outside.md\n@//instruction-path.invalid/share/guide.md\n",
        encoding="utf-8",
    )
    resolve = Path.resolve

    def resolve_with_probe_check(path, *args, **kwargs):
        assert path != outside
        assert "instruction-path.invalid" not in str(path)
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_with_probe_check)
    content = instructions.load_project_guidelines(repository)
    assert "INSIDE_CONTENT" in content
    assert "OUTSIDE_CONTENT" not in content


def test_root_markers_and_fallback_names_are_validated_before_probing(repository, monkeypatch):
    exists = Path.exists

    def exists_with_probe_check(path):
        assert "instruction-path.invalid" not in str(path)
        return exists(path)

    monkeypatch.setattr(Path, "exists", exists_with_probe_check)
    assert instructions.load_project_guidelines(
        repository,
        project_root_markers=["//instruction-path.invalid/share/marker"],
        project_doc_fallback_filenames=["//instruction-path.invalid/share/instructions.md"],
    ) == ""
