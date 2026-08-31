from __future__ import annotations

from backend.artifact.store import ArtifactStore
from backend.tools.agent_tools import TaskTool
from backend.tools.registry import ToolRegistry
from backend.tools.subagent_control_tools import TaskStatusTool, TaskStopTool
from backend.tools.swarm_tools import (
    MessageListTool,
    SendMessageTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskOutputTool,
    TaskUpdateTool,
    TeamCreateTool,
    TeamDeleteTool,
    TeamListTool,
)
from backend.tools.tool_search import DeferredToolCatalog


def _schema_names(registry: ToolRegistry) -> set[str]:
    return {
        str((schema.get("function") or {}).get("name") or "")
        for schema in registry.get_schemas()
    }


def _registry(tmp_path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(TaskTool(artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts")))
    registry.register(TaskStatusTool())
    registry.register(TaskStopTool())
    registry.register(SendMessageTool())
    registry.register(MessageListTool())
    registry.register(TaskCreateTool())
    registry.register(TaskListTool())
    registry.register(TaskGetTool())
    registry.register(TaskUpdateTool())
    registry.register(TaskOutputTool())
    registry.register(TeamCreateTool())
    registry.register(TeamListTool())
    registry.register(TeamDeleteTool())
    return registry


def test_default_multiagent_surface_keeps_coordination_tools_deferred(tmp_path) -> None:
    registry = _registry(tmp_path)

    assert _schema_names(registry) == set(registry.list_tools())
def test_coordination_tools_are_discoverable_in_default_catalog(tmp_path) -> None:
    registry = _registry(tmp_path)

    names = {entry.name for entry in DeferredToolCatalog(registry).entries()}

    assert "workflow" not in names
    assert names == set()


def test_model_facing_task_schema_keeps_runtime_single_and_parallel_fields(tmp_path) -> None:
    registry = _registry(tmp_path)

    task_schema = next(
        schema["function"]
        for schema in registry.get_schemas()
        if schema["function"]["name"] == "task"
    )
    properties = set(task_schema["parameters"]["properties"])

    assert properties == {
        "description",
        "prompt",
        "agent_type",
        "model",
        "provider",
        "reasoning_effort",
        # Claude's Agent/Task surface carries teammate routing on the single
        # delegation shape; runtime derives the internal team metadata from
        # these public fields.
        "name",
        "team_name",
        "mode",
        "parallel_tasks",
        "run_in_background",
        "read_only",
        "write_scope",
        "detach_from_parent",
        "cancel_with_parent",
        "isolation",
        "cwd",
    }
    assert task_schema["parameters"].get("required") is None
    assert task_schema["parameters"]["anyOf"] == [
        {"required": ["description", "prompt"]},
        {"required": ["parallel_tasks"]},
    ]
    parallel_properties = set(
        task_schema["parameters"]["properties"]["parallel_tasks"]["items"]["properties"]
    )
    assert parallel_properties == {
        "description",
        "prompt",
        "agent_type",
        "model",
        "provider",
        "reasoning_effort",
        "read_only",
        "write_scope",
        "detach_from_parent",
        "cancel_with_parent",
        "isolation",
        "cwd",
    }
    assert "workflow_id" not in properties
