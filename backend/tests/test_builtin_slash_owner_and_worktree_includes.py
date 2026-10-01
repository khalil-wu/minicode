from __future__ import annotations

import asyncio
import builtins
import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from backend.agent.message import AgentEvent, UserCommand
from backend.artifact.store import ArtifactStore
from backend.commands.registry import CommandRegistry, slash_conversation_id
from backend.commands.slash_commands import _handle_clear, _handle_permissions, _handle_plan
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.services.conversation_payload_service import copy_worktree_includes
from backend.tools.command_tool import RunCommandTool
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession


class Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


@pytest_asyncio.fixture
async def session(tmp_path, monkeypatch):
    artifacts = ArtifactStore(storage_dir=tmp_path / 'artifacts')
    tools = ToolRegistry()
    tools.register(RunCommandTool(artifacts))
    session = WebSocketSession(
        'builtin-owner-oracle', Socket(), SimpleNamespace(), artifacts, tools,
        PermissionChecker(PermissionSettings()), AppConfig(llm=LLMSettings(api_key='')),
        mcp_manager=None,
    )
    session.session_lifecycle.schedule_task_runtime_update = lambda: None
    session._ensure_extension_commands_for_conversation = AsyncMock()
    monkeypatch.setattr('backend.hooks.runtime.run_session_end_hook', AsyncMock())
    # Keep canonical protocol handlers, while isolating only catalog discovery.
    session.command_registry.clear_slash_handlers()
    session.command_registry.register_slash('/permissions', _handle_permissions)
    session.command_registry.register_slash('/plan', _handle_plan)
    session.command_registry.register_slash('/clear', _handle_clear)
    for cid in ('conv-original-a', 'conv-target-b'):
        record = session.conversation_repo.create_conversation(
            conversation_id=cid, permission_mode='confirm',
            context_snapshot={'history': [{'role': 'user', 'content': f'history-{cid}'}]},
        )
        session.conversation_repo.append_transcript_message(cid, {
            'id': f'user-{cid}', 'role': 'user', 'content': f'original-{cid}',
        })
    session.active_conversation_id = 'conv-original-a'
    session.permission_context = session.permission_checker.build_context(mode='confirm')
    session.context_builder.load_snapshot(
        session.conversation_repo.get_conversation('conv-original-a').context_snapshot,
    )
    event = AgentEvent.approval_request('pending-a', 'run_command', {'command': 'echo approval-only'})
    event.data.update(conversation_id='conv-original-a', turn_id='turn-a', message_id='assistant-a')
    session.build_approval_request_payload(event)
    future = asyncio.get_running_loop().create_future()
    session.turn_wait_state.register_waiter('pending-a', future, kind='approval')
    session._oracle_a_future = future
    yield session
    await session.session_lifecycle.shutdown(reason='oracle_cleanup')


async def send_target_b(session, content):
    assert await session.command_dispatcher._handle_command(UserCommand('user_message', {
        'conversation_id': 'conv-target-b', 'content': content,
        'user_message_id': 'target-b-command', 'assistant_message_id': 'target-b-reply',
    }))
    await session.event_outbox.drain_delivery()
    await session.event_outbox.drain_persistence()


@pytest.mark.asyncio
@pytest.mark.parametrize('content,expected_mode,cleared', [
    ('/permissions bypass', 'bypass', False),
    ('/plan', 'plan', False),
    ('/clear', 'confirm', True),
])
async def test_targeted_builtin_keeps_a_context_approval_and_active_owner(session, content, expected_mode, cleared):
    a_before = session.conversation_repo.get_conversation('conv-original-a').to_dict()
    context_before = session.context_builder.export_snapshot()
    await send_target_b(session, content)
    a = session.conversation_repo.get_conversation('conv-original-a')
    b = session.conversation_repo.get_conversation('conv-target-b')
    assert a.to_dict() == a_before
    assert b.permission_mode == expected_mode
    assert session.active_conversation_id == a.id
    assert session.permission_context.mode == 'confirm'
    assert session.context_builder.export_snapshot() == context_before
    assert not session._oracle_a_future.done()
    assert not any(p['type'] == 'approval.cancelled' and p.get('conversation_id') == a.id for p in session.ws.sent)
    if cleared:
        assert b.transcript == []
        assert b.context_snapshot.get('history', []) == []
    else:
        assert b.transcript[0]['content'] == 'original-conv-target-b'
    results = [p for p in session.ws.sent if p['type'] == 'command.result']
    assert results
    assert all(p['conversation_id'] == b.id for p in results)
    assert slash_conversation_id() is None


@pytest.mark.asyncio
async def test_plan_remainder_admission_uses_original_b_permission_context(session):
    admissions = []

    async def capture_admission(content, *, conversation_id, metadata, attachments):
        record = session.conversation_repo.get_conversation(conversation_id)
        admissions.append((content, conversation_id, record.permission_mode))
        # This is the scheduling boundary, not a claim of provider/tool execution.
        return 'captured-b-admission'

    session.start_agent_run = capture_admission
    await send_target_b(session, '/plan inspect B')
    assert admissions == [('inspect B', 'conv-target-b', 'plan')]
    assert session.active_conversation_id == 'conv-original-a'
    assert session.permission_context.mode == 'confirm'
    assert not session._oracle_a_future.done()


@pytest.mark.asyncio
async def test_existing_slash_scope_is_task_local_and_restored_on_failure():
    registry = CommandRegistry()
    entered = {owner: asyncio.Event() for owner in ('b', 'c')}
    release = asyncio.Event()
    seen = []

    async def callback(_session, arg, _attachments):
        seen.append((arg, 'before', slash_conversation_id()))
        entered[arg].set()
        await release.wait()
        seen.append((arg, 'after', slash_conversation_id()))
        if arg == 'c':
            raise RuntimeError('callback-failure')
        return True

    registry.register_slash('/scope', callback)
    b = asyncio.create_task(registry.dispatch_slash(None, '/scope', 'b', None, scope_id='b'))
    c = asyncio.create_task(registry.dispatch_slash(None, '/scope', 'c', None, scope_id='c'))
    await asyncio.gather(*(event.wait() for event in entered.values()))
    assert slash_conversation_id() is None
    release.set()
    assert await b == (True, 'b')
    with pytest.raises(RuntimeError, match='callback-failure'):
        await c
    assert seen == [('b', 'before', 'b'), ('c', 'before', 'c'), ('b', 'after', 'b'), ('c', 'after', 'c')]
    assert slash_conversation_id() is None


@pytest.mark.skipif(os.name != 'nt', reason='actual Windows junction contract')
def test_worktree_include_junction_never_reads_outside_and_preserves_normal_scope(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    repo = root / 'repo'
    normal = root / 'normal-worktree'
    linked = root / 'linked-worktree'
    outside = root / 'outside-repo'
    for path in (repo, normal, linked, outside):
        path.mkdir()
    (repo / 'allowed').mkdir()
    (repo / 'allowed' / 'inside.txt').write_text('INSIDE', encoding='utf-8')
    (outside / 'secret.txt').write_text('OUTSIDE-MARKER', encoding='utf-8')
    (repo / '.worktreeinclude').write_text('allowed\n', encoding='utf-8')
    assert copy_worktree_includes(repo, normal) == (['allowed'], [])
    assert (normal / 'allowed' / 'inside.txt').read_text(encoding='utf-8') == 'INSIDE'
    junction = repo / 'allowed' / 'junction'
    assert junction.parent.resolve().is_relative_to(root)
    assert outside.resolve().is_relative_to(root)
    subprocess.run(
        ['cmd', '/c', 'mklink', '/J', str(junction), str(outside)],
        check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert not junction.is_symlink()
    assert not junction.resolve().is_relative_to(repo.resolve())
    original_open = builtins.open
    outside_reads = []

    def record_open(file, mode='r', *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)) and 'r' in mode:
            if Path(file).resolve().is_relative_to(outside.resolve()):
                outside_reads.append(os.fspath(file))
        return original_open(file, mode, *args, **kwargs)

    from pathlib import Path
    with monkeypatch.context() as capture:
        capture.setattr(builtins, 'open', record_open)
        assert copy_worktree_includes(repo, linked) == ([], ['allowed'])
    assert outside_reads == []
    assert not (linked / 'allowed').exists()
    assert (outside / 'secret.txt').read_text(encoding='utf-8') == 'OUTSIDE-MARKER'
    # Whole-entry refusal and existing destination no-overwrite remain intact.
    assert copy_worktree_includes(repo, normal) == ([], ['allowed'])
    assert (normal / 'allowed' / 'inside.txt').read_text(encoding='utf-8') == 'INSIDE'
