"""Extension operations on the canonical query, without a window/session alias."""
from __future__ import annotations

import asyncio
import json
from contextlib import aclosing, nullcontext
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from backend.agent.message import AgentEvent, UserCommand
from backend.agent.extension_history import REWIND_KEY, extension_values
from backend.conversations.projection_log import apply_value_change, value_change
from backend.agent.nested_tool_events import publish_tool_event
from backend.agent.tool_batch_runner import ToolBatchRunner
from backend.async_cleanup import to_thread_cancel_safe
from backend.llm.base import ToolCallEvent
from backend.permissions.context import ToolCallSource
from backend.tools.base import ToolResult
from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY, ToolsetPolicy

if TYPE_CHECKING:
    from backend.agent.query_engine import QueryTurnContext
    from backend.permissions.context import ToolExecutionContext


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or "")
    if isinstance(value, (list, tuple)):
        return "\n".join(_message_text(item) for item in value)
    return str(value or "")


class ExtensionExecutionActions:
    def __init__(self, query: QueryTurnContext):
        self.query: QueryTurnContext | None = query
        self._builder = query.context_builder
        self.execution_task = asyncio.current_task()
        self.tool_context: ToolExecutionContext | None = None
        self.tool_schemas: list[dict] | None = None
        self._revision = 0
        self._flushed_revision = 0
        self._flush_lock = asyncio.Lock()
        self._changes: list[dict] = []
        self._recorded_state = deepcopy(extension_values(self._builder.extension_state))
        self._builder.extension_state.setdefault(REWIND_KEY, {
            "floor": len(self._builder._history) if self._recorded_state else 0, "changes": [],
        })
        self._builder._extension_history_floor = len(self._builder._history) + int(
            not query.metadata.get("_query_engine_recovery_restored") and not query.metadata.get("_turn_admission_restored")
        )
        self._commit_prefix = "extension_state_" + uuid4().hex
        self._builder.extension_cursor = {"run_id": query.metadata["run_id"], "revision": 0}
        self.runtime_actions = {name: getattr(self, name) for name in (
            "send_message", "send_user_message", "append_entry", "set_label", "set_session_name", "get_session_name",
            "get_active_tools", "get_all_tools", "set_active_tools", "exec", "shutdown",
        )}
        self.context_actions = {name: getattr(self, name) for name in (
            "compact", "get_context_usage", "has_pending_messages", "abort", "is_idle", "is_project_trusted",
            "get_system_prompt", "get_system_prompt_options",
        )}
        self.context_actions["session_manager"] = lambda: self

    def _live(self) -> QueryTurnContext:
        if self.query is None:
            raise RuntimeError("This extension execution has ended; use the next query's context.")
        return self.query

    def close(self) -> None:
        self.query = None
        self.tool_context = None

    @property
    def data(self) -> dict:
        return self._builder.extension_state

    def changed(self, change: dict) -> None:
        updated = extension_values(apply_value_change(self._recorded_state, change))
        undo = value_change(updated, self._recorded_state)
        if undo is not None:
            timeline = self.data[REWIND_KEY]
            index = len(timeline["changes"])
            item = {"history_end": max(len(self._builder._history), self._builder._extension_history_floor), "undo": undo}
            timeline["changes"].append(item)
            rewind_change = {
                "fields": {"changes": {"items": {str(index): {"value": item}}, "length": index + 1}}, "remove": [],
            }
            change = {**change, "fields": {**change["fields"], REWIND_KEY: rewind_change}}
        self._recorded_state = updated
        self._revision += 1
        self._changes.append(change)
        self._builder.extension_cursor = {"run_id": self._live().metadata["run_id"], "revision": self._revision}

    def _set_field(self, key: str, value: Any) -> None:
        self.changed({"fields": {key: {"value": value}}, "remove": []})

    def _append(self, key: str, value: Any) -> None:
        items = self.data.setdefault(key, [])
        index = len(items)
        items.append(value)
        change = {"value": [value]} if index == 0 else {"items": {str(index): {"value": value}}, "length": index + 1}
        self.changed({"fields": {key: change}, "remove": []})

    async def flush(self) -> None:
        async with self._flush_lock:
            if self._revision == self._flushed_revision:
                return
            query = self._live()
            revision = self._revision
            count = len(self._changes)
            changes = self._changes[:count]
            if query.run_context.execution_journal is not None:
                await to_thread_cancel_safe(query.run_context.execution_journal.append_once,
                    "system", {"lifecycle": "extension_state_delta", "extension_changes": changes,
                        "conversation_id": query.state.conversation_id,
                        "run_id": query.metadata["run_id"], "message_id": query.metadata.get("assistant_message_id", ""),
                        "base_revision": self._flushed_revision, "revision": revision},
                    event_id=f"{self._commit_prefix}_{revision}")
            del self._changes[:count]
            self._flushed_revision = revision

    def send_message(self, message: Any, options: Any = None) -> None:
        self._live()
        content = _message_text(message)
        self._append("pending_messages", content)

    def send_user_message(self, content: Any, options: Any = None) -> Any:
        query = self._live()
        options = options or {}
        mode = str(options.get("deliverAs", options.get("deliver_as", "steer"))).lower()
        if mode not in {"steer", "followup"}:
            raise ValueError("deliverAs must be steer or followUp")
        command = UserCommand("user_message", {"content": _message_text(content),
            "conversation_id": query.state.conversation_id, "user_message_id": "user_extension_" + uuid4().hex,
            "assistant_message_id": "assistant_extension_" + uuid4().hex, "streaming_behavior": mode})
        if mode == "steer":
            accepted = query.run_context.turn_input_queue.enqueue_command(command)
            if accepted is not None:
                return accepted
        if query.run_context.enqueue_extension_followup is not None:
            return query.run_context.enqueue_extension_followup(command)
        self._append("followups", deepcopy(command.data))
        return command

    def append_entry(self, custom_type: str, data: Any = None) -> str:
        self._live()
        # JSON is the durable extension API boundary, not a model message.
        entry = json.loads(json.dumps({"id": "ext_" + uuid4().hex, "type": "custom",
            "custom_type": str(custom_type), "data": data}, ensure_ascii=False, allow_nan=False))
        self._append("entries", entry)
        return entry["id"]

    def get_entries(self) -> list[dict]:
        return deepcopy(self.data.get("entries", []))

    def set_label(self, entry_id: str, label: str | None = None) -> None:
        self._live()
        first = "labels" not in self.data
        labels = self.data.setdefault("labels", {})
        if label is None:
            labels.pop(entry_id, None)
            change = {"fields": {}, "remove": [entry_id]}
        else:
            labels[entry_id] = str(label)
            change = {"fields": {entry_id: {"value": str(label)}}, "remove": []}
        self.changed({"fields": {"labels": {"value": dict(labels)} if first else change}, "remove": []})

    def get_label(self, entry_id: str) -> str | None:
        return self.data.get("labels", {}).get(entry_id)

    def set_session_name(self, name: str) -> None:
        query = self._live()
        normalized = str(name).strip()
        if query.run_context.extension_name_setter is not None:
            query.run_context.extension_name_setter(normalized)
        self.data["name"] = normalized
        self._set_field("name", normalized)

    def get_session_name(self) -> str:
        return str(self.data.get("name", ""))

    def get_active_tools(self) -> list[str]:
        query = self._live()
        context = query.run_context.lifecycle_runtime.execution_tool_context
        if context is not None:
            policy = context.metadata[ACTIVE_TOOLSET_POLICY_METADATA_KEY]
            return [name for name in context.tool_registry.list_tools()
                    if policy.is_directly_visible(context.tool_registry.get_tool_spec(name))]
        registry = query.run_context.active_tool_registry or query.session.tool_registry
        selected = query.session.active_tool_names
        if selected is not None:
            return list(selected)
        policy = query.run_context.session_toolset_policy or ToolsetPolicy.default()
        return [name for name in registry.list_tools()
                if policy.is_directly_visible(registry.get_tool_spec(name))]

    def get_all_tools(self) -> list[dict]:
        query = self._live()
        context = query.run_context.lifecycle_runtime.execution_tool_context
        registry = context.tool_registry if context is not None else query.run_context.active_tool_registry or query.session.tool_registry
        source_info = getattr(query.run_context.lifecycle_runtime, "source_info_for_tool", None)
        tools = []
        for name in registry.list_tools():
            tool = registry.get_tool(name)
            schema = tool.model_schema() or tool.get_schema()
            definition = getattr(tool, "definition", tool)
            guidelines = getattr(definition, "prompt_guidelines", ())
            source = source_info(name) if source_info is not None else None
            tools.append({"name": name, "description": schema.description, "parameters": schema.parameters,
                "promptGuidelines": list(guidelines) if guidelines else None,
                "sourceInfo": source or {"path": f"<builtin:{name}>", "source": "builtin", "scope": "temporary", "origin": "top-level"}})
        return tools

    def set_active_tools(self, names: list[str]) -> list[str]:
        query = self._live()
        # Selection is a next-step control action, so newly registered tools
        # must be selectable while the current step still uses its old plan.
        available = set(query.session.tool_registry.list_tools())
        selected = list(dict.fromkeys(name for name in names if name in available))
        query.session.active_tool_names = tuple(selected)
        if query.run_context.extension_tool_selection_setter is not None:
            query.run_context.extension_tool_selection_setter(selected)
        return selected

    def abort(self) -> None:
        self._live().cancel_event.set()

    def shutdown(self) -> bool:
        self.abort()
        return True

    def is_idle(self) -> bool:
        return not self._live().session.active_turn

    def is_project_trusted(self) -> bool:
        from backend.workspace.trust import is_workspace_trusted
        root = self._live().state.workspace_root
        return root is not None and is_workspace_trusted(root)

    def has_pending_messages(self) -> bool:
        query = self._live()
        return bool(self.data.get("pending_messages") or self.data.get("followups")
                    or query.run_context.turn_input_queue.snapshot())

    def get_context_usage(self) -> dict:
        query = self._live()
        usage = query.context_builder.get_budget_snapshot(query.state, tool_schemas=self.tool_schemas)
        return {"tokens": usage["used"], "contextWindow": usage["total"],
                "percent": 100 * usage["used"] / usage["total"]}

    def get_system_prompt(self) -> str:
        query = self._live()
        return str(query.metadata.get("_extension_system_prompt") or query.metadata.get("system_prompt") or "")

    def get_system_prompt_options(self) -> dict:
        return {"cwd": str(self._live().state.workspace_root or "")}

    async def exec(self, command: str, args: Any = None, options: Any = None) -> ToolResult:
        import os
        import shlex
        import subprocess

        query = self._live()
        if query.state.terminal_status is not None:
            raise RuntimeError("Extension commands must finish before the query reaches terminal state")
        context = query.run_context.lifecycle_runtime.execution_tool_context or self.tool_context
        if context is None:
            raise RuntimeError("Extension exec requires the query's tool execution phase")
        argv = [str(command), *(str(value) for value in (args or ()))]
        command_text = (subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)) if args else str(command)
        call = ToolCallEvent(id="extension_exec_" + uuid4().hex, name="run_command",
            arguments={"command": command_text, **dict(options or {})})
        results = []
        source = ToolCallSource(kind="extension", parent_call_id=context.tool_call_id)
        async def publish(kind, data):
            await publish_tool_event(query.run_context, AgentEvent(kind, data), source)
        nested = replace(context, result_sink=lambda _call, result: results.append(result),
            call_sources={call.id: source}, emit_event=publish)
        registry = context.tool_registry
        runner = ToolBatchRunner(ctx=query.context_builder, state=query.state,
            tool_registry=registry, permission_checker=query.session.permission_checker,
            approval_handler=query.session.approval_handler, skill_manager=query.skill_manager, permission_context=context.permission, tool_ctx=nested)
        gate = query.run_context.tool_execution_gate
        async with gate.suspend() if gate is not None else nullcontext():
            async with aclosing(runner.run([call])) as events:
                async for event in events:
                    await publish_tool_event(query.run_context, event, source)
        return results[0]

    async def compact(self, options: Any = None) -> str:
        from backend.services.context_budget import build_context_compacted_event, context_ledger_snapshot

        query = self._live()
        if query.state.terminal_status is not None:
            raise RuntimeError("Extension compaction must run before the query reaches terminal state")
        options = options or {}
        before = context_ledger_snapshot(query.context_builder)
        summary = await query.context_builder.compact(
            focus=str(options.get("customInstructions") or options.get("focus") or ""), restore_state=query.state)
        event = build_context_compacted_event(summary, before, context_ledger_snapshot(query.context_builder))
        await query.run_context.publish_nested_event(event)
        return summary
