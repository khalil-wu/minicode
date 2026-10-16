"""Project initialization using the selected executor's tools and files."""
from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import anyio

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from backend.workspace.executor_commands import require_workspace_registration, run_workbench_command, target_programs
from backend.workspace.executor_service import executor_registration_id


async def executor_environment_status(service):
    programs = await target_programs(service)
    windows = service.cwd.convention == "windows"
    names = ("py", "python", "python3") if windows else ("python3", "python")
    interpreter = next((name for name in names if name in programs), None)
    python = [programs[interpreter], *(["-3"] if interpreter == "py" else [])] if interpreter else None
    commands = {}
    backend_python, version = None, ""
    if python is not None:
        result = await service.run([*python, "-c", "import sys,json;print(json.dumps([sys.executable,sys.version.split()[0]]))"])
        if result.returncode:
            raise HTTPException(503, result.stderr.decode("utf-8", errors="replace").strip())
        backend_python, version = json.loads(result.stdout)
        commands["python_venv"] = [backend_python, "-m", "venv", service.cwd.joinpath(".venv").native]
    project_python = service.cwd.joinpath(".venv/Scripts/python.exe" if windows else ".venv/bin/python")
    installed = await service.metadata(project_python)
    if installed is not None and installed["isFile"]:
        if await service.metadata(service.cwd.joinpath("requirements.txt")) is not None:
            commands["python_dependencies"] = [project_python.native, "-m", "pip", "install", "-r", "requirements.txt"]
        elif await service.metadata(service.cwd.joinpath("pyproject.toml")) is not None:
            commands["python_dependencies"] = [project_python.native, "-m", "pip", "install", "-e", "."]
    if await service.metadata(service.cwd.joinpath("package.json")) is not None:
        manager = "pnpm" if await service.metadata(service.cwd.joinpath("pnpm-lock.yaml")) is not None \
            else "yarn" if await service.metadata(service.cwd.joinpath("yarn.lock")) is not None else "npm"
        if manager in programs:
            action = "ci" if manager == "npm" and await service.metadata(service.cwd.joinpath("package-lock.json")) is not None else "install"
            commands["node_dependencies"] = [programs[manager], action]
    return {"backend_python": backend_python, "python_version": version,
        "project_python": project_python.native if installed is not None and installed["isFile"] else None,
        "node": programs.get("node"), "commands": commands,
        "display_commands": {name: subprocess.list2cmdline(argv) if windows else shlex.join(argv) for name, argv in commands.items()},
        "programs": programs, "registration_id": executor_registration_id(service.context), **service.identity()}


async def executor_environment_setup(service, request):
    require_workspace_registration(service, request.expected_registration_id)
    status = await executor_environment_status(service)
    if request.action not in status["commands"]:
        raise HTTPException(409, "当前项目尚不具备此初始化条件，请刷新环境信息。")
    argv = status["commands"][request.action]

    async def events():
        queue = asyncio.Queue(maxsize=16)
        streaming = True
        async def output(text, _stream):
            if streaming:
                await queue.put({"output": text})
        task = asyncio.create_task(run_workbench_command(service, argv, timeout=None,
            stream_callback=output, allow_failure=True), name="executor-project-setup")
        waiting = None
        try:
            while True:
                waiting = asyncio.create_task(queue.get())
                finished, _ = await asyncio.wait({task, waiting}, return_when=asyncio.FIRST_COMPLETED)
                if waiting in finished:
                    yield json.dumps(waiting.result(), ensure_ascii=False) + "\n"
                if task in finished:
                    if not waiting.done():
                        waiting.cancel()
                        await asyncio.gather(waiting, return_exceptions=True)
                    while not queue.empty():
                        yield json.dumps(queue.get_nowait(), ensure_ascii=False) + "\n"
                    result = await task
                    yield json.dumps({"exit_code": result.exit_code}) + "\n"
                    return
        finally:
            # Starlette cancels the streaming generator inside an AnyIO scope.
            # Keep that level cancellation out of the original command's close.
            with anyio.CancelScope(shield=True):
                streaming = False
                while not queue.empty():
                    queue.get_nowait()
                if waiting is not None and not waiting.done():
                    waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)
                if not task.done() and not task.cancelling():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    return StreamingResponse(events(), media_type="application/x-ndjson")
