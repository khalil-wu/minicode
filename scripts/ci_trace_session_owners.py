"""Opt-in pytest trace for the creator of a closed-loop notification owner.

Load only for a diagnostic CI run with ``-p scripts.ci_trace_session_owners``.
The original subscribe/callback implementations remain the execution authority.
"""
from __future__ import annotations

import contextvars
import json
import sys
import threading
import traceback
import weakref
from pathlib import Path


def pytest_addoption(parser):
    parser.addoption("--session-owner-trace-output", default="pytest-session-owners.jsonl",
        help="JSONL evidence path for the explicitly loaded session-owner diagnostic plugin")


def pytest_configure(config):
    trace = _SessionOwnerTrace(Path(config.getoption("session_owner_trace_output")))
    trace.install()
    config.pluginmanager.register(trace, "ci-session-owner-trace")


class _SessionOwnerTrace:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self.node = "<collection>"
        self.creation_node = contextvars.ContextVar("ci_session_owner_creation_node", default="<collection>")
        self.owners = weakref.WeakKeyDictionary()
        self.lock = threading.Lock()

    def emit(self, event, owner, *, runtime=None, error=None, stderr=False):
        loop = owner._notification_loop
        record = {"event": event, "during": self.node,
            "callback_context_node": self.creation_node.get(),
            "owner_id": id(owner), "owner_type": type(owner).__name__,
            "session_id": str(getattr(owner._session, "session_id", "")),
            "loop_id": id(loop) if loop is not None else None,
            "loop_closed": loop.is_closed() if loop is not None else None,
            "wakes_closed": owner._notification_wakes_closed,
            "origin": self.owners.get(owner)}
        if runtime is not None:
            record["event_runtime_id"] = id(runtime)
        if error is not None:
            record.update(error_type=type(error).__name__, error=str(error))
        line = json.dumps(record, ensure_ascii=False)
        with self.lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            if stderr:
                print("CI_SESSION_OWNER_TRACE " + line, file=sys.stderr, flush=True)

    def install(self):
        from backend.ws import run_manager as manager_module

        original_subscribe = manager_module.subscribe_subagent_activity_changed
        original_activity = manager_module.SessionRunManager._on_subagent_activity_changed
        trace = self

        def subscribe(callback):
            unsubscribe = original_subscribe(callback)
            owner = getattr(callback, "__self__", None)
            if isinstance(owner, manager_module.SessionRunManager):
                origin = {"nodeid": trace.creation_node.get(), "active_test_nodeid": trace.node,
                    "callback": original_activity.__qualname__, "creation_stack": [
                        {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                        for frame in traceback.extract_stack()[:-1]]}
                with trace.lock:
                    trace.owners[owner] = origin
                trace.emit("subscribed", owner)
            return unsubscribe

        def activity(owner, runtime):
            loop = owner._notification_loop
            if loop is not None and loop.is_closed() and not owner._notification_wakes_closed:
                trace.emit("before_closed_loop_callback", owner, runtime=runtime, stderr=True)
            try:
                return original_activity(owner, runtime)
            except BaseException as error:
                trace.emit("callback_raised", owner, runtime=runtime, error=error, stderr=True)
                raise

        manager_module.subscribe_subagent_activity_changed = subscribe
        manager_module.SessionRunManager._on_subagent_activity_changed = activity

    def pytest_runtest_setup(self, item):
        self.node = item.nodeid
        self.creation_node.set(item.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        for owner in tuple(self.owners.keys()):
            loop = owner._notification_loop
            if loop is not None and loop.is_closed() and not owner._notification_wakes_closed:
                self.emit("session_finished_with_closed_loop_owner", owner)
