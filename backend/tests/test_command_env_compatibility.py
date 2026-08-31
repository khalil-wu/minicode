import asyncio

from backend.artifact.store import ArtifactStore
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.command_support import _validated_env
from backend.tools.command_tool import RunCommandTool
from backend.tools.registry import ToolRegistry


def test_blank_optional_env_is_treated_as_absent() -> None:
    assert _validated_env("") == ({}, "")


def test_nonempty_string_env_remains_invalid() -> None:
    resolved, error = _validated_env("NAME=value")

    assert resolved == {}
    assert error == "env must be an object of string values"


def test_registry_rejects_non_string_env_values_before_command_execution(tmp_path) -> None:
    registry = ToolRegistry()
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    registry.register(tool)

    result = asyncio.run(
        registry.execute(
            "run_command",
            {"command": "Write-Output ok", "env": {"RETRIES": 3}},
        )
    )

    assert result.is_error is True
    assert result.error_kind == "validation_error"
    assert result.status == "failed"
    assert "env.RETRIES" in result.content
    assert "string" in result.content


def test_registry_runs_tool_semantic_validation_after_schema_validation() -> None:
    class SemanticTool(BaseTool):
        name = "semantic"
        description = "semantic validation probe"

        def __init__(self) -> None:
            self.executed = False

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description=self.description,
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            )

        def validate_input(self, args=None) -> str:
            return "value must be approved" if (args or {}).get("value") != "approved" else ""

        async def execute(self, args, context=None) -> ToolResult:
            self.executed = True
            return ToolResult(content="ok")

    registry = ToolRegistry()
    tool = SemanticTool()
    registry.register(tool)

    result = asyncio.run(registry.execute("semantic", {"value": "rejected"}))

    assert result.error_kind == "validation_error"
    assert "value must be approved" in result.content
    assert tool.executed is False
