"""Source throttle timing and private filesystem Skill invalidation."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.skills import runtime_roots
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.skills.runtime_roots import RuntimeSkillRoots


def _controlled_cooldown(monkeypatch):
    waits = asyncio.Queue()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def sleep(delay):
        waits.put_nowait(delay)
        try:
            await release.wait()
        finally:
            finished.set()

    monkeypatch.setattr(runtime_roots, "asyncio", SimpleNamespace(
        get_running_loop=asyncio.get_running_loop, create_task=asyncio.create_task,
        sleep=sleep, to_thread=asyncio.to_thread, CancelledError=asyncio.CancelledError,
    ))
    return waits, release, finished


@pytest.mark.asyncio
async def test_first_event_is_immediate_and_changes_during_callback_and_cooldown_coalesce(monkeypatch):
    waits, release_cooldown, _ = _controlled_cooldown(monkeypatch)
    notices = asyncio.Queue()
    release_callback = asyncio.Event()
    delivered = []

    async def changed():
        delivered.append(roots.revision)
        notices.put_nowait(roots.revision)
        if len(delivered) == 1:
            await release_callback.wait()

    roots = RuntimeSkillRoots(changed)
    try:
        roots._changed()
        assert await asyncio.wait_for(notices.get(), 1) == 1
        assert waits.empty(), "The first event must not enter the ten-second cooldown"
        for _ in range(20):
            roots._changed()
        release_callback.set()
        delay = await asyncio.wait_for(waits.get(), 1)
        assert 0 < delay <= 10
        for _ in range(20):
            roots._changed()
        release_cooldown.set()
        assert await asyncio.wait_for(notices.get(), 1) == 2
        await roots._pending
        assert delivered == [1, 2]
        assert waits.empty()
    finally:
        await roots.aclose()


@pytest.mark.asyncio
async def test_callback_time_counts_toward_the_interval(monkeypatch):
    waits, _, _ = _controlled_cooldown(monkeypatch)
    notices = asyncio.Queue()
    release_callback = asyncio.Event()
    clock = [100.0]
    delivered = []

    async def changed():
        delivered.append(clock[0])
        notices.put_nowait(roots.revision)
        if len(delivered) == 1:
            await release_callback.wait()

    roots = RuntimeSkillRoots(changed)
    roots._loop = SimpleNamespace(time=lambda: clock[0])
    try:
        roots._changed()
        assert await asyncio.wait_for(notices.get(), 1) == 1
        clock[0] = 111.0
        roots._changed()
        release_callback.set()
        assert await asyncio.wait_for(notices.get(), 1) == 2
        await roots._pending
        assert delivered == [100.0, 111.0]
        assert waits.empty(), "A slow callback must not start another full interval"
    finally:
        await roots.aclose()


@pytest.mark.asyncio
async def test_close_cancels_pending_delivery_before_waiting_for_observer_join(monkeypatch):
    waits, _, cancelled = _controlled_cooldown(monkeypatch)
    notices = asyncio.Queue()

    async def changed():
        notices.put_nowait(roots.revision)

    roots = RuntimeSkillRoots(changed)
    roots._changed()
    assert await asyncio.wait_for(notices.get(), 1) == 1
    roots._changed()
    await asyncio.wait_for(waits.get(), 1)
    pending = roots._pending
    join_started = asyncio.Event()
    release_join = threading.Event()
    loop = asyncio.get_running_loop()

    class JoiningObserver:
        stopped = False

        def stop(self):
            self.stopped = True

        def join(self):
            loop.call_soon_threadsafe(join_started.set)
            release_join.wait()

    observer = JoiningObserver()
    roots._observer = observer
    closing = asyncio.create_task(roots.aclose())
    try:
        await asyncio.wait_for(join_started.wait(), 1)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert pending.cancelled()
        assert observer.stopped
        roots._changed()
        assert roots.revision == 1 and notices.empty()
    finally:
        release_join.set()
        await closing


@pytest.mark.asyncio
async def test_replacing_roots_retires_old_pending_work_without_losing_the_new_batch(tmp_path, monkeypatch):
    waits, release, _ = _controlled_cooldown(monkeypatch)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    notices = asyncio.Queue()

    async def changed():
        notices.put_nowait((roots.revision, roots.roots))

    roots = RuntimeSkillRoots(changed)
    try:
        await roots.set_extra_roots([str(first)])
        assert await notices.get() == (1, (first,))
        roots._changed()
        assert await asyncio.wait_for(notices.get(), 1) == (2, (first,))
        deadline = roots._next_allowed
        roots._changed()
        await asyncio.wait_for(waits.get(), 1)
        old_pending = roots._pending
        await roots.set_extra_roots([str(second)])
        assert await notices.get() == (3, (second,))
        assert roots._next_allowed == deadline
        roots._changed()
        assert 0 < await asyncio.wait_for(waits.get(), 1) <= 10
        assert roots._pending is not old_pending and old_pending.cancelled()
        release.set()
        assert await asyncio.wait_for(notices.get(), 1) == (4, (second,))
        await roots._pending
        assert notices.empty()
    finally:
        await roots.aclose()
    assert not roots._observer.is_alive()


@pytest.mark.asyncio
async def test_replacing_roots_cancels_an_active_callback_before_it_can_publish_for_the_new_roots(tmp_path, monkeypatch):
    waits, release_cooldown, _ = _controlled_cooldown(monkeypatch)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    callback_started = asyncio.Event()
    callback_cancelled = asyncio.Event()
    release_old_callback = asyncio.Event()
    notices = asyncio.Queue()

    async def changed():
        if roots.roots == (first,) and roots.revision == 2:
            callback_started.set()
            try:
                await release_old_callback.wait()
            finally:
                callback_cancelled.set()
        notices.put_nowait((roots.revision, roots.roots))

    roots = RuntimeSkillRoots(changed)
    try:
        await roots.set_extra_roots([str(first)])
        assert await notices.get() == (1, (first,))
        roots._changed()
        await asyncio.wait_for(callback_started.wait(), 1)
        old_pending = roots._pending
        deadline = roots._next_allowed
        await roots.set_extra_roots([str(second)])
        assert await notices.get() == (3, (second,))
        await asyncio.wait_for(callback_cancelled.wait(), 1)
        assert old_pending.cancelled() and roots._next_allowed == deadline
        release_old_callback.set()
        roots._changed()
        await asyncio.wait_for(waits.get(), 1)
        release_cooldown.set()
        assert await asyncio.wait_for(notices.get(), 1) == (4, (second,))
        await roots._pending
        assert notices.empty()
    finally:
        await roots.aclose()


@pytest.mark.asyncio
async def test_real_interval_limits_a_burst_and_an_idle_event_does_not_restart_the_wait():
    notices = asyncio.Queue()
    loop = asyncio.get_running_loop()

    async def changed():
        notices.put_nowait(loop.time())

    roots = RuntimeSkillRoots(changed, throttle_seconds=.05)
    try:
        roots._changed()
        await asyncio.wait_for(notices.get(), 1)
        deadline = roots._next_allowed
        for _ in range(30):
            roots._changed()
        second = await asyncio.wait_for(notices.get(), 1)
        assert second >= deadline
        await roots._pending
        assert roots.revision == 2 and notices.empty()
        await asyncio.sleep(.06)
        roots._changed()
        await asyncio.wait_for(notices.get(), 1)
        assert roots.revision == 3
    finally:
        await roots.aclose()


@pytest.mark.asyncio
async def test_private_file_event_refreshes_receivers_once_and_preserves_captured_authority(tmp_path, monkeypatch):
    extra = tmp_path / "extra"
    files = {}
    for name in ("workflow", "secret"):
        file = extra / name / "SKILL.md"
        file.parent.mkdir(parents=True)
        file.write_text(f"---\nname: {name}\ndescription: Original {name}\n---\nOriginal instructions", encoding="utf8")
        files[name] = file
    first_workspace, second_workspace = tmp_path / "project-a", tmp_path / "project-b"
    first_workspace.mkdir()
    second_workspace.mkdir()
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda self: [("user", root) for root in self._extra_roots])
    read_text = Path.read_text
    reads = []

    def counted_read(path, *args, **kwargs):
        if path.name == "SKILL.md" and path.is_relative_to(extra):
            reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read)
    notices = asyncio.Queue()

    async def changed():
        notices.put_nowait(roots.revision)

    roots = RuntimeSkillRoots(changed, throttle_seconds=.05)
    first = SkillManager(SkillLoader(first_workspace), runtime_roots=roots)
    second = SkillManager(SkillLoader(second_workspace), runtime_roots=roots)
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "settings.json")), {}),))
    first._loader._config_stack = second._loader._config_stack = stack
    try:
        await roots.set_extra_roots([str(extra)])
        await notices.get()
        first.discover()
        second.discover()
        captured = first.snapshot(config_stack=stack)
        executor_authority = object()
        captured.attach_executor_catalog(executor_authority)
        restricted = captured.snapshot(role_config={"config": [{"name": "secret", "enabled": False}]}, config_stack=stack)
        assert restricted.get_meta("secret") is None
        before = len(reads)
        files["workflow"].write_text("---\nname: workflow\ndescription: Edited workflow\n---\nNew instructions", encoding="utf8")
        await asyncio.wait_for(notices.get(), 2)
        assert len(reads) == before, "Watch invalidation must not scan Skill files in the callback"
        assert first.get_meta("workflow").description == "Edited workflow"
        assert second.get_meta("workflow").description == "Edited workflow"
        assert len(reads) - before == 4
        first.list_metas()
        second.list_metas()
        assert len(reads) - before == 4
        assert restricted.get_meta("workflow").description == "Original workflow"
        assert restricted.executor_catalog is executor_authority
        assert captured.get_meta("secret") is not None
        assert second.executor_catalog is None
        assert first._loader._project_root == first_workspace and second._loader._project_root == second_workspace
        refreshed_turn = first.snapshot(config_stack=stack, force_reload=True)
        assert refreshed_turn.get_meta("workflow").description == "Edited workflow"
        assert len(reads) - before == 6
        assert refreshed_turn.executor_catalog is None
        assert first.get_meta("workflow").source_level == "user"
    finally:
        await roots.aclose()
    assert not roots._observer.is_alive()
