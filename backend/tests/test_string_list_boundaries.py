"""Regression coverage for string-list parsing and projection boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from backend.agent.public_projection import public_string_list
from backend.agents import loader as agents_loader
from backend.config_requirements import (
    ConfigRequirementsError,
    RequirementSource,
    RequirementsLayerEntry,
    compose_requirements,
    normalize_string_array,
    parse_string_array,
)
from backend.conversations.models import ConversationRecord, ConversationSummary
from backend.conversations.public_projection import project_public_conversation
from backend.mcp import project_settings


def test_parse_string_array_returns_a_copy_and_accepts_none() -> None:
    raw = ["  docs  ", "docs"]

    parsed = parse_string_array(raw, field_name="tools", source="config.toml")

    assert parsed == raw
    assert parsed is not raw
    assert parse_string_array(None, field_name="tools", source="config.toml") == []


@pytest.mark.parametrize(
    "value",
    [
        "read_file",
        7,
        ("read_file",),
        ["read_file", 7],
    ],
)
def test_parse_string_array_rejects_scalars_and_mixed_types(value: Any) -> None:
    with pytest.raises(
        ConfigRequirementsError,
        match=r"tools in config\.toml must be a string array",
    ):
        parse_string_array(value, field_name="tools", source="config.toml")


def test_normalize_string_array_trims_lowercases_deduplicates_and_drops_blanks() -> None:
    assert normalize_string_array(
        [" Docs ", "docs", "", "  SEARCH", "search", "   "],
        field_name="servers",
        source="settings",
        lowercase=True,
    ) == ["docs", "search"]


def test_normalize_string_array_can_reject_empty_items() -> None:
    with pytest.raises(
        ConfigRequirementsError,
        match=r"servers in settings must contain non-empty strings",
    ):
        normalize_string_array(
            ["docs", "  "],
            field_name="servers",
            source="settings",
            reject_empty=True,
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("allowed_approval_policies", "on-request"),
        ("allowed_sandbox_modes", ["read-only", 7]),
    ],
)
def test_managed_requirement_allowlists_use_strict_string_array_parser(
    field_name: str,
    value: Any,
) -> None:
    source = RequirementSource("system_requirements_toml", location="requirements.toml")

    with pytest.raises(ConfigRequirementsError, match=field_name):
        compose_requirements(
            [RequirementsLayerEntry(source, {field_name: value})]
        )


def _write_project_local_settings(workspace: Path, payload: dict[str, Any]) -> Path:
    path = workspace / ".minicode" / "mcp.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_mcp_local_server_lists_are_normalized_at_file_boundary(tmp_path: Path) -> None:
    _write_project_local_settings(
        tmp_path,
        {
            "enabled_servers": ["  docs ", "docs", " search "],
            "disabled_servers": [" blocked ", "blocked"],
        },
    )

    settings = project_settings.read_project_local_settings(tmp_path)

    assert settings["enabled_servers"] == ["docs", "search"]
    assert settings["disabled_servers"] == ["blocked"]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("enabled_servers", "docs"),
        ("enabled_servers", ["docs", 7]),
        ("enabled_servers", ["docs", "  "]),
        ("disabled_servers", "blocked"),
        ("disabled_servers", ["blocked", 7]),
        ("disabled_servers", ["blocked", "  "]),
    ],
)
def test_mcp_local_server_lists_reject_scalar_mixed_and_empty_values(
    tmp_path: Path,
    field_name: str,
    value: Any,
) -> None:
    _write_project_local_settings(tmp_path, {field_name: value})

    with pytest.raises(ConfigRequirementsError, match=field_name):
        project_settings.read_project_local_settings(tmp_path)


def test_mcp_server_status_uses_normalized_enabled_and_disabled_lanes(
    tmp_path: Path,
) -> None:
    _write_project_local_settings(
        tmp_path,
        {
            "enabled_servers": [" docs ", "docs"],
            "disabled_servers": [" blocked ", "blocked"],
        },
    )

    assert (
        project_settings.project_mcp_server_status("docs", tmp_path)
        == project_settings.PROJECT_MCP_APPROVED
    )
    assert (
        project_settings.project_mcp_server_status("blocked", tmp_path)
        == project_settings.PROJECT_MCP_REJECTED
    )
    assert (
        project_settings.project_mcp_server_status("other", tmp_path)
        == project_settings.PROJECT_MCP_PENDING
    )


def test_mcp_server_approval_mutations_share_the_normalized_list_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(project_settings, "is_workspace_trusted", lambda _root: True)
    _write_project_local_settings(
        tmp_path,
        {
            "enabled_servers": [" docs ", "docs"],
            "disabled_servers": [" blocked ", "blocked"],
        },
    )

    project_settings.approve_project_mcp_server("blocked", tmp_path)
    approved = project_settings.read_project_local_settings(tmp_path)
    assert approved["enabled_servers"] == ["docs", "blocked"]
    assert "disabled_servers" not in approved

    project_settings.reject_project_mcp_server("docs", tmp_path)
    rejected = project_settings.read_project_local_settings(tmp_path)
    assert rejected["enabled_servers"] == ["blocked"]
    assert rejected["disabled_servers"] == ["docs"]


def _write_agent(directory: Path, filename: str, content: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(content, encoding="utf-8")


def test_agent_frontmatter_requires_string_tool_arrays_and_skips_invalid_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    _write_agent(
        agents_dir,
        "valid.md",
        "---\n"
        "name: valid\n"
        "tools: [' read_file ', read_file, ' grep_files ']\n"
        "disallowed_tools: [' write_file ', write_file]\n"
        "---\n"
        "Use the valid agent.\n",
    )
    _write_agent(
        agents_dir,
        "scalar.md",
        "---\nname: scalar\ntools: read_file\n---\nSkip this agent.\n",
    )
    _write_agent(
        agents_dir,
        "mixed.md",
        "---\nname: mixed\ntools: [read_file, 7]\n---\nSkip this agent.\n",
    )
    _write_agent(
        agents_dir,
        "bad-disallowed.md",
        "---\nname: bad-disallowed\n"
        "disallowed_tools: [write_file, false]\n"
        "---\nSkip this agent.\n",
    )
    monkeypatch.setattr(agents_loader, "_agent_search_dirs", lambda _root=None: [agents_dir])

    discovered = agents_loader.discover_agents(tmp_path)

    assert set(discovered) == {"valid"}
    assert discovered["valid"].tools == ["read_file", "grep_files"]
    assert discovered["valid"].disallowed_tools == ["write_file"]


@pytest.mark.parametrize("factory", [ConversationSummary.from_dict, ConversationRecord.from_dict])
def test_conversation_memory_pollution_sources_are_normalized(
    factory: Callable[[dict[str, Any]], Any],
) -> None:
    conversation = factory(
        {
            "id": "conversation-1",
            "title": "Memory test",
            "memory_pollution_sources": [
                " Web_Search ",
                "web_search",
                " MCP__GitHub__Issues ",
                "",
            ],
        }
    )

    assert conversation.memory_pollution_sources == [
        "web_search",
        "mcp__github__issues",
    ]
    assert conversation.memory_polluted is True
    assert conversation.memory_mode == "polluted"


@pytest.mark.parametrize(
    "value",
    ["web_search", ["web_search", 7]],
)
@pytest.mark.parametrize("factory", [ConversationSummary.from_dict, ConversationRecord.from_dict])
def test_conversation_memory_pollution_sources_reject_non_string_arrays(
    factory: Callable[[dict[str, Any]], Any],
    value: Any,
) -> None:
    with pytest.raises(ConfigRequirementsError, match="memory_pollution_sources"):
        factory(
            {
                "id": "conversation-1",
                "title": "Memory test",
                "memory_pollution_sources": value,
            }
        )


def test_public_string_list_trims_deduplicates_and_bounds_items() -> None:
    projected = public_string_list(
        [" first\nitem ", "first item", "second", "x" * 20, "third"],
        maximum=4,
        item_max_chars=10,
    )

    assert projected == ["first item", "second", "xxxxxxx..."]


def test_public_conversation_projection_bounds_and_deduplicates_pollution_sources() -> None:
    long_source = "source-" + "x" * 5_000
    projected = project_public_conversation(
        {
            "id": "conversation-1",
            "memory_pollution_sources": [
                " web_search ",
                "web_search",
                long_source,
                long_source,
            ],
        },
        include_transcript=False,
    )

    assert projected["memory_pollution_sources"] == [
        "web_search",
        long_source[:4_093] + "...",
    ]
