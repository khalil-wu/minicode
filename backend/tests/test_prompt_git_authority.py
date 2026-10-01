import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

import backend.agent.context as context_module
from backend.agent.context import ContextBuilder, clone_context_builder
from backend.agent.query_engine import QueryEngine
from backend.agent.state import AgentState
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox.policy import NetworkSandboxPolicy, SandboxPolicy


def execution_context(root):
    return ToolExecutionContext(
        permission=PermissionContext(), workspace_root=root,
        sandbox_policy=SandboxPolicy(workspace_root=root, readable_roots=(root,)),
        cancel_event=asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_repository_probe_uses_exact_turn_authority_and_owner(tmp_path, monkeypatch):
    calls = []

    async def probe(root, *, context=None):
        calls.append((root, context))
        return "captured-status"

    monkeypatch.setattr(context_module, "build_git_status_context_async", probe)
    builder = ContextBuilder()
    context = execution_context(tmp_path)
    builder.bind_tool_context(context)
    await builder._ensure_git_status_context(tmp_path)
    await builder._ensure_git_status_context(tmp_path)
    assert calls == [(tmp_path, context)]
    assert calls[0][1].cancel_event is context.cancel_event
    assert calls[0][1].pending_cleanup_tasks is context.pending_cleanup_tasks


@pytest.mark.asyncio
async def test_live_authority_change_invalidates_git_and_prepared_prompt(tmp_path, monkeypatch):
    policies = []

    async def probe(root, *, context=None):
        policies.append(context.sandbox_policy)
        return f"status-{len(policies)}"

    monkeypatch.setattr(context_module, "build_git_status_context_async", probe)
    builder = ContextBuilder()
    context = execution_context(tmp_path)
    builder.bind_tool_context(context)
    await builder._ensure_git_status_context(tmp_path)
    builder._prepared_prompt_parts = object()
    builder._prepared_prompt_state = object()
    profile = replace(context.sandbox_policy.permission_profile, network=NetworkSandboxPolicy.ENABLED)
    context.sandbox_policy = replace(context.sandbox_policy, permission_profile=profile)
    await builder._ensure_git_status_context(tmp_path)
    assert len(policies) == 2
    assert builder._git_status_context == "status-2"
    assert builder._prepared_prompt_parts is None
    assert builder._prepared_prompt_state is None


@pytest.mark.asyncio
async def test_environment_change_is_authority_but_timeout_is_not(tmp_path, monkeypatch):
    calls = []

    async def probe(root, *, context=None):
        calls.append(context.sandbox_policy)
        return "status"

    monkeypatch.setattr(context_module, "build_git_status_context_async", probe)
    builder = ContextBuilder()
    context = execution_context(tmp_path)
    builder.bind_tool_context(context)
    await builder._ensure_git_status_context(tmp_path)
    context.sandbox_policy = replace(context.sandbox_policy, timeout=7)
    await builder._ensure_git_status_context(tmp_path)
    assert len(calls) == 1
    context.sandbox_policy = replace(context.sandbox_policy, env_overrides={"MINICODE_GIT_ENV_ORACLE":"1"})
    await builder._ensure_git_status_context(tmp_path)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_restored_projection_does_not_restore_execution_authority(tmp_path, monkeypatch):
    calls = []

    async def probe(root, *, context=None):
        calls.append(context)
        return "fresh-status"

    monkeypatch.setattr(context_module, "build_git_status_context_async", probe)
    builder = ContextBuilder()
    builder.load_snapshot({"git_status_context":"stale-status", "git_status_workspace":str(tmp_path)})
    context = execution_context(tmp_path)
    builder.bind_tool_context(context)
    await builder._ensure_git_status_context(tmp_path)
    assert calls == [context]
    assert builder._git_status_context == "fresh-status"


@pytest.mark.asyncio
async def test_branch_builder_never_borrows_parent_tool_context(tmp_path, monkeypatch):
    calls = []

    async def probe(root, *, context=None):
        calls.append(context)
        return "status"

    monkeypatch.setattr(context_module, "build_git_status_context_async", probe)
    parent = ContextBuilder()
    parent_context = execution_context(tmp_path)
    parent.bind_tool_context(parent_context)
    await parent._ensure_git_status_context(tmp_path)
    branch = clone_context_builder(parent)
    assert branch._tool_execution_context is None
    child_context = execution_context(tmp_path)
    branch.bind_tool_context(child_context)
    await branch._ensure_git_status_context(tmp_path)
    assert calls == [parent_context, child_context]


@pytest.mark.asyncio
async def test_early_observer_projection_does_not_execute_repository_probe(tmp_path, monkeypatch):
    calls = []

    async def probe(root, *, context=None):
        calls.append(context)
        return "UNBOUND_REPOSITORY_PROBE"

    monkeypatch.setattr(context_module, "build_git_status_context_async", probe)
    builder = ContextBuilder()
    builder.bind_tool_context(execution_context(tmp_path))
    turn = SimpleNamespace(context_builder=builder, state=AgentState(user_message="fixture", workspace_root=tmp_path), metadata={})
    await QueryEngine._publish_system_prompt(turn)
    assert calls == []
    assert turn.metadata["system_prompt"]
    assert "UNBOUND_REPOSITORY_PROBE" not in turn.metadata["system_prompt"]


@pytest.mark.asyncio
async def test_startup_preview_does_not_reuse_previous_instruction_hooks(tmp_path, monkeypatch):
    builders = []

    def guidelines(self, workspace_root, *, consume_load_reason=True):
        builders.append(self)
        assert self._hook_manager is None
        self._guideline_load_reason = "preview-consumed"
        return ""

    monkeypatch.setattr(ContextBuilder, "_get_project_guidelines", guidelines)
    builder = ContextBuilder()
    builder.bind_hook_manager(object())
    builder._guideline_load_reason = "compact"
    turn = SimpleNamespace(context_builder=builder, state=AgentState(user_message="fixture", workspace_root=tmp_path), metadata={})
    await QueryEngine._publish_system_prompt(turn)
    assert builders and all(candidate is not builder for candidate in builders)
    assert builder._guideline_load_reason == "compact"
    assert builder._hook_manager is not None


def test_beginning_new_turn_drops_previous_git_execution_and_projection(tmp_path):
    builder = ContextBuilder()
    builder.bind_tool_context(execution_context(tmp_path))
    builder._git_status_context = "OLD_TURN_STATUS"
    builder._prepared_prompt_parts = object()
    builder._prepared_prompt_state = object()
    builder.bind_tool_context(None)
    assert builder._tool_execution_context is None
    assert builder._git_status_context is None
    assert builder._prepared_prompt_parts is None
    assert builder._prepared_prompt_state is None
