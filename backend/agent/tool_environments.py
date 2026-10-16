"""Per-batch ownership of the exact attachments selected by tool requests."""
from __future__ import annotations

import asyncio
from dataclasses import replace

from backend.agent.environment_selection import (
    bind_tool_execution_environment, close_tool_execution_environment, select_turn_environment,
)


class ToolEnvironmentBindings:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.bound = {}
        self.selected = {}

    def for_call(self, call, context, tool):
        if tool is None or not tool.requires_execution_environment:
            return context
        environment_id = call.arguments.get("environment_id")
        if self.snapshot is None:
            if context.turn_environment is None:
                if environment_id not in {None, "local"}:
                    raise ValueError(f"unknown turn environment id `{environment_id}`")
                return context
            environment = context.turn_environment
            if environment_id is not None and environment_id != environment.selection.environment_id:
                raise ValueError(f"unknown turn environment id `{environment_id}`")
        else:
            run = context.run_context
            features = run.model_execution.config.feature_flags if run is not None and run.model_execution is not None else None
            environment = select_turn_environment(self.snapshot, environment_id,
                stable_environment_tools=features.enabled("stable_environment_tools", default=False) if features is not None else False,
                legacy_unavailable_message="No usable execution environment is available.")
        key = call.id, id(environment.environment)
        if key not in self.bound:
            self.bound[key] = bind_tool_execution_environment(context, environment)
        bound = self.bound[key]
        self.selected[call.id] = bound
        return replace(context, turn_environment=bound.turn_environment, environment_id=bound.environment_id,
            permission=replace(context.permission, environment_scope=bound.permission.environment_scope))

    def admitted_context(self, call, context):
        bound = self.selected.get(call.id)
        if bound is None:
            return context
        return replace(context, turn_environment=bound.turn_environment, environment_id=bound.environment_id,
            permission=replace(context.permission, environment_scope=bound.permission.environment_scope))

    async def _close(self):
        results = await asyncio.gather(*(close_tool_execution_environment(context)
            for context in self.bound.values()), return_exceptions=True)
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("tool execution attachment cleanup failed", errors)

    async def close_after_tools(self, context):
        if not self.bound:
            await self._close()
            return
        call_ids = tuple(self.selected)
        cleanup_tasks_by_call = context.cleanup_tasks_by_call
        pending = tuple(task for call_id in call_ids
            if (task := cleanup_tasks_by_call.get(call_id)) is not None and not task.done())
        if not pending:
            await self._close()
            return

        async def close_when_settled():
            # A cancelled wrapper can hand its call to the actual tool child.
            # Follow only these bound calls; their receipts retain the errors.
            remaining = pending
            while remaining:
                await asyncio.gather(*remaining, return_exceptions=True)
                remaining = tuple(task for call_id in call_ids
                    if (task := cleanup_tasks_by_call.get(call_id)) is not None and not task.done())
            await self._close()

        task = asyncio.create_task(close_when_settled(), name="tool-environment-attachment-cleanup")
        if context.run_context is not None:
            context.run_context.retain_lifecycle_task(task, label="tool_environment_attachment_cleanup")
        else:
            from backend.async_cleanup import retain_cleanup_task

            retain_cleanup_task(task, context.pending_cleanup_tasks)


def execution_context_for_call(call, context, tool):
    owner = context.environment_bindings
    return owner.for_call(call, context, tool) if owner is not None else context


def execution_environment_identity(context):
    environment = context.turn_environment
    if environment is None:
        return None
    return {**context.permission.environment_scope.identity(),
        "registration": id(environment.environment), "exec_policy": environment.config.exec_policy}
