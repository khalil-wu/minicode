"""Own the original Windows process tree before its suspended child runs."""
from __future__ import annotations

import asyncio
import time


class WindowsProcessJob:
    def __init__(self):
        import win32job

        self.job = win32job.CreateJobObject(None, "")
        limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
        self.pid = None
        self.closed = False

    def attach_suspended(self, process) -> None:
        import psutil
        import win32api
        import win32con
        import win32job
        import win32process

        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid)
        try:
            win32job.AssignProcessToJobObject(self.job, handle)
            self.pid = process.pid
            # CREATE_SUSPENDED gives the original process one primary thread;
            # no workload code or descendant can run before this assignment.
            primary = psutil.Process(process.pid).threads()[0].id
            thread = win32api.OpenThread(win32con.THREAD_SUSPEND_RESUME, False, primary)
            try:
                win32process.ResumeThread(thread)
            finally:
                thread.Close()
        finally:
            handle.Close()

    async def terminate(self) -> None:
        import win32job

        win32job.TerminateJobObject(self.job, 1)
        deadline = time.monotonic() + 5
        while win32job.QueryInformationJobObject(self.job, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Windows job for local process {self.pid} still has live descendants")
            await asyncio.sleep(0.01)

    async def aclose(self) -> None:
        if not self.closed:
            await self.terminate()
            self.job.Close()
            self.closed = True
