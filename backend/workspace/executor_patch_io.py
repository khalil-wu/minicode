"""Patch verification and mutation against the same captured filesystem."""
from __future__ import annotations

from dataclasses import dataclass

from backend.environments.errors import ExecutorError
from backend.atomic_io import preserve_text_line_endings, normalize_text_newlines
from backend.tools.apply_patch_parser import ApplyPatchError, ChangeKind, apply_update_hunks, parse_patch
from backend.tools.file_tools_common import _emit_write_diff
from backend.permissions.review import build_structured_diff_payload, generate_unified_diff
from backend.workspace.executor_paths import ExecutorPath
from backend.workspace.executor_service import ExecutorWorkspaceService, record_executor_file_hash, text_hash


@dataclass(frozen=True)
class ExecutorChange:
    kind: ChangeKind
    path: ExecutorPath
    destination: ExecutorPath
    old: str | None
    new: str | None


async def plan_executor_patch(service: ExecutorWorkspaceService, patch: str) -> tuple[list[ExecutorChange], dict[str, str]]:
    plans, expected, claimed = [], {}, set()
    for change in parse_patch(patch):
        path = service.resolve(change.path)
        destination = service.resolve(change.move_to) if change.move_to else path
        touched = {path, destination}
        if claimed & touched:
            raise ApplyPatchError(f"Patch targets the same path more than once: {change.path}")
        claimed.update(touched)
        value = await service.metadata(path)
        if change.kind is ChangeKind.ADD:
            if value is not None:
                raise ApplyPatchError(f"Add File '{change.path}': file already exists. Use Update File instead.")
            old, new = None, change.new_content or ""
        else:
            if value is None:
                raise ApplyPatchError(f"{change.kind.value.title()} File '{change.path}': file does not exist.")
            old = normalize_text_newlines(await service.read_text(path))
            new = (None if change.kind is ChangeKind.DELETE else
                apply_update_hunks(old, change.hunks, change.path) if change.hunks else old)
        expected[path.uri] = "missing" if old is None else text_hash(old)
        if destination != path:
            if await service.metadata(destination) is not None:
                raise ApplyPatchError(f"Move to '{change.move_to}': destination already exists.")
            expected[destination.uri] = "missing"
        plans.append(ExecutorChange(change.kind, path, destination, old, new))
    return plans, expected


def executor_changes_diff(service: ExecutorWorkspaceService, plans: list[ExecutorChange]) -> dict | None:
    files, additions, deletions = [], 0, 0
    for plan in plans:
        display = service.display(plan.destination)
        status = "added" if plan.old is None else "deleted" if plan.new is None else "renamed" if plan.destination != plan.path else "modified"
        patch = generate_unified_diff(display, plan.old or "", plan.new or "")
        if plan.destination != plan.path and not patch.strip():
            patch = f"--- a/{service.display(plan.path)}\n+++ b/{display}\n"
        payload = build_structured_diff_payload(display, patch, status=status,
            old_path=service.display(plan.path) if plan.destination != plan.path else None,
            size_bytes=len((plan.new or "").encode("utf-8")))
        if payload["format"] == "structured":
            entry = payload["files"][0]
            entry.update(environment_id=service.scope.environment_id, path_uri=plan.destination.uri,
                         old_path_uri=plan.path.uri)
            files.append(entry)
            additions += entry["additions"]
            deletions += entry["deletions"]
    return {"format": "structured", "stats": {"files_count": len(files), "additions": additions, "deletions": deletions},
            "files": files} if files else None


async def emit_executor_change(service: ExecutorWorkspaceService, plan: ExecutorChange) -> None:
    await _emit_write_diff(service.context, file_path=plan.path.uri, old_content=plan.old, new_content=plan.new,
        display_path=service.display(plan.destination), old_display_path=service.display(plan.path),
        old_executor_uri=plan.path.uri, new_executor_uri=plan.destination.uri)


async def execute_executor_patch(tool, args: dict, context):
    async with ExecutorWorkspaceService(context) as service:
        committed = []
        try:
            plans, current = await plan_executor_patch(service, args["patch"])
            reviewed = args.get("_expected_hashes", current)
            if reviewed != current:
                raise ApplyPatchError("File changed on executor after review. Re-read and retry.")
            async with service.mutate([path for plan in plans for path in (plan.path, plan.destination)]):
                # Re-verify through the original attachment inside the same
                # mutation queue. Approval never authorizes a later snapshot.
                plans, current = await plan_executor_patch(service, args["patch"])
                if reviewed != current:
                    raise ApplyPatchError("File changed on executor after review. Re-read and retry.")
                for plan in plans:
                    if plan.new is None:
                        await service.remove(plan.path)
                        record_executor_file_hash(context, plan.path.uri, None)
                    else:
                        original = await service.read_bytes(plan.path) if plan.old is not None else None
                        written = preserve_text_line_endings(plan.new, original) if original is not None else plan.new
                        await service.write_bytes(plan.destination, written.encode("utf-8"))
                        record_executor_file_hash(context, plan.destination.uri, text_hash(plan.new))
                        if plan.destination != plan.path:
                            # A completed destination remains evidence even if
                            # removing the original fails. Never roll it back.
                            committed.append(ExecutorChange(ChangeKind.ADD, plan.destination, plan.destination, None, plan.new))
                            await service.remove(plan.path)
                            record_executor_file_hash(context, plan.path.uri, None)
                            committed.pop()
                    committed.append(plan)
        except (OSError, ValueError, ExecutorError) as error:
            # This owner records every completed mutation before reporting an
            # error; cancellation is deliberately not caught as an Exception.
            result = tool._error_result(tool._partial_apply_message(str(error), [service.display(plan.destination) for plan in committed]))
            if committed:
                result.status = "partial"
                result.user_summary = "补丁仅部分完成，已写入的改动已列出。"
            for plan in committed:
                await emit_executor_change(service, plan)
            result.runtime_metadata["committed_diff"] = executor_changes_diff(service, committed)
            return result
        for plan in committed:
            await emit_executor_change(service, plan)
        payload = executor_changes_diff(service, committed)
        additions = payload["stats"]["additions"] if payload else 0
        deletions = payload["stats"]["deletions"] if payload else 0
        result = tool._success_result(f"Applied patch to {len(committed)} file(s): +{additions} -{deletions}.\n" +
            "\n".join(f"  {plan.kind.value[0].upper()} {service.display(plan.destination)}" for plan in committed))
        result.runtime_metadata["committed_diff"] = payload
        return result
