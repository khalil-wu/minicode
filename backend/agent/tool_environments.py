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
        if tool.execution_environment_fields:
            existing = self.selected.get(call.id)
            if existing is not None:
                for field, environment in existing.named_turn_environments.items():
                    if call.arguments[field] != environment.selection.environment_id:
                        raise ValueError(f"PreToolUse hook cannot change the tool environment ({field})")
                return self.admitted_context(call, context)
            if self.snapshot is None:
                raise ValueError("No usable execution environment is available.")
            selected = {}
            for field in tool.execution_environment_fields:
                environment = select_turn_environment(self.snapshot, call.arguments[field])
                key = call.id, id(environment.environment)
                if key not in self.bound:
                    self.bound[key] = bind_tool_execution_environment(context, environment)
                selected[field] = self.bound[key].turn_environment
            target = selected[tool.execution_environment_fields[-1]]
            bound = replace(context, turn_environment=target, environment_id=target.selection.environment_id,
                permission=replace(context.permission, environment_scope=target.permission_scope),
                named_turn_environments=selected)
            from backend.hooks.environment import with_tool_environment_hooks

            bound.environment_hook_manager = with_tool_environment_hooks(bound, selected.values())
            self.selected[call.id] = bound
            return bound
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
            permission=replace(context.permission, environment_scope=bound.permission.environment_scope),
            named_turn_environments=bound.named_turn_environments,
            environment_hook_manager=bound.environment_hook_manager)

    def admitted_context(self, call, context):
        bound = self.selected.get(call.id)
        if bound is None:
            return context
        return replace(context, turn_environment=bound.turn_environment, environment_id=bound.environment_id,
            permission=replace(context.permission, environment_scope=bound.permission.environment_scope),
            named_turn_environments=bound.named_turn_environments,
            environment_hook_manager=bound.environment_hook_manager)

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
        pending += tuple(task for bound in self.selected.values() if bound.environment_hook_manager is not None
            for task in bound.environment_hook_manager._async_tasks if not task.done())
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
    if context.named_turn_environments:
        return {name: {**permission_scope_for_attachment(context, environment).identity(), "registration": id(environment.environment),
                      "exec_policy": environment.config.exec_policy}
            for name, environment in context.named_turn_environments.items()}
    environment = context.turn_environment
    if environment is None:
        return None
    return {**context.permission.environment_scope.identity(),
        "registration": id(environment.environment), "exec_policy": environment.config.exec_policy}


def permission_scope_for_attachment(context, environment):
    return environment.permission_scope.with_additional_permissions(
        context.permission_grants.granted(environment.selection.environment_id, context.turn_id))
