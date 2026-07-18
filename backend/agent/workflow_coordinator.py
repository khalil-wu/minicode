"""Workflow coordination state separated from AgentRuntime persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

WorkflowLauncher = Callable[[list[Any]], Awaitable[Any]]


class WorkflowRuntimePort(Protocol):
    """Narrow persistence/metrics surface required by workflow algorithms."""

    def list_workflow_tasks(self, workflow_id: str, conversation_id: str) -> list[Any]: ...

    def update_workflow_task(self, task_id: str, patch: dict[str, Any]) -> Any | None: ...

    def write_workflow_metric(self, event: str, payload: dict[str, Any]) -> None: ...


class WorkflowCoordinator:
    """Owns workflow launchers, serialization locks, and terminal markers."""

    def __init__(self, *, max_launch_batch: int = 5) -> None:
        self._launchers: dict[str, WorkflowLauncher] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._completed: set[str] = set()
        self._cancelled: set[str] = set()
        self._max_launch_batch = max(1, int(max_launch_batch))

    def register_launcher(self, workflow_id: str, launcher: WorkflowLauncher) -> bool:
        workflow_id = str(workflow_id or "").strip()
        if not workflow_id:
            return False
        self._launchers[workflow_id] = launcher
        return True

    def launcher(self, workflow_id: str) -> WorkflowLauncher | None:
        return self._launchers.get(workflow_id)

    def lock(self, workflow_id: str) -> asyncio.Lock:
        return self._locks.setdefault(workflow_id, asyncio.Lock())

    def is_completed(self, workflow_id: str) -> bool:
        return workflow_id in self._completed

    def mark_completed(self, workflow_id: str) -> None:
        self._completed.add(workflow_id)

    def is_cancelled(self, workflow_id: str) -> bool:
        return workflow_id in self._cancelled

    def mark_cancelled(self, workflow_id: str) -> None:
        self._cancelled.add(workflow_id)

    @staticmethod
    def _ordered(tasks: list[Any]) -> list[Any]:
        return sorted(tasks, key=lambda item: (item.created_at, item.seq, item.task_id))

    @staticmethod
    def _dependencies_complete(task: Any, by_id: dict[str, Any]) -> bool:
        blockers = [item for item in task.blocked_by if item]
        return all(
            by_id.get(blocker) is not None and by_id[blocker].status == "completed"
            for blocker in blockers
        )

    async def _launch_tasks(
        self,
        port: WorkflowRuntimePort,
        tasks: list[Any],
        *,
        rollback_status: str,
        launcher: WorkflowLauncher,
    ) -> tuple[list[Any], bool, str, str]:
        launch_summary = ""
        launch_error = ""
        try:
            launch_result = await launcher(tasks)
            launch_summary = str(getattr(launch_result, "content", launch_result) or "")
            if bool(getattr(launch_result, "is_error", False)):
                launch_error = launch_summary or "Workflow launcher failed"
        except Exception as exc:
            launch_error = str(exc)

        if launch_error:
            tasks = [
                port.update_workflow_task(task.task_id, {"status": rollback_status}) or task
                for task in tasks
            ]
        return tasks, not launch_error, launch_summary, launch_error

    async def resume_pending_workflow(
        self,
        port: WorkflowRuntimePort,
        workflow_id: str,
        *,
        conversation_id: str = "",
    ) -> dict[str, Any]:
        workflow_id = str(workflow_id or "").strip()
        if not workflow_id:
            return {"workflow_id": "", "resumed_tasks": [], "launched": False}

        async with self.lock(workflow_id):
            launcher = self.launcher(workflow_id)
            if launcher is None:
                return {
                    "workflow_id": workflow_id,
                    "resumed_tasks": [],
                    "launched": False,
                    "launch_error": "No workflow launcher registered.",
                }

            tasks = port.list_workflow_tasks(workflow_id, conversation_id)
            by_id = {task.task_id: task for task in tasks}
            ready = [
                task for task in tasks
                if task.status == "pending" and self._dependencies_complete(task, by_id)
            ]
            if not ready:
                return {"workflow_id": workflow_id, "resumed_tasks": [], "launched": False}

            resumed = [
                port.update_workflow_task(task.task_id, {"status": "in_progress"}) or task
                for task in self._ordered(ready)[:self._max_launch_batch]
            ]
            resumed, launched, launch_summary, launch_error = await self._launch_tasks(
                port,
                resumed,
                rollback_status="pending",
                launcher=launcher,
            )
            payload = {
                "workflow_id": workflow_id,
                "resumed_tasks": [task.to_dict() for task in resumed],
                "launched": launched,
                "launch_summary": launch_summary,
                "launch_error": launch_error,
            }
            port.write_workflow_metric("workflow_resumed", payload)
            return payload

    async def advance_workflow(
        self,
        port: WorkflowRuntimePort,
        workflow_id: str,
        *,
        conversation_id: str = "",
    ) -> dict[str, Any]:
        workflow_id = str(workflow_id or "").strip()
        if not workflow_id:
            return {"workflow_id": "", "ready_tasks": [], "launched": False}

        async with self.lock(workflow_id):
            tasks = port.list_workflow_tasks(workflow_id, conversation_id)
            by_id = {task.task_id: task for task in tasks}
            ready = [
                task for task in tasks
                if task.status == "blocked" and self._dependencies_complete(task, by_id)
            ]
            if not ready:
                return {"workflow_id": workflow_id, "ready_tasks": [], "launched": False}

            launcher = self.launcher(workflow_id)
            target_status = "in_progress" if launcher is not None else "pending"
            advanced = [
                port.update_workflow_task(task.task_id, {"status": target_status}) or task
                for task in self._ordered(ready)[:self._max_launch_batch]
            ]
            launched = False
            launch_summary = ""
            launch_error = ""
            if launcher is not None:
                advanced, launched, launch_summary, launch_error = await self._launch_tasks(
                    port,
                    advanced,
                    rollback_status="pending",
                    launcher=launcher,
                )

            payload = {
                "workflow_id": workflow_id,
                "ready_tasks": [task.to_dict() for task in advanced],
                "launched": launched,
                "launch_summary": launch_summary,
                "launch_error": launch_error,
            }
            port.write_workflow_metric("workflow_advanced", payload)
            return payload

    async def cancel_workflow_dependents(
        self,
        port: WorkflowRuntimePort,
        workflow_id: str,
        *,
        conversation_id: str = "",
    ) -> dict[str, Any]:
        workflow_id = str(workflow_id or "").strip()
        if not workflow_id:
            return {"workflow_id": "", "cancelled_tasks": [], "workflow_cancelled": False}

        async with self.lock(workflow_id):
            tasks = port.list_workflow_tasks(workflow_id, conversation_id)
            by_id = {task.task_id: task for task in tasks}
            cancelled_ids = {task.task_id for task in tasks if task.status == "cancelled"}
            newly_cancelled: list[Any] = []
            changed = True
            while changed:
                changed = False
                for task in self._ordered(list(by_id.values())):
                    if task.status not in {"blocked", "pending"}:
                        continue
                    if any(blocker in cancelled_ids for blocker in task.blocked_by):
                        updated = port.update_workflow_task(task.task_id, {"status": "cancelled"}) or task
                        by_id[updated.task_id] = updated
                        cancelled_ids.add(updated.task_id)
                        newly_cancelled.append(updated)
                        changed = True

            snapshot = self.workflow_completion_snapshot(
                port,
                workflow_id,
                conversation_id=conversation_id,
            )
            fatal = any(
                isinstance(task, dict)
                and bool(task.get("required_for_final", True))
                and str(task.get("status") or "") == "cancelled"
                for task in snapshot.get("tasks", [])
            )
            workflow_cancelled = fatal and not self.is_completed(workflow_id) and not self.is_cancelled(workflow_id)
            if workflow_cancelled:
                self.mark_cancelled(workflow_id)

            name = str(snapshot.get("workflow_name") or workflow_id)
            cancelled_total = int(snapshot.get("cancelled_total") or 0)
            summary = f"{name} cancelled: {cancelled_total} node(s) cancelled."
            result_lines = [summary]
            for task in snapshot.get("tasks", []):
                if not isinstance(task, dict) or str(task.get("status") or "") != "cancelled":
                    continue
                node = str(task.get("node_id") or task.get("task_id") or "").strip()
                title = str(task.get("title") or task.get("task_id") or "").strip()
                blockers = ", ".join(str(item) for item in task.get("blocked_by", []) if item) or "-"
                result_lines.extend(["", f"## {node}: {title}", f"Cancelled. Blocked by: {blockers}"])

            payload = {
                **snapshot,
                "workflow_id": workflow_id,
                "cancelled_tasks": [task.to_dict() for task in newly_cancelled],
                "workflow_cancelled": workflow_cancelled,
                "summary": summary,
                "result_content": "\n".join(result_lines).strip(),
            }
            port.write_workflow_metric(
                "workflow_cancelled" if workflow_cancelled else "workflow_cancel_propagated",
                payload,
            )
            return payload

    def workflow_completion_snapshot(
        self,
        port: WorkflowRuntimePort,
        workflow_id: str,
        *,
        conversation_id: str = "",
    ) -> dict[str, Any]:
        workflow_id = str(workflow_id or "").strip()
        ordered = self._ordered(
            port.list_workflow_tasks(workflow_id, conversation_id) if workflow_id else []
        )
        required = [task for task in ordered if task.required_for_final]
        required_basis = required or ordered
        required_total = len(required_basis)
        required_completed = sum(1 for task in required_basis if task.status == "completed")
        complete = required_total > 0 and required_completed == required_total
        workflow_name = next((task.workflow_name for task in ordered if task.workflow_name), "")
        workflow_mode = next((task.workflow_mode for task in ordered if task.workflow_mode), "")
        outputs: list[dict[str, Any]] = []
        for task in ordered:
            latest_output = task.outputs[-1].to_dict() if task.outputs else None
            outputs.append({
                "task_id": task.task_id,
                "node_id": task.node_id,
                "title": task.title,
                "role": task.role,
                "objective": task.objective,
                "status": task.status,
                "required_for_final": task.required_for_final,
                "output_count": len(task.outputs),
                "latest_output": latest_output,
            })

        name = workflow_name or workflow_id
        summary = (
            f"{name} completed: {required_completed}/{required_total} required node(s) completed."
            if complete
            else f"{name}: {required_completed}/{required_total} required node(s) completed."
        )
        result_lines = [summary]
        by_id = {task.task_id: task for task in ordered}
        for item in outputs:
            if not item["required_for_final"]:
                continue
            task = by_id.get(str(item.get("task_id") or ""))
            contents = [
                str(output.content or "").strip()
                for output in (task.outputs if task is not None else [])
                if str(output.content or "").strip()
            ]
            title = str(item.get("title") or item.get("task_id") or "").strip()
            node = str(item.get("node_id") or item.get("task_id") or "").strip()
            result_lines.extend([
                "",
                f"## {node}: {title}",
                "\n\n".join(contents) or f"[{item.get('status')}] No output attached.",
            ])

        return {
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "workflow_mode": workflow_mode,
            "complete": complete,
            "summary": summary,
            "result_content": "\n".join(result_lines).strip(),
            "required_total": required_total,
            "required_completed": required_completed,
            "total": len(ordered),
            "completed_total": sum(1 for task in ordered if task.status == "completed"),
            "running_total": sum(1 for task in ordered if task.status == "in_progress"),
            "blocked_total": sum(1 for task in ordered if task.status == "blocked"),
            "pending_total": sum(1 for task in ordered if task.status == "pending"),
            "cancelled_total": sum(1 for task in ordered if task.status == "cancelled"),
            "tasks": [task.to_dict() for task in ordered],
            "outputs": outputs,
        }

    def complete_workflow_if_ready(
        self,
        port: WorkflowRuntimePort,
        workflow_id: str,
        *,
        conversation_id: str = "",
    ) -> dict[str, Any] | None:
        workflow_id = str(workflow_id or "").strip()
        if not workflow_id or self.is_completed(workflow_id):
            return None
        snapshot = self.workflow_completion_snapshot(port, workflow_id, conversation_id=conversation_id)
        if not snapshot.get("complete"):
            return None
        self.mark_completed(workflow_id)
        port.write_workflow_metric("workflow_completed", snapshot)
        return snapshot
