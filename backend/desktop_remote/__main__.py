"""Installed CLI used by SSH and WSL; it never installs or replaces a peer."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

from filelock import FileLock


def _daemon_paths(desktop_id: str, state_root: str | None) -> tuple[Path, Path]:
    if re.fullmatch(r"[a-f0-9]{32}", desktop_id) is None:
        raise ValueError("desktop id must be its installation's 128-bit hex identity")
    root = Path(state_root).expanduser().resolve() if state_root else Path.home() / ".minicode"
    daemon_dir = root / "desktop-hosts" / desktop_id
    daemon_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return daemon_dir, root


def _attach(state: dict, action: str) -> socket.socket:
    connection = socket.create_connection(("127.0.0.1", state["port"]), timeout=10)
    connection.sendall(json.dumps({"action": action, "token": state["token"]}).encode() + b"\n")
    response = bytearray()
    while not response.endswith(b"\n"):
        chunk = connection.recv(1)
        if not chunk:
            connection.close()
            raise RuntimeError("remote desktop daemon closed before accepting attachment")
        response.extend(chunk)
    reply = json.loads(response)
    if "error" in reply:
        connection.close()
        raise RuntimeError(reply["error"])
    connection.settimeout(None)
    return connection


def ensure_daemon(daemon_dir: Path, state_root: Path) -> socket.socket:
    state_file = daemon_dir / "daemon.json"
    with FileLock(str(daemon_dir / "startup.lock"), timeout=65):
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
            try:
                return _attach(state, "relay")
            except ConnectionRefusedError:
                # A proven dead loopback listener is stale; an active owner's
                # authentication/refusal is an error and never starts a rival.
                state_file.unlink()
        log_path = daemon_dir / "host.log"
        with log_path.open("ab", buffering=0) as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "backend.desktop_remote", "serve", "--desktop-id", daemon_dir.name,
                 "--state-root", str(state_root)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=os.name != "nt",
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0,
            )
        deadline = time.monotonic() + 60
        while not state_file.exists():
            if child.poll() is not None:
                raise RuntimeError(f"remote MiniCode host exited with code {child.returncode}; see {log_path}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"remote MiniCode host did not become ready; see {log_path}")
            time.sleep(0.025)
        return _attach(json.loads(state_file.read_text(encoding="utf-8")), "relay")


def proxy(connection: socket.socket, marker: str) -> None:
    import threading

    detached = threading.Event()

    def send_stdin() -> None:
        try:
            # No buffered-stdin lock may survive an independently closed
            # daemon socket when this short-lived proxy interpreter exits.
            while data := os.read(sys.stdin.fileno(), 64 * 1024):
                connection.sendall(data)
        except OSError:
            if not detached.is_set():
                raise
        finally:
            if not detached.is_set():
                connection.shutdown(socket.SHUT_WR)

    thread = threading.Thread(target=send_stdin, name="remote-desktop-proxy-stdin", daemon=True)
    thread.start()
    try:
        sys.stdout.buffer.write(bytes.fromhex(marker))
        sys.stdout.buffer.flush()
        while data := connection.recv(64 * 1024):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    finally:
        detached.set()
        connection.close()


def main() -> None:
    from backend.version import __version__

    parser = argparse.ArgumentParser(prog="minicode-host")
    parser.add_argument("--version", action="version", version=f"MiniCode {__version__} desktop-host/1")
    parser.add_argument("action", choices=("proxy", "serve", "stop"))
    parser.add_argument("--desktop-id", required=True)
    parser.add_argument("--state-root")
    parser.add_argument("--marker")
    args = parser.parse_args()
    daemon_dir, state_root = _daemon_paths(args.desktop_id, args.state_root)
    if args.action == "serve":
        from .host import serve

        asyncio.run(serve(daemon_dir, state_root))
    elif args.action == "proxy":
        if args.marker is None or re.fullmatch(r"[a-f0-9]{64}", args.marker) is None:
            parser.error("proxy requires a 256-bit --marker")
        proxy(ensure_daemon(daemon_dir, state_root), args.marker)
    else:
        state = json.loads((daemon_dir / "daemon.json").read_text(encoding="utf-8"))
        _attach(state, "stop").close()


if __name__ == "__main__":
    main()
