from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import backend.sandbox.runner as runner_module
from backend.sandbox import SandboxPolicy, SandboxRunner
from backend.sandbox.runner import _OutputCapture
from backend.subprocesses import terminate_process_tree
from backend.tools.base import truncate_text_tail


def _python_command(script: str) -> str:
    args = [sys.executable, "-u", "-c", script]
    return subprocess.list2cmdline(args) if sys.platform == "win32" else shlex.join(args)


@pytest.mark.parametrize("chunk_size", [4096, 160000])
@pytest.mark.parametrize("content", [
    "".join(f"{number:06d} " + "x" * 72 + "\n" for number in range(2000)),
    "".join(f"{number:06d}\n" for number in range(12000)),
    "\u4e2d" * 50000,
], ids=["byte-limit", "line-limit", "long-unicode-line"])
def test_capture_snapshot_is_the_contiguous_end_of_the_stream(content, chunk_size):
    capture = _OutputCapture(preserve_full_output=False, prefix="test")
    encoded = content.encode("utf-8")
    for offset in range(0, len(encoded), chunk_size):
        capture.append(encoded[offset:offset + chunk_size])

    assert capture.snapshot() == truncate_text_tail(content).content
    assert capture.total_bytes == len(encoded)
    assert len(capture._tail) <= runner_module._CAPTURED_OUTPUT_BYTES


def test_capture_preserves_the_complete_file_and_finishes_once(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "DATA_ROOT", tmp_path)
    capture = _OutputCapture(preserve_full_output=True, prefix="test")
    payload = b"BEGIN\n" + b"middle\n" * 30000 + b"END\n"
    for offset in range(0, len(payload), 4096):
        capture.append(payload[offset:offset + 4096])
    capture.finish()
    first_path = capture.path
    capture.finish()
    capture.close()

    assert capture.path == first_path
    assert Path(first_path).read_bytes() == payload
    assert len(list((tmp_path / "tool-results").iterdir())) == 1


def test_failed_finish_never_publishes_a_new_empty_output_file(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "DATA_ROOT", tmp_path)
    capture = _OutputCapture(preserve_full_output=True, prefix="test")
    capture.append(b"x" * 70000)

    with monkeypatch.context() as fault:
        fault.setattr(runner_module.os, "fsync", Mock(side_effect=OSError(28, "AUDIT_DISK_FULL")))
        with pytest.raises(OSError, match="AUDIT_DISK_FULL"):
            capture.finish()
    capture.finish()

    assert capture.path == ""
    assert list((tmp_path / "tool-results").iterdir()) == []


@pytest.mark.parametrize("failure", ["create", "write", "fsync"])
def test_capture_failure_reports_the_error_and_releases_process_and_readers(
    tmp_path, monkeypatch, failure,
):
    monkeypatch.setattr(runner_module, "DATA_ROOT", tmp_path)
    if failure == "create":
        monkeypatch.setattr(
            runner_module.tempfile, "NamedTemporaryFile",
            Mock(side_effect=FileNotFoundError("AUDIT_OUTPUT_DIRECTORY_UNAVAILABLE")),
        )
    elif failure == "write":
        create_file = runner_module.tempfile.NamedTemporaryFile

        def failing_file(**kwargs):
            handle = create_file(**kwargs)
            wrapped = Mock(wraps=handle)
            wrapped.name = handle.name
            wrapped.write.side_effect = OSError(28, "AUDIT_DISK_FULL")
            return wrapped

        monkeypatch.setattr(runner_module.tempfile, "NamedTemporaryFile", failing_file)
    else:
        monkeypatch.setattr(runner_module.os, "fsync", Mock(side_effect=OSError(28, "AUDIT_DISK_FULL")))

    async def run():
        owned = []
        script = "import sys,time; sys.stdout.buffer.write(b'x'*70000); sys.stdout.flush()"
        if failure != "fsync":
            script += "; time.sleep(20)"
        try:
            result = await SandboxRunner(SandboxPolicy.bypass(timeout=10)).run(
                _python_command(script), cwd=tmp_path,
                process_ready_callback=owned.append, preserve_full_output=True,
            )
            assert owned[0].returncode is not None
            assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            return result
        finally:
            for process in owned:
                await terminate_process_tree(process)

    result = asyncio.run(run())

    assert result.exit_code == 1
    assert "AUDIT_" in result.stderr
    assert "Command not found" not in result.stderr
    assert result.stdout and result.stdout_total_bytes >= 51200
    assert not result.stdout_path and not result.stderr_path
    assert not result.cleanup_pending
    assert list((tmp_path / "tool-results").glob("*.log")) == []


def test_started_callback_future_is_awaited_and_failure_releases_the_process(tmp_path):
    async def run():
        owned = []

        def failed_start(_pid):
            future = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(
                future.set_exception, RuntimeError("AUDIT_OWNER_WRITE_FAILED"),
            )
            return future

        try:
            with pytest.raises(RuntimeError, match="AUDIT_OWNER_WRITE_FAILED"):
                await SandboxRunner(SandboxPolicy.bypass(timeout=10)).run(
                    _python_command("import time; time.sleep(20)"), cwd=tmp_path,
                    process_ready_callback=owned.append, process_started_callback=failed_start,
                )
            assert owned[0].returncode is not None
            assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        finally:
            for process in owned:
                await terminate_process_tree(process)

    asyncio.run(run())
