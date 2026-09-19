from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

from backend.agent.message import UserCommand
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.manager import WebSocketManager


def test_unrelated_windows_proceed_but_same_task_and_global_changes_wait():
    async def scenario():
        manager = WebSocketManager()
        entered = []
        release = asyncio.Event()
        held = asyncio.Event()

        def dispatcher(name):
            lock = asyncio.Lock()
            session = SimpleNamespace(
                active_conversation_id=name, ws_manager=manager, connection_generation=1,
                conversation_lifecycle_lock=lambda: lock,
                event_outbox=SimpleNamespace(bind_connection_generation=lambda _: nullcontext()),
            )
            instance = object.__new__(SessionCommandDispatcher)
            instance._session = session
            async def handler(command):
                entered.append((name, command.type))
                if name == "a":
                    held.set()
                    await release.wait()
            instance._handle_command_inner = handler
            return instance

        a, b, other_a = dispatcher("a"), dispatcher("b"), dispatcher("a")
        task = asyncio.create_task(a._handle_command(UserCommand(type="workspace.set")))
        await held.wait()
        await asyncio.wait_for(b._handle_command(UserCommand(type="user_message")), timeout=1)
        assert ("b", "user_message") in entered
        same_task = asyncio.create_task(other_a._handle_command(UserCommand(type="conversation.rename")))
        global_change = asyncio.create_task(b._handle_command(UserCommand(type="llm.config.set")))
        await asyncio.sleep(0)
        assert ("a", "conversation.rename") not in entered
        assert ("b", "llm.config.set") not in entered
        await asyncio.wait_for(b._handle_command(UserCommand(type="interrupt")), timeout=1)
        release.set()
        await asyncio.wait_for(asyncio.gather(task, same_task, global_change), timeout=1)
        assert ("b", "llm.config.set") in entered

    asyncio.run(scenario())


def test_cancelled_global_waiter_does_not_block_future_task_admission():
    async def scenario():
        manager = WebSocketManager()
        async with manager.conversation_lifecycle_scope(("a",)):
            async def global_work():
                async with manager.conversation_lifecycle_scope((), exclusive=True):
                    raise AssertionError("a is still active")
            waiter = asyncio.create_task(global_work())
            await asyncio.sleep(0)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            async with manager.conversation_lifecycle_scope(("b",)):
                pass
        assert manager._lifecycle_readers == 0
        assert not manager._lifecycle_writer

    asyncio.run(scenario())
