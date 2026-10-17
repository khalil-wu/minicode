from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from backend.mcp.cua_repl import CuaRuntime, CuaReplMCPClient, CuaReplCompanions, CuaTurn, ComputerUseHost, is_configured_cua, SOURCE_PREFIX
from backend.mcp.manager import MCPServerConfig, MCPServerManager, ServerStatus
from backend.mcp.node_repl import NodeReplMCPClient


def selected_runtime(tmp_path):
    root = tmp_path / 'resources/cua_node/bin'
    modules = root / 'node_modules'
    paths = [root / 'node.exe', root / 'node_repl.exe',
        modules / '@oai/cua-repl/bin/cua-repl.mjs',
        modules / '@oai/sky/dist/project/cua/sky_js/src/targets/windows/internal/helper_transport.js',
        modules / '@oai/sky/bin/windows/codex-computer-use.exe',
        root.parent.parent / 'codex.exe']
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture-only')
    (modules / '@oai/sky/package.json').write_text('{"name":"@oai/sky","version":"fixture"}')
    return MCPServerConfig('cua_repl', command=str(paths[0]), args=[str(paths[2])],
        env={'CUA_REPL_NODE_REPL_PATH': str(paths[1]), 'CUA_REPL_ENABLED_SURFACES': 'computer'})


def test_only_explicit_selected_runtime_and_computer_surface_are_admitted(tmp_path):
    config = selected_runtime(tmp_path)
    assert is_configured_cua(config)
    runtime = CuaRuntime.selected(config)
    assert runtime.root == Path(config.env['CUA_REPL_NODE_REPL_PATH']).parent
    assert runtime.helper.is_file()
    assert not is_configured_cua(replace(config, env={}))
    with pytest.raises(ValueError, match='only the computer surface'):
        CuaRuntime.selected(replace(config, env={**config.env, 'CUA_REPL_ENABLED_SURFACES': 'browser,computer'}))
    with pytest.raises(ValueError, match='command/args'):
        CuaRuntime.selected(replace(config, args=[str(runtime.node_repl)]))
    runtime.helper.unlink()
    with pytest.raises(FileNotFoundError, match='Selected CUA runtime file'):
        CuaRuntime.selected(config)


@pytest.mark.asyncio
async def test_host_catalog_records_config_without_starting_desktop_or_faking_connection(tmp_path):
    config = selected_runtime(tmp_path)
    manager = MCPServerManager(config_path=tmp_path / 'mcp.json', workspace_root=tmp_path, runtime_configs=(replace(config, required=True),))
    try:
        await manager.start_all()
        assert manager.get_client(config.name) is None
        assert manager._servers[config.name].status == ServerStatus.OFFLINE
        assert not manager._servers[config.name].tools
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_builtin_cua_without_its_captured_runtime_is_an_actual_error_not_a_host_connection(tmp_path):
    config = replace(selected_runtime(tmp_path), source=SOURCE_PREFIX + "missing-original-attachment")
    manager = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path, runtime_configs=(config,))
    try:
        await manager.start_all()
        state = manager._servers[config.name]
        assert state.status == ServerStatus.ERROR
        assert "captured receiving environment" in state.last_error
        assert manager.get_client(config.name) is None and not state.tools
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_wire_metadata_uses_real_conversation_and_turn_instead_of_external_overrides(monkeypatch):
    original = AsyncMock(return_value='original-result')
    monkeypatch.setattr(NodeReplMCPClient, 'call_tool', original)
    client = object.__new__(CuaReplMCPClient)
    cancel = asyncio.Event()
    owner = {'session_id': 'host-session', 'conversation_id': 'actual-thread', 'turn_id': 'actual-turn', 'cancel_event': cancel}
    result = await client.call_tool('js', {'code': 'cua.listWindows()'}, request_owner=owner,
        request_meta={'x-codex-turn-metadata': {'session_id': 'foreign-thread'}, 'other': 'preserved'})
    assert result == 'original-result'
    assert original.await_args.kwargs['request_owner'] is owner
    assert original.await_args.kwargs['request_meta'] == {'other': 'preserved', 'x-codex-turn-metadata': {
        'session_id': 'actual-thread', 'thread_id': 'actual-thread', 'turn_id': 'actual-turn'}}
    with pytest.raises(ValueError, match='real conversation and turn'):
        await client.call_tool('js', {'code': 'cua.listWindows()'}, request_owner={'conversation_id': 'actual-thread'})


@pytest.mark.asyncio
async def test_official_companion_uses_only_owned_home_and_original_source_host(tmp_path, monkeypatch):
    import backend.mcp.cua_repl as module

    runtime = CuaRuntime.selected(selected_runtime(tmp_path))
    host = SimpleNamespace(pipe_path='owned-source-pipe')
    acquire, release = AsyncMock(return_value=host), AsyncMock()
    monkeypatch.setattr(module, '_acquire_host', acquire)
    monkeypatch.setattr(module, '_release_host', release)
    client = object.__new__(CuaReplMCPClient)
    client.runtime, client.home, client.host = runtime, tmp_path / 'minicode-owned-home', None
    client._env = {'CODEX_HOME': 'foreign-account-home', 'NODE_REPL_TRUSTED_SERVICES': '{"foreign":"unowned"}'}

    @asynccontextmanager
    async def original_transport(original):
        assert original is client
        assert client.host is host
        assert client._env['CODEX_HOME'] == str(client.home)
        assert client._env['SKY_CUA_NATIVE_PIPE_DIRECTORY'] == host.pipe_path
        assert client._env['NODE_REPL_NODE_PATH'] == str(runtime.node)
        assert json.loads(client._env['NODE_REPL_TRUSTED_SERVICES']) == {'sky': '@oai/sky/service'}
        yield 'original-streams'

    monkeypatch.setattr(NodeReplMCPClient, '_executor_transport', original_transport)
    async with client._executor_transport() as streams:
        assert streams == 'original-streams'
    acquire.assert_awaited_once_with(runtime, client.home)
    release.assert_awaited_once_with(host)
    assert client.host is None


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != 'nt', reason='Local Windows CUA admission contract')
async def test_same_thread_retains_real_companion_and_retires_original_permission_scope(tmp_path):
    config = selected_runtime(tmp_path)
    scope = SimpleNamespace(identity=lambda: {'scope': 'first'})
    attachment = SimpleNamespace(is_remote=False, environment=object(), permission_scope=scope,
        config=SimpleNamespace(mcp_policy=None, shell_environment_policy=SimpleNamespace(inherit='core',
            ignore_default_excludes=False, exclude=(), set_values={}, include_only=())),
        selection=SimpleNamespace(cwd=tmp_path.as_uri()))
    owner = CuaReplCompanions()
    first = await owner.admit(attachment, None, config)
    again = await owner.admit(attachment, None, config)
    assert first.source == again.source
    original = owner.instances[owner.active_key]
    original.client = SimpleNamespace(close_thread=AsyncMock())
    changed = replace(config, env={**config.env, 'SOURCE_RUNTIME_SETTING': 'changed'})
    next_config = await owner.admit(attachment, None, changed)
    assert next_config.source != first.source
    original.client.close_thread.assert_awaited_once()
    assert original.closed
    await owner.retire(next_config)
    assert not owner.instances
    await owner.aclose()


@pytest.mark.asyncio
async def test_original_turn_closes_only_after_original_consumers_finish():
    request = AsyncMock()
    client = SimpleNamespace(host=SimpleNamespace(request=request))
    turn = CuaTurn(SimpleNamespace(client=client), 'original-thread', 'original-turn')
    consumer = asyncio.get_running_loop().create_future()
    finishing = asyncio.create_task(turn.finish_after((consumer,)))
    await asyncio.sleep(0)
    request.assert_not_awaited()
    consumer.set_result(None)
    await finishing
    request.assert_awaited_once_with('closeActiveTurn', {'sessionId': 'original-thread', 'turnId': 'original-turn'})


@pytest.mark.asyncio
async def test_original_turn_control_closes_even_when_retirement_reports_error():
    request = AsyncMock()
    turn = CuaTurn(SimpleNamespace(client=SimpleNamespace(host=SimpleNamespace(request=request))), 'thread', 'turn')
    failure = asyncio.get_running_loop().create_future()
    failure.set_exception(RuntimeError('original consumer failed'))
    with pytest.raises(RuntimeError, match='original consumer failed'):
        await turn.finish_after((failure,))
    request.assert_awaited_once_with('closeActiveTurn', {'sessionId': 'thread', 'turnId': 'turn'})


@pytest.mark.asyncio
async def test_source_host_pipe_approval_serialization_errors_and_original_turn_closure(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('A local Node runtime is required for the pipe contract fixture')
    fixture = Path(__file__).parent / 'fixtures'
    host = Path(__file__).parents[1] / 'mcp/computer_use_host.mjs'
    pipe = '\\\\.\\pipe\\minicode-cua-fixture-' + uuid4().hex if os.name == 'nt' else str(tmp_path / 'cua.sock')
    process = await asyncio.create_subprocess_exec(node, str(fixture / 'cua_pipe_probe.mjs'), str(host),
        str(fixture / 'cua_source_transport.mjs'), pipe, str(tmp_path / 'events.jsonl'),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    assert all(json.loads(stdout).values())


@pytest.mark.asyncio
async def test_original_host_readiness_and_natural_shutdown_use_source_package_boundary(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('A local Node runtime is required for the host lifecycle fixture')
    config = selected_runtime(tmp_path)
    runtime = replace(CuaRuntime.selected(config), node=Path(node),
        transport=Path(__file__).parent / 'fixtures/cua_source_transport.mjs', helper=tmp_path / 'events.jsonl')
    original = ComputerUseHost(runtime, tmp_path / 'owned-home')
    if os.name != 'nt':
        original.pipe_path = str(tmp_path / 'host.sock')
    await original.start()
    assert original.process.returncode is None
    assert not runtime.helper.exists(), 'Readiness must not start the Source native helper'
    assert await original.request('closeActiveTurn', {'sessionId': 'foreign', 'turnId': 'foreign'}) is False
    await original.aclose()
    assert original.process.returncode == 0
    assert all(reader.done() for reader in original.readers)
