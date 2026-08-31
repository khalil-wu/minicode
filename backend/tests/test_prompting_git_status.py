from __future__ import annotations

import subprocess

from backend.agent.prompting import build_git_status_context


def test_git_status_context_tolerates_successful_command_without_stdout(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        args = command[2:]
        if args == ["branch", "--show-current"]:
            return subprocess.CompletedProcess(command, 0, stdout="main\n", stderr="")
        if args == ["status", "--short"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout=None, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    context = build_git_status_context(tmp_path)

    assert "Current branch: main" in context
    assert "Status:\n(clean)" in context
    assert "Recent commits:\n" in context
