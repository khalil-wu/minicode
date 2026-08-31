import asyncio
from pathlib import Path

from backend.agent.query_engine import QueryEngine, QuerySubmission
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.permissions.checker import PermissionChecker
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


class _SchemaTool(BaseTool):
    def __init__(self, name: str, description: str = "Test tool.") -> None:
        self.name = name
        self.description = description

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args, context=None) -> ToolResult:
        return ToolResult(content="ok")


def test_query_engine_rebinds_permission_checker_to_submission_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "RAG"
    workspace.mkdir()
    seen_roots: list[Path] = []

    async def runner(**kwargs):
        checker = kwargs["permission_checker"]
        seen_roots.append(checker._workspace_root)
        if False:
            yield None

    engine = QueryEngine(runner=runner)
    submission = QuerySubmission(
        user_message="read README",
        llm=object(),
        tool_registry=object(),
        artifact_store=object(),
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path / "MiniCode"),
        agent_settings=AgentSettings(),
        token_budget=TokenBudget(),
        workspace_root=workspace,
    )

    async def collect() -> None:
        async for _ in engine.submit(submission):
            pass

    asyncio.run(collect())

    assert seen_roots == [workspace.resolve()]


def test_tool_schemas_are_filtered_and_annotated_by_permission_context() -> None:
    registry = ToolRegistry()
    registry.register(_SchemaTool("read_file", "Read files."))
    registry.register(_SchemaTool("run_command", "Run shell commands."))
    registry.register(_SchemaTool("write_file", "Write files."))

    checker = PermissionChecker(
        PermissionSettings(
            auto_allow=["read_*"],
            require_confirm=["run_*"],
            require_diff_review=["write_file"],
            always_deny=[],
        )
    )
    context = checker.build_context(
        mode="auto",
        tool_deny_rules=["run_command"],
        session_overrides={"write_file": PermissionLevel.DIFF_REVIEW},
        source="test",
    )

    schemas = registry.get_schemas(
        permission_checker=checker,
        permission_context=context,
    )
    by_name = {schema["function"]["name"]: schema for schema in schemas}

    assert set(by_name) == {"read_file", "write_file"}
    # Phase 1.3: model-facing descriptions must not carry permission/UI noise.
    assert "Permission:" not in by_name["read_file"]["function"]["description"]
    assert "Permission:" not in by_name["write_file"]["function"]["description"]
    # Permission level is surfaced via runtime metadata instead.
    metadata = registry.get_runtime_metadata(
        permission_checker=checker,
        permission_context=context,
    )
    assert metadata["read_file"]["permission"] == "auto"
    assert metadata["write_file"]["permission"] == "diff"
