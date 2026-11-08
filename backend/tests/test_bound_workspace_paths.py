from pathlib import Path
from types import SimpleNamespace

from backend.permissions.context import PermissionContext
from backend.ws.session_lifecycle import SessionLifecycle


def lifecycle_for(path):
    record = SimpleNamespace(execution_cwd=str(path), worktree_path="", workspace_root=str(path))
    session = SimpleNamespace(active_conversation_id="bound", permission_context=PermissionContext(),
        conversation_repo=SimpleNamespace(get_conversation_summary=lambda _: record))
    lifecycle = SessionLifecycle(session)
    lifecycle.workspace_root = path
    lifecycle.workspace_context = SimpleNamespace(root_path=lifecycle.workspace_root)
    return lifecycle, session, record


def test_bound_workspace_projections_reuse_the_admitted_absolute_path(tmp_path, monkeypatch):
    lifecycle, session, record = lifecycle_for(tmp_path)
    admitted = lifecycle.workspace_root
    def unexpected_resolution(*args, **kwargs):
        raise AssertionError("A bound workspace projection must not probe the filesystem again")
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "resolve", unexpected_resolution)
        assert lifecycle.workspace_root_for_conversation() is admitted
        assert lifecycle.workspace_context_for_conversation() is lifecycle.workspace_context
        assert lifecycle.current_workspace_root() is admitted
        assert lifecycle._current_sandbox_capability_scope() == ("bound", admitted, session.permission_context)


def test_changed_conversation_path_resolves_once_and_does_not_reuse_previous_mount(tmp_path, monkeypatch):
    lifecycle, _, record = lifecycle_for(tmp_path)
    changed = tmp_path / "other"
    changed.mkdir()
    record.execution_cwd = str(changed)
    calls = []
    resolve = Path.resolve
    def observed(path, *args, **kwargs):
        calls.append(path)
        return resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", observed)
    assert lifecycle.current_workspace_root() is None
    assert calls == [changed]
    assert lifecycle.workspace_root == tmp_path


def test_remote_and_unhydrated_owners_do_not_inherit_a_local_mount(tmp_path):
    lifecycle, _, record = lifecycle_for(tmp_path)
    record.execution_cwd = "file:///remote/workspace"
    assert lifecycle.workspace_root_for_conversation() is None
    assert lifecycle.current_workspace_root() is None
    record.execution_cwd = record.workspace_root = ""
    assert lifecycle.workspace_root_for_conversation() is None
    assert lifecycle.current_workspace_root() is None
