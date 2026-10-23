"""Source V2 tool contracts backed by MiniCode's shared agent control plane."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

from backend.agents.source_roles import source_agent_options
from backend.llm.responses_input import agent_message_response_item
from backend.tools.agent_control_plane import AgentControlPlane, CHILD_AGENT_OPTIONS_METADATA_KEY, normalize_agent_fork_turns, normalize_agent_task_name
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec
from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY


def source_agent_status(record, control):
    status = record["status"]
    if status == "pending":
        return "pending_init"
    if status == "running":
        return "running"
    if status in {"cancelled", "interrupted"}:
        return "interrupted"
    agent_id = record.get("subagent_id")
    snapshot = control.subagent_snapshot(agent_id, include_result=True) if agent_id else None
    result = snapshot.get("result", {}) if snapshot is not None else {}
    if status in {"completed", "partial"}:
        return {"completed": result.get("content") or None}
    return {"errored": result.get("error") or record.get("summary") or status}


async def wait_for_coordination(context, *, timeout_seconds: float, agent_ids=()):
    control = AgentControlPlane(context, source_v2=True)
    cursor = context.metadata.get("_agent_wait_cursor", control.activity_cursor())
    queue = context.run_context.turn_input_queue
    if queue is not None and queue.pending_count():
        return {"kind": "steer"}
    if queue is not None and queue.has_message_board_notifications():
        return {"kind": "message"}
    if agent_ids and any((control.subagent_snapshot(agent_id, include_result=False) or {}).get("status") not in {"pending", "running"} for agent_id in agent_ids):
        return {"kind": "completed"}
    async def incoming_activity():
        after_seq = cursor
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            event = await control.wait_for_activity(list(agent_ids), after_seq=after_seq,
                timeout_seconds=deadline - time.monotonic(), kinds=("message", "completed", "cancel_requested"))
            if event is None:
                return None
            after_seq = event["seq"]
            root_delivery = not control.actor_is_subagent and any(
                (record := control.runtime.get_run(recipient)) is not None and record.conversation_id == control.conversation_id
                for recipient in event.get("recipient_ids", ()))
            if event["kind"] != "message" or not event.get("recipient_ids") or control.actor_id in event["recipient_ids"] or root_delivery:
                return event
        return None
    waiters = {asyncio.create_task(incoming_activity()): "agent"}
    if agent_ids:
        waiters[asyncio.create_task(control.wait_for_any(list(agent_ids), timeout_seconds=timeout_seconds))] = "completion"
    if queue is not None:
        waiters[asyncio.create_task(queue.wait_for_activity(after_seq=queue.activity_cursor(), timeout=timeout_seconds))] = "steer"
    if context.cancel_event is not None:
        waiters[asyncio.create_task(context.cancel_event.wait())] = "cancel"
    try:
        done, _ = await asyncio.wait(waiters, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
        for waiter in done:
            outcome = waiter.result()
            if outcome:
                return outcome if waiters[waiter] == "agent" else {"kind": "completed" if waiters[waiter] == "completion" else waiters[waiter]}
        return None
    finally:
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        context.metadata["_agent_wait_cursor"] = control.activity_cursor()


class CollaborationTool(BaseTool):
    should_defer = False
    permission = PermissionLevel.AUTO
    read_only = True
    idempotent = False
    result_kind = "subagent"
    activity_kind = "genericTool"
    timeout_seconds = None

    def get_spec(self):
        return ToolSpec(name=self.name, capability="agent.coordinate", toolset="agent", exposure="core")


class SpawnAgentTool(CollaborationTool):
    name = "spawn_agent"
    description = "Start a child agent and return its identity immediately. Full parent history is inherited by default; use fork_turns=none for independent context or a positive integer for recent turns."
    display_label = "Start subagent"

    def __init__(self, task_tool):
        self.task_tool = task_tool

    def get_spec(self):
        return ToolSpec(name=self.name, capability="agent.delegate", toolset="agent", exposure="core",
            required_args=("message", "task_name"))

    def get_schema(self):
        task = self.task_tool.get_schema().parameters["properties"]
        return ToolSchema(name=self.name, description=self.description, parameters={"type": "object",
            "properties": {"message": {"type": "string", "description": "Complete task instructions."},
                "task_name": {"type": "string", "description": "Canonical child task name."},
                "fork_turns": {"type": "string", "description": "all (default), none, or a positive integer string."},
                **{name: task[name] for name in ("agent_type", "model", "reasoning_effort")}},
            "required": ["task_name", "message"], "additionalProperties": False})

    async def execute(self, args, context=None):
        task_name = normalize_agent_task_name(args["task_name"], required=True)
        fork = normalize_agent_fork_turns(args.get("fork_turns"), default="all")
        child_context = context
        prompt = args["message"]
        if "source_agent_message_encrypted" in context.metadata:
            if not prompt.strip():
                return self._error_result("Missing message argument")
            author = AgentControlPlane(context, source_v2=True).actor_public_tree_path()
            encrypted = context.metadata["source_agent_message_encrypted"]
            item = agent_message_response_item(author=author, recipient=f"{author}/{task_name}",
                content="" if encrypted else prompt, encrypted_content=prompt if encrypted else None,
                trigger_turn=True)
            child_context = replace(context, metadata={**context.metadata, CHILD_AGENT_OPTIONS_METADATA_KEY: {
                **context.metadata.get(CHILD_AGENT_OPTIONS_METADATA_KEY, {}),
                "agent_input_messages": [{"content": "", "provider_items": [item]}],
            }})
            prompt = ""
        result = await self.task_tool.execute({"description": task_name, "prompt": prompt,
            "task_name": task_name, "fork_turns": fork, "run_in_background": True,
            **{name: args[name] for name in ("agent_type", "model", "reasoning_effort") if name in args}}, child_context)
        if result.is_error:
            return result
        agent_id = result.runtime_metadata["subagent_id"]
        control = AgentControlPlane(context, source_v2=True)
        output = {"task_name": control.public_tree_path(agent_id)}
        policy = context.metadata.get(ACTIVE_TOOLSET_POLICY_METADATA_KEY)
        options = policy.coordination_options if policy is not None else None
        if options is not None and not options["hide_spawn_agent_metadata"]:
            output["nickname"] = None
        return replace(result, content=json.dumps(output))


class ListAgentsTool(CollaborationTool):
    name = "list_agents"
    description = "List agents in this task tree, optionally filtered by canonical task-path prefix."
    display_label = "Check agents"

    def get_schema(self):
        return ToolSchema(name=self.name, description=self.description, parameters={"type": "object",
            "properties": {"path_prefix": {"type": "string"}}, "additionalProperties": False})

    async def execute(self, args, context=None):
        control = AgentControlPlane(context, source_v2=True)
        if not control.can_use_operation("observe"):
            return self._error_result("Current agent cannot inspect this task tree")
        prefix = args.get("path_prefix", "")
        records = control.select_agents(scope="tree", path_prefix=prefix)
        agents = [{"agent_name": control.public_tree_path(record), "agent_status": source_agent_status(record, control)} for record in records]
        if not prefix or "/root".startswith(prefix):
            root = control.root_tree_record()
            if root is not None:
                agents.insert(0, {"agent_name": "/root", "agent_status": source_agent_status(root, control)})
        return ToolResult(content=json.dumps({"agents": agents}), result_kind="subagent")


class WaitAgentTool(CollaborationTool):
    name = "wait_agent"
    description = "Wait for agent mailbox activity or completion. New user input ends the wait early without consuming that input."
    display_label = "Wait for agents"

    def __init__(self, config):
        self.options = source_agent_options(config)

    def get_schema(self):
        return ToolSchema(name=self.name, description=self.description, parameters={"type": "object",
            "properties": {"timeout_ms": {"type": "number", "description":
                f"Timeout in milliseconds. Defaults to {self.options['default_wait_timeout_ms']}, "
                f"min {self.options['min_wait_timeout_ms']}, max {self.options['max_wait_timeout_ms']}."}},
            "additionalProperties": False})

    async def execute(self, args, context=None):
        control = AgentControlPlane(context, source_v2=True)
        if not control.can_use_operation("wait"):
            return self._error_result("Current agent cannot wait on this task tree")
        requested = args.get("timeout_ms", self.options["default_wait_timeout_ms"])
        if requested > self.options["max_wait_timeout_ms"]:
            return self._error_result(f"timeout_ms must be at most {self.options['max_wait_timeout_ms']}")
        outcome = await wait_for_coordination(context, timeout_seconds=max(requested, self.options["min_wait_timeout_ms"]) / 1000)
        message = "Wait timed out." if outcome is None else "Wait interrupted by new input." if outcome["kind"] == "steer" else "Wait completed."
        return ToolResult(content=json.dumps({"message": message, "timed_out": outcome is None}), result_kind="subagent")


class InterruptAgentTool(CollaborationTool):
    name = "interrupt_agent"
    description = "Interrupt another agent and return its previous status. The agent remains addressable for follow-up tasks."
    display_label = "Interrupt agent"

    def get_schema(self):
        return ToolSchema(name=self.name, description=self.description, parameters={"type": "object",
            "properties": {"target": {"type": "string"}}, "required": ["target"], "additionalProperties": False})

    async def execute(self, args, context=None):
        control = AgentControlPlane(context, source_v2=True)
        if not control.can_use_operation("interrupt"):
            return self._error_result("Current agent cannot interrupt this task")
        if control.normalize_public_tree_path(args["target"]) == "/root":
            return self._error_result("root is not a spawned agent")
        target = control.resolve_target(args["target"], scope="tree")
        if target is None:
            return self._error_result("Agent is not part of the current task tree")
        if target.subagent_id == control.actor_id:
            return self._error_result("An agent cannot interrupt itself; return the result to its parent")
        outcome = control.interrupt(target, reason="interrupted")
        return ToolResult(content=json.dumps({"previous_status": source_agent_status(target.record, control)}), result_kind="subagent",
            runtime_metadata={"recipient_id": target.subagent_id, "previous_status": outcome.previous_status})
