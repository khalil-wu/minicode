"""Locked-down MiniCode agent used for memory consolidation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from backend.agent.context import ContextBuilder
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.agent_artifact_tools import ReadArtifactTool
from backend.tools.apply_patch import ApplyPatchTool
from backend.tools.edit_file import EditFileTool
from backend.tools.list_files import ListFilesTool
from backend.tools.read_file import ReadFileTool
from backend.tools.registry import ToolRegistry
from backend.tools.search_tools import GlobFilesTool, GrepFilesTool
from backend.tools.write_file import WriteFileTool


class MemoryConsolidationAgentError(RuntimeError):
    pass


def _tool_registry(artifact_store: ArtifactStore) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFileTool(artifact_store))
    registry.register(ListFilesTool())
    registry.register(GrepFilesTool())
    registry.register(GlobFilesTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(ApplyPatchTool())
    registry.register(ReadArtifactTool(artifact_store))
    return registry


def _permission_settings() -> PermissionSettings:
    return PermissionSettings(
        auto_allow=[
            "read_file",
            "list_files",
            "grep_files",
            "glob_files",
            "read_artifact",
        ],
        require_confirm=[],
        require_diff_review=["write_file", "edit_file", "apply_patch"],
        always_deny=[],
        path_allowlist=["."],
        path_denylist=[
            ".git/**",
            "memories_1.sqlite3",
            "memories_1.sqlite3-shm",
            "memories_1.sqlite3-wal",
            "*.lock",
        ],
    )


async def run_memory_consolidation_agent(
    *,
    llm: object,
    memory_root: Path,
    prompt: str,
    token_budget: int = 0,
    cancel_event: asyncio.Event | None = None,
) -> None:
    """Run one ephemeral internal agent and require a clean terminal outcome."""

    root = memory_root.expanduser().resolve()
    artifact_store = ArtifactStore()
    registry = _tool_registry(artifact_store)
    permission_checker = PermissionChecker(_permission_settings(), root)
    permission_context = PermissionContext(
        mode="auto",
        filesystem_constraints={"allowlist": ["."]},
        workspace_scope="project",
        source="memory_consolidation",
        approval_policy="never",
        sandbox_mode="workspace-write",
        allow_unsandboxed_commands=False,
        sandbox_fail_if_unavailable=True,
    )
    budget = TokenBudget(total=token_budget) if token_budget > 0 else TokenBudget()
    settings = AgentSettings(live_text_streaming=False)
    state = AgentState(user_message=prompt)
    state.prompt_context["internal_session_source"] = "memory_consolidation"
    session_id = f"memory-consolidation-{uuid4().hex}"
    task_id = f"{session_id}:turn"
    owner_token = artifact_store.bind_owner(session_id, str(root))
    try:
        runtime = AgentLoopSessionContext(
            permission_context=permission_context,
            workspace_root=root,
            session_id=session_id,
            task_id=task_id,
            cancel_event=cancel_event,
            metadata={
                "cancel_event": cancel_event,
                "cwd": str(root),
                "agent_mode": "internal",
                "agent_role": "memory_consolidation",
                "memory_generation_disabled": True,
                "memory_use_disabled": True,
            },
        )
        agent_session = AgentSession(
            llm=llm,  # type: ignore[arg-type]
            tool_registry=registry,
            artifact_store=artifact_store,
            permission_checker=permission_checker,
            agent_settings=settings,
            token_budget=budget,
            context_builder=ContextBuilder(
                token_budget=budget,
                agent_settings=settings,
                llm=llm,
            ),
        )
        async for _event in QueryEngine().submit(QuerySubmission(
            user_message=prompt,
            session=agent_session,
            state=state,
            runtime=runtime,
        )):
            pass
    finally:
        artifact_store.reset_owner(owner_token)

    if state.terminal_status != "completed":
        raise MemoryConsolidationAgentError(
            "Memory consolidation agent did not complete "
            f"(status={state.terminal_status or 'unknown'}, "
            f"reason={state.stopped_reason or 'unknown'})"
        )
