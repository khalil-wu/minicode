"""App-server-owned, replaceable Source skill roots and their live watcher."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class RuntimeSkillRoots:
    def __init__(self, on_changed: Callable[[], Awaitable[None]], *, throttle_seconds: float = 10):
        self.roots: tuple[Path, ...] = ()
        self.revision = 0
        self._on_changed = on_changed
        self._throttle_seconds = throttle_seconds
        self._observer = None
        self._pending: asyncio.Task | None = None
        self._dirty = False
        self._next_allowed = 0.0
        self._loop = asyncio.get_running_loop()
        self._closed = False

    async def set_extra_roots(self, values: list[str]) -> None:
        if not isinstance(values, list) or any(not isinstance(value, str) or not Path(value).is_absolute() for value in values):
            raise ValueError("extraRoots must be an array of absolute paths")
        self.roots = tuple(dict.fromkeys(Path(value).resolve() for value in values))
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
        self._pending = None
        self._dirty = False
        if self._observer is None and self.roots:
            self._observer = Observer()
        if self._observer is not None:
            self._observer.unschedule_all()
            owner = self

            class Handler(FileSystemEventHandler):
                def on_any_event(self, event):
                    if event.event_type not in {"modified", "created", "deleted", "moved"}:
                        return
                    paths = [Path(event.src_path)]
                    if event.event_type == "moved":
                        paths.append(Path(event.dest_path))
                    if any(path == root or path.is_relative_to(root) or (event.is_directory and root.is_relative_to(path))
                            for path in paths for root in owner.roots):
                        owner._loop.call_soon_threadsafe(owner._changed)

            watched = set()
            for root in self.roots:
                path = root
                while not path.is_dir():
                    path = path.parent
                if path not in watched:
                    self._observer.schedule(Handler(), str(path), recursive=True)
                    watched.add(path)
            if not self._observer.is_alive():
                self._observer.start()
        self.revision += 1
        await self._on_changed()

    def _changed(self):
        if self._closed:
            return
        self._dirty = True
        if self._pending is not None and not self._pending.done():
            return

        async def notify():
            while self._dirty:
                delay = self._next_allowed - self._loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._dirty = False
                self.revision += 1
                self._next_allowed = self._loop.time() + self._throttle_seconds
                await self._on_changed()

        self._pending = asyncio.create_task(notify(), name="skills-extra-roots-changed")

    async def aclose(self):
        self._closed = True
        self._dirty = False
        if self._pending is not None:
            self._pending.cancel()
        if self._observer is not None:
            self._observer.stop()
            await asyncio.to_thread(self._observer.join)
        if self._pending is not None:
            try:
                await self._pending
            except asyncio.CancelledError:
                pass
