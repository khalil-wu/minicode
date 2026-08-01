"""Shared swarm coordination tools for parent agents and subagents."""

from __future__ import annotations

from typing import Any

from backend.agent.runtime import AgentRuntime, SwarmTaskStatus, default_runtime
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.subagent_runtime import runtime_from_context

TASK_STATUSES = ("pending", "in_progress", "blocked", "completed", "cancelled")

# Subagent statuses with no live task loop: mail sent to these recipients is
# persisted but never consumed unless the agent is resumed (cc SendMessageTool
# auto-resumes stopped agents from their transcript).
_ENDED_SUBAGENT_STATUSES = frozenset(
    {"completed", "partial", "failed", "cancelled", "interrupted"}
)


def _runtime(context: ToolExecutionContext | None) -> AgentRuntime:
    return runtime_from_context(context) or default_runtime()


def _actor_id(context: ToolExecutionContext | None, explicit: Any = None) -> str:
    candidate = str(explicit or "").strip()
    if candidate:
        return candidate
    if context is not None:
        metadata = context.metadata if isinstance(context.metadata, dict) else {}
        for key in ("run_id", "agent_id", "parent_run_id"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value
        if context.task_id:
            return context.task_id
    return "main"


def _actor_mailbox_epoch(context: ToolExecutionContext | None) -> int | None:
    if context is None or not isinstance(context.metadata, dict):
        return None
    raw = context.metadata.get("mailbox_epoch")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _message_recipient_allowed(
    runtime: AgentRuntime,
    *,
    sender: str,
    recipient: str,
    conversation_id: str,
) -> bool:
    if recipient == "parent":
        return sender != "main"
    target = runtime.get_subagent(recipient)
    if target is None:
        # Persist coordinator mail even if the worker start event has not been
        # committed yet; the worker can consume it after registration.
        parent_run = runtime.get_run(sender)
        return recipient.startswith("subagent-") and sender != "main" and (
            parent_run is None
            or not conversation_id
            or parent_run.conversation_id == conversation_id
        )
    sender_record = runtime.get_subagent(sender)
    if sender_record is not None:
        return bool(sender_record.parent_run_id) and sender_record.parent_run_id == target.parent_run_id
    parent_run = runtime.get_run(sender)
    target_parent_run = runtime.get_run(str(target.parent_run_id or ""))
    same_conversation_handoff = bool(
        parent_run is not None
        and target_parent_run is not None
        and parent_run.conversation_id
        and parent_run.conversation_id == target_parent_run.conversation_id
        and (target.background or target.detach_from_parent)
    )
    return bool(
        (target.parent_run_id == sender or same_conversation_handoff)
        and (
            parent_run is None
            or not conversation_id
            or parent_run.conversation_id == conversation_id
        )
    )


async def _emit_swarm_event(
    context: ToolExecutionContext | None,
    *,
    subagent_id: str,
    event: dict[str, Any],
) -> None:
    emit = context.emit_event if context else None
    if emit is None:
        return
    payload = {
        "subagent_id": subagent_id or "swarm",
        "event": event,
    }
    await emit("subagent.event", payload)


def _conversation_id(context: ToolExecutionContext | None) -> str:
    return str(getattr(context, "conversation_id", "") or "").strip()


def _task_lines(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "No shared swarm tasks matched."
    lines = [f"{len(tasks)} shared swarm task(s):"]
    for index, task in enumerate(tasks, 1):
        assignee = f" -> {task.get('assignee')}" if task.get("assignee") else ""
        outputs = task.get("outputs") if isinstance(task.get("outputs"), list) else []
        lines.append(
            f"{index}. {task.get('task_id')} [{task.get('status')}] "
            f"{task.get('title')}{assignee} ({len(outputs)} output(s))"
        )
        blocks = task.get("blocks") if isinstance(task.get("blocks"), list) else []
        blocked_by = task.get("blocked_by") if isinstance(task.get("blocked_by"), list) else []
        if blocks or blocked_by:
            lines.append(
                f"   deps: blocks={','.join(map(str, blocks)) or '-'} "
                f"blocked_by={','.join(map(str, blocked_by)) or '-'}"
            )
    return "\n".join(lines)


def _team_lines(teams: list[dict[str, Any]]) -> str:
    if not teams:
        return "No swarm teams matched."
    lines = [f"{len(teams)} swarm team(s):"]
    for index, team in enumerate(teams, 1):
        members = team.get("members") if isinstance(team.get("members"), list) else []
        lines.append(
            f"{index}. {team.get('team_name')} ({len(members)} member(s)) "
            f"seq={team.get('seq')}"
        )
        for member in members:
            if not isinstance(member, dict):
                continue
            role = f": {member.get('role')}" if member.get("role") else ""
            agent_type = f" [{member.get('agent_type')}]" if member.get("agent_type") else ""
            lines.append(f"   - {member.get('id')}{role}{agent_type}")
    return "\n".join(lines)


class SendMessageTool(BaseTool):
    """Send a coordination message between agents."""

    name = "send_message"
    should_defer = False
    description = (
        "Send a coordination message to another agent or the parent agent. "
        "Use for parent-to-subagent or subagent-to-parent updates that should be visible in the Agents panel."
    )
    permission = PermissionLevel.AUTO
    result_kind = "subagent"
    activity_kind = "genericTool"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "description": "Target teammate name/agent_type, agent id, subagent id, or 'parent'.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Concise message to deliver.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Optional 5-10 word summary shown as a preview in the Agents panel.",
                    },
                    "sender": {
                        "type": "string",
                        "description": "Optional sender id. Defaults to the current run/subagent id.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Optional shared swarm task id this message concerns.",
                    },
                    "team_name": {
                        "type": "string",
                        "description": "Optional logical team name.",
                    },
                },
                "required": ["recipient", "message"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        recipient = str(args.get("recipient") or "").strip()
        message = str(args.get("message") or "").strip()
        if not recipient:
            return self._error_result("Missing recipient argument")
        if not message:
            return self._error_result("Missing message argument")

        runtime = _runtime(context)
        # Resolve by-name addressing before authorization: a recipient that is
        # not a live subagent id and not "parent" may be a teammate name /
        # agent_type label (latest-wins registry, cc-style).
        if recipient != "parent" and runtime.get_subagent(recipient) is None:
            resolve = getattr(runtime, "resolve_subagent_name", None)
            resolved = resolve(recipient) if callable(resolve) else ""
            if resolved:
                recipient = resolved
        derived_sender = _actor_id(context)
        explicit_sender = str(args.get("sender") or "").strip()
        sender = _actor_id(context, explicit_sender)
        conversation_id = _conversation_id(context)
        summary = str(args.get("summary") or "").strip()
        if explicit_sender and explicit_sender != derived_sender:
            return self._error_result("Sender must match the current task identity")
        if not _message_recipient_allowed(
            runtime,
            sender=sender,
            recipient=recipient,
            conversation_id=conversation_id,
        ):
            return self._error_result("Recipient is not part of the current task tree")
        recipient_record = runtime.get_subagent(recipient)
        recipient_ended = bool(
            recipient_record is not None
            and str(recipient_record.status or "") in _ENDED_SUBAGENT_STATUSES
        )
        recipient_epoch = (
            None
            if recipient_ended
            else int(recipient_record.mailbox_epoch or 0)
            if recipient_record is not None
            else None
        )
        try:
            record = runtime.send_swarm_message(
                sender_id=sender,
                recipient_id=recipient,
                content=message,
                conversation_id=conversation_id,
                team_name=str(args.get("team_name") or "").strip(),
                task_id=str(args.get("task_id") or "").strip(),
                sender_mailbox_epoch=_actor_mailbox_epoch(context),
                recipient_mailbox_epoch=recipient_epoch,
            )
        except ValueError as exc:
            return self._error_result(str(exc))
        payload = record.to_dict()
        if summary:
            payload["summary"] = summary
        await _emit_swarm_event(
            context,
            subagent_id=recipient if recipient != "parent" else sender,
            event={"type": "message", "message": payload},
        )

        if recipient_ended:
            return ToolResult(
                content=(
                    f"Message {record.message_id} sent from {sender} to {recipient}. "
                    f"Recipient has already ended ({recipient_record.status}); "
                    "the message was stored but was not delivered to a running agent."
                ),
                display_summary=summary or f"Message to {recipient}",
                result_kind="subagent",
            )

        return ToolResult(
            content=f"Message {record.message_id} sent from {sender} to {recipient}.",
            display_summary=summary or f"Message to {recipient}",
            result_kind="subagent",
        )

class MessageListTool(BaseTool):
    """Read the shared swarm mailbox for an agent or team participant."""

    name = "message_list"
    description = (
        "Read shared swarm mailbox messages. Use participant_id to fetch messages for an agent, "
        "and since_seq to poll only messages newer than the last seen sequence."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    result_kind = "subagent"
    activity_kind = "genericTool"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "participant_id": {
                        "type": "string",
                        "description": "Optional sender/recipient id to filter mailbox messages.",
                    },
                    "since_seq": {
                        "type": "integer",
                        "description": "Only return messages with seq greater than this high-water value.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum messages to return, default 20, max 100.",
                    },
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        runtime = _runtime(context)
        actor_id = _actor_id(context)
        actor_record = runtime.get_subagent(actor_id)
        if actor_record is not None and not runtime.accepts_subagent_incarnation(
            actor_id,
            agent_path=str((context.metadata if context and isinstance(context.metadata, dict) else {}).get("agent_path") or ""),
            mailbox_epoch=_actor_mailbox_epoch(context),
            require_running=True,
        ):
            return self._error_result("Stale subagent incarnation cannot read the current mailbox")
        default_participant = actor_id if actor_record is not None else "parent"
        participant_id = str(args.get("participant_id") or default_participant).strip()
        if actor_record is not None and participant_id != actor_id:
            return self._error_result("Subagents may only read their own mailbox")
        since_seq = int(args.get("since_seq") or 0)
        limit = int(args.get("limit") or 20)
        participant_record = runtime.get_subagent(participant_id)
        mailbox_epoch = (
            int(participant_record.mailbox_epoch or 0)
            if participant_record is not None
            else None
        )
        messages = runtime.list_swarm_messages(
            participant_id=participant_id,
            conversation_id=_conversation_id(context),
            since_seq=since_seq,
            limit=limit,
            mailbox_epoch=mailbox_epoch,
        )
        if not messages:
            return ToolResult(content="No swarm mailbox messages matched.", result_kind="subagent")
        lines = [f"{len(messages)} swarm mailbox message(s):"]
        for message in messages:
            lines.append(
                f"{message.seq}. {message.sender_id} -> {message.recipient_id}: {message.content}"
                + (f" (task {message.task_id})" if message.task_id else "")
            )
        lines.append(f"High-water: {max(message.seq for message in messages)}")
        return ToolResult(content="\n".join(lines), result_kind="subagent")


class TeamCreateTool(BaseTool):
    name = "team_create"
    description = "Create or replace a named swarm team with member roles and preferred subagent types."
    permission = PermissionLevel.AUTO
    result_kind = "subagent"
    activity_kind = "genericTool"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Stable team name."},
                    "description": {"type": "string", "description": "Team purpose or coordination notes."},
                    "members": {
                        "type": "array",
                        "description": "Team members with role and preferred agent type.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Stable member id or role id."},
                                "role": {"type": "string", "description": "Human-readable responsibility."},
                                "agent_type": {"type": "string", "description": "Preferred subagent type."},
                                "description": {"type": "string", "description": "Extra member instructions."},
                            },
                            "required": ["id"],
                        },
                    },
                },
                "required": ["team_name", "members"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        team_name = str(args.get("team_name") or "").strip()
        members = args.get("members")
        if not team_name:
            return self._error_result("Missing team_name argument")
        if not isinstance(members, list) or not members:
            return self._error_result("Missing members argument")
        team = _runtime(context).create_swarm_team(
            team_name=team_name,
            description=str(args.get("description") or "").strip(),
            members=[member for member in members if isinstance(member, dict)],
            conversation_id=_conversation_id(context),
            created_by=_actor_id(context),
        )
        payload = team.to_dict()
        await _emit_swarm_event(
            context,
            subagent_id=f"team:{team.team_name}",
            event={"type": "team_created", "team": payload},
        )
        return ToolResult(
            content=f"Created swarm team {team.team_name} with {len(team.members)} member(s).",
            display_summary=f"Team created: {team.team_name}",
            result_kind="subagent",
        )


class TeamListTool(BaseTool):
    name = "team_list"
    description = "List named swarm teams and their member role assignments."
    permission = PermissionLevel.AUTO
    read_only = True
    result_kind = "subagent"
    activity_kind = "genericTool"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Optional team name filter."},
                    "since_seq": {"type": "integer", "description": "Only return teams with seq greater than this high-water value."},
                    "limit": {"type": "integer", "description": "Maximum teams to return, default 50, max 100."},
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        teams = [
            team.to_dict()
            for team in _runtime(context).list_swarm_teams(
                conversation_id=_conversation_id(context),
                team_name=str(args.get("team_name") or "").strip(),
                since_seq=int(args.get("since_seq") or 0),
                limit=int(args.get("limit") or 50),
            )
        ]
        return ToolResult(content=_team_lines(teams), result_kind="subagent")


class TeamDeleteTool(BaseTool):
    name = "team_delete"
    description = "Delete a named swarm team. Existing shared tasks and messages remain for audit/history."
    permission = PermissionLevel.AUTO
    result_kind = "subagent"
    activity_kind = "genericTool"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name to delete."},
                },
                "required": ["team_name"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        team_name = str(args.get("team_name") or "").strip()
        if not team_name:
            return self._error_result("Missing team_name argument")
        team = _runtime(context).delete_swarm_team(
            conversation_id=_conversation_id(context),
            team_name=team_name,
        )
        if team is None:
            return self._error_result(f"Swarm team not found: {team_name}")
        payload = team.to_dict()
        await _emit_swarm_event(
            context,
            subagent_id=f"team:{team.team_name}",
            event={"type": "team_deleted", "team": payload},
        )
        return ToolResult(
            content=f"Deleted swarm team {team.team_name}.",
            display_summary=f"Team deleted: {team.team_name}",
            result_kind="subagent",
        )


class TaskCreateTool(BaseTool):
    name = "task_create"
    description = "Create a shared swarm task that multiple agents can list, claim, update, and attach outputs to."
    permission = PermissionLevel.AUTO
    result_kind = "subagent"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short task title."},
                    "description": {"type": "string", "description": "Detailed task instructions or context."},
                    "assignee": {"type": "string", "description": "Optional agent/subagent id assigned to the task."},
                    "status": {"type": "string", "enum": list(TASK_STATUSES), "description": "Initial task status."},
                    "priority": {"type": "string", "enum": ["low", "normal", "high"], "description": "Task priority."},
                    "team_name": {"type": "string", "description": "Optional logical team name."},
                    "created_by": {"type": "string", "description": "Optional creator id. Defaults to current run/subagent."},
                    "blocks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional task ids that this task blocks.",
                    },
                    "blocked_by": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional task ids that must complete before this task can proceed.",
                    },
                },
                "required": ["title"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        title = str(args.get("title") or "").strip()
        if not title:
            return self._error_result("Missing title argument")
        raw_status = str(args.get("status") or "pending").strip()
        status: SwarmTaskStatus = raw_status if raw_status in TASK_STATUSES else "pending"  # type: ignore[assignment]
        task = _runtime(context).create_swarm_task(
            title=title,
            description=str(args.get("description") or "").strip(),
            assignee=str(args.get("assignee") or "").strip(),
            conversation_id=_conversation_id(context),
            status=status,
            priority=str(args.get("priority") or "normal").strip() or "normal",
            team_name=str(args.get("team_name") or "").strip(),
            created_by=_actor_id(context, args.get("created_by")),
            blocks=[str(item).strip() for item in args.get("blocks", []) if str(item).strip()] if isinstance(args.get("blocks"), list) else None,
            blocked_by=[str(item).strip() for item in args.get("blocked_by", []) if str(item).strip()] if isinstance(args.get("blocked_by"), list) else None,
        )
        payload = task.to_dict()
        await _emit_swarm_event(
            context,
            subagent_id=task.assignee or "swarm",
            event={"type": "task_created", "task": payload},
        )
        return ToolResult(
            content=f"Created shared swarm task {task.task_id}: {task.title}",
            display_summary=f"Task created: {task.title}",
            result_kind="subagent",
        )


class TaskListTool(BaseTool):
    name = "task_list"
    description = "List shared swarm tasks, optionally filtered by assignee, status, or team."
    permission = PermissionLevel.AUTO
    read_only = True
    result_kind = "subagent"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "assignee": {"type": "string", "description": "Optional assignee filter."},
                    "status": {"type": "string", "enum": list(TASK_STATUSES), "description": "Optional status filter."},
                    "team_name": {"type": "string", "description": "Optional team filter."},
                    "since_seq": {
                        "type": "integer",
                        "description": "Only return tasks with seq greater than this high-water value.",
                    },
                    "limit": {"type": "integer", "description": "Maximum tasks to return, default 50, max 100."},
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        limit = int(args.get("limit") or 50)
        tasks = [
            task.to_dict()
            for task in _runtime(context).list_swarm_tasks(
                assignee=str(args.get("assignee") or "").strip(),
                status=str(args.get("status") or "").strip(),
                team_name=str(args.get("team_name") or "").strip(),
                conversation_id=_conversation_id(context),
                since_seq=int(args.get("since_seq") or 0),
                limit=limit,
            )
        ]
        return ToolResult(
            content=_task_lines(tasks),
            result_kind="subagent",
        )


class TaskGetTool(BaseTool):
    name = "task_get"
    description = "Read one shared swarm task, including any attached outputs."
    permission = PermissionLevel.AUTO
    read_only = True
    result_kind = "subagent"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Shared swarm task id."},
                },
                "required": ["task_id"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return self._error_result("Missing task_id argument")
        task = _runtime(context).get_swarm_task(task_id)
        if task is None:
            return self._error_result(f"Shared swarm task not found: {task_id}")
        data = task.to_dict()
        lines = [
            f"{data['task_id']} [{data['status']}] {data['title']}",
            f"Assignee: {data.get('assignee') or 'unassigned'}",
            f"Priority: {data.get('priority') or 'normal'}",
        ]
        blocks = data.get("blocks") if isinstance(data.get("blocks"), list) else []
        blocked_by = data.get("blocked_by") if isinstance(data.get("blocked_by"), list) else []
        if blocks or blocked_by:
            lines.append(f"Blocks: {', '.join(map(str, blocks)) or '-'}")
            lines.append(f"Blocked by: {', '.join(map(str, blocked_by)) or '-'}")
        if data.get("description"):
            lines.append(f"Description: {data['description']}")
        outputs = data.get("outputs") if isinstance(data.get("outputs"), list) else []
        if outputs:
            lines.append("Outputs:")
            for output in outputs[-10:]:
                lines.append(f"- {output.get('author_id')}: {output.get('content')}")
        return ToolResult(content="\n".join(lines), result_kind="subagent")


class TaskUpdateTool(BaseTool):
    name = "task_update"
    description = "Update a shared swarm task's status, assignee, priority, title, or description."
    permission = PermissionLevel.AUTO
    result_kind = "subagent"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Shared swarm task id."},
                    "status": {"type": "string", "enum": list(TASK_STATUSES), "description": "New task status."},
                    "assignee": {"type": "string", "description": "New assignee."},
                    "priority": {"type": "string", "enum": ["low", "normal", "high"], "description": "New priority."},
                    "title": {"type": "string", "description": "New title."},
                    "description": {"type": "string", "description": "New description."},
                    "team_name": {"type": "string", "description": "New team name."},
                    "blocks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Replace task ids that this task blocks.",
                    },
                    "blocked_by": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Replace task ids that must complete before this task can proceed.",
                    },
                },
                "required": ["task_id"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return self._error_result("Missing task_id argument")
        patch = {
            key: args[key]
            for key in ("status", "assignee", "priority", "title", "description", "team_name", "blocks", "blocked_by")
            if key in args
        }
        if not patch:
            return self._error_result("No task fields provided to update")
        task = _runtime(context).update_swarm_task(task_id, patch)
        if task is None:
            return self._error_result(f"Shared swarm task not found: {task_id}")
        payload = task.to_dict()
        await _emit_swarm_event(
            context,
            subagent_id=task.assignee or "swarm",
            event={"type": "task_updated", "task": payload},
        )
        return ToolResult(
            content=f"Updated shared swarm task {task.task_id}: {task.status}",
            display_summary=f"Task updated: {task.title}",
            result_kind="subagent",
        )


class TaskOutputTool(BaseTool):
    name = "task_output"
    description = "Attach an output, finding, or handoff note to a shared swarm task."
    permission = PermissionLevel.AUTO
    result_kind = "subagent"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Shared swarm task id."},
                    "content": {"type": "string", "description": "Output or finding to attach."},
                    "author": {"type": "string", "description": "Optional author id. Defaults to current run/subagent."},
                    "status": {
                        "type": "string",
                        "enum": list(TASK_STATUSES),
                        "description": "Optional status to set after attaching output.",
                    },
                },
                "required": ["task_id", "content"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        task_id = str(args.get("task_id") or "").strip()
        content = str(args.get("content") or "").strip()
        if not task_id:
            return self._error_result("Missing task_id argument")
        if not content:
            return self._error_result("Missing content argument")
        runtime = _runtime(context)
        task = runtime.append_swarm_task_output(
            task_id,
            author_id=_actor_id(context, args.get("author")),
            content=content,
        )
        if task is None:
            return self._error_result(f"Shared swarm task not found: {task_id}")
        raw_status = str(args.get("status") or "").strip()
        if raw_status:
            task = runtime.update_swarm_task(task_id, {"status": raw_status}) or task
        payload = task.to_dict()
        await _emit_swarm_event(
            context,
            subagent_id=task.assignee or "swarm",
            event={"type": "task_output", "task": payload, "output": payload["outputs"][-1]},
        )
        return ToolResult(
            content=f"Attached output to shared swarm task {task.task_id}.",
            display_summary=f"Task output: {task.title}",
            result_kind="subagent",
        )
