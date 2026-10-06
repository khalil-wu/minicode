from __future__ import annotations

import pytest

from backend.artifact.store import ArtifactStore
from backend.tools.agent_tools import TaskTool
from backend.tools.base import validate_tool_input
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
from backend.tools.schema import code_mode_parameters


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
    ordinary, teammate, batch = task_schema["parameters"]["anyOf"]
    assert ordinary["required"] == ["description", "prompt"]
    assert teammate["required"] == ["description", "prompt", "name"]
    assert batch["required"] == ["parallel_tasks"]
    assert all(branch["additionalProperties"] is False for branch in (ordinary, teammate, batch))
    assert not {"name", "team_name", "mode", "parallel_tasks"} & set(ordinary["properties"])
    assert "parallel_tasks" not in teammate["properties"]
    assert set(batch["properties"]) == {"parallel_tasks", "run_in_background"}
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


@pytest.mark.parametrize("routing_field", ["name", "team_name", "mode"])
def test_task_batch_rejects_teammate_routing_in_the_published_execution_contract(tmp_path, routing_field):
    tool = _registry(tmp_path).get_tool("task")
    args = {"parallel_tasks": [
        {"description": "Parser implementation", "prompt": "Implement parser.py", "write_scope": ["parser.py"]},
        {"description": "Parser tests", "prompt": "Implement test_parser.py", "write_scope": ["test_parser.py"]},
    ], "run_in_background": True}
    assert validate_tool_input(tool, args) == ""
    args["parallel_tasks"][0][routing_field] = "auto" if routing_field == "mode" else "parser-summary"
    assert validate_tool_input(tool, args)
    assert tool.model_schema().parameters == tool.get_execution_schema().parameters


def test_code_mode_task_signature_presents_distinct_single_teammate_and_batch_inputs(tmp_path):
    tool = _registry(tmp_path).get_tool("task")
    parameters = tool.model_schema().parameters
    ordinary, teammate, batch = parameters["anyOf"]
    batch_signature = code_mode_parameters(batch)
    assert code_mode_parameters(parameters).endswith(batch_signature)
    assert "parallel_tasks:" in batch_signature
    assert "name?:" not in batch_signature
    assert "team_name?:" not in batch_signature
    assert "mode?:" not in batch_signature
    assert "description:" in batch_signature and "prompt:" in batch_signature
    assert "name:" in code_mode_parameters(teammate)
    assert validate_tool_input(tool, {"description": "Inspect parser", "prompt": "Review parser.py", "read_only": True}) == ""
    assert validate_tool_input(tool, {"description": "Team parser", "prompt": "Review parser.py", "name": "parser", "team_name": "review"}) == ""
    assert validate_tool_input(tool, {"description": "Review", "prompt": "Review parser.py", "team_name": "review"})
    assert validate_tool_input(tool, {"parallel_tasks": [
        {"description": "A", "prompt": "Review A", "read_only": True},
        {"description": "B", "prompt": "Review B", "read_only": True},
    ], "name": "parser"})
    assert "description is its UI/task title" in tool.model_description()
