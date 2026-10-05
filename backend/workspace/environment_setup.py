from __future__ import annotations
import asyncio
import json
import os
import shutil
import sys
import subprocess
import codecs
import shlex
from pathlib import Path
from typing import Literal
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from backend.runtime_env import sanitized_subprocess_env
from backend.subprocesses import spawn_exec, terminate_process_tree


class EnvironmentSetupRequest(BaseModel):
    action: Literal["python_venv", "python_dependencies", "node_dependencies"]


def environment_commands(root: Path) -> dict[str, list[str]]:
    launcher = shutil.which("py") if os.name == "nt" else None
    interpreter = [launcher, "-3"] if launcher else [sys.executable]
    commands = {"python_venv": [*interpreter, "-m", "venv", str(root / ".venv")]}
    python = root / (
        ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
    )
    if python.is_file():
        if (root / "requirements.txt").is_file():
            commands["python_dependencies"] = [
                str(python),
                "-m",
                "pip",
                "install",
                "-r",
                "requirements.txt",
            ]
        elif (root / "pyproject.toml").is_file():
            commands["python_dependencies"] = [
                str(python),
                "-m",
                "pip",
                "install",
                "-e",
                ".",
            ]
    if (root / "package.json").is_file():
        manager = (
            "pnpm"
            if (root / "pnpm-lock.yaml").is_file()
            else "yarn"
            if (root / "yarn.lock").is_file()
            else "npm"
        )
        executable = shutil.which(manager)
        if executable:
            commands["node_dependencies"] = [
                executable,
                "ci"
                if manager == "npm" and (root / "package-lock.json").is_file()
                else "install",
            ]
    return commands


def environment_setup_status(root: Path):
    python = root / (
        ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
    )
    commands = environment_commands(root)
    return {
        "backend_python": sys.executable,
        "python_version": sys.version.split()[0],
        "project_python": str(python) if python.is_file() else None,
        "node": shutil.which("node"),
        "commands": commands,
        "display_commands": {
            name: subprocess.list2cmdline(command)
            if os.name == "nt"
            else shlex.join(command)
            for name, command in commands.items()
        },
    }


def environment_setup_response(root: Path, request: EnvironmentSetupRequest):
    commands = environment_commands(root)
    if request.action not in commands:
        raise HTTPException(
            status_code=409, detail="当前项目尚不具备此初始化条件，请刷新环境信息。"
        )
    command = commands[request.action]
    if os.name == "nt" and Path(command[0]).suffix.lower() in {".cmd", ".bat"}:
        command = [
            os.environ["COMSPEC"],
            "/d",
            "/s",
            "/c",
            '"' + subprocess.list2cmdline(command) + '"',
        ]

    async def run():
        process = await spawn_exec(
            *command,
            cwd=root,
            env=sanitized_subprocess_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while chunk := await process.stdout.read(4096):
                yield (
                    json.dumps({"output": decoder.decode(chunk)}, ensure_ascii=False)
                    + "\n"
                )
            final_text = decoder.decode(b"", final=True)
            if final_text:
                yield json.dumps({"output": final_text}, ensure_ascii=False) + "\n"
            code = await process.wait()
            yield json.dumps({"exit_code": code}) + "\n"
        finally:
            if process.returncode is None:
                await terminate_process_tree(process)

    return StreamingResponse(run(), media_type="application/x-ndjson")
