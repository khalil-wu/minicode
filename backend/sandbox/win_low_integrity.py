"""Windows low-integrity sandbox backend.

Enforces the workspace write boundary without administrator setup using
Windows mandatory integrity control:

* the command runs under a copy of the backend's own token lowered to the
  Low integrity level. Reads are unaffected; a Low process cannot write to,
  delete or replace any object whose mandatory label is Medium or above,
  which is every file the user owns unless it is labelled otherwise;
* every writable root receives an inheritable Low label with
  ``NO_WRITE_UP``, so existing and future files inside it are writable by
  the child while everything outside stays read-only. Read-only subpaths and
  protected workspace metadata that exist get an explicit Medium label, which
  wins over the inherited Low one;
* a private per-run TEMP under the state root, labelled Low;
* a Job Object with kill-on-close so the whole tree dies with the launcher;
* an environment rewrite that points common network clients at a dead
  loopback proxy when the policy denies network. This is not a packet
  filter (that needs an administrator-installed WFP filter) and the
  capability reports ``network_isolated=False`` accordingly.

The backend process cannot hand a foreign primary token to ``asyncio``'s
subprocess machinery, so ``SandboxRunner`` spawns this module as a launcher
(``python -m backend.sandbox.win_low_integrity <launch.json>``). The
launcher inherits the runner's stdio pipes, creates the Low child with those
handles, waits, and exits with the child's exit code. Killing the launcher's
tree (the runner's normal teardown) kills the child through both the parent
relationship and the job.
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path
from typing import Any

_LOW_LABEL_SID = "S-1-16-4096"
_MEDIUM_LABEL_SID = "S-1-16-8192"
_LABEL_SECURITY_INFORMATION = 0x10
_SYSTEM_MANDATORY_LABEL_NO_WRITE_UP = 0x1
_SE_GROUP_INTEGRITY = 0x20
_FILE_PERSISTENT_ACLS = 0x8
_DEAD_PROXY = "http://127.0.0.1:9"
_NO_NETWORK_ENV: dict[str, str] = {
    "MINICODE_SANDBOX_NO_NETWORK": "1",
    "HTTP_PROXY": _DEAD_PROXY,
    "HTTPS_PROXY": _DEAD_PROXY,
    "ALL_PROXY": _DEAD_PROXY,
    "http_proxy": _DEAD_PROXY,
    "https_proxy": _DEAD_PROXY,
    "all_proxy": _DEAD_PROXY,
    "NO_PROXY": "localhost,127.0.0.1,::1",
    "no_proxy": "localhost,127.0.0.1,::1",
    "PIP_NO_INDEX": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "NPM_CONFIG_OFFLINE": "true",
    "CARGO_NET_OFFLINE": "true",
    "GIT_HTTP_PROXY": _DEAD_PROXY,
    "GIT_HTTPS_PROXY": _DEAD_PROXY,
    "GIT_SSH_COMMAND": "cmd /c exit 1",
    "GIT_ALLOW_PROTOCOL": "file",
}
_DENYBIN_STUBS = ("ssh", "scp", "sftp")
BACKEND_NAME = "low-integrity"


class LabelError(RuntimeError):
    """A mandatory label could not be applied; the boundary is unenforceable."""


def is_supported() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import win32job  # noqa: F401
        import win32process  # noqa: F401
        import win32security  # noqa: F401
    except ImportError:
        return False
    return True


def volume_supports_labels(path: Path) -> bool:
    """Mandatory labels live in the SACL, which needs an NTFS-style volume."""
    import pywintypes
    import win32api

    root = Path(path).expanduser().resolve().anchor
    try:
        _name, _serial, _max_len, flags, _fs = win32api.GetVolumeInformation(root)
    except pywintypes.error:
        return False
    return bool(flags & _FILE_PERSISTENT_ACLS)


# ── labels ──────────────────────────────────────────────────────────────


def _label_of(path: Path) -> tuple[str, int] | None:
    import win32security

    sd = win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT, _LABEL_SECURITY_INFORMATION,
    )
    sacl = sd.GetSecurityDescriptorSacl()
    if sacl is None or sacl.GetAceCount() == 0:
        return None
    (_kind, flags), _mask, sid = sacl.GetAce(0)
    return win32security.ConvertSidToStringSid(sid), flags


def _grant_self_write_owner(path: Path) -> None:
    """Writing a label needs WRITE_OWNER, which ownership alone does not imply."""
    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    me = win32security.GetTokenInformation(
        win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY),
        win32security.TokenUser,
    )[0]
    mask = ntsecuritycon.WRITE_OWNER | ntsecuritycon.WRITE_DAC | ntsecuritycon.READ_CONTROL
    sd = win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = sd.GetSecurityDescriptorDacl()
    for index in range(dacl.GetAceCount()):
        (kind, _flags), ace_mask, sid = dacl.GetAce(index)
        if kind == win32security.ACCESS_ALLOWED_ACE_TYPE and sid == me and ace_mask & mask == mask:
            return
    dacl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION_DS,
        win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
        mask,
        me,
    )
    win32security.SetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION,
        None, None, dacl, None,
    )


def set_label(path: Path, label_sid: str) -> None:
    """Apply an inheritable NO_WRITE_UP label; no-op when already in place."""
    import pywintypes
    import win32con
    import win32security

    inherit = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
    try:
        current = _label_of(path)
        if current is not None and current[0] == label_sid and current[1] & inherit == inherit:
            return
        _grant_self_write_owner(path)
        sacl = win32security.ACL()
        sacl.AddMandatoryAce(
            win32security.ACL_REVISION_DS,
            inherit,
            _SYSTEM_MANDATORY_LABEL_NO_WRITE_UP,
            win32security.ConvertStringSidToSid(label_sid),
        )
        win32security.SetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT, _LABEL_SECURITY_INFORMATION,
            None, None, None, sacl,
        )
    except pywintypes.error as exc:
        raise LabelError(f"cannot label {path}: {exc.strerror.strip()}") from exc


# ── launch preparation (runner side) ───────────────────────────────────


def sandbox_temp_base() -> Path:
    from backend.config_helpers import DATA_ROOT

    return Path(DATA_ROOT) / "sandbox-temp"


def prepare_launch(
    *,
    command_line: str,
    cwd: Path,
    writable_roots: list[Path],
    deny_write_paths: list[Path],
    allow_network: bool,
) -> tuple[Path, Path]:
    """Label the roots, create the private TEMP and write the launch spec.

    Returns ``(spec_path, temp_dir)``; the caller removes ``temp_dir`` after
    the run (the spec lives inside it). Raises ``LabelError`` when the
    boundary cannot be applied, which the runner reports as an unavailable
    sandbox instead of running the command unprotected.
    """
    for root in writable_roots:
        set_label(root, _LOW_LABEL_SID)
    for path in deny_write_paths:
        if path.exists():
            set_label(path, _MEDIUM_LABEL_SID)
    temp_base = sandbox_temp_base()
    temp_base.mkdir(parents=True, exist_ok=True)
    set_label(temp_base, _LOW_LABEL_SID)
    while True:
        temp_dir = temp_base / f"minicode-sbx-{random.getrandbits(48):012x}"
        try:
            temp_dir.mkdir()
            break
        except FileExistsError:
            continue
    spec = {
        "command_line": command_line,
        "cwd": str(cwd),
        "temp_dir": str(temp_dir),
        "allow_network": bool(allow_network),
    }
    spec_path = temp_dir / "launch.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return spec_path, temp_dir


def launcher_argv(spec_path: Path) -> list[str]:
    return [sys.executable, "-m", "backend.sandbox.win_low_integrity", str(spec_path)]


# ── launcher (child side) ───────────────────────────────────────────────


def _set_env(env: dict[str, str], name: str, value: str) -> None:
    """Assign case-insensitively so ``Path``/``PATH`` never coexist in the block."""
    for key in [key for key in env if key.upper() == name.upper()]:
        del env[key]
    env[name] = value


def _get_env(env: dict[str, str], name: str) -> str:
    for key, value in env.items():
        if key.upper() == name.upper():
            return value
    return ""


def _no_network_env(env: dict[str, str], temp_dir: Path) -> None:
    for name, value in _NO_NETWORK_ENV.items():
        _set_env(env, name, value)
    denybin = temp_dir / "denybin"
    denybin.mkdir(exist_ok=True)
    stub = "@echo off\r\necho %~n0 is unavailable inside the sandbox 1>&2\r\nexit /b 1\r\n"
    for tool in _DENYBIN_STUBS:
        for ext in (".cmd", ".bat"):
            (denybin / f"{tool}{ext}").write_text(stub)
    _set_env(env, "PATH", str(denybin) + os.pathsep + _get_env(env, "PATH"))
    # cmd.exe resolves a bare name through PATHEXT in order; the stubs only
    # win when their extensions come before .EXE.
    pathext = [ext for ext in _get_env(env, "PATHEXT").split(os.pathsep) if ext]
    _set_env(
        env, "PATHEXT",
        os.pathsep.join([".CMD", ".BAT", *[ext for ext in pathext if ext.upper() not in {".CMD", ".BAT"}]]),
    )


def _low_integrity_token():
    import win32api
    import win32con
    import win32security

    base = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_ALL_ACCESS)
    token = win32security.DuplicateTokenEx(
        base, win32security.SecurityImpersonation, win32con.TOKEN_ALL_ACCESS,
        win32security.TokenPrimary, None,
    )
    win32security.SetTokenInformation(
        token, win32security.TokenIntegrityLevel,
        (win32security.ConvertStringSidToSid(_LOW_LABEL_SID), _SE_GROUP_INTEGRITY),
    )
    return token


def _inheritable_std_handle(std_id: int):
    import pywintypes
    import win32api
    import win32con
    import win32file

    handle = None
    try:
        handle = win32api.GetStdHandle(std_id)
    except pywintypes.error:
        handle = None
    if not handle or int(handle) in (0, -1):
        access = win32con.GENERIC_READ if std_id == win32api.STD_INPUT_HANDLE else win32con.GENERIC_WRITE
        handle = win32file.CreateFile(
            "NUL", access, win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE, None,
            win32con.OPEN_EXISTING, 0, None,
        )
    win32api.SetHandleInformation(handle, win32con.HANDLE_FLAG_INHERIT, win32con.HANDLE_FLAG_INHERIT)
    return handle


def _launch(spec: dict[str, Any]) -> int:
    import win32api
    import win32con
    import win32event
    import win32job
    import win32process

    temp_dir = Path(spec["temp_dir"])
    env = dict(os.environ)
    for name in ("TEMP", "TMP", "TMPDIR"):
        _set_env(env, name, str(temp_dir))
    _set_env(env, "MINICODE_SANDBOX", BACKEND_NAME)
    if not spec.get("allow_network", False):
        _no_network_env(env, temp_dir)

    job = win32job.CreateJobObject(None, "")
    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    limits["BasicLimitInformation"]["LimitFlags"] |= (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | win32job.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
    )
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)

    startup = win32process.STARTUPINFO()
    startup.dwFlags = win32con.STARTF_USESTDHANDLES | win32con.STARTF_USESHOWWINDOW
    startup.wShowWindow = win32con.SW_HIDE
    startup.hStdInput = _inheritable_std_handle(win32api.STD_INPUT_HANDLE)
    startup.hStdOutput = _inheritable_std_handle(win32api.STD_OUTPUT_HANDLE)
    startup.hStdError = _inheritable_std_handle(win32api.STD_ERROR_HANDLE)

    process, thread, _pid, _tid = win32process.CreateProcessAsUser(
        _low_integrity_token(), None, spec["command_line"], None, None, True,
        win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW | win32process.CREATE_UNICODE_ENVIRONMENT,
        env, spec["cwd"], startup,
    )
    win32job.AssignProcessToJobObject(job, process)
    win32process.ResumeThread(thread)
    win32event.WaitForSingleObject(process, win32event.INFINITE)
    return int(win32process.GetExitCodeProcess(process))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m backend.sandbox.win_low_integrity <launch.json>", file=sys.stderr)
        return 2
    try:
        spec = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        return _launch(spec)
    except Exception as exc:  # the runner reports this as a failed command
        print(f"low-integrity sandbox launcher failed: {exc}", file=sys.stderr)
        return 126


if __name__ == "__main__":
    sys.exit(main())
