"""Explicit MCP OAuth has one manager-owned attempt, with controlled SDK peers."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace

import pytest

from backend.mcp.manager import MCPServerConfig, MCPServerManager, ServerStatus
from backend.mcp.oauth import MCPAuthenticationRequired
from backend.tests.test_mcp_startup_owner_chain import GatePeer


class OAuthPeer(GatePeer):
    def __init__(self, interactive, *, ready=True, fail=None):
        super().__init__(ready=ready, fail=fail, cacheable=False, tools=[])
        self.interactive = interactive
        self.config = None

    @property
    def has_valid_token(self):
        return self.interactive and self.connected

    async def connect(self):
        await super().connect()
        if not self.interactive:
            raise MCPAuthenticationRequired("https://authorization.invalid/explicit-only")


def host(tmp_path, monkeypatch, *peers):
    manager = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path)
    plans, created = deque(peers), []

    def create(config, *, interactive_oauth=False, **_kwargs):
        peer = plans.popleft()
        assert peer.interactive is interactive_oauth
        peer.config = config
        created.append(peer)
        return peer

    monkeypatch.setattr(manager, "_create_client", create)
    return manager, created


def server(name="docs"):
    return MCPServerConfig(name, transport="http", url=f"https://{name}.invalid/mcp")


async def entered(peer):
    await asyncio.wait_for(peer.entered.wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_waiters_share_one_login_and_cancelling_either_observer_keeps_the_owner(tmp_path, monkeypatch, cancel_first):
    silent, interactive, next_login = OAuthPeer(False), OAuthPeer(True, ready=False), OAuthPeer(True)
    manager, created = host(tmp_path, monkeypatch, silent, interactive, next_login)
    config = server()
    waiters = []
    try:
        await manager.start_server(config)
        assert manager._servers[config.name].to_lifecycle_dict()["phase"] == "auth_required"
        waiters.append(asyncio.create_task(manager.oauth_login(config.name)))
        await entered(interactive)
        waiters.append(asyncio.create_task(manager.oauth_login(config.name)))
        await asyncio.sleep(0)
        assert created == [silent, interactive]
        cancelled, remaining = (waiters[0], waiters[1]) if cancel_first else (waiters[1], waiters[0])
        cancelled.cancel()
        assert isinstance((await asyncio.gather(cancelled, return_exceptions=True))[0], asyncio.CancelledError)
        assert not remaining.done() and interactive.closes == 0
        interactive.release.set()
        await remaining
        assert manager.get_client(config.name) is interactive
        assert manager._oauth_login_attempts == {}
        # A later explicit action owns a new attempt, not a cached old result.
        await manager.oauth_login(config.name)
        assert created == [silent, interactive, next_login]
        assert manager.get_client(config.name) is next_login and interactive.closes == 1
    finally:
        await manager.stop_all()
        await asyncio.gather(*waiters, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop_all", "cancel_startup", "logout"])
async def test_owner_retirement_cancels_and_drains_the_shared_authentication(tmp_path, monkeypatch, operation):
    silent, interactive = OAuthPeer(False), OAuthPeer(True, ready=False)
    manager, created = host(tmp_path, monkeypatch, silent, interactive)
    config = server()
    await manager.start_server(config)
    first = asyncio.create_task(manager.oauth_login(config.name))
    await entered(interactive)
    second = asyncio.create_task(manager.oauth_login(config.name))
    await asyncio.sleep(0)
    if operation == "logout":
        await manager.oauth_logout(config.name)
    else:
        await getattr(manager, operation)()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert created == [silent, interactive] and interactive.closes == 1
    assert manager._oauth_login_attempts == {} and manager.get_client(config.name) is None
    assert manager._servers[config.name].status is ServerStatus.OFFLINE
    await manager.stop_all()


@pytest.mark.asyncio
async def test_manager_keeps_the_producer_after_its_only_observer_leaves_and_drains_it_on_close(tmp_path, monkeypatch):
    interactive = OAuthPeer(True, ready=False)
    manager, _ = host(tmp_path, monkeypatch, OAuthPeer(False), interactive)
    config = server()
    await manager.start_server(config)
    observer = asyncio.create_task(manager.oauth_login(config.name))
    await entered(interactive)
    observer.cancel()
    assert isinstance((await asyncio.gather(observer, return_exceptions=True))[0], asyncio.CancelledError)
    assert interactive.closes == 0
    await manager.stop_all()
    assert manager._oauth_login_attempts == {} and interactive.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_owner", [False, True])
async def test_config_or_server_owner_replacement_cannot_join_the_old_login(tmp_path, monkeypatch, replace_owner):
    silent, old_login, new_silent, new_login = OAuthPeer(False), OAuthPeer(True, ready=False), OAuthPeer(False), OAuthPeer(True)
    peers = (silent, old_login, new_login) if replace_owner else (silent, old_login, new_silent, new_login)
    manager, created = host(tmp_path, monkeypatch, *peers)
    config = server()
    await manager.start_server(config)
    original_state = manager._servers[config.name]
    waiting = asyncio.create_task(manager.oauth_login(config.name))
    await entered(old_login)
    if replace_owner:
        await manager.remove_server(config.name)
        await manager.register_config(config)
        assert manager._servers[config.name] is not original_state
        replacement = config
    else:
        replacement = replace(config, url="https://different-owner.invalid/mcp", headers={"X-Account": "new-owned-account"})
        await manager.start_server(replacement)
    assert isinstance((await asyncio.gather(waiting, return_exceptions=True))[0], asyncio.CancelledError)
    assert old_login.closes == 1
    try:
        await manager.oauth_login(config.name)
        assert manager.get_client(config.name) is new_login
        assert new_login.config.url == replacement.url
        assert created.count(old_login) == 1 and manager._oauth_login_attempts == {}
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_different_workspace_owners_do_not_share_or_cancel_authentication(tmp_path, monkeypatch):
    first_peer, second_peer = OAuthPeer(True, ready=False), OAuthPeer(True, ready=False)
    first, _ = host(tmp_path / "first", monkeypatch, OAuthPeer(False), first_peer)
    second, _ = host(tmp_path / "second", monkeypatch, OAuthPeer(False), second_peer)
    config = server()
    await asyncio.gather(first.start_server(config), second.start_server(config))
    first_waiter = asyncio.create_task(first.oauth_login(config.name))
    second_waiter = asyncio.create_task(second.oauth_login(config.name))
    await asyncio.gather(entered(first_peer), entered(second_peer))
    await first.stop_all()
    assert isinstance((await asyncio.gather(first_waiter, return_exceptions=True))[0], asyncio.CancelledError)
    assert not second_waiter.done() and second_peer.closes == 0
    second_peer.release.set()
    try:
        await second_waiter
        assert second.get_client(config.name) is second_peer
    finally:
        await second.stop_all()


@pytest.mark.asyncio
async def test_failed_login_is_shared_but_the_next_explicit_attempt_can_retry(tmp_path, monkeypatch):
    failed = OAuthPeer(True, ready=False, fail=MCPAuthenticationRequired())
    replacement = OAuthPeer(True)
    manager, created = host(tmp_path, monkeypatch, OAuthPeer(False), failed, replacement)
    config = server()
    await manager.start_server(config)
    first = asyncio.create_task(manager.oauth_login(config.name))
    await entered(failed)
    second = asyncio.create_task(manager.oauth_login(config.name))
    await asyncio.sleep(0)
    failed.release.set()
    await asyncio.gather(first, second)
    assert len(created) == 2 and manager.get_client(config.name) is None
    assert manager._servers[config.name].to_lifecycle_dict()["phase"] == "auth_required"
    assert manager._oauth_login_attempts == {}
    try:
        await manager.oauth_login(config.name)
        assert created[-1] is replacement and manager.get_client(config.name) is replacement
    finally:
        await manager.stop_all()
