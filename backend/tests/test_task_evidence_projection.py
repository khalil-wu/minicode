from copy import deepcopy

from backend.tools.swarm_tools import _task_user_lines


def test_task_evidence_keeps_user_data_and_does_not_modify_canonical_routing():
    task = {
        "task_id": "task_private", "status": "blocked", "title": "Review business code",
        "description": "Keep constraints and report failures", "priority": "high",
        "assignee": "subagent-private", "blocked_by": ["task_dependency"],
        "outputs": [{"author_id": "run_private", "content": 'const sample = "cell_private_runtime";'}],
    }
    original = deepcopy(task)
    evidence = _task_user_lines([task])
    assert "[blocked] Review business code" in evidence
    assert "Priority: high" in evidence
    assert "Waiting for 1 prerequisite task(s)." in evidence
    assert 'const sample = "cell_private_runtime";' in evidence
    assert "task_private" not in evidence
    assert "subagent-private" not in evidence
    assert "run_private" not in evidence
    assert task == original


def test_empty_task_inventory_is_not_rendered_as_successful_work():
    assert _task_user_lines([]) == "No shared swarm tasks matched."
