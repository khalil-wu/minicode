from __future__ import annotations

import subprocess

from backend.agent.prompting import build_git_status_context
from backend.tests.git_sandbox_policy import readable_host_git_policy


def test_git_status_context_tolerates_successful_command_without_stdout(monkeypatch, tmp_path):
    from backend.tools import git_support

    async def fake_run(command, **kwargs):
        args = command[1:]
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(command, 0, stdout=b"true\n", stderr=b"")
        if args == ["branch", "--show-current"]:
            return subprocess.CompletedProcess(command, 0, stdout=b"main\n", stderr=b"")
        # The real bounded Git transport returns empty bytes, not an absent
        # stdout attribute/None. Keep this fixture on its production contract.
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(git_support, "_run_git", fake_run)

    context = build_git_status_context(tmp_path)

    assert "Current branch: main" in context
    assert "Status:\n(clean)" in context
    assert "Recent commits:\n" in context


# Focused launch-boundary regressions; no audit of legacy test bodies.
def _prompt_git_launch_review_result(argv):
    import subprocess
    values = {"rev-parse": b"true\n", "branch": b"review-branch\n",
              "symbolic-ref": b"refs/remotes/origin/main\n", "config": b"review-user\n",
              "status": b" M tracked.py\n", "log": b"abcdef fixture\n"}
    return subprocess.CompletedProcess(argv, 0, values[argv[1]], b"")


def _prompt_git_launch_review_context(root, snapshot):
    from backend.agent.run_context import RunContext
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    loans = []
    owner = set()
    run = RunContext(lifecycle_cleanup_tasks=owner,
                          retain_model=lambda adapter, task: loans.append((adapter, task)))
    context = ToolExecutionContext(PermissionContext(mode="plan", allow_unsandboxed_commands=False),
                                   workspace_root=root, sandbox_policy=snapshot, run_context=run,
                                   pending_cleanup_tasks=owner, llm=object())
    return context, loans


def test_prompt_git_launch_review_both_entrypoints_pin_captured_policy_and_adapter(monkeypatch, tmp_path):
    import asyncio
    from backend.agent import prompting
    from backend.sandbox import SandboxPolicy
    from backend.tools import git_support

    snapshot = SandboxPolicy(workspace_root=tmp_path)
    replacement = SandboxPolicy.workspace_default(tmp_path)
    calls = []
    captured = []
    async def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs["sandbox_policy"] is snapshot
        assert kwargs["timeout"] == prompting._GIT_COMMAND_TIMEOUT_SECONDS
        kwargs["context"].sandbox_policy = replacement
        kwargs["context"].llm = object()
        return _prompt_git_launch_review_result(argv)
    monkeypatch.setattr(git_support, "_run_git", execute)
    for synchronous in (False, True):
        context, loans = _prompt_git_launch_review_context(tmp_path, snapshot)
        captured.append(context.llm)
        result = (prompting.build_git_status_context(tmp_path, context=context)
                  if synchronous else asyncio.run(prompting.build_git_status_context_async(tmp_path, context=context)))
        assert "Current branch: review-branch" in result
        assert "Git user: review-user" in result
        assert "M tracked.py" in result
        assert len(loans) == 6
        assert all(adapter is captured[-1] for adapter, _task in loans)
        assert not context.pending_cleanup_tasks
    assert len(calls) == 12


def test_prompt_git_launch_review_explicit_policy_keyword_and_projectless(monkeypatch, tmp_path):
    import asyncio
    from backend.agent import prompting
    from backend.sandbox import SandboxPolicy
    from backend.tools import git_support

    snapshot = SandboxPolicy(workspace_root=tmp_path)
    context, _loans = _prompt_git_launch_review_context(tmp_path, SandboxPolicy.bypass())
    calls = []
    async def execute(argv, **kwargs):
        calls.append(kwargs)
        assert kwargs["sandbox_policy"] is snapshot
        return _prompt_git_launch_review_result(argv)
    monkeypatch.setattr(git_support, "_run_git", execute)
    assert prompting.build_git_status_context(tmp_path, context=context, sandbox_policy=snapshot)
    assert asyncio.run(prompting.build_git_status_context_async(None, context=context)) == ""
    assert prompting.build_git_status_context(None, context=context) == ""
    assert len(calls) == 6


def test_prompt_git_launch_review_gather_error_keeps_actual_sibling_owners(monkeypatch, tmp_path):
    import asyncio
    import pytest
    from backend.agent import prompting
    from backend.sandbox import SandboxPolicy
    from backend.tools import git_support

    async def exercise():
        context, loans = _prompt_git_launch_review_context(tmp_path, SandboxPolicy(workspace_root=tmp_path))
        release = asyncio.Event()
        started = asyncio.Event()
        count = 0
        async def execute(argv, **kwargs):
            nonlocal count
            if argv[1] == "rev-parse":
                return _prompt_git_launch_review_result(argv)
            count += 1
            if count == 5:
                started.set()
            await started.wait()
            if argv[1] == "status":
                raise RuntimeError("observed Git launch failure")
            await release.wait()
            return _prompt_git_launch_review_result(argv)
        monkeypatch.setattr(git_support, "_run_git", execute)
        try:
            with pytest.raises(RuntimeError, match="observed Git launch failure"):
                await prompting.build_git_status_context_async(tmp_path, context=context)
            await asyncio.sleep(0)
            pending = set(context.pending_cleanup_tasks)
            assert len(pending) == 4
            assert pending <= {task for _adapter, task in loans}
            assert all(not task.done() for task in pending)
        finally:
            release.set()
            await asyncio.gather(*context.pending_cleanup_tasks, return_exceptions=True)
            await asyncio.sleep(0)
        assert not context.pending_cleanup_tasks
    asyncio.run(exercise())


def test_prompt_git_launch_review_cancel_does_not_retire_stubborn_child(monkeypatch, tmp_path):
    import asyncio
    import pytest
    from backend.agent import prompting
    from backend.sandbox import SandboxPolicy
    from backend.tools import git_support

    async def exercise():
        context, loans = _prompt_git_launch_review_context(tmp_path, SandboxPolicy(workspace_root=tmp_path))
        started = asyncio.Event()
        cleanup_started = asyncio.Event()
        release = asyncio.Event()
        count = 0
        status_task = None
        async def execute(argv, **kwargs):
            nonlocal count, status_task
            if argv[1] == "rev-parse":
                return _prompt_git_launch_review_result(argv)
            if argv[1] == "status":
                status_task = asyncio.current_task()
            count += 1
            if count == 5:
                started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if argv[1] == "status":
                    cleanup_started.set()
                    await release.wait()
                raise
        monkeypatch.setattr(git_support, "_run_git", execute)
        parent = asyncio.create_task(prompting.build_git_status_context_async(tmp_path, context=context))
        try:
            await asyncio.wait_for(started.wait(), 1)
            parent.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(parent, 1)
            await asyncio.wait_for(cleanup_started.wait(), 1)
            assert not status_task.done()
            assert status_task in context.pending_cleanup_tasks
            assert status_task in {task for _adapter, task in loans}
        finally:
            release.set()
            await asyncio.gather(*context.pending_cleanup_tasks, return_exceptions=True)
            await asyncio.sleep(0)
        assert not context.pending_cleanup_tasks
    asyncio.run(exercise())


def test_prompt_git_launch_review_failed_status_is_not_clean_or_host_retry(monkeypatch, tmp_path):
    import asyncio
    import subprocess
    import pytest
    from backend.agent import prompting
    from backend.sandbox import SandboxPolicy, SandboxUnavailableError
    from backend.tools import git_support

    async def failed_status(argv, **kwargs):
        if argv[1] == "status":
            return subprocess.CompletedProcess(argv, 7, b"", b"permission denied")
        return _prompt_git_launch_review_result(argv)
    monkeypatch.setattr(git_support, "_run_git", failed_status)
    assert asyncio.run(prompting.build_git_status_context_async(tmp_path, sandbox_policy=SandboxPolicy(workspace_root=tmp_path))) == ""
    async def unavailable(argv, **kwargs):
        raise SandboxUnavailableError("actual sandbox boundary unavailable")
    monkeypatch.setattr(git_support, "_run_git", unavailable)
    unavailable_snapshot = prompting.build_git_status_context(tmp_path, sandbox_policy=SandboxPolicy(workspace_root=tmp_path))
    assert "snapshot is unavailable" in unavailable_snapshot
    assert "does not mean the workspace is clean" in unavailable_snapshot
    assert "(clean)" not in unavailable_snapshot


def test_prompt_git_launch_review_real_fsmonitor_and_cancel(tmp_path, monkeypatch):
    import asyncio
    import json
    import subprocess
    import pytest
    from backend.agent import prompting
    from backend.runtime_env import sanitized_git_env
    from backend.sandbox import SandboxPolicy, SandboxRunner
    from backend.sandbox.runner import _container_readable_target
    from backend.tools import git_support

    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "tracked.py").write_text("initial\n", encoding="utf-8")
    def host(*args):
        return subprocess.run(["git", *args], cwd=root, env=sanitized_git_env(),
                              capture_output=True, timeout=10, check=True)
    host("init")
    host("config", "user.name", "Prompt Git Review")
    host("config", "user.email", "prompt-git@example.invalid")
    host("add", "-A")
    host("commit", "-m", "fixture")
    snapshot = readable_host_git_policy(SandboxPolicy(workspace_root=root, readable_roots=(outside,)))
    capability = SandboxRunner(snapshot).capability(cwd=root)
    assert capability.available and capability.filesystem_isolated, capability.reason
    resolved = snapshot.resolve(cwd=root)
    target = outside.as_posix()
    if capability.backend in {"docker", "podman"}:
        target = next(_container_readable_target(path, index, resolved, root)[0]
                      for index, path in enumerate(resolved.readable_roots) if path == outside)
    monitor = root / "fsmonitor.sh"
    monitor.write_text(
        "#!/bin/sh\necho PROMPT_GIT_FSMONITOR_ATTEMPTED >&2\n"
        f"if [ -d /workspace/.git ]; then target='{target}'; else target='{outside.as_posix()}'; fi\n"
        "printf escaped > \"$target/marker\"\nprintf 'token\\0/\\0'\n",
        encoding="utf-8", newline="\n")
    monitor.chmod(0o755)
    host("config", "core.fsmonitor", "./fsmonitor.sh")
    baseline = host("status", "--short")
    assert b"PROMPT_GIT_FSMONITOR_ATTEMPTED" in baseline.stderr
    assert (outside / "marker").read_text() == "escaped"
    (outside / "marker").unlink()
    (root / "tracked.py").write_text("changed\n", encoding="utf-8")
    original = git_support._run_git
    observed = []
    async def record(argv, **kwargs):
        result = await original(argv, **kwargs)
        observed.append((argv, kwargs, result))
        return result
    monkeypatch.setattr(git_support, "_run_git", record)
    context, _loans = _prompt_git_launch_review_context(root, snapshot)
    result = asyncio.run(prompting.build_git_status_context_async(root, context=context))
    assert "M tracked.py" in result
    status = next(entry for entry in observed if entry[0][1] == "status")
    assert b"PROMPT_GIT_FSMONITOR_ATTEMPTED" in status[2].stderr
    assert all(entry[1]["sandbox_policy"] is snapshot for entry in observed)
    assert not (outside / "marker").exists()
    assert not context.pending_cleanup_tasks

    monitor.write_text("#!/bin/sh\nprintf started > prompt-hook-started\nsleep 60\nprintf late > prompt-hook-late\nprintf 'token\\0'\n",
                       encoding="utf-8", newline="\n")
    writable = readable_host_git_policy(SandboxPolicy.workspace_default(root))
    async def cancel_actual():
        context, _loans = _prompt_git_launch_review_context(root, writable)
        context.cancel_event = asyncio.Event()
        parent = asyncio.create_task(prompting.build_git_status_context_async(root, context=context))
        try:
            async def started():
                while not (root / "prompt-hook-started").exists():
                    if parent.done():
                        parent.result()
                        pytest.fail("real prompt status hook did not start")
                    await asyncio.sleep(0.02)
            await asyncio.wait_for(started(), 10)
        finally:
            context.cancel_event.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(parent, 10)
            # The gather can finish before another cancelled Git child's cleanup.
            await asyncio.wait_for(asyncio.gather(*context.pending_cleanup_tasks, return_exceptions=True), 10)
            await asyncio.sleep(0)
        assert not context.pending_cleanup_tasks
        assert not (root / "prompt-hook-late").exists()
    asyncio.run(cancel_actual())
    print("PROMPT_GIT_REAL_ORACLE=" + json.dumps({"backend": capability.backend,
          "host_positive_outside_write": True, "restricted_prompt_fsmonitor_ran": True,
          "restricted_outside_write": False, "captured_snapshot_forwarded": True,
          "cancel_actual_hook_started": True, "late_mutation": False, "shared_owner_settled": True}))
