import asyncio
import json
import shutil
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from backend.preview import launcher
from backend.sandbox.runner import SandboxUnavailableError


TEST_ROOT = Path(".testdata_preview_launcher")


def _workspace(name: str) -> Path:
    path = TEST_ROOT / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path.resolve()


def test_preview_launcher_infers_package_json_dev_script():
    workspace = _workspace("infer_package_json")
    (workspace / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite --host 127.0.0.1"}}),
        encoding="utf-8",
    )

    configs = launcher.load_preview_launch_configs(workspace)

    assert len(configs) == 1
    assert configs[0].name == "npm run dev"
    assert configs[0].command == "npm run dev"
    assert configs[0].port == 5173
    assert configs[0].source == "package.json"


def test_preview_launcher_reads_minicode_launch_json():
    workspace = _workspace("minicode_launch_json")
    minicode = workspace / ".minicode"
    minicode.mkdir()
    (minicode / "launch.json").write_text(
        json.dumps({
            "configurations": [
                {
                    "name": "web",
                    "command": "npm run dev",
                    "cwd": ".",
                    "port": 3000,
                    "url": "http://127.0.0.1:3000",
                }
            ]
        }),
        encoding="utf-8",
    )

    configs = launcher.load_preview_launch_configs(workspace)

    assert len(configs) == 1
    assert configs[0].name == "web"
    assert configs[0].cwd == str(workspace)
    assert configs[0].source == ".minicode/launch.json"


def test_preview_launcher_rejects_malformed_launch_json_instead_of_guessing():
    """A misconfigured launch.json must never be replaced by a package.json guess.

    Before this, a trailing comma or a wrong key yielded ``configs == []`` and the
    loader silently substituted an inferred ``npm run dev``, so the user's
    explicit choice was overruled with no signal.
    """
    workspace = _workspace("malformed_launch_json")
    (workspace / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}}),
        encoding="utf-8",
    )
    minicode = workspace / ".minicode"
    minicode.mkdir()
    (minicode / "launch.json").write_text('{"configurations": [,]}', encoding="utf-8")

    with pytest.raises(launcher.PreviewLaunchConfigError) as excinfo:
        launcher.load_preview_launch_configs(workspace)

    assert excinfo.value.source == ".minicode/launch.json"
    assert "invalid JSON" in excinfo.value.reason


def test_preview_launcher_rejects_launch_json_entry_without_a_command():
    workspace = _workspace("launch_json_missing_command")
    minicode = workspace / ".minicode"
    minicode.mkdir()
    (minicode / "launch.json").write_text(
        json.dumps({"configurations": [{"name": "web", "port": 3000}]}),
        encoding="utf-8",
    )

    with pytest.raises(launcher.PreviewLaunchConfigError) as excinfo:
        launcher.load_preview_launch_configs(workspace)

    assert "configurations[0]" in excinfo.value.reason


def test_preview_launcher_start_stop_with_fake_process(monkeypatch):
    launcher._RUNNING.clear()
    workspace = _workspace("start_stop")
    (workspace / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}}),
        encoding="utf-8",
    )

    class FakeProcess:
        pid = 4321
        returncode = None
        terminated = False

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.returncode = -9

        async def wait(self):
            self.returncode = 0 if self.terminated and self.returncode is None else self.returncode
            return self.returncode

    calls = []

    class FakeSandboxRunner:
        def __init__(self, policy):
            self.policy = policy

        async def spawn_shell_interactive(self, *args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess()

        async def terminate(self, process):
            process.terminate()
            await process.wait()
            return True

    async def fake_monitor(*args, **kwargs):
        return None

    monkeypatch.setattr(launcher, "SandboxRunner", FakeSandboxRunner)
    monkeypatch.setattr(launcher, "_monitor_process", fake_monitor)

    process = asyncio.run(launcher.start_preview_launch(
        workspace,
        session_id="session-start-stop",
        conversation_id="conv-start-stop",
    ))
    stopped = asyncio.run(launcher.stop_preview_launch(
        session_id="session-start-stop",
        conversation_id="conv-start-stop",
    ))

    assert process.process.pid == 4321
    assert process.config.name == "npm run dev"
    assert calls[0][0] == ("npm run dev",)
    assert stopped[0].id == process.id


def test_preview_process_identity_is_scoped_by_workspace(monkeypatch):
    launcher._RUNNING.clear()
    first_workspace = _workspace("identity-one")
    second_workspace = _workspace("identity-two")
    for workspace in (first_workspace, second_workspace):
        (workspace / "package.json").write_text(
            json.dumps({"scripts": {"dev": "vite"}}),
            encoding="utf-8",
        )

    class FakeProcess:
        pid = 4321
        returncode = None
        stdout = None
        stderr = None

        async def wait(self):
            return 0

    class FakeSandboxRunner:
        def __init__(self, policy):
            self.policy = policy

        async def spawn_shell_interactive(self, *_args, **_kwargs):
            return FakeProcess()

        async def terminate(self, process):
            process.returncode = 0
            return True

    async def fake_monitor(*_args, **_kwargs):
        return None

    monkeypatch.setattr(launcher, "SandboxRunner", FakeSandboxRunner)
    monkeypatch.setattr(launcher, "_monitor_process", fake_monitor)

    async def scenario():
        first = await launcher.start_preview_launch(
            first_workspace,
            session_id="session-identity",
            conversation_id="conv-identity",
        )
        second = await launcher.start_preview_launch(
            second_workspace,
            session_id="session-identity",
            conversation_id="conv-identity",
        )
        assert first.id != second.id
        assert first is not second

    asyncio.run(scenario())


def test_preview_list_and_stop_are_isolated_by_conversation_owner(monkeypatch, tmp_path):
    launcher._RUNNING.clear()

    class FakeProcess:
        pid = 4321

        def __init__(self):
            self.returncode = None

    async def fake_terminate(process):
        process.returncode = 0
        return True

    monkeypatch.setattr(launcher, "terminate_process_tree", fake_terminate)
    workspace = str(tmp_path.resolve())
    config = launcher.PreviewLaunchConfig(
        name="web",
        command="npm run dev",
        cwd=workspace,
        port=5173,
        url="http://127.0.0.1:5173",
    )
    first = launcher.PreviewLaunchProcess(
        id="preview-owner-a",
        config=config,
        process=FakeProcess(),
        session_id="session-shared",
        conversation_id="conv-a",
        workspace_root=workspace,
    )
    second = launcher.PreviewLaunchProcess(
        id="preview-owner-b",
        config=config,
        process=FakeProcess(),
        session_id="session-shared",
        conversation_id="conv-b",
        workspace_root=workspace,
    )
    launcher._RUNNING[first.id] = first
    launcher._RUNNING[second.id] = second

    assert launcher.running_preview_processes(
        session_id="session-shared",
        conversation_id="conv-a",
        workspace_root=workspace,
    ) == [first]
    assert launcher.running_preview_processes(
        session_id="session-shared",
        conversation_id="conv-b",
        workspace_root=workspace,
    ) == [second]

    stopped = asyncio.run(launcher.stop_preview_launch(
        session_id="session-shared",
        conversation_id="conv-a",
        workspace_root=workspace,
    ))

    assert stopped == [first]
    assert first.id not in launcher._RUNNING
    assert launcher._RUNNING[second.id] is second
    launcher._RUNNING.clear()


def test_preview_launcher_monitor_broadcasts_ready_and_crashed():
    class FakeStream:
        def __init__(self, lines):
            self.lines = [line.encode("utf-8") for line in lines]

        async def readline(self):
            await asyncio.sleep(0)
            if self.lines:
                return self.lines.pop(0)
            return b""

    class FakeProcess:
        pid = 8765
        returncode = 1

        def __init__(self):
            self.stdout = FakeStream(["Local: http://127.0.0.1:5179\n"])
            self.stderr = FakeStream(["build failed\n"])

        async def wait(self):
            return self.returncode

    events = []

    async def broadcast(event):
        events.append(event)

    config = launcher.PreviewLaunchConfig(
        name="web",
        command="npm run dev",
        cwd=str(Path.cwd()),
        port=5173,
        url="http://127.0.0.1:5173",
    )
    launched = launcher.PreviewLaunchProcess(
        id="web",
        config=config,
        process=FakeProcess(),
        session_id="session-monitor",
        conversation_id="conv-monitor",
        workspace_root=str(Path.cwd()),
    )

    asyncio.run(launcher._monitor_process(launched, broadcast))

    output_events = [event for event in events if event["type"] == "preview.server.output"]
    ready_events = [event for event in events if event["type"] == "preview.server.ready"]
    assert any(
        event["id"] == "web"
        and event["stream"] == "stdout"
        and event["line"] == "Local: http://127.0.0.1:5179"
        and event["conversation_id"] == "conv-monitor"
        for event in output_events
    )
    assert any(
        event["id"] == "web"
        and event["stream"] == "stderr"
        and event["line"] == "build failed"
        and event["conversation_id"] == "conv-monitor"
        for event in output_events
    )
    assert ready_events[0]["url"] == "http://127.0.0.1:5179"
    assert launched.status == "crashed"
    assert list(launched.output_tail) == [
        {"stream": "stdout", "line": "Local: http://127.0.0.1:5179"},
        {"stream": "stderr", "line": "build failed"},
    ]
    assert events[-1]["type"] == "preview.server.crashed"
    assert events[-1]["stderr_tail"] == ["build failed"]


def test_static_preview_preserves_html_url_and_stops_cleanly(tmp_path):
    launcher._RUNNING.clear()
    html = tmp_path / "snake.html"
    html.write_text("<!doctype html><title>Snake</title>", encoding="utf-8")

    async def scenario():
        process = await launcher.start_static_preview(
            tmp_path,
            "snake.html",
            session_id="session-static",
            conversation_id="conv-static",
        )
        try:
            from backend.preview.verifier import wait_until_ready

            verification = await wait_until_ready(process.effective_url, timeout=10.0, interval=0.1)
            assert verification.ok
            await launcher.mark_preview_ready(process)
            assert process.effective_url.endswith("/snake.html")
            assert process.config.source == "static-html"
            assert process.status == "ready"
        finally:
            await launcher.stop_preview_launch(
                process.id,
                session_id="session-static",
                conversation_id="conv-static",
            )

    try:
        asyncio.run(scenario())
    except SandboxUnavailableError as exc:
        pytest.skip(f"enforceable preview sandbox unavailable: {exc}")


def test_static_preview_serves_assets_but_denies_workspace_secrets(tmp_path):
    launcher._RUNNING.clear()
    (tmp_path / "index.html").write_text('<link rel="stylesheet" href="site.css">', encoding="utf-8")
    (tmp_path / "site.css").write_text("body { color: green; }\n", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=secret\n", encoding="utf-8")

    async def scenario():
        process = await launcher.start_static_preview(
            tmp_path,
            "index.html",
            session_id="session-static-secret",
            conversation_id="conv-static-secret",
        )
        try:
            from backend.preview.verifier import wait_until_ready

            verification = await wait_until_ready(process.effective_url, timeout=10.0, interval=0.1)
            assert verification.ok
            base_url = process.effective_url.rsplit("/", 1)[0]
            css = await asyncio.to_thread(lambda: urlopen(f"{base_url}/site.css", timeout=3).read())
            assert b"color: green" in css
            try:
                await asyncio.to_thread(lambda: urlopen(f"{base_url}/.env", timeout=3).read())
            except HTTPError as exc:
                assert exc.code == 404
            else:
                raise AssertionError("static preview exposed .env")
        finally:
            await launcher.stop_preview_launch(
                process.id,
                session_id="session-static-secret",
                conversation_id="conv-static-secret",
            )

    try:
        asyncio.run(scenario())
    except SandboxUnavailableError as exc:
        pytest.skip(f"enforceable preview sandbox unavailable: {exc}")


def test_static_monitor_does_not_replace_file_url_with_server_root():
    class FakeStream:
        def __init__(self, lines):
            self.lines = [line.encode("utf-8") for line in lines]

        async def readline(self):
            await asyncio.sleep(0)
            return self.lines.pop(0) if self.lines else b""

    class FakeProcess:
        pid = 1234
        returncode = 0

        def __init__(self):
            self.stdout = FakeStream(["Serving HTTP on 127.0.0.1 port 43123 (http://127.0.0.1:43123/)\n"])
            self.stderr = FakeStream([])

        async def wait(self):
            return self.returncode

    events = []
    config = launcher.PreviewLaunchConfig(
        name="static-test",
        command="python -m http.server",
        cwd=str(Path.cwd()),
        port=43123,
        url="http://127.0.0.1:43123/snake.html",
        source="static-html",
    )
    launched = launcher.PreviewLaunchProcess(
        "static-test",
        config,
        FakeProcess(),
        session_id="session-static-monitor",
        conversation_id="conv-static-monitor",
        workspace_root=str(Path.cwd()),
    )

    async def broadcast(event):
        events.append(event)

    asyncio.run(launcher._monitor_process(launched, broadcast))

    ready = next(event for event in events if event["type"] == "preview.server.ready")
    assert ready["url"].endswith("/snake.html")
    assert launched.effective_url.endswith("/snake.html")
