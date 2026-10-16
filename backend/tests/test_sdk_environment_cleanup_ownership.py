"""Real SDK source, optional attachment, capability retirement and query fence."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
import json

import pytest

from backend.agent.conversation_query_guard import conversation_query_guards
from backend.agent.run_context import RunContext
from backend.mcp.environment_runtime import PreparedEnvironmentCapabilities
from backend.sdk import create_session, create_tool_registry, query
from backend.tests.test_sdk_session_ownership_chain import sdk_owner, ToolModel
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema


@pytest.mark.asyncio
@pytest.mark.parametrize("requires_environment", [True, False], ids=["bound", "unbound"])
@pytest.mark.parametrize("late_registry_handoff", [False, True], ids=["early-source-settlement", "late-registry-handoff"])
async def test_real_sdk_cleanup_fence_waits_for_source_and_original_capabilities(sdk_owner, monkeypatch, record_testsuite_property, requires_environment, late_registry_handoff):
    workspace, runtime, artifacts, config = sdk_owner
    monkeypatch.setattr("backend.agent.tool_execution.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.001)
    release_source, source_finished = asyncio.Event(), asyncio.Event()
    close_entered, release_capabilities, capabilities_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    captured = []
    retirements = []
    registry_drain_entered, release_registry_drain = asyncio.Event(), asyncio.Event()
    if late_registry_handoff:
        from backend.tools import registry as registry_module
        original_registry_drain = registry_module.cancel_and_drain_receipt
        monkeypatch.setattr(registry_module, "CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.001)
        async def handoff_after_return(*args, **kwargs):
            registry_drain_entered.set()
            await release_registry_drain.wait()
            return await original_registry_drain(*args, **kwargs)
        monkeypatch.setattr(registry_module, "cancel_and_drain_receipt", handoff_after_return)
    original_close = PreparedEnvironmentCapabilities.aclose
    original_retire = PreparedEnvironmentCapabilities.retire

    def retire_original_capabilities(prepared, run_context):
        task = original_retire(prepared, run_context)
        original_call_tasks = task.get_coro().cr_frame.f_locals["call_tasks"]
        retirements.append({"prepared": prepared, "task": task, "call_tasks": original_call_tasks,
            "close_entered": asyncio.Event()})
        return task
    monkeypatch.setattr(PreparedEnvironmentCapabilities, "retire", retire_original_capabilities)

    async def close_original_capabilities(prepared):
        if prepared is not tool.original_capabilities:
            await original_close(prepared)
            return
        captured.append(prepared)
        next(record for record in retirements if record["prepared"] is prepared)["close_entered"].set()
        close_entered.set()
        await release_capabilities.wait()
        await original_close(prepared)
        capabilities_closed.set()
    monkeypatch.setattr(PreparedEnvironmentCapabilities, "aclose", close_original_capabilities)

    class ResistantTool(BaseTool):
        name = "sdk_bound_resistant" if requires_environment else "sdk_unbound_resistant"
        permission = PermissionLevel.AUTO
        read_only = True
        requires_execution_environment = requires_environment
        timeout_seconds = 0.01
        context = None
        source_task = None
        original_capabilities = None
        original_call_tasks = None

        def get_schema(self):
            return ToolSchema(name=self.name, description="Hold the actual SDK attachment", parameters={"type": "object", "properties": {}})

        async def execute(self, _args, context=None):
            self.context, self.source_task = context, asyncio.current_task()
            self.original_capabilities = context.run_context.environment_capabilities
            self.original_call_tasks = context.cleanup_tasks_by_call
            try:
                while not release_source.is_set():
                    try:
                        await release_source.wait()
                    except asyncio.CancelledError:
                        continue
                return ToolResult(content="released original source")
            finally:
                source_finished.set()

    tool = ResistantTool()
    run = RunContext(agent_runtime=runtime)
    conversation_id = "sdk-original-cleanup-fence"
    model = ToolModel(tool.name)
    registry = create_tool_registry(tool)
    session = create_session(session_id="sdk-bound-cleanup", workspace_root=workspace, config=config,
        llm=model, artifact_store=artifacts, tool_registry=registry, run_context=run,
        metadata={"conversation_id": conversation_id})

    async def collect(stream):
        async with aclosing(stream):
            return [event async for event in stream]

    async def assert_competing_query_is_fenced():
        events = await collect(query("Competing query", session_id="sdk-competing-owner", config=config,
            llm=model, tool_registry=registry, workspace_root=workspace,
            metadata={"conversation_id": conversation_id}))
        assert any(event.type == "error" and event.data["error_type"] == "conversation_busy" for event in events)

    try:
        events = await asyncio.wait_for(collect(session.query("Run the original held tool")), 10)
        assert any(event.type == "tool_result" and "timed out" in event.data["summary"] for event in events)
        context = tool.context
        assert context is not None
        assert context.run_context.node_repl_owner is session
        assert session.node_repl_companions is not None
        source = tool.source_task
        assert source is not None and not source.done()
        original_tool_owner = context.cleanup_tasks_by_call[context.tool_call_id]
        assert not original_tool_owner.done()
        registry_call = original_tool_owner.get_coro()
        while registry_call.cr_code.co_qualname != "CapabilityRegistry.execute":
            registry_call = registry_call.cr_await
        assert registry_call.cr_frame.f_locals["execution_task"] is source
        binding_owners = {task for task in session.lifecycle_cleanup_tasks
            if task.get_name() == "tool-environment-attachment-cleanup"}
        pending_retirements = [record for record in retirements if not record["task"].done()]
        capability_owners = {record["task"] for record in pending_retirements}
        assert capability_owners and all(not task.done() for task in capability_owners)
        original_retirement = next(record for record in pending_retirements if record["prepared"] is tool.original_capabilities)
        assert context.cleanup_tasks_by_call is tool.original_call_tasks is original_retirement["call_tasks"]
        assert session.lifecycle_cleanup_tasks == {original_tool_owner, *binding_owners, *capability_owners}
        original_attachment = context.turn_environment
        original_capability_snapshot = tool.original_capabilities.runtime.snapshot
        original_capability_attachment = original_capability_snapshot.primary()
        if requires_environment:
            assert len(binding_owners) == 1 and len(context.environment_bindings.bound) == 1
            assert original_capability_attachment.environment is original_attachment.environment
            assert not original_attachment.lease.released
        else:
            assert not binding_owners and not context.environment_bindings.bound
        assert not original_capability_attachment.lease.released
        guards = conversation_query_guards()
        claim = guards.active_claim(conversation_id)
        assert claim is not None and guards.owns(claim)
        assert not close_entered.is_set()
        await assert_competing_query_is_fenced()

        if late_registry_handoff:
            assert registry_drain_entered.is_set()
            release_registry_drain.set()
            handed_off = await asyncio.wait_for(asyncio.gather(original_tool_owner, return_exceptions=True), 2)
            assert isinstance(handed_off[0], asyncio.CancelledError)
            await asyncio.wait({original_retirement["task"], *binding_owners}, timeout=0.02)
            record_testsuite_property("late_handoff_state_" + ("bound" if requires_environment else "unbound"), json.dumps({
                "original_wrapper_done": original_tool_owner.done(), "original_source_done": source.done(),
                "original_call_owner_preserved": context.cleanup_tasks_by_call is tool.original_call_tasks is original_retirement["call_tasks"],
                "original_source_is_registered": tool.original_call_tasks.get(context.tool_call_id) is source,
                "binding_cleanup_pending": bool(binding_owners) and all(not task.done() for task in binding_owners),
                "original_attachment_released": original_attachment.lease.released if requires_environment else None,
                "original_capability_close_entered": original_retirement["close_entered"].is_set(),
                "original_capability_lease_released": original_capability_attachment.lease.released,
                "query_fence_still_owned": guards.owns(claim),
            }))
            assert not source.done() and context.cleanup_tasks_by_call[context.tool_call_id] is source
            if requires_environment:
                assert all(not task.done() for task in binding_owners) and not original_attachment.lease.released
            assert not original_retirement["close_entered"].is_set() and not original_capability_attachment.lease.released
            await assert_competing_query_is_fenced()

        closing_session = asyncio.create_task(session.aclose())
        await asyncio.sleep(0)
        assert not closing_session.done()
        release_source.set()
        await asyncio.wait_for(source_finished.wait(), 2)
        settled = await asyncio.wait_for(asyncio.gather(original_tool_owner, return_exceptions=True), 2)
        assert isinstance(settled[0], asyncio.CancelledError)
        assert source.result().content == "released original source"
        if binding_owners:
            await asyncio.wait_for(asyncio.gather(*(asyncio.shield(task) for task in binding_owners)), 2)
        await asyncio.wait_for(original_retirement["close_entered"].wait(), 2)
        assert source.done()
        if requires_environment:
            assert original_attachment.lease.released
        assert len(captured) == 1 and captured[0] is tool.original_capabilities
        assert not original_capability_attachment.lease.released
        assert not original_retirement["task"].done() and not capabilities_closed.is_set()
        assert not closing_session.done()
        assert guards.owns(claim)
        await assert_competing_query_is_fenced()

        release_capabilities.set()
        await asyncio.wait_for(asyncio.gather(*(asyncio.shield(task) for task in capability_owners)), 2)
        await asyncio.gather(*tuple(session._lifecycle_cleanup_waiters))
        await asyncio.wait_for(closing_session, 2)
        await asyncio.sleep(0)
        assert capabilities_closed.is_set() and original_capability_attachment.lease.released
        assert original_capability_snapshot._resources.close_task.done()
        assert original_capability_snapshot._resources.close_task.result() is None
        assert not session.lifecycle_cleanup_tasks and not session._lifecycle_cleanup_waiters
        assert not tool.original_call_tasks
        assert context.cleanup_receipts[context.tool_call_id]["completed"]
        assert context.cleanup_receipts[context.tool_call_id]["pending"] == 0
        assert guards.active_claim(conversation_id) is None
        receipts = run.lifecycle_cleanup_evidence()["lifecycle_cleanup_receipts"]
        assert all(receipt["completed"] and receipt["pending"] == 0 for receipt in receipts.values())
    finally:
        release_registry_drain.set()
        release_source.set()
        release_capabilities.set()
        await asyncio.wait_for(session.aclose(), 5)
