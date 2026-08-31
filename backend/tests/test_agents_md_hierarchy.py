"""Tests for MiniCode's hierarchical project instruction discovery."""
import json
import logging
import tempfile
from pathlib import Path

import pytest

from backend.agent.instruction_discovery import (
    INSTRUCTIONS_MAX_BYTES,
    _instruction_scope_chain,
    _find_project_root,
    clear_guideline_cache,
    guideline_change_metadata,
    load_project_guideline_bundle,
)
from backend.config_layers import (
    ConfigLayer,
    ConfigLayerError,
    ConfigLayerSource,
    _project_root_markers,
    load_config_layers_state,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_guideline_cache()
    yield
    clear_guideline_cache()


def _agent_blocks(bundle):
    return [b for b in bundle.blocks if b.source_kind == "project_instruction"]


def test_guideline_change_metadata_covers_instructions_rules_and_imports(tmp_path: Path):
    agents = tmp_path / ".minicode" / "INSTRUCTIONS.md"
    agents.parent.mkdir()
    agents.write_text("Project instructions", encoding="utf-8")
    rule = tmp_path / ".minicode" / "rules" / "python.md"
    rule.parent.mkdir(parents=True)
    rule.write_text("Python rules", encoding="utf-8")
    imported = tmp_path / "shared-instructions.txt"
    imported.write_text("Shared instructions", encoding="utf-8")
    agents.write_text("@../shared-instructions.txt", encoding="utf-8")

    load_project_guideline_bundle(workspace_dir=tmp_path)

    assert guideline_change_metadata(agents) == {
        "path": str(agents.resolve()),
        "source_kind": "direct",
    }
    assert guideline_change_metadata(rule) == {
        "path": str(rule.resolve()),
        "source_kind": "direct",
    }
    assert guideline_change_metadata(imported) == {
        "path": str(imported.resolve()),
        "source_kind": "import",
        "parent_path": str(agents.resolve()),
    }
    assert guideline_change_metadata(tmp_path / "README.md") is None


def test_find_project_root_stops_at_git_marker():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / ".git").mkdir()
        sub = root / "pkg" / "mod"
        sub.mkdir(parents=True)
        assert _find_project_root(sub) == root


def test_instruction_chain_is_root_first_within_git_project():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / ".git").mkdir()
        sub = root / "pkg" / "service"
        sub.mkdir(parents=True)
        chain = _instruction_scope_chain(sub)
        assert chain[0] == root
        assert chain[-1] == sub


def test_hierarchical_instructions_merge_root_first():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / ".git").mkdir()
        (root / ".minicode").mkdir()
        (root / ".minicode" / "INSTRUCTIONS.md").write_text("ROOT level guidance", encoding="utf-8")
        sub = root / "pkg" / "service"
        sub.mkdir(parents=True)
        (sub / ".minicode").mkdir()
        (sub / ".minicode" / "INSTRUCTIONS.md").write_text("SERVICE level guidance", encoding="utf-8")

        bundle = load_project_guideline_bundle(workspace_dir=sub)
        blocks = _agent_blocks(bundle)
        assert len(blocks) == 2
        md = bundle.rendered_markdown
        assert md.index("ROOT level") < md.index("SERVICE level")


def test_local_instruction_override_takes_precedence_over_default():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / ".git").mkdir()
        (root / ".minicode").mkdir()
        (root / ".minicode" / "INSTRUCTIONS.md").write_text("default content", encoding="utf-8")
        (root / ".minicode" / "INSTRUCTIONS.local.md").write_text("OVERRIDE content", encoding="utf-8")

        bundle = load_project_guideline_bundle(workspace_dir=root)
        blocks = _agent_blocks(bundle)
        assert len(blocks) == 1
        assert "OVERRIDE" in blocks[0].content
        assert "default content" not in blocks[0].content


def test_project_instruction_total_byte_cap():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / ".git").mkdir()
        (root / ".minicode").mkdir()
        (root / ".minicode" / "INSTRUCTIONS.md").write_text("X" * (INSTRUCTIONS_MAX_BYTES + 5000), encoding="utf-8")

        bundle = load_project_guideline_bundle(workspace_dir=root)
        block = _agent_blocks(bundle)[0]
        assert len(block.content.encode("utf-8")) == INSTRUCTIONS_MAX_BYTES
        assert block.content == "X" * INSTRUCTIONS_MAX_BYTES


def test_configured_project_root_marker_controls_instruction_hierarchy():
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp).resolve()
        root = parent / "repo"
        sub = root / "pkg"
        sub.mkdir(parents=True)
        (root / ".workspace-root").write_text("", encoding="utf-8")
        (root / ".minicode").mkdir()
        (sub / ".minicode").mkdir()
        (root / ".minicode" / "INSTRUCTIONS.md").write_text("ROOT guidance", encoding="utf-8")
        (sub / ".minicode" / "INSTRUCTIONS.md").write_text("SUB guidance", encoding="utf-8")

        bundle = load_project_guideline_bundle(
            workspace_dir=sub,
            project_root_markers=[".workspace-root"],
        )

        blocks = _agent_blocks(bundle)
        assert [block.path for block in blocks] == [
            root / ".minicode" / "INSTRUCTIONS.md",
            sub / ".minicode" / "INSTRUCTIONS.md",
        ]


def test_empty_project_root_markers_disable_parent_traversal():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        sub = root / "pkg"
        sub.mkdir(parents=True)
        (root / ".minicode").mkdir()
        (sub / ".minicode").mkdir()
        (root / ".minicode" / "INSTRUCTIONS.md").write_text("ROOT guidance", encoding="utf-8")
        (sub / ".minicode" / "INSTRUCTIONS.md").write_text("SUB guidance", encoding="utf-8")

        bundle = load_project_guideline_bundle(
            workspace_dir=sub,
            project_root_markers=[],
        )

        blocks = _agent_blocks(bundle)
        assert [block.path for block in blocks] == [sub / ".minicode" / "INSTRUCTIONS.md"]


def test_agents_md_is_loaded_root_first_without_minicode_wrapper(tmp_path):
    root = tmp_path / "repo"
    nested = root / "pkg"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("root agent rules", encoding="utf-8")
    (nested / "AGENTS.md").write_text("nested agent rules", encoding="utf-8")

    bundle = load_project_guideline_bundle(workspace_dir=nested)

    blocks = _agent_blocks(bundle)
    assert [block.path for block in blocks] == [
        root / "AGENTS.md",
        nested / "AGENTS.md",
    ]
    assert bundle.rendered_markdown.index("root agent rules") < (
        bundle.rendered_markdown.index("nested agent rules")
    )


def test_minicode_instruction_overrides_agents_file_in_same_scope(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".minicode").mkdir()
    (root / "AGENTS.override.md").write_text("agents override", encoding="utf-8")
    (root / ".minicode" / "INSTRUCTIONS.md").write_text(
        "minicode native",
        encoding="utf-8",
    )

    bundle = load_project_guideline_bundle(workspace_dir=root)

    blocks = _agent_blocks(bundle)
    assert [block.path for block in blocks] == [
        root / ".minicode" / "INSTRUCTIONS.md"
    ]
    assert "agents override" not in bundle.rendered_markdown


def test_shared_agents_file_is_read_without_deprecation_warning(tmp_path, caplog):
    # AGENTS.md is the cross-tool convention published at agents.md, so reading
    # a repository's existing one is interop and must stay quiet.
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "AGENTS.md").write_text("shared convention rules", encoding="utf-8")

    with caplog.at_level(
        logging.WARNING, logger="backend.agent.instruction_discovery"
    ):
        bundle = load_project_guideline_bundle(workspace_dir=root)

    assert [block.path for block in _agent_blocks(bundle)] == [root / "AGENTS.md"]
    assert "deprecated" not in caplog.text


def test_agents_override_is_not_a_minicode_instruction_source(tmp_path, caplog):
    # AGENTS.override.md is not part of MiniCode's instruction protocol.
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "AGENTS.override.md").write_text("legacy override", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="backend.agent.instruction_discovery"):
        bundle = load_project_guideline_bundle(workspace_dir=root)

    assert _agent_blocks(bundle) == []
    assert "legacy override" not in bundle.rendered_markdown
    assert "AGENTS.override.md" not in caplog.text


def test_project_doc_fallback_filename_is_used_after_instruction_candidates():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / ".git").mkdir()
        (root / ".minicode").mkdir()
        (root / ".minicode" / "WORKFLOW.md").write_text("fallback guidance", encoding="utf-8")

        fallback = load_project_guideline_bundle(
            workspace_dir=root,
            project_doc_fallback_filenames=["WORKFLOW.md"],
        )
        assert [block.path.name for block in _agent_blocks(fallback)] == ["WORKFLOW.md"]

        clear_guideline_cache()
        (root / ".minicode" / "INSTRUCTIONS.md").write_text("primary guidance", encoding="utf-8")
        primary = load_project_guideline_bundle(
            workspace_dir=root,
            project_doc_fallback_filenames=["WORKFLOW.md"],
        )
        assert [block.path.name for block in _agent_blocks(primary)] == ["INSTRUCTIONS.md"]


def test_project_doc_max_bytes_zero_disables_project_instructions():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / ".git").mkdir()
        (root / ".minicode").mkdir()
        (root / ".minicode" / "INSTRUCTIONS.md").write_text("must not load", encoding="utf-8")

        bundle = load_project_guideline_bundle(
            workspace_dir=root,
            project_doc_max_bytes=0,
        )

        assert _agent_blocks(bundle) == []


def test_project_doc_truncation_uses_raw_bytes_and_lossy_utf8():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / ".git").mkdir()
        (root / ".minicode").mkdir()
        (root / ".minicode" / "INSTRUCTIONS.md").write_bytes(b" A\xffB ")

        bundle = load_project_guideline_bundle(
            workspace_dir=root,
            project_doc_max_bytes=4,
        )

        block = _agent_blocks(bundle)[0]
        assert block.content == " A\ufffdB"


def test_instruction_discovery_resolves_symlink_paths(tmp_path):
    physical = tmp_path / "physical-repo"
    nested = physical / "pkg"
    nested.mkdir(parents=True)
    (physical / ".git").mkdir()
    (physical / ".minicode").mkdir()
    (nested / ".minicode").mkdir()
    (physical / ".minicode" / "INSTRUCTIONS.md").write_text("root", encoding="utf-8")
    (nested / ".minicode" / "INSTRUCTIONS.md").write_text("nested", encoding="utf-8")
    logical = tmp_path / "logical-repo"
    try:
        logical.symlink_to(physical, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    bundle = load_project_guideline_bundle(workspace_dir=logical / "pkg")

    assert [block.path for block in _agent_blocks(bundle)] == [
        physical / ".minicode" / "INSTRUCTIONS.md",
        physical / "pkg" / ".minicode" / "INSTRUCTIONS.md",
    ]


def test_project_instruction_root_markers_ignore_project_layer(tmp_path):
    state_root = tmp_path / "state"
    settings_json = tmp_path / "settings.json"
    settings_json.write_text(
        json.dumps(
            {
                "project_root_markers": [".workspace-root"],
                "project_doc_max_bytes": 123,
                "project_doc_fallback_filenames": ["WORKFLOW.md"],
            }
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "repo"
    (workspace / ".workspace-root").mkdir(parents=True)
    local_minicode = workspace / ".minicode"
    local_minicode.mkdir()
    (local_minicode / "config.toml").write_text(
        'project_root_markers = [".malicious-root"]\n'
        'project_doc_max_bytes = 1\n'
        'project_doc_fallback_filenames = ["LOCAL.md"]\n',
        encoding="utf-8",
    )

    stack = load_config_layers_state(
        state_root=state_root,
        user_config_file=settings_json,
        cwd=workspace,
        trust_resolver=lambda _path: True,
    )

    assert stack.effective_config()["project_doc_max_bytes"] == 1
    discovery = stack.project_instruction_config()
    assert discovery["project_root_markers"] == [".workspace-root"]
    assert discovery["project_doc_max_bytes"] == 1
    assert discovery["project_doc_fallback_filenames"] == ["LOCAL.md"]


def test_minicode_default_project_root_marker_is_git_only():
    assert _project_root_markers({}) == (".git",)
    assert _project_root_markers({"project_root_markers": [".hg"]}) == (".hg",)


@pytest.mark.parametrize(
    "payload",
    [
        {"project_root_markers": ".git"},
        {"project_doc_fallback_filenames": ["WORKFLOW.md", 1]},
        {"project_doc_max_bytes": -1},
        {"project_doc_max_bytes": True},
    ],
)
def test_invalid_project_document_config_is_rejected(payload):
    with pytest.raises(ConfigLayerError):
        ConfigLayer(ConfigLayerSource("user"), payload)


def test_no_git_project_uses_cwd_only():
    with tempfile.TemporaryDirectory() as tmp:
        # Plant a .git at base so this temp tree is its own isolated project
        # boundary, independent of any ancestor git repo on the host.
        base = Path(tmp).resolve()
        (base / ".git").mkdir()
        sub = base / "a" / "b"
        sub.mkdir(parents=True)
        # base and sub each have MiniCode instructions inside the project so
        # both should load, root-first.
        (base / ".minicode").mkdir()
        (sub / ".minicode").mkdir()
        (base / ".minicode" / "INSTRUCTIONS.md").write_text("BASE guidance", encoding="utf-8")
        (sub / ".minicode" / "INSTRUCTIONS.md").write_text("SUB guidance", encoding="utf-8")

        bundle = load_project_guideline_bundle(workspace_dir=sub)
        md = bundle.rendered_markdown
        assert "BASE guidance" in md
        assert "SUB guidance" in md
        assert md.index("BASE guidance") < md.index("SUB guidance")
