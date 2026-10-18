"""Explicitly upgrade legacy worktree ownership from its creation evidence."""
from __future__ import annotations

from pathlib import Path

from backend.agent.execution_journal import ExecutionJournal


class WorktreeOwnerUpgradeError(RuntimeError):
    pass


def upgrade_legacy_worktree_owners(record: dict, *, journal_root: Path, read_run, states: set[str]) -> bool:
    resources = [resource for resource in record.get("cleanup_resources", [])
        if resource["resource_kind"] == "worktree" and resource["state"] in states
        and "worktree_branch" not in resource["metadata"] and Path(resource["resource_id"]).exists()]
    if not resources:
        return False
    # read_events validates the exact agent id and the complete contiguous
    # journal prefix; a later resume must never substitute its current branch.
    events = ExecutionJournal(record["subagent_id"], base_dir=journal_root).read_events()
    prompt = next((event for event in events if event.event_type == "user_prompt"), None)
    for resource in resources:
        registration = next((event for event in events if event.event_type == "cleanup"
            and event.payload.get("registered") is True and event.payload.get("resource_kind") == "worktree"
            and event.payload.get("resource_id") == resource["resource_id"]), None)
        if registration is None or prompt is None or registration.seq >= prompt.seq:
            raise WorktreeOwnerUpgradeError(f"Worktree ownership upgrade needs its original registration and admitted prompt: {resource['resource_id']}")
        created = registration.payload["metadata"]
        branch = prompt.payload.get("worktree_branch")
        expected_path = Path(created["git_root"]) / ".minicode" / "worktrees" / record["subagent_id"]
        if (not isinstance(branch, str) or not branch or created.get("branch") != branch
                or Path(resource["resource_id"]) != expected_path
                or prompt.payload.get("worktree_path") != resource["resource_id"]
                or created.get("git_root") != resource["metadata"].get("git_root")):
            raise WorktreeOwnerUpgradeError(f"Worktree creation path or branch evidence does not match its resource: {resource['resource_id']}")
        run = read_run(prompt.payload.get("run_id", ""))
        if (run is None or run.get("task_id") != record["subagent_id"]
                or run.get("agent_path") != record["agent_path"]
                or not str(run.get("role", "")).startswith("subagent:")
                or not 0 < run.get("mailbox_epoch", 0) <= record["mailbox_epoch"]):
            raise WorktreeOwnerUpgradeError(f"Worktree creation prompt has no matching owned incarnation: {resource['resource_id']}")
        proof = {"version": 1, "registration_event_id": registration.event_id,
            "prompt_event_id": prompt.event_id, "run_id": run["run_id"],
            "subagent_id": record["subagent_id"], "agent_path": run["agent_path"],
            "mailbox_epoch": run["mailbox_epoch"]}
        resource["metadata"] = {**resource["metadata"], "worktree_branch": branch, "worktree_owner_upgrade": proof}
        config = record["resume_config"]
        if config.get("worktree_path") == resource["resource_id"]:
            if "worktree_branch" in config and config["worktree_branch"] != branch:
                raise WorktreeOwnerUpgradeError("Persisted resume ownership disagrees with the worktree creation evidence")
            config["worktree_branch"] = branch
    return True
