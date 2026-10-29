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
        import ctypes
        from ctypes import wintypes
        import win32api
        import win32con
        import win32job

        handle = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE | win32con.PROCESS_SUSPEND_RESUME,
            False, process.pid)
        try:
            win32job.AssignProcessToJobObject(self.job, handle)
            self.pid = process.pid
            # Resume the captured process after containment, without a system-wide thread scan.
            ntdll = ctypes.WinDLL("ntdll")
            resume = ntdll.NtResumeProcess
            resume.argtypes, resume.restype = [wintypes.HANDLE], wintypes.LONG
            status = resume(int(handle))
            if status < 0:
                convert = ntdll.RtlNtStatusToDosError
                convert.argtypes, convert.restype = [wintypes.LONG], wintypes.ULONG
                raise ctypes.WinError(convert(status))
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
