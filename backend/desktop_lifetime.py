"""Native ownership of the desktop backend and its descendants."""
from __future__ import annotations

import os
import signal
import sys
import threading
from typing import BinaryIO, Callable


def bind_desktop_lifetime(parent_pipe: BinaryIO) -> threading.Thread:
    # Bind before importing the app or starting tools so descendants inherit ownership.
    terminate: Callable[[], None]
    if sys.platform == "win32":
        import win32api
        import win32job

        job = win32job.CreateJobObject(None, "")
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
        terminate = lambda: win32job.TerminateJobObject(job, 1)
    else:
        # Electron starts the sidecar in its own session. Never signal the desktop's group.
        group = os.getpgrp()
        if group != os.getpid():
            raise RuntimeError("Desktop backend was not launched in its own process group.")
        # Uvicorn owns SIGTERM and runs lifespan teardown for tools that have
        # separate sessions. Killing only this group would orphan those tools.
        terminate = lambda: os.kill(os.getpid(), signal.SIGTERM)

    parent_descriptor = parent_pipe.fileno()

    def wait_for_parent() -> None:
        try:
            # The lifetime watcher may remain blocked during normal Python
            # shutdown. Reading the descriptor does not acquire stdin's
            # BufferedReader lock, which finalization must acquire to close it.
            while os.read(parent_descriptor, 1):
                pass
        finally:
            terminate()

    watcher = threading.Thread(target=wait_for_parent, name="desktop-lifetime", daemon=True)
    watcher.start()
    return watcher
