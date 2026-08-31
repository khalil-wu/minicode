from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.agents.loader as agents_loader
import backend.api.routes_agents as routes_agents
import backend.services.agent_editor_service as agent_editor
from backend.agents.loader import AgentDefinition, discover_agents
from backend.api.routes_agents import AgentUpsertRequest
from backend.llm.model_runtime import ModelDefinition
from backend.services.agent_editor_service import (
    AgentEditorServiceError,
    AgentUpsertPayload,
)


def _write_agent(
    directory: Path,
    filename: str,
    *,
    name: str,
    description: str,
    prompt: str = "Do the work.",
    extra: str = "",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f'description: "{description}"\n'
        f"{extra}"
        "---\n\n"
        f"{prompt}\n",
        encoding="utf-8",
    )
    return path


def _configure_scopes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / "home"
    managed = tmp_path / "managed"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".git").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(agents_loader, "_get_managed_minicode_dir", lambda: managed)
    monkeypatch.setattr(agent_editor, "_active_workspace", lambda: workspace)
    return SimpleNamespace(
        home=home,
        managed=managed,
        workspace=workspace,
        user_agents=home / ".minicode" / "agents",
        managed_agents=managed / "agents",
        project_agents=workspace / ".minicode" / "agents",
    )


def test_list_returns_all_shadowed_sources_and_marks_managed_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scopes = _configure_scopes(monkeypatch, tmp_path)
    _write_agent(
        scopes.user_agents,
        "reviewer-user.md",
        name="reviewer",
        description="user",
    )
    _write_agent(
        scopes.project_agents,
        "reviewer-project.md",
        name="reviewer",
        description="project",
    )
    managed_path = _write_agent(
        scopes.managed_agents,
        "reviewer-managed.md",
        name="reviewer",
        description="managed",
    )

    payload = agent_editor.list_agents()

    reviewers = [item for item in payload["agents"] if item["name"] == "reviewer"]
    assert [item["source"] for item in reviewers] == [
        "policy",
        "user",
        "project",
    ]
    assert [item["source"] for item in reviewers if item["active"]] == [
        "policy"
    ]
    managed = next(item for item in reviewers if item["source"] == "policy")
    assert managed["source_path"] == str(managed_path.resolve())
    assert managed["editable"] is False
    assert managed["deletable"] is False
    assert managed["can_override"] is False


def test_closest_project_agent_overrides_parent_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    managed = tmp_path / "managed"
    root = tmp_path / "repo"
    nested = root / "packages" / "app"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(agents_loader, "_get_managed_minicode_dir", lambda: managed)
    _write_agent(
        nested / ".minicode" / "agents",
        "audit.md",
        name="audit",
        description="nested",
    )
    _write_agent(
        root / ".minicode" / "agents",
        "audit.md",
        name="audit",
        description="root",
    )

    active = discover_agents(nested)["audit"]

    assert active.description == "nested"
    assert active.source_path is not None
    assert active.source_path.resolve() == (
        nested / ".minicode" / "agents" / "audit.md"
    ).resolve()


def test_edit_and_delete_target_the_exact_shadowed_user_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scopes = _configure_scopes(monkeypatch, tmp_path)
    user_path = _write_agent(
        scopes.user_agents,
        "reviewer-user.md",
        name="reviewer",
        description="user",
    )
    project_path = _write_agent(
        scopes.project_agents,
        "reviewer-project.md",
        name="reviewer",
        description="project",
    )

    saved = agent_editor.upsert_agent(
        AgentUpsertPayload(
            name="reviewer",
            description="updated user",
            prompt="Updated user prompt.",
            source="user",
            source_path=str(user_path),
        )
    )["agent"]

    assert saved["source"] == "user"
    assert "updated user" in user_path.read_text(encoding="utf-8")
    assert "description: \"project\"" in project_path.read_text(encoding="utf-8")
    assert agent_editor.delete_agent(
        "reviewer",
        source="user",
        source_path=str(user_path),
    ) == {"deleted": True, "name": "reviewer"}
    assert not user_path.exists()
    assert project_path.exists()


def test_managed_and_mismatched_source_paths_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scopes = _configure_scopes(monkeypatch, tmp_path)
    managed_path = _write_agent(
        scopes.managed_agents,
        "managed.md",
        name="managed",
        description="managed",
    )
    user_path = _write_agent(
        scopes.user_agents,
        "user.md",
        name="user-agent",
        description="user",
    )
    outside_path = _write_agent(
        tmp_path / "outside",
        "outside.md",
        name="outside",
        description="outside",
    )

    with pytest.raises(AgentEditorServiceError, match="read-only"):
        agent_editor.upsert_agent(
            AgentUpsertPayload(
                name="managed",
                source="project",
                source_path=str(managed_path),
            )
        )
    with pytest.raises(AgentEditorServiceError, match="does not match"):
        agent_editor.upsert_agent(
            AgentUpsertPayload(
                name="user-agent",
                source="project",
                source_path=str(user_path),
            )
        )
    with pytest.raises(AgentEditorServiceError, match="no longer available"):
        agent_editor.upsert_agent(
            AgentUpsertPayload(
                name="outside",
                source="user",
                source_path=str(outside_path),
            )
        )
    with pytest.raises(AgentEditorServiceError, match="not deletable"):
        agent_editor.delete_agent(
            "managed",
            source="project",
            source_path=str(managed_path),
        )


def test_create_locations_duplicate_boundary_and_no_workspace_behavior(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scopes = _configure_scopes(monkeypatch, tmp_path)

    project = agent_editor.upsert_agent(
        AgentUpsertPayload(
            name="project-agent",
            description="project",
            prompt="Project prompt.",
            location="project",
        )
    )["agent"]
    user = agent_editor.upsert_agent(
        AgentUpsertPayload(
            name="user-agent",
            description="user",
            prompt="User prompt.",
            location="user",
        )
    )["agent"]

    assert Path(project["source_path"]) == (
        scopes.project_agents / "project-agent.md"
    ).resolve()
    assert Path(user["source_path"]) == (
        scopes.user_agents / "user-agent.md"
    ).resolve()
    with pytest.raises(AgentEditorServiceError, match="already exists"):
        agent_editor.upsert_agent(
            AgentUpsertPayload(
                name="project-agent",
                prompt="Must not overwrite.",
                location="project",
            )
        )
    assert "Project prompt." in (
        scopes.project_agents / "project-agent.md"
    ).read_text(encoding="utf-8")

    monkeypatch.setattr(agent_editor, "_active_workspace", lambda: None)
    user_without_workspace = agent_editor.upsert_agent(
        AgentUpsertPayload(
            name="global-only",
            prompt="Global prompt.",
            location="user",
        )
    )["agent"]
    assert Path(user_without_workspace["source_path"]) == (
        scopes.user_agents / "global-only.md"
    ).resolve()
    with pytest.raises(AgentEditorServiceError, match="Open a workspace"):
        agent_editor.upsert_agent(
            AgentUpsertPayload(
                name="project-without-workspace",
                location="project",
            )
        )


def test_frontmatter_uses_canonical_schema_and_round_trips_supported_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scopes = _configure_scopes(monkeypatch, tmp_path)
    description = 'Windows C:\\repo says "hello"\nnext line'

    saved = agents_loader.save_custom_agent(
        "reviewer",
        description=description,
        prompt="Review carefully.",
        model="sonnet",
        effort="high",
        tools=["Read", "Grep", "Bash"],
        disallowed_tools=["Write", "Edit"],
        workspace_root=scopes.workspace,
        source="project",
    )
    raw = saved.source_path.read_text(encoding="utf-8")  # type: ignore[union-attr]

    assert "tools:\n- Read\n- Grep\n- Bash" in raw
    assert "disallowed_tools:\n- Write\n- Edit" in raw
    assert "disallowedTools" not in raw
    assert "model: sonnet" in raw
    assert "effort: high" in raw

    parsed = agents_loader.discover_agents(scopes.workspace)["reviewer"]
    assert parsed.tools == ["Read", "Grep", "Bash"]
    assert parsed.disallowed_tools == ["Write", "Edit"]
    assert parsed.model == "sonnet"
    assert parsed.effort == "high"
    assert parsed.description == description
    assert parsed.prompt == "Review carefully."

    wildcard = agents_loader.save_custom_agent(
        "all-tools",
        prompt="Use all tools.",
        tools=["*"],
        workspace_root=scopes.workspace,
        source="project",
    )
    wildcard_raw = wildcard.source_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "tools:" not in wildcard_raw


def test_noncanonical_agent_effort_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scopes = _configure_scopes(monkeypatch, tmp_path)
    _write_agent(
        scopes.project_agents,
        "numeric.md",
        name="numeric",
        description="numeric",
        extra="effort: 72\n",
    )
    _write_agent(
        scopes.project_agents,
        "invalid.md",
        name="invalid",
        description="invalid",
        extra="effort: turbo\n",
    )

    agents = agents_loader.discover_agents(scopes.workspace)

    assert agents["numeric"].effort == ""
    assert agents["invalid"].effort == ""


class _CatalogRuntime:
    def __init__(self, model: ModelDefinition, *, fail_provider: bool = False) -> None:
        self.active = True
        self._model = model
        self._fail_provider = fail_provider

    def get_available_snapshot(self):
        return (self._model,)

    def get_registered_provider_config(self, _provider: str):
        return {}

    def get_provider(self, provider: str):
        if self._fail_provider:
            raise RuntimeError("Model runtime belongs to a retired extension generation")
        return SimpleNamespace(name=f"{provider} display")


class _CatalogSession:
    is_connected = True
    active_conversation_id = "conversation-1"

    def __init__(self, workspace: Path, runtime: _CatalogRuntime) -> None:
        self._workspace = workspace
        self._runtime = runtime
        self.session_lifecycle = SimpleNamespace(
            workspace_root_for_conversation=lambda: self._workspace,
        )

    def _model_runtime_for_conversation(self, _conversation_id: str):
        return self._runtime


def _catalog_model(provider: str, model_id: str) -> ModelDefinition:
    return ModelDefinition(
        provider=provider,
        id=model_id,
        name=f"{model_id} display",
        api="openai-completions",
        base_url="https://example.invalid/v1",
        reasoning=True,
        thinking_level_map={"xhigh": "max"},
        context_window=128_000,
        max_tokens=16_384,
    )


def test_agent_api_catalog_uses_live_model_runtime_and_survives_retirement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fallback = _CatalogSession(
        workspace,
        _CatalogRuntime(_catalog_model("zai", "glm-5")),
    )
    stale = _CatalogSession(
        workspace,
        _CatalogRuntime(
            _catalog_model("stale", "old-model"),
            fail_provider=True,
        ),
    )
    manager = SimpleNamespace(iter_sessions=lambda: [fallback, stale])
    monkeypatch.setattr(routes_agents._state, "ws_manager", manager)
    monkeypatch.setattr(
        routes_agents,
        "get_explicit_active_workspace_root",
        lambda: workspace,
    )

    catalog = routes_agents._live_agent_model_catalog()

    assert catalog == [
        {
            "provider": "zai",
            "provider_name": "zai display",
            "model": "glm-5",
            "model_name": "glm-5 display",
            "reasoning_effort_levels": [
                "off",
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
            ],
            "default_reasoning_effort": "medium",
        }
    ]


def test_agent_request_list_defaults_are_not_shared() -> None:
    first = AgentUpsertRequest(name="first")
    second = AgentUpsertRequest(name="second")

    first.tools.append("Read")
    first.disallowed_tools.append("Write")

    assert second.tools == []
    assert second.disallowed_tools == []
