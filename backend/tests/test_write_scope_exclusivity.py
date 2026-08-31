from backend.tools.agent_tools import _exclusive_parallel_task_scopes


def test_write_scope_overlap_rejects_parallel_batch() -> None:
    tasks = [
        {
            "description": "Worker A",
            "prompt": "edit backend/a.py",
            "write_scope": ["backend/a.py"],
        },
        {
            "description": "Worker B",
            "prompt": "edit backend/a.py too",
            "write_scope": ["backend/a.py", "backend/b.py"],
        },
    ]
    assert _exclusive_parallel_task_scopes(tasks) == []


def test_write_scope_disjoint_allows_parallel_batch() -> None:
    tasks = [
        {
            "description": "Worker A",
            "prompt": "edit backend/a.py",
            "write_scope": ["backend/a.py"],
        },
        {
            "description": "Worker B",
            "prompt": "edit frontend/app.tsx",
            "write_scope": ["frontend/app.tsx"],
        },
    ]
    scopes = _exclusive_parallel_task_scopes(tasks)
    assert len(scopes) == 2
    assert scopes[0]
    assert scopes[1]
