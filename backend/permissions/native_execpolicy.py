"""Process transport to the bundled, pinned Source execution-policy engine."""
from __future__ import annotations

import json
import platform
from pathlib import Path
import subprocess
import sys
from typing import Any


SOURCE_COMMIT = "822e58cc3d666166c7446c5b1ea2e52f5d09594c"


def packaged_execpolicy_directory() -> Path:
    target = {
        ("win32", "amd64"): "x86_64-pc-windows-msvc",
        ("win32", "x86_64"): "x86_64-pc-windows-msvc",
        ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
        ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
        ("darwin", "arm64"): "aarch64-apple-darwin",
        ("darwin", "x86_64"): "x86_64-apple-darwin",
    }[(sys.platform, platform.machine().lower())]
    return Path(__file__).with_name("resources") / "execpolicy" / target


class NativeExecPolicyError(RuntimeError):
    """The real Source engine rejected a policy or durable amendment."""

    def __init__(self, operation: str, error: dict[str, Any]):
        super().__init__(error["message"])
        self.operation = operation
        self.error = error
        self.location = error["location"]


class NativeExecPolicyProcessError(RuntimeError):
    def __init__(self, returncode: int, stderr: str):
        super().__init__(f"Native execpolicy exited with {returncode}: {stderr}")
        self.returncode = returncode
        self.stderr = stderr


class NativeExecPolicyRuntime:
    def __init__(self, directory: Path | None = None):
        self.directory = (Path(directory) if directory is not None else packaged_execpolicy_directory()).resolve()
        self.binary = self.directory / ("minicode-execpolicy.exe" if sys.platform == "win32" else "minicode-execpolicy")

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = subprocess.run([str(self.binary)], cwd=self.directory,
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            check=False)
        if result.returncode not in (0, 1):
            raise NativeExecPolicyProcessError(result.returncode, result.stderr.decode("utf-8"))
        value = json.loads(result.stdout)
        if result.returncode == 1:
            raise NativeExecPolicyError(payload["operation"], value["error"])
        return value

    def identity(self) -> dict[str, Any]:
        return self.invoke({"operation": "identity"})
