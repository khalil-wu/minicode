"""Install or repair MiniCode's elevated Windows sandbox state.

The compatible runtime performs the UAC-elevated account, ACL, firewall and
WFP setup. Credentials remain DPAPI ciphertext under MiniCode's own state root.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import DATA_ROOT
from backend.sandbox import windows_native


_RUNTIME_VERSION = "minicode-windows-sandbox 0.158.0-alpha.2.1 owner-v3"


def _runtime_path(explicit: Path | None) -> Path:
    if explicit is not None:
        candidate = explicit.expanduser().resolve()
    else:
        candidate = next(
            (path.resolve() for path in windows_native._candidate_executables() if path.is_file()),
            None,
        )
    if candidate is None:
        raise RuntimeError(
            "No MiniCode Windows sandbox runtime was found. Run the desktop "
            "runtime preparation step or set MINICODE_WINDOWS_SANDBOX_EXECUTABLE."
        )
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    reported = subprocess.run(
        [str(candidate), "--version"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if reported != _RUNTIME_VERSION:
        raise RuntimeError(f"Windows sandbox runtime has a different identity: {reported}")
    return candidate


def _identity(home: Path, runtime: Path) -> dict[str, object]:
    identity = windows_native.inspect_runtime_owner(runtime, home)
    if identity["schema"] != 3:
        raise RuntimeError("The Windows sandbox runtime has no owner namespace v3")
    return identity


def _status(home: Path, runtime: Path) -> dict[str, object]:
    identity = _identity(home, runtime)
    previous_home = os.environ.get("MINICODE_WINDOWS_SANDBOX_HOME")
    previous_runtime = os.environ.get("MINICODE_WINDOWS_SANDBOX_EXECUTABLE")
    os.environ["MINICODE_WINDOWS_SANDBOX_HOME"] = str(home)
    os.environ["MINICODE_WINDOWS_SANDBOX_EXECUTABLE"] = str(runtime)
    try:
        discovered, reason = windows_native.discover_runtime()
    finally:
        if previous_home is None:
            os.environ.pop("MINICODE_WINDOWS_SANDBOX_HOME", None)
        else:
            os.environ["MINICODE_WINDOWS_SANDBOX_HOME"] = previous_home
        if previous_runtime is None:
            os.environ.pop("MINICODE_WINDOWS_SANDBOX_EXECUTABLE", None)
        else:
            os.environ["MINICODE_WINDOWS_SANDBOX_EXECUTABLE"] = previous_runtime
    if discovered is not None and (
        discovered.offline_username, discovered.online_username
    ) != (identity["offline_username"], identity["online_username"]):
        discovered = None
        reason = "sandbox account state belongs to a legacy or different owner"
    return {
        "ready": discovered is not None,
        "home": str(home),
        "runtime": str(runtime),
        "reason": reason,
        "offline_username": discovered.offline_username if discovered else "",
        "online_username": discovered.online_username if discovered else "",
        "identity": identity,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-home", type=Path, default=DATA_ROOT / "windows-sandbox")
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("the elevated Windows sandbox can only be prepared on Windows")

    home = args.target_home.expanduser().resolve()
    runtime = _runtime_path(args.runtime)
    if not args.status:
        home.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                str(runtime),
                "sandbox",
                "setup",
                "--elevated",
                "--current-user",
                "--codex-home",
                str(home),
            ],
            check=True,
        )
        interpreter_dir = Path(sys.executable).resolve().parent
        if interpreter_dir.is_relative_to(Path.home().resolve()):
            subprocess.run(
                [
                    "icacls.exe",
                    str(interpreter_dir),
                    "/grant",
                    f"{_identity(home, runtime)['group']}:(OI)(CI)(RX)",
                    "/T",
                    "/C",
                    "/Q",
                ],
                check=True,
            )
    status = _status(home, runtime)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
