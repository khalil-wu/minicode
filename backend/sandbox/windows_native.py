"""MiniCode elevated Windows sandbox adapter.

The native helper owns the dedicated sandbox accounts, ACL setup, Job Object,
Windows Firewall rules, and persistent WFP filters. MiniCode owns only the
translation from its canonical permission profile to helper argv and the
private desktop handle that must remain alive for the child process.
"""
from __future__ import annotations

import base64
import hashlib
import ctypes
from ctypes import wintypes
from contextlib import ExitStack
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Mapping

from backend.config import DATA_ROOT, PROJECT_ROOT
from backend.runtime_env import sanitized_subprocess_env
from backend.sandbox.policy import ResolvedSandboxPolicy


_RUNNER_ENV = "MINICODE_WINDOWS_SANDBOX_EXECUTABLE"
_HOME_ENV = "MINICODE_WINDOWS_SANDBOX_HOME"
_APP_RESOURCES_ENV = "MINICODE_APP_RESOURCES_DIR"
_PRIVATE_DESKTOP_PREFIX = "MiniCodeSandboxDesktop-"
_RUNTIME_VERSION = "minicode-windows-sandbox 0.158.0-alpha.2.1 owner-v3"
_DESKTOP_ALL_ACCESS = 0x000F01FF
_DESKTOP_PARTICIPANT_ACCESS = _DESKTOP_ALL_ACCESS & ~(
    0x00040000 | 0x00080000 | 0x00010000
)


@dataclass(frozen=True, slots=True)
class WindowsNativeRuntime:
    executable: Path
    home: Path
    offline_username: str
    online_username: str
    owner_id: str
    user_sid: str
    group: str


def _candidate_executables() -> Iterable[Path]:
    configured = str(os.environ.get(_RUNNER_ENV) or "").strip()
    if configured:
        yield Path(configured).expanduser()
        return
    resources_dir = str(os.environ.get(_APP_RESOURCES_ENV) or "").strip()
    resources_root = Path(resources_dir) if resources_dir else PROJECT_ROOT
    yield resources_root / "windows-sandbox" / "codex.exe"
    if resources_root.resolve() == PROJECT_ROOT.resolve():
        yield PROJECT_ROOT / "desktop" / "windows-sandbox-runtime" / "codex.exe"


def discover_runtime() -> tuple[WindowsNativeRuntime | None, str]:
    if sys.platform != "win32":
        return None, "the native Windows sandbox is Windows-only"
    configured_home = str(os.environ.get(_HOME_ENV) or "").strip()
    home = (Path(configured_home).expanduser() if configured_home else DATA_ROOT / "windows-sandbox").resolve()
    secret_file = home / ".sandbox-secrets" / "sandbox_users.json"
    try:
        secrets_payload = json.loads(secret_file.read_text(encoding="utf-8"))
        offline = secrets_payload["offline"]["username"]
        online = secrets_payload["online"]["username"]
        saved_owner = secrets_payload.get("owner_id")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"native sandbox account state is unavailable: {exc}"
    if not saved_owner:
        return None, "legacy native sandbox home requires its original runtime; automatic migration is not supported"
    marker_file = home / ".sandbox" / "setup_marker.json"
    if marker_file.exists():
        try:
            marker = json.loads(marker_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            # Setup writes this marker atomically after repairing ACL/WFP state.
            # A truncated marker is precisely the interrupted-repair state the
            # helper is designed to recover from; the DPAPI secrets remain the
            # account identity authority.
            return None, f"native sandbox setup at {home} is interrupted; run the setup helper"
        if not isinstance(marker, dict):
            return None, f"native sandbox setup at {home} is interrupted; run the setup helper"
        accounts = (marker.get("offline_username"), marker.get("online_username"))
        if accounts != (offline, online):
            return None, "native sandbox setup marker belongs to different accounts"
    else:
        return None, f"native sandbox setup marker is missing at {home}; run the setup helper"
    executable = next(
        (candidate.resolve() for candidate in _candidate_executables() if candidate.is_file()),
        None,
    )
    if executable is None:
        return None, f"set {_RUNNER_ENV} to a compatible Codex Windows sandbox runtime"
    try:
        identity = inspect_runtime_owner(executable, home)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return None, f"native sandbox owner inspection failed: {exc}"
    sid = _current_user_sid()
    digest = hashlib.sha256(
        b"minicode.windows-sandbox.owner.v3\0" + sid.encode("ascii") + b"\0"
        + str(home).replace("\\", "/").encode("utf-8").lower()
    ).digest()
    owner_id = digest.hex()
    label = base64.b32encode(digest[:10]).decode("ascii").lower()
    expected_accounts = (f"MC{label}O", f"MC{label}N")
    group = f"MiniCodeSandboxUsers-{owner_id}"
    if (
        identity["schema"] != 3 or identity["owner_id"] != owner_id
        or identity["user_sid"] != sid
        or os.path.normcase(str(Path(identity["home"]).resolve())) != os.path.normcase(str(home))
        or (identity["offline_username"], identity["online_username"]) != expected_accounts
        or identity["group"] != group
    ):
        return None, "native sandbox runtime owner does not match the current Windows SID and canonical home"
    if (
        saved_owner != owner_id or marker.get("owner_id") != owner_id
        or secrets_payload.get("version") != 6 or marker.get("version") != 6
        or (offline, online) != expected_accounts
    ):
        return None, "native sandbox credential/marker receipt belongs to a different owner or version"
    return WindowsNativeRuntime(executable, home, offline, online, owner_id, sid, group), ""


def inspect_runtime_owner(executable: Path, home: Path) -> dict[str, Any]:
    """Read-only ABI; neither command provisions accounts nor decrypts credentials."""
    version = subprocess.run(
        [str(executable), "--version"], check=True, capture_output=True,
        text=True, encoding="utf-8", timeout=5,
    ).stdout.strip()
    if version != _RUNTIME_VERSION:
        raise ValueError(f"incompatible Windows sandbox runtime version: {version}")
    result = subprocess.run(
        [str(executable), "sandbox", "identity", "--codex-home", str(home)],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=5,
    )
    identity = json.loads(result.stdout)
    for key in ("schema", "owner_id", "user_sid", "home", "offline_username", "online_username", "group"):
        identity[key]
    Path(identity["home"])
    return identity


def _account_sid(account: str) -> str:
    import win32security

    sid, _domain, _kind = win32security.LookupAccountName(None, account)
    return win32security.ConvertSidToStringSid(sid)


def _current_user_sid() -> str:
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    return win32security.ConvertSidToStringSid(sid)


@dataclass(slots=True)
class PrivateDesktop:
    name: str
    handle: int
    account: str

    @classmethod
    def create(cls, account: str) -> "PrivateDesktop":
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        convert.restype = wintypes.BOOL
        descriptor = ctypes.c_void_p()
        descriptor_size = wintypes.DWORD()
        sddl = (
            f"D:P(A;;0x{_DESKTOP_ALL_ACCESS:x};;;{_current_user_sid()})"
            f"(A;;0x{_DESKTOP_PARTICIPANT_ACCESS:x};;;{_account_sid(account)})"
        )
        if not convert(sddl, 1, ctypes.byref(descriptor), ctypes.byref(descriptor_size)):
            raise ctypes.WinError(ctypes.get_last_error())

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL),
            ]

        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        name = _PRIVATE_DESKTOP_PREFIX + secrets.token_hex(16)
        create = user32.CreateDesktopW
        create.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
            wintypes.DWORD, ctypes.POINTER(SecurityAttributes),
        ]
        create.restype = wintypes.HANDLE
        try:
            handle = create(name, None, None, 0, _DESKTOP_ALL_ACCESS, ctypes.byref(attributes))
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            return cls(name, int(handle), account)
        finally:
            kernel32.LocalFree(descriptor)

    def close(self) -> None:
        if self.handle:
            ctypes.WinDLL("user32", use_last_error=True).CloseDesktop(
                wintypes.HANDLE(self.handle)
            )
            self.handle = 0


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _permission_profile(resolved: ResolvedSandboxPolicy, cwd: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for entry in resolved.entries:
        if entry.path.kind == "special":
            value = {"kind": entry.path.value.value}
            if entry.path.subpath is not None:
                value["subpath"] = entry.path.subpath
            path = {"type": "special", "value": value}
        elif entry.path.kind == "glob_pattern":
            path = {"type": "glob_pattern", "pattern": str(entry.path.value)}
        else:
            raw = Path(entry.path.value)
            absolute = raw if raw.is_absolute() else cwd / raw
            path = {"type": "path", "path": str(absolute.resolve())}
        entries.append({"path": path, "access": entry.access.value})
    file_system: dict[str, Any] = {"type": "restricted", "entries": entries}
    if resolved.glob_scan_max_depth:
        file_system["glob_scan_max_depth"] = resolved.glob_scan_max_depth
    return {
        "type": "managed",
        "file_system": file_system,
        "network": "enabled" if resolved.allow_network else "restricted",
    }


def _powershell_argv(command: str) -> list[str]:
    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "if ($null -ne $PSStyle) { $PSStyle.OutputRendering = 'PlainText' }; "
        "$ProgressPreference = 'SilentlyContinue'; "
        "$minicodePrivateTemp = $env:TEMP; "
        "try { "
        "$global:LASTEXITCODE = $null; "
        f"{command}\n"
        "$minicodeCommandSucceeded = $?; "
        "$minicodeNativeExit = $global:LASTEXITCODE "
        "} finally { "
        "Get-ChildItem -LiteralPath $minicodePrivateTemp -Force -ErrorAction SilentlyContinue | "
        "Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "
        "}; "
        "if ($null -ne $minicodeNativeExit) { exit $minicodeNativeExit } "
        "elseif ($minicodeCommandSucceeded) { exit 0 } else { exit 1 }"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    # The dedicated account is guaranteed read access to Windows platform
    # files. A user-installed pwsh can live under an arbitrary ACL and fail at
    # CreateProcessAsUserW before policy execution starts.
    executable = str(
        Path(os.environ.get("SYSTEMROOT") or r"C:\Windows")
        / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    return [
        executable, "-NoLogo", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-OutputFormat", "Text",
        "-EncodedCommand", encoded,
    ]


def prepare_command(
    command: str,
    *,
    cwd: Path,
    resolved: ResolvedSandboxPolicy,
    env: Mapping[str, str],
    workspace_roots: Iterable[Path],
    deny_read_paths: Iterable[Path],
    deny_write_paths: Iterable[Path],
) -> tuple[list[str], PrivateDesktop, Path]:
    runtime, reason = discover_runtime()
    if runtime is None:
        raise RuntimeError(reason)
    account = runtime.online_username if resolved.allow_network else runtime.offline_username
    with ExitStack() as preparation:
        desktop = PrivateDesktop.create(account)
        preparation.callback(desktop.close)
        private_temp = DATA_ROOT / "sandbox-native-temp" / secrets.token_hex(12)
        private_temp.mkdir(parents=True)
        preparation.callback(private_temp.rmdir)
        write_roots = [
            str(root.root.resolve())
            for root in resolved.writable_roots
            if root.root.resolve() != Path(tempfile.gettempdir()).resolve()
        ]
        write_roots.append(str(private_temp.resolve()))
        profile = _permission_profile(resolved, cwd)
        child_env = dict(env)
        if not resolved.allow_network:
            child_env = {
                key: value for key, value in child_env.items()
                if key.upper() not in {
                    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                    "CODEX_WINDOWS_SANDBOX_PROXY_PORTS",
                    "MINICODE_WINDOWS_SANDBOX_PROXY_PORTS",
                }
            }
        child_env.update({"TEMP": str(private_temp), "TMP": str(private_temp)})
        args = [
            str(runtime.executable), "--run-as-windows-sandbox",
            "--codex-home", str(runtime.home), "--command-cwd", str(cwd),
            "--permission-profile", _json(profile), "--env-json", _json(child_env),
            "--windows-sandbox-level", "elevated",
            "--windows-sandbox-private-desktop-name", desktop.name,
        ]
        for root in workspace_roots:
            args.extend(("--workspace-root", str(root.resolve())))
        args.extend(("--write-roots-json", _json(write_roots)))
        deny_read = list(dict.fromkeys(str(path.resolve()) for path in deny_read_paths))
        deny_write = list(dict.fromkeys(str(path.resolve()) for path in deny_write_paths))
        if deny_read:
            args.extend(("--deny-read-paths-json", _json(deny_read)))
        if deny_write:
            args.extend(("--deny-write-paths-json", _json(deny_write)))
        args.append("--")
        args.extend(_powershell_argv(command))
        preparation.pop_all()
        return args, desktop, private_temp


def cleanup_private_temp(
    private_temp: Path,
    *,
    cwd: Path,
    desktop: PrivateDesktop,
) -> bool:
    """Remove owner-only child files as the same dedicated sandbox account."""
    temp_root = (DATA_ROOT / "sandbox-native-temp").resolve()
    target = private_temp.resolve()
    if not target.is_relative_to(temp_root) or target == temp_root:
        raise ValueError(f"Native sandbox TEMP escaped its owned root: {target}")
    runtime, reason = discover_runtime()
    if runtime is None:
        raise RuntimeError(reason)
    profile = {
        "type": "managed",
        "file_system": {
            "type": "restricted",
            "entries": [
                {
                    "path": {"type": "special", "value": {"kind": "root"}},
                    "access": "read",
                },
                {
                    "path": {"type": "path", "path": str(target)},
                    "access": "write",
                },
            ],
        },
        "network": "enabled" if desktop.account == runtime.online_username else "restricted",
    }
    system_root = Path(os.environ.get("SYSTEMROOT") or r"C:\Windows")
    child_env = {
        "SYSTEMROOT": str(system_root),
        "WINDIR": str(system_root),
        "PATH": str(system_root / "System32"),
        "TEMP": str(target),
        "TMP": str(target),
    }
    command = (
        "Get-ChildItem -LiteralPath '"
        + str(target).replace("'", "''")
        + "' -Force | Remove-Item -Recurse -Force"
    )
    args = [
        str(runtime.executable), "--run-as-windows-sandbox",
        "--codex-home", str(runtime.home), "--command-cwd", str(cwd),
        "--permission-profile", _json(profile), "--env-json", _json(child_env),
        "--windows-sandbox-level", "elevated",
        "--windows-sandbox-private-desktop-name", desktop.name,
        "--preserve-proxy-settings",
        "--workspace-root", str(cwd),
        "--read-roots-include-platform-defaults", "--read-roots-json", "[]",
        "--write-roots-json", _json([str(target)]),
        "--", *_powershell_argv(command),
    ]
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=sanitized_subprocess_env(),
        capture_output=True,
        timeout=30,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace")[:800]
        raise RuntimeError(
            f"native TEMP cleanup helper exited {completed.returncode}: {detail}"
        )
    return True
