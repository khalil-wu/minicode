import asyncio
import logging
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.config import AgentSettings, AppConfig, LLMSettings, PermissionSettings
from backend.artifact.store import ArtifactStore
from backend.atomic_io import canonical_file_path_key
from backend.checkpoint import CheckpointManager, CheckpointStore
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.checker import PermissionChecker
from backend.permissions.profiles import (
    permission_profile_for_mode,
    sandbox_status_for,
    workspace_scope_for,
)
from backend.sandbox import SandboxPolicy
from backend.tools.command_tool import RunCommandTool
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.file_tools_common import content_hash
from backend.tools.file_tools import (
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    WriteFileTool,
)
from backend.tools.git_tools import GitDiffTool, GitLogTool, GitStatusTool
from backend.tools.search_tools import GlobFilesTool, GrepFilesTool
from backend.tools.web_tools import WebFetchTool
from backend.tools.registry import CapabilityRegistry
from backend.tools.contracts import ToolSpec
from backend.agent.message import UserCommand
from backend.agent.message import AgentEvent
from backend.agent.run_events import should_emit_event
from backend.agent.context import ContextBuilder
from backend.agent.prompting import build_static_environment_info
from backend.agent.state import AgentState, ToolCallRecord
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.permission_runtime import SessionPermissionRuntimeMixin
from backend.ws.turn_wait_state import TurnWaitState


def _python_command(script: str) -> str:
    parts = [sys.executable, "-c", script]
    if sys.platform == "win32":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(part) for part in parts)


class _DummyTool(BaseTool):
    name = "dummy_tool"
    description = "Dummy tool for registry tests."
    should_defer = True

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args: dict[str, object]) -> ToolResult:
        return ToolResult(content="ok")


class _CoreDummyTool(_DummyTool):
    name = "core_dummy"
    should_defer = False

    def get_spec(self) -> ToolSpec | None:
        return ToolSpec(
            name=self.name,
            capability="test.core",
            toolset="core",
            exposure="core",
        )


class _HiddenDummyTool(_DummyTool):
    name = "mcp__demo__hidden_dummy"


class _ContextAwareTool(BaseTool):
    name = "context_tool"
    description = "Context-aware tool for registry tests."

    def __init__(self) -> None:
        self.received_contexts: list[ToolExecutionContext | None] = []

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {}},
        )

    async def execute(
        self,
        args: dict[str, object],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.received_contexts.append(context)
        return ToolResult(content="ok")


class _ApprovalRuntime(SessionApprovalRuntimeMixin):
    def __init__(self) -> None:
        self.turn_wait_state = TurnWaitState()
        self.approval_diff_cache: dict[str, dict[str, object]] = {}
        self.sent_events: list[AgentEvent] = []
        # cc waits indefinitely by default; the timeout path is an explicit
        # opt-in exercised by the timeout test.
        self.config = AppConfig(
            agent=AgentSettings(approval_timeout_seconds=300.0),
            llm=LLMSettings(api_key=""),
        )

    async def send_event(self, event: AgentEvent) -> None:
        self.sent_events.append(event)


class _PermissionRuntime(SessionPermissionRuntimeMixin):
    pass


def test_capability_registry_tracks_tools_commands_skills_and_version() -> None:
    registry = CapabilityRegistry()

    tool = _DummyTool()
    registry.register(tool)
    registry.register_command("conversation.list", lambda payload: payload)
    registry.register_skill(
        "summary-memory", {"name": "summary-memory", "description": "remember summary"}
    )

    assert registry.has_tool("dummy_tool") is True
    assert registry.list_commands() == ["conversation.list"]
    assert registry.list_skills() == ["summary-memory"]
    assert registry.get_commands()["conversation.list"] is not None
    assert registry.get_skills()["summary-memory"]["description"] == "remember summary"
    assert registry.version >= 3


def test_capability_registry_builds_stable_snapshot() -> None:
    registry = CapabilityRegistry()

    tool = _DummyTool()
    registry.register(tool)
    registry.register_command("conversation.list", {"source": "builtin"})
    registry.register_skill(
        "summary-memory",
        {
            "name": "summary-memory",
            "description": "remember summary",
            "version": "1.0.0",
        },
    )

    snapshot = registry.build_snapshot()

    assert snapshot["version"] == registry.version
    assert snapshot["tools"] == []
    assert snapshot["tool_views"] == [
        {
            "name": "dummy_tool",
            "exposure": "deferred",
            "direct": False,
            "schema_available": True,
            "toolset": "default",
            "capability": "",
            "permission": "auto",
            "read_only": False,
            "short_description": "Dummy tool for registry tests",
        },
    ]
    assert snapshot["commands"] == [
        {"name": "conversation.list", "source": "builtin"},
    ]
    assert snapshot["skills"] == [
        {
            "description": "remember summary",
            "name": "summary-memory",
            "version": "1.0.0",
        },
    ]
    assert snapshot["summary"] == {
        "tools_total": 1,
        "direct_tools": 0,
        "core_tools": 0,
        "deferred_tools": 1,
        "hidden_tools": 0,
        "mcp_proxy_tools": 0,
        "commands": 1,
        "skills": 1,
        "mcp_resource_bridge": False,
        "mcp_resource_template_bridge": False,
        "mcp_resource_subscription_bridge": False,
        "mcp_prompt_bridge": False,
        "deferred_bridge": False,
        "skill_catalog": True,
    }


def test_capability_registry_tool_views_explain_direct_deferred_and_hidden_tools() -> (
    None
):
    registry = CapabilityRegistry()
    registry.register(_CoreDummyTool())
    registry.register(_DummyTool())
    registry.register(_HiddenDummyTool())

    snapshot = registry.build_snapshot()
    views = {view["name"]: view for view in snapshot["tool_views"]}

    assert views["core_dummy"] == {
        "name": "core_dummy",
        "exposure": "core",
        "direct": True,
        "schema_available": True,
        "toolset": "core",
        "capability": "test.core",
        "permission": "auto",
        "read_only": False,
        "short_description": "Dummy tool for registry tests",
    }
    assert views["dummy_tool"]["exposure"] == "deferred"
    assert views["dummy_tool"]["direct"] is False
    assert views["dummy_tool"]["schema_available"] is True
    assert views["mcp__demo__hidden_dummy"]["exposure"] == "deferred"
    assert views["mcp__demo__hidden_dummy"]["direct"] is False
    assert views["mcp__demo__hidden_dummy"]["schema_available"] is True


def test_capability_registry_snapshot_respects_permission_context() -> None:
    registry = CapabilityRegistry()
    registry.register(_CoreDummyTool())
    registry.register(_DummyTool())
    checker = PermissionChecker(PermissionSettings())
    context = checker.build_context(
        mode="confirm",
        tool_deny_rules=["core_dummy"],
        source="test",
    )

    snapshot = registry.build_snapshot(
        permission_checker=checker,
        permission_context=context,
    )
    views = {view["name"]: view for view in snapshot["tool_views"]}

    assert views["core_dummy"]["exposure"] == "hidden"
    assert views["core_dummy"]["direct"] is False
    assert views["core_dummy"]["schema_available"] is False
    assert views["core_dummy"]["permission"] == "deny"
    assert snapshot["summary"]["hidden_tools"] == 1
    assert snapshot["summary"]["direct_tools"] == 0
    assert "core_dummy" not in {
        schema["function"]["name"] for schema in snapshot["tools"]
    }


def test_capability_registry_passes_tool_execution_context_when_supported() -> None:
    registry = CapabilityRegistry()
    tool = _ContextAwareTool()
    registry.register(tool)

    execution_context = ToolExecutionContext(
        permission=PermissionContext(mode="confirm", source="test"),
        session_id="session_123",
        task_id="task_123",
    )

    result = asyncio.run(
        registry.execute("context_tool", {}, context=execution_context)
    )

    assert result.is_error is False
    assert tool.received_contexts == [execution_context]


def test_run_command_tool_returns_recoverable_result_when_cancelled() -> None:
    class _FakeStdout:
        def __init__(self, owner: "_FakeProcess") -> None:
            self._owner = owner

        async def read(self, _size: int = -1) -> bytes:
            self._owner._stream_started.set()
            await self._owner._killed.wait()
            return b""

    class _FakeStderr:
        async def read(self, _size: int = -1) -> bytes:
            return b""

    class _FakeProcess:
        def __init__(self) -> None:
            class _FakeStdin:
                def write(self, _data: bytes) -> None:
                    return None

                async def drain(self) -> None:
                    return None

                def close(self) -> None:
                    return None

            self.returncode = None
            self.killed = False
            self._stream_started = asyncio.Event()
            self._killed = asyncio.Event()
            self.stdin = _FakeStdin()
            self.stdout = _FakeStdout(self)
            self.stderr = _FakeStderr()

        async def wait(self) -> int:
            await self._killed.wait()
            return int(self.returncode or -9)

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self._killed.set()

    fake_process = _FakeProcess()

    async def _fake_create_subprocess_shell(*args, **kwargs):
        return fake_process

    async def _exercise() -> ToolResult:
        original = asyncio.create_subprocess_shell
        asyncio.create_subprocess_shell = _fake_create_subprocess_shell  # type: ignore[assignment]
        try:
            cancel_event = asyncio.Event()
            tool = RunCommandTool(
                ArtifactStore(storage_dir=Path.cwd() / "tmp-artifacts-test")
            )
            task = asyncio.create_task(
                tool.execute(
                    {"command": "sleep 10"},
                    context=ToolExecutionContext(
                        # Windows default mode is intentionally fail-closed when
                        # no OS filesystem/network sandbox is available. Use
                        # explicit bypass here to exercise cancellation itself.
                        permission=PermissionContext(mode="bypass", source="test"),
                        cancel_event=cancel_event,
                    ),
                )
            )
            await fake_process._stream_started.wait()
            cancel_event.set()
            return await task
        finally:
            asyncio.create_subprocess_shell = original  # type: ignore[assignment]

    result = asyncio.run(_exercise())

    assert result.is_error is True
    assert "cancel" in result.content.lower() or "interrupt" in result.content.lower()
    assert fake_process.killed is True


def test_run_command_tool_rejects_cwd_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    result = asyncio.run(
        tool.execute(
            {"command": "echo should-not-run", "cwd": str(outside)},
            context=ToolExecutionContext(
                permission=PermissionContext(mode="confirm", source="test"),
                workspace_root=workspace,
            ),
        )
    )

    assert result.is_error is True
    assert "cwd must stay inside workspace" in result.content


def test_run_command_tool_projects_permission_allowlist_into_sandbox_mounts(
    tmp_path: Path,
) -> None:
    from backend.tools.command_support import _sandbox_writable_roots

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    root_file = workspace / "note.txt"
    root_file.write_text("before", encoding="utf-8")
    checker = PermissionChecker(
        PermissionSettings(path_allowlist=["./src"]),
        workspace,
    )
    context = ToolExecutionContext(
        permission=checker.build_context(mode="confirm", source="test"),
        permission_checker=checker,
        workspace_root=workspace,
    )

    assert _sandbox_writable_roots(workspace, context) == (
        (workspace / "src").resolve(),
    )
    assert root_file.read_text(encoding="utf-8") == "before"


def test_run_command_tool_allows_relative_cwd_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    python_command = "python" if sys.platform == "win32" else "python3"

    result = asyncio.run(
        tool.execute(
            {
                "command": f'{python_command} -c "import os; print(os.path.basename(os.getcwd()))"',
                "cwd": "nested",
            },
            context=ToolExecutionContext(
                permission=PermissionContext(mode="confirm", source="test"),
                workspace_root=workspace,
            ),
        )
    )

    if (
        sys.platform == "win32"
        and result.is_error
        and (
            "拒绝访问" in result.content
            or "sandbox is unavailable" in result.content.lower()
        )
    ):
        pytest.skip(
            "MiniCode restricted-token cwd ACL requires an elevated Temp parent on this host"
        )
    assert result.is_error is False
    assert "nested" in result.content


def test_run_command_tool_injects_structured_environment_in_bypass_mode(
    tmp_path: Path,
) -> None:
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    result = asyncio.run(
        tool.execute(
            {
                "command": _python_command(
                    "import os; print(os.environ['MINICODE_STRUCTURED_ENV'])"
                ),
                "cwd": str(tmp_path),
                "env": {"MINICODE_STRUCTURED_ENV": "structured-value"},
            },
            context=ToolExecutionContext(
                permission=PermissionContext(mode="bypass", source="test"),
                workspace_root=tmp_path,
            ),
        )
    )

    assert result.is_error is False
    assert "structured-value" in result.content


def test_run_command_tool_rejects_invalid_structured_environment(
    tmp_path: Path,
) -> None:
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    result = asyncio.run(
        tool.execute(
            {"command": "python --version", "env": {"BAD-NAME": "value"}},
            context=ToolExecutionContext(
                permission=PermissionContext(mode="bypass", source="test"),
                workspace_root=tmp_path,
            ),
        )
    )

    assert result.is_error is True
    assert "Invalid environment variable name" in result.content


def test_run_command_tool_reads_large_stderr_without_deadlock(tmp_path: Path) -> None:
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    result = asyncio.run(
        tool.execute(
            {
                "command": "python -c \"import sys; sys.stderr.write('x' * 200000); sys.stderr.flush()\"",
                "timeout": 5,
            },
            context=ToolExecutionContext(
                permission=PermissionContext(mode="bypass", source="test")
            ),
        )
    )

    if sys.platform == "win32" and result.is_error and "拒绝访问" in result.content:
        pytest.skip(
            "MiniCode restricted-token cwd ACL requires an elevated Temp parent on this host"
        )
    assert result.is_error is False
    assert "Exit code: 0" in result.content
    assert (
        result.artifact_id
        or "bytes truncated; showing beginning and end" in result.content
    )


def test_run_command_tool_marks_nonzero_exit_as_failed(tmp_path: Path) -> None:
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    result = asyncio.run(
        tool.execute(
            {
                "command": _python_command(
                    "import sys; sys.stderr.write('boom\\n'); sys.exit(7)"
                ),
                "timeout": 5,
            },
            context=ToolExecutionContext(
                permission=PermissionContext(mode="bypass", source="test")
            ),
        )
    )

    assert result.is_error is True
    assert result.status == "failed"
    assert "Exit code: 7 (failed)" in result.content
    assert "boom" in result.content


def test_run_command_wraps_powershell_cmdlets_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from backend.tools import command_tool
    from backend.tools.command_support import _windows_powershell_shell_command

    wrapped = _windows_powershell_shell_command("Get-ChildItem -Name")

    assert wrapped.startswith(("powershell.exe ", "pwsh.exe "))
    assert " -EncodedCommand " in wrapped
    encoded = wrapped.rsplit(" ", 1)[-1].strip('"')
    decoded = base64.b64decode(encoded).decode("utf-16-le")
    assert "[Console]::OutputEncoding" in decoded
    assert "Get-ChildItem -Name" in decoded

    monkeypatch.setattr(command_tool.sys, "platform", "win32")
    normalized = command_tool._host_shell_command(
        'curl -s "https://arxiv.org/list/cs.CL/new" -o "C:\\Desktop\\MiniCode\\arxiv_new.html" -m 15'
    )

    assert normalized.startswith(("powershell.exe ", "pwsh.exe "))
    encoded = normalized.rsplit(" ", 1)[-1].strip('"')
    decoded = base64.b64decode(encoded).decode("utf-16-le")
    assert "curl.exe -s " in decoded
    assert " -m 15" in decoded
    assert "Invoke-WebRequest" not in decoded


def test_windows_host_shell_uses_powershell_unless_shell_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.tools import command_tool

    monkeypatch.setattr(command_tool.sys, "platform", "win32")
    monkeypatch.setattr(command_tool.shutil, "which", lambda name: None)

    assert command_tool._host_shell_command("python -m pytest").startswith(
        "powershell.exe "
    )
    assert command_tool._host_shell_command("cmd /c dir") == "cmd /c dir"
    assert command_tool._host_shell_command("bash -lc 'pwd'") == "bash -lc 'pwd'"


def test_windows_shell_command_normalizes_bare_curl_aliases() -> None:
    from backend.terminal.shell_commands import normalize_windows_shell_command

    command = (
        'curl -s "https://arxiv.org/list/cs.CL/new" '
        '-o "C:\\Desktop\\MiniCode\\arxiv_new.html" -m 15 2>&1'
    )

    normalized = normalize_windows_shell_command(command, platform="win32")

    assert normalized.startswith("curl.exe -s ")
    assert " -m 15" in normalized
    assert normalize_windows_shell_command(
        "curl.exe -s https://example.com", platform="win32"
    ).startswith("curl.exe ")
    assert (
        normalize_windows_shell_command("echo curl -m 15", platform="win32")
        == "echo curl -m 15"
    )
    compound = (
        'Write-Output "curl -s quoted"; curl -s https://example.com '
        "| Out-Null\n& curl -I https://example.com"
    )
    normalized_compound = normalize_windows_shell_command(compound, platform="win32")
    assert '"curl -s quoted"' in normalized_compound
    assert normalized_compound.count("curl.exe") == 2
    assert normalize_windows_shell_command(command, platform="linux") == command


def test_windows_command_failure_explains_posix_recovery(monkeypatch) -> None:
    from backend.tools import command_tool

    monkeypatch.setattr(command_tool.sys, "platform", "win32")

    inline_env_hint = command_tool._windows_command_portability_hint(
        "PYTHONPATH=.. python -m pytest",
        "The term 'PYTHONPATH=..' is not recognized as the name of a cmdlet.",
        1,
    )
    posix_hint = command_tool._windows_command_portability_hint(
        "head -n 20 src/_pytest/pathlib.py",
        "head : The term 'head' is not recognized as the name of a cmdlet.",
        1,
    )

    assert "structured env" in inline_env_hint
    assert "do not repeat" in inline_env_hint
    assert "Get-Content -Head N" in posix_hint
    assert "do not repeat" in posix_hint


@pytest.mark.skipif(
    sys.platform != "win32", reason="PowerShell shell selection is Windows-specific"
)
def test_run_command_tool_executes_powershell_cmdlets_on_windows(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    result = asyncio.run(
        tool.execute(
            {
                "command": "Get-ChildItem -Path . -Depth 0 -Name",
                "cwd": str(tmp_path),
                "timeout": 10,
            },
            context=ToolExecutionContext(
                permission=PermissionContext(mode="confirm", source="test"),
                workspace_root=tmp_path,
            ),
        )
    )

    if result.is_error and (
        "拒绝访问" in result.content
        or "sandbox is unavailable" in result.content.lower()
    ):
        pytest.skip(
            "MiniCode restricted-token cwd ACL requires an elevated Temp parent on this host"
        )
    assert result.is_error is False
    assert "README.md" in result.content
    assert "not recognized" not in result.content.lower()


def test_legacy_search_and_git_tools_return_current_tool_result_shape(
    tmp_path: Path,
) -> None:
    (tmp_path / "sample.py").write_text(
        "def hello():\n    return 'world'\n", encoding="utf-8"
    )

    async def _exercise() -> list[ToolResult]:
        return [
            await GitStatusTool(tmp_path).execute({}),
            await GitDiffTool(tmp_path).execute({}),
            await GitLogTool(tmp_path).execute({"limit": 1}),
            await GlobFilesTool(tmp_path).execute({"pattern": "*.py"}),
            await GrepFilesTool(tmp_path).execute({"pattern": "hello", "glob": "*.py"}),
        ]

    results = asyncio.run(_exercise())

    assert all(isinstance(result, ToolResult) for result in results)
    assert all(isinstance(result.content, str) for result in results)


def test_git_status_uses_execution_context_workspace_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()

    async def _exercise() -> ToolResult:
        return await GitStatusTool(Path.cwd()).execute(
            {"path": "./src"},
            context=ToolExecutionContext(
                permission=PermissionContext(mode="confirm", source="test"),
                workspace_root=repo,
            ),
        )

    result = asyncio.run(_exercise())

    assert "src" in result.content or result.is_error is False


def test_legacy_search_tools_use_execution_context_workspace_root(
    tmp_path: Path,
) -> None:
    startup_root = tmp_path / "startup"
    active_root = tmp_path / "active"
    startup_root.mkdir()
    active_root.mkdir()
    (startup_root / "README.md").write_text("startup marker\n", encoding="utf-8")
    (active_root / "README.md").write_text("active marker\n", encoding="utf-8")
    context = ToolExecutionContext(
        permission=PermissionContext(mode="confirm", source="test"),
        workspace_root=active_root,
    )

    async def _exercise() -> tuple[ToolResult, ToolResult, ToolResult]:
        grep_tool = GrepFilesTool(startup_root)
        return (
            await GlobFilesTool(startup_root).execute(
                {"pattern": "README.md"}, context=context
            ),
            await grep_tool.execute(
                {"pattern": "active marker", "glob": "README.md"}, context=context
            ),
            await grep_tool.execute(
                {"pattern": "startup marker", "glob": "README.md"}, context=context
            ),
        )

    glob_result, active_result, startup_result = asyncio.run(_exercise())

    assert glob_result.is_error is False
    assert "README.md" in glob_result.content
    assert active_result.is_error is False
    assert "active marker" in active_result.content
    assert startup_result.is_error is False
    # grep_files output is localized; "startup marker" lives only in startup_root,
    # so searching the execution-context workspace (active_root) yields no matches.
    assert "startup marker" not in "\n".join(startup_result.content.splitlines()[2:])


def test_read_file_large_result_includes_actionable_preview(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file_path = workspace / "large.html"
    marker = "<section id='important'>keep this context visible</section>"
    # MiniCode's Read contract is 2,000 complete lines or 50 KiB, whichever comes
    # first. This fixture intentionally crosses that contract.
    file_path.write_text((marker + "\n") * 2_100, encoding="utf-8")
    tool = ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    result = asyncio.run(
        tool.execute(
            {"file_path": "large.html"},
            context=ToolExecutionContext(
                permission=PermissionContext(mode="confirm", source="test"),
                workspace_root=workspace,
            ),
        )
    )
    context = result.to_context_string()

    assert result.artifact_id
    assert marker in context
    assert result.artifact_preview


def test_read_file_returns_explicit_ranges_after_prior_reads(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file_path = workspace / "sample.py"
    file_path.write_text(
        "".join(f"line_{line_number}\n" for line_number in range(1, 21)),
        encoding="utf-8",
    )
    tool = ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    context = ToolExecutionContext(
        permission=PermissionContext(mode="confirm", source="test"),
        workspace_root=workspace,
    )

    first = asyncio.run(
        tool.execute(
            {"file_path": "sample.py", "start_line": 1, "end_line": 10},
            context=context,
        )
    )
    covered = asyncio.run(
        tool.execute(
            {"file_path": "sample.py", "start_line": 3, "end_line": 5},
            context=context,
        )
    )
    unseen = asyncio.run(
        tool.execute(
            {"file_path": "sample.py", "start_line": 11, "end_line": 12},
            context=context,
        )
    )

    assert "line_5" in first.content
    # A prior range may have been compacted out of the model context (especially
    # for long-running subagents). Returning the requested lines is therefore
    # safer than a stub that tells the model to refer to unavailable history.
    assert "line_3" in covered.content
    assert "line_5" in covered.content
    assert "line_11" in unseen.content

    file_path.write_text(
        file_path.read_text(encoding="utf-8") + "line_21\n", encoding="utf-8"
    )
    after_change = asyncio.run(
        tool.execute(
            {"file_path": "sample.py", "start_line": 3, "end_line": 5},
            context=context,
        )
    )

    assert "already returned earlier" not in after_change.content
    assert "line_3" in after_change.content


def test_focused_read_exposes_write_safe_full_file_hash(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file_path = workspace / "module.py"
    full_content = "".join(f"line_{line_number}\n" for line_number in range(1, 80))
    file_path.write_text(full_content, encoding="utf-8")
    tool = ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass", source="test"),
        workspace_root=workspace,
        metadata={"_read_file_hashes": {}},
    )

    result = asyncio.run(
        tool.execute(
            {"file_path": "module.py", "start_line": 20, "end_line": 24},
            context=context,
        )
    )

    assert "line_20" in result.content
    assert f"content_hash: {content_hash(full_content)}" in result.content
    assert context.metadata["_read_file_hashes"][
        canonical_file_path_key(file_path)
    ] == content_hash(full_content)

    # The range result can be used directly by the guarded edit tool without
    # forcing the model to request the complete file merely to obtain a hash.
    edited = asyncio.run(
        EditFileTool().execute(
            {
                "file_path": "module.py",
                "old_string": "line_20\n",
                "new_string": "line_20_changed\n",
                "expected_hash": content_hash(full_content),
            },
            context,
        )
    )
    assert edited.is_error is False
    assert "line_20_changed" in file_path.read_text(encoding="utf-8")


def test_read_file_refuses_dangerous_files_not_credentials(tmp_path: Path) -> None:
    # MiniCode hard-refuses dangerous config paths (.gitconfig/.bashrc/.git)
    # but has no credential-file hard-refuse on read (filesystem.ts:57-79).
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".gitconfig").write_text("[user]\n", encoding="utf-8")
    (workspace / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    tool = ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    context = ToolExecutionContext(
        permission=PermissionContext(mode="confirm", source="test"),
        workspace_root=workspace,
    )

    template = asyncio.run(tool.execute({"file_path": ".env.example"}, context=context))

    assert template.is_error is False
    assert "OPENAI_API_KEY=" in template.content


def test_static_environment_info_does_not_call_blocking_platform_probe(
    monkeypatch, tmp_path: Path
) -> None:
    import platform

    def _fail_platform_probe() -> str:
        raise AssertionError(
            "platform probe should not be used while building chat context"
        )

    monkeypatch.setattr(platform, "system", _fail_platform_probe)
    monkeypatch.setattr(platform, "version", _fail_platform_probe)

    info = build_static_environment_info(tmp_path)

    assert "Windows" in info
    assert str(tmp_path) not in info


def test_context_builder_preserves_bare_continue_as_user_input() -> None:
    state = AgentState(
        user_message="continue",
        task_summary="optimizing src/mario.html UI",
        tool_calls=[
            ToolCallRecord(
                tool_name="read_file",
                tool_input={"file_path": "src/mario.html"},
                tool_output="loaded current HTML",
            )
        ],
    )

    messages = asyncio.run(ContextBuilder().build("continue", state))
    user_content = messages[-1].content

    assert user_content.startswith("<system-reminder>")
    assert user_content.endswith("continue")


def test_run_command_tool_uses_context_background_manager_for_background_runs(
    monkeypatch,
) -> None:
    class _FakeBackgroundCommand:
        command_id = "bg_test123"
        cwd = "C:\\workspace"

    class _FakeBackgroundManager:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def run_background(self, **kwargs):
            self.calls.append(kwargs)
            return _FakeBackgroundCommand()

    manager = _FakeBackgroundManager()
    tool = RunCommandTool(ArtifactStore(storage_dir=Path.cwd() / "tmp-artifacts-test"))
    monkeypatch.setattr(
        "backend.tools.command_tool.SandboxRunner.capability",
        lambda _runner, *, cwd=None: SimpleNamespace(
            available=True, reason="", backend="full-access"
        ),
    )

    result = asyncio.run(
        tool.execute(
            {
                "command": "python backend\\server.py --port 8082",
                "cwd": "C:\\workspace",
                "run_in_background": True,
            },
            context=ToolExecutionContext(
                permission=PermissionContext(mode="confirm", source="test"),
                background_manager=manager,  # type: ignore[arg-type]
            ),
        )
    )

    assert result.is_error is False
    assert "bg_test123" in result.content
    assert len(manager.calls) == 1
    call = manager.calls[0]
    assert call["command"] == "python backend\\server.py --port 8082"
    assert call["cwd"] == "C:\\workspace"
    # Background commands are durable tasks.  A zero timeout means the
    # launcher does not kill them after the foreground command default; the
    # background manager still owns cancellation and explicit deadlines.
    assert call["timeout_ms"] == 0
    assert call["description"] == "python backend\\server.py --port 8082"
    policy = call["sandbox_policy"]
    assert isinstance(policy, SandboxPolicy)
    assert policy.disable_os_sandbox is False
    assert policy.allow_network is False
    assert policy.writable_roots == (Path("C:\\workspace"),)


def test_run_command_tool_background_bypass_uses_bypass_policy() -> None:
    class _FakeBackgroundCommand:
        command_id = "bg_bypass"
        cwd = "C:\\workspace"

    class _FakeBackgroundManager:
        def __init__(self) -> None:
            self.call: dict[str, object] = {}

        async def run_background(self, **kwargs):
            self.call = kwargs
            return _FakeBackgroundCommand()

    manager = _FakeBackgroundManager()
    tool = RunCommandTool(ArtifactStore(storage_dir=Path.cwd() / "tmp-artifacts-test"))

    result = asyncio.run(
        tool.execute(
            {
                "command": "python backend\\server.py --port 8082",
                "cwd": "C:\\workspace",
                "run_in_background": True,
            },
            context=ToolExecutionContext(
                permission=PermissionContext(mode="bypass", source="test"),
                background_manager=manager,  # type: ignore[arg-type]
            ),
        )
    )

    if result.is_error and "拒绝访问" in result.content:
        pytest.skip(
            "MiniCode restricted-token cwd ACL requires an elevated Temp parent on this host"
        )
    assert result.is_error is False
    policy = manager.call["sandbox_policy"]
    assert isinstance(policy, SandboxPolicy)
    assert policy.disable_os_sandbox is True
    assert policy.allow_network is True


def test_run_command_tool_does_not_infer_background_mode_from_command_text(
    monkeypatch,
) -> None:
    class _FakeBackgroundCommand:
        command_id = "bg_server"
        cwd = "C:\\workspace"

    class _FakeBackgroundManager:
        def __init__(self) -> None:
            self.commands: list[str] = []

        async def run_background(self, **kwargs):
            self.commands.append(str(kwargs["command"]))
            return _FakeBackgroundCommand()

    manager = _FakeBackgroundManager()
    tool = RunCommandTool(ArtifactStore(storage_dir=Path.cwd() / "tmp-artifacts-test"))

    async def fake_foreground(
        command,
        cwd,
        timeout,
        context,
        *,
        escalated=False,
        env_overrides=None,
    ):
        return ToolResult(content=f"foreground: {command}")

    monkeypatch.setattr(tool, "_execute_foreground", fake_foreground)

    result = asyncio.run(
        tool.execute(
            {
                "command": "python backend\\server.py --port 8082",
                "cwd": "C:\\workspace",
            },
            context=ToolExecutionContext(
                permission=PermissionContext(mode="confirm", source="test"),
                background_manager=manager,  # type: ignore[arg-type]
            ),
        )
    )

    assert result.is_error is False
    assert "foreground:" in result.content
    assert manager.commands == []


def test_run_command_tool_does_not_rewrite_windows_start_b(monkeypatch) -> None:
    class _FakeBackgroundCommand:
        command_id = "bg_startb"
        cwd = "C:\\workspace"

    class _FakeBackgroundManager:
        def __init__(self) -> None:
            self.commands: list[str] = []

        async def run_background(self, **kwargs):
            self.commands.append(str(kwargs["command"]))
            return _FakeBackgroundCommand()

    manager = _FakeBackgroundManager()
    tool = RunCommandTool(ArtifactStore(storage_dir=Path.cwd() / "tmp-artifacts-test"))

    foreground: list[str] = []

    async def fake_foreground(
        command,
        cwd,
        timeout,
        context,
        *,
        escalated=False,
        env_overrides=None,
    ):
        foreground.append(command)
        return ToolResult(content="foreground")

    monkeypatch.setattr(tool, "_execute_foreground", fake_foreground)

    result = asyncio.run(
        tool.execute(
            {
                "command": "start /B python backend\\server.py --port 8082",
                "cwd": "C:\\workspace",
            },
            context=ToolExecutionContext(
                permission=PermissionContext(mode="confirm", source="test"),
                background_manager=manager,  # type: ignore[arg-type]
            ),
        )
    )

    assert result.is_error is False
    assert foreground == ["start /B python backend\\server.py --port 8082"]
    assert manager.commands == []


def test_run_command_tool_requires_manager_for_explicit_background_mode() -> None:
    tool = RunCommandTool(ArtifactStore(storage_dir=Path.cwd() / "tmp-artifacts-test"))

    result = asyncio.run(
        tool.execute(
            {"command": "npm run dev", "run_in_background": True},
            context=ToolExecutionContext(
                permission=PermissionContext(mode="confirm", source="test")
            ),
        )
    )

    assert result.is_error is True
    assert "Background command execution is unavailable" in result.content


def test_write_tools_emit_file_changed_without_waiting_for_watcher(
    tmp_path: Path,
) -> None:
    from backend.agent.tool_execution import run_tool
    from backend.llm.base import ToolCallEvent
    from backend.tools.file_tools import EditFileTool, WriteFileTool
    from backend.tools.file_tools_common import content_hash
    from backend.tools.registry import ToolRegistry

    events: list[tuple[str, dict[str, object]]] = []

    async def emit_event(event_type: str, data: dict[str, object]) -> None:
        events.append((event_type, data))

    registry = ToolRegistry()
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass", source="test"),
        workspace_root=tmp_path,
        emit_event=emit_event,
        checkpoint_manager=CheckpointManager(CheckpointStore(tmp_path / "checkpoints")),
        conversation_id="conv-file-events",
    )

    write_result = asyncio.run(
        run_tool(
            ToolCallEvent(
                id="write_1",
                name="write_file",
                arguments={
                    "file_path": "src/new.txt",
                    "content": "hello\n",
                    "expected_hash": "",
                },
            ),
            registry,
            context,
        )
    )

    assert write_result.is_error is False
    file_events = [event for event in events if event[0] == "file.changed"]
    assert file_events == [
        (
            "file.changed",
            {
                "path": "src/new.txt",
                "event": "created",
                "workspace_root": str(tmp_path),
            },
        )
    ]

    events.clear()
    edit_result = asyncio.run(
        run_tool(
            ToolCallEvent(
                id="edit_1",
                name="edit_file",
                arguments={
                    "file_path": "src/new.txt",
                    "old_string": "hello",
                    "new_string": "updated",
                    "expected_hash": content_hash("hello\n"),
                },
            ),
            registry,
            context,
        )
    )

    assert edit_result.is_error is False
    file_events = [event for event in events if event[0] == "file.changed"]
    assert file_events == [
        (
            "file.changed",
            {
                "path": "src/new.txt",
                "event": "modified",
                "workspace_root": str(tmp_path),
            },
        )
    ]


def test_command_registry_dispatches_registered_handlers() -> None:
    from backend.commands.registry import CommandRegistry

    recorded: list[str] = []
    registry = CommandRegistry()

    async def _handler(payload: dict[str, object]) -> bool:
        recorded.append(str(payload["value"]))
        return True

    registry.register("conversation.list", _handler)
    result = asyncio.run(registry.dispatch("conversation.list", {"value": "ok"}))

    assert result is True
    assert recorded == ["ok"]
    assert registry.list_commands() == ["conversation.list"]


def test_command_registry_warns_on_duplicate_registration(caplog) -> None:
    from backend.commands.registry import CommandRegistry

    async def _handler(payload: dict[str, object]) -> bool:
        return True

    registry = CommandRegistry()

    registry.register("conversation.list", _handler)
    with caplog.at_level(logging.WARNING):
        registry.register("conversation.list", _handler)

    assert "Command name conflict detected for 'conversation.list'" in caplog.text


def test_permission_checker_builds_context_with_mode_and_overrides() -> None:
    checker = PermissionChecker(
        PermissionSettings(
            auto_allow=["read_*"],
            require_confirm=["run_*"],
            always_deny=["delete_*"],
        )
    )

    context = checker.build_context(
        mode="plan",
        session_overrides={"run_command": PermissionLevel.AUTO},
        source="websocket",
    )

    assert context.mode == "plan"
    assert checker.check("run_command", context=context) == PermissionLevel.ALWAYS_DENY
    assert checker.check("delete_file", context=context) == PermissionLevel.ALWAYS_DENY
    assert checker.check("read_file", context=context) == PermissionLevel.ALWAYS_DENY

    confirm_context = checker.build_context(
        mode="confirm",
        session_overrides={"run_command": PermissionLevel.AUTO},
        source="websocket",
    )
    assert checker.check("run_command", context=confirm_context) == PermissionLevel.AUTO


def test_permission_modes_apply_predictable_safety_policy() -> None:
    checker = PermissionChecker(
        PermissionSettings(
            auto_allow=["*"],
            require_confirm=[],
            require_diff_review=["write_file", "edit_file"],
            always_deny=["delete_*"],
        )
    )

    plan_context = checker.build_context(mode="plan", source="test")
    confirm_context = checker.build_context(mode="confirm", source="test")
    bypass_context = checker.build_context(mode="bypass", source="test")

    assert (
        checker.check("read_file", context=plan_context) == PermissionLevel.ALWAYS_DENY
    )
    assert (
        checker.check("grep_files", context=plan_context) == PermissionLevel.ALWAYS_DENY
    )
    assert (
        checker.check("mcp__websearch__search", context=plan_context)
        == PermissionLevel.ALWAYS_DENY
    )
    assert (
        checker.check("mcp__websearch__fetch_page", context=plan_context)
        == PermissionLevel.ALWAYS_DENY
    )
    assert (
        checker.check("mcp__github__create_issue", context=plan_context)
        == PermissionLevel.ALWAYS_DENY
    )
    assert (
        checker.check("write_file", context=plan_context) == PermissionLevel.ALWAYS_DENY
    )
    assert (
        checker.check("run_command", context=plan_context)
        == PermissionLevel.ALWAYS_DENY
    )

    assert checker.check("read_file", context=confirm_context) == PermissionLevel.AUTO
    assert (
        checker.check(
            "web_fetch", {"url": "https://example.com"}, context=confirm_context
        )
        == PermissionLevel.AUTO
    )
    assert (
        checker.check("write_file", context=confirm_context)
        == PermissionLevel.DIFF_REVIEW
    )

    assert checker.check("write_file", context=bypass_context) == PermissionLevel.AUTO
    assert (
        checker.check("delete_file", context=bypass_context)
        == PermissionLevel.ALWAYS_DENY
    )


def test_auto_permission_mode_routes_workspace_edits_to_diff_review() -> None:
    checker = PermissionChecker(
        PermissionSettings(require_diff_review=["write_file", "edit_file"])
    )
    auto_context = checker.build_context(mode="auto", source="test")
    ask_context = checker.build_context(mode="confirm", source="test")

    write_tool = WriteFileTool()
    edit_tool = EditFileTool()

    assert (
        checker.check("write_file", context=auto_context, tool=write_tool)
        == PermissionLevel.DIFF_REVIEW
    )
    assert (
        checker.check("edit_file", context=auto_context, tool=edit_tool)
        == PermissionLevel.DIFF_REVIEW
    )
    assert (
        checker.check("write_file", context=ask_context, tool=write_tool)
        == PermissionLevel.DIFF_REVIEW
    )
    assert (
        checker.check("edit_file", context=ask_context, tool=edit_tool)
        == PermissionLevel.DIFF_REVIEW
    )


def test_minicode_permission_profile_mapping_and_sandbox_status() -> None:
    from backend.conversations.models import (
        normalize_permission_mode as normalize_conversation_permission_mode,
    )
    from backend.ws.utils import normalize_permission_mode

    assert permission_profile_for_mode("confirm") == "confirm"
    assert permission_profile_for_mode("plan") == "plan"
    assert permission_profile_for_mode("auto") == "auto"
    assert permission_profile_for_mode("bypass") == "bypass"

    assert workspace_scope_for(workspace_root=None, worktree_path="") == "computer"
    assert workspace_scope_for(workspace_root="C:/repo", worktree_path="") == "project"
    assert (
        workspace_scope_for(
            workspace_root="C:/repo", worktree_path="C:/repo/.minicode/worktrees/a"
        )
        == "worktree"
    )

    with patch(
        "backend.permissions.profiles._has_native_os_sandbox", return_value=False
    ):
        auto_status = sandbox_status_for("auto", platform_name="win32")
        assert auto_status == {"os": "app_layer", "network": "approval_required"}
    with patch(
        "backend.permissions.profiles._has_native_os_sandbox", return_value=True
    ):
        auto_status = sandbox_status_for("auto", platform_name="win32")
        assert auto_status == {"os": "enforced", "network": "approval_required"}

    full_status = sandbox_status_for("bypass", platform_name="win32")
    assert full_status == {"os": "disabled", "network": "enabled"}

    checker = PermissionChecker(PermissionSettings())
    assert checker.build_context(mode="confirm", source="test").mode == "confirm"
    assert checker.build_context(mode="bypass", source="test").mode == "bypass"
    assert normalize_permission_mode("confirm") == "confirm"
    assert normalize_permission_mode("bypass") == "bypass"
    assert normalize_conversation_permission_mode("bypass") == "bypass"
    with pytest.raises(ValueError):
        checker.build_context(mode="unsupported_mode", source="test")


def test_auto_network_policy_requires_review_for_local_targets() -> None:
    checker = PermissionChecker(
        PermissionSettings(auto_allow=["*"], require_confirm=[])
    )
    auto_context = checker.build_context(mode="auto", source="test")
    bypass_context = checker.build_context(mode="bypass", source="test")
    web_tool = WebFetchTool(
        ArtifactStore(storage_dir=Path.cwd() / "tmp-artifacts-test")
    )

    assert (
        checker.check(
            "web_fetch",
            {"url": "https://example.com/docs"},
            context=auto_context,
            tool=web_tool,
        )
        == PermissionLevel.AUTO
    )
    assert (
        checker.check(
            "web_fetch",
            {"url": "http://127.0.0.1:8000"},
            context=auto_context,
            tool=web_tool,
        )
        == PermissionLevel.CONFIRM
    )
    assert (
        checker.check(
            "web_fetch", {"url": "http://127.0.0.1:8000"}, context=bypass_context
        )
        == PermissionLevel.AUTO
    )
    assert (
        checker.check(
            "mcp__websearch__fetch_page",
            {"url": "https://example.com/docs"},
            context=auto_context,
        )
        == PermissionLevel.CONFIRM
    )
    assert (
        checker.check(
            "mcp__websearch__fetch_page",
            {"url": "http://localhost:8000"},
            context=auto_context,
        )
        == PermissionLevel.CONFIRM
    )


def test_auto_network_policy_checks_url_like_mcp_arguments() -> None:
    from backend.mcp.client import MCPToolDef
    from backend.mcp.registry import MCPToolProxy

    class _Client:
        connected = True

        async def call_tool(self, name, args):
            raise AssertionError("permission test should not execute the MCP tool")

    checker = PermissionChecker(
        PermissionSettings(auto_allow=["*"], require_confirm=[])
    )
    auto_context = checker.build_context(mode="auto", source="test")
    bypass_context = checker.build_context(mode="bypass", source="test")
    proxy = MCPToolProxy(
        "docs",
        MCPToolDef(
            name="read_endpoint",
            description="Read an HTTP endpoint",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": True, "openWorldHint": False},
        ),
        _Client(),  # type: ignore[arg-type]
    )

    assert (
        checker.check(
            proxy.name,
            {"href": "https://example.com/docs"},
            context=auto_context,
            tool=proxy,
        )
        == PermissionLevel.CONFIRM
    )
    assert (
        checker.check(
            proxy.name,
            {"href": "http://localhost:8000"},
            context=auto_context,
            tool=proxy,
        )
        == PermissionLevel.CONFIRM
    )
    assert (
        checker.check(
            proxy.name,
            {"request": {"host": "127.0.0.1:8000"}},
            context=auto_context,
            tool=proxy,
        )
        == PermissionLevel.CONFIRM
    )
    assert (
        checker.check(
            proxy.name,
            {"urls": ["https://example.com/docs", "http://192.168.1.20/status"]},
            context=auto_context,
            tool=proxy,
        )
        == PermissionLevel.CONFIRM
    )
    assert (
        checker.check(
            proxy.name,
            {"endpoint": "http://127.0.0.1:8000/api"},
            context=bypass_context,
            tool=proxy,
        )
        == PermissionLevel.AUTO
    )


def test_auto_policy_requires_review_for_open_world_mcp_tools() -> None:
    from backend.mcp.client import MCPToolDef
    from backend.mcp.registry import MCPToolProxy

    class _Client:
        connected = True

        async def call_tool(self, name, args):
            raise AssertionError("permission test should not execute the MCP tool")

    checker = PermissionChecker(
        PermissionSettings(auto_allow=["*"], require_confirm=[])
    )
    auto_context = checker.build_context(mode="auto", source="test")
    bypass_context = checker.build_context(mode="bypass", source="test")
    proxy = MCPToolProxy(
        "figma-desktop",
        MCPToolDef(
            name="get_design_context",
            description="Read selected design context",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": True, "openWorldHint": True},
        ),
        _Client(),  # type: ignore[arg-type]
    )

    assert (
        checker.check(proxy.name, {}, context=auto_context, tool=proxy)
        == PermissionLevel.CONFIRM
    )
    assert (
        checker.check(proxy.name, {}, context=bypass_context, tool=proxy)
        == PermissionLevel.CONFIRM
    )


def test_auto_policy_requires_review_for_destructive_mcp_tools() -> None:
    from backend.mcp.client import MCPToolDef
    from backend.mcp.registry import MCPToolProxy

    class _Client:
        connected = True

        async def call_tool(self, name, args):
            raise AssertionError("permission test should not execute the MCP tool")

    checker = PermissionChecker(
        PermissionSettings(auto_allow=["*"], require_confirm=[])
    )
    auto_context = checker.build_context(mode="auto", source="test")
    bypass_context = checker.build_context(mode="bypass", source="test")
    proxy = MCPToolProxy(
        "github",
        MCPToolDef(
            name="delete_issue",
            description="Delete an issue",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": True, "destructiveHint": True},
        ),
        _Client(),  # type: ignore[arg-type]
    )

    assert (
        checker.check(proxy.name, {"issue": 123}, context=auto_context, tool=proxy)
        == PermissionLevel.CONFIRM
    )
    assert (
        checker.check(proxy.name, {"issue": 123}, context=bypass_context, tool=proxy)
        == PermissionLevel.CONFIRM
    )


def test_bypass_skips_workspace_path_policy_for_local_file_tools(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    checker = PermissionChecker(
        PermissionSettings(
            path_allowlist=["./src", "./backend"],
            path_denylist=[".env", "*.pem"],
        ),
        workspace,
    )
    bypass_context = checker.build_context(mode="bypass", source="test")

    assert (
        checker.get_denial_reason(
            "write_file",
            {"file_path": "README.md"},
            context=bypass_context,
        )
        is None
    )
    assert (
        checker.get_denial_reason(
            "write_file",
            {"file_path": ".env"},
            context=bypass_context,
        )
        is None
    )
    assert (
        checker.get_denial_reason(
            "write_file",
            {"file_path": str(outside)},
            context=bypass_context,
        )
        is None
    )


def test_bypass_allows_read_file_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    target = outside / "note.txt"
    target.write_text("outside ok\n", encoding="utf-8")
    checker = PermissionChecker(PermissionSettings(), workspace)
    default_context = checker.build_context(mode="confirm", source="test")
    bypass_context = checker.build_context(mode="bypass", source="test")

    assert (
        checker.get_denial_reason(
            "read_file", {"file_path": str(target)}, context=default_context
        )
        is None
    )
    assert (
        checker.get_denial_reason(
            "read_file", {"file_path": str(target)}, context=bypass_context
        )
        is None
    )


def test_bypass_read_file_tool_can_read_absolute_paths_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    target = outside / ".env"
    target.write_text("TOKEN=visible-in-bypass\n", encoding="utf-8")
    tool = ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    default_result = asyncio.run(
        tool.execute(
            {"file_path": str(target)},
            context=ToolExecutionContext(
                permission=PermissionContext(mode="confirm", source="test"),
                workspace_root=workspace,
            ),
        )
    )
    bypass_result = asyncio.run(
        tool.execute(
            {"file_path": str(target)},
            context=ToolExecutionContext(
                permission=PermissionContext(mode="bypass", source="test"),
                workspace_root=workspace,
            ),
        )
    )

    assert default_result.is_error is True
    assert "workspace boundary" in default_result.content
    assert bypass_result.is_error is False
    assert "TOKEN=visible-in-bypass" in bypass_result.content


def test_bypass_read_only_discovery_tools_can_use_absolute_paths_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "note.txt").write_text("outside ok\n", encoding="utf-8")
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass", source="test"),
        workspace_root=workspace,
    )

    listed = asyncio.run(
        ListFilesTool().execute({"directory": str(outside)}, context=context)
    )
    globbed = asyncio.run(
        GlobFilesTool().execute(
            {"directory": str(outside), "pattern": "*.txt"}, context=context
        )
    )

    assert listed.is_error is False
    assert "note.txt" in listed.content
    assert globbed.is_error is False
    assert "note.txt" in globbed.content


def test_run_command_bypass_allows_cwd_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    default_context = ToolExecutionContext(
        permission=PermissionContext(mode="confirm", source="test"),
        workspace_root=workspace,
    )
    bypass_context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass", source="test"),
        workspace_root=workspace,
    )

    try:
        tool._resolve_cwd(str(outside), default_context)
    except ValueError as exc:
        default_error = str(exc)
    else:
        default_error = ""

    assert "inside workspace" in default_error
    assert tool._resolve_cwd(str(outside), bypass_context) == str(outside.resolve())


def test_run_command_blocks_catastrophic_commands_without_runtime_checker(
    tmp_path: Path,
) -> None:
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    result = asyncio.run(
        tool.execute(
            {"command": "rm -rf /"},
            context=ToolExecutionContext(
                permission=PermissionContext(mode="bypass", source="test"),
                workspace_root=None,
            ),
        )
    )

    assert result.is_error is True
    assert "recursive delete" in result.content


def test_bypass_write_file_can_write_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    target = outside / "note.txt"
    tool = WriteFileTool()

    result = asyncio.run(
        tool.execute(
            {"file_path": str(target), "content": "outside ok\n"},
            context=ToolExecutionContext(
                permission=PermissionContext(mode="bypass", source="test"),
                workspace_root=workspace,
            ),
        )
    )

    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "outside ok\n"


def test_bypass_edit_file_can_edit_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    target = outside / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    tool = EditFileTool()

    result = asyncio.run(
        tool.execute(
            {
                "file_path": str(target),
                "old_string": "before",
                "new_string": "after",
                "expected_hash": "unused",
            },
            context=ToolExecutionContext(
                permission=PermissionContext(mode="bypass", source="test"),
                workspace_root=workspace,
            ),
        )
    )

    assert result.is_error is True
    assert "actual_hash" in result.content

    from backend.tools.file_tools_common import content_hash

    result = asyncio.run(
        tool.execute(
            {
                "file_path": str(target),
                "old_string": "before",
                "new_string": "after",
                "expected_hash": content_hash("before\n"),
            },
            context=ToolExecutionContext(
                permission=PermissionContext(mode="bypass", source="test"),
                workspace_root=workspace,
            ),
        )
    )

    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "after\n"


def test_path_allowlist_normalizes_dot_prefixed_relative_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    checker = PermissionChecker(
        PermissionSettings(path_allowlist=["./src", "./backend"]),
        workspace,
    )

    assert checker.is_path_allowed("./backend/server.py")
    assert checker.is_path_allowed("backend/server.py")
    assert checker.is_path_allowed("./src/mario.html")
    assert checker.is_path_allowed("src/mario.html")
    assert not checker.is_path_allowed("README.md")


def test_workspace_root_discovery_paths_survive_narrow_allowlist(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "backend").mkdir()
    (workspace / "frontend").mkdir()
    (workspace / "README.md").write_text("# Project", encoding="utf-8")
    (workspace / "settings.json").write_text("{}", encoding="utf-8")
    checker = PermissionChecker(
        PermissionSettings(
            path_allowlist=["./backend", "./frontend"],
            path_denylist=["settings.json", ".env", ".git/**"],
        ),
        workspace,
    )

    assert (
        checker.get_denial_reason("list_files", {"directory": str(workspace)}) is None
    )
    assert checker.get_denial_reason("list_files", {"directory": "."}) is None
    assert (
        checker.get_denial_reason(
            "glob_files", {"directory": str(workspace), "pattern": "*.py"}
        )
        is None
    )
    assert (
        checker.get_denial_reason("glob_files", {"directory": ".", "pattern": "*.py"})
        is None
    )

    assert (
        checker.get_denial_reason(
            "grep_files", {"directory": str(workspace), "pattern": "Project"}
        )
        is None
    )
    assert (
        checker.get_denial_reason("read_file", {"file_path": "settings.json"}) is None
    )


def test_permission_checker_does_not_duplicate_file_tool_path_enforcement(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "index.html").write_text("<html></html>", encoding="utf-8")
    (workspace / ".env").write_text("SECRET=value", encoding="utf-8")
    checker = PermissionChecker(PermissionSettings(), workspace)

    assert checker.get_denial_reason("read_file", {"file_path": "index.html"}) is None
    assert checker.get_denial_reason("read_file", {"file_path": ".env"}) is None


def test_default_path_policy_is_enforced_by_file_tools_not_permission_names(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Project", encoding="utf-8")
    (workspace / "settings.json").write_text("{}", encoding="utf-8")
    git_dir = workspace / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n", encoding="utf-8")
    checker = PermissionChecker(PermissionSettings(), workspace)

    assert PermissionSettings().path_allowlist == ["."]
    assert checker.get_denial_reason("read_file", {"file_path": "README.md"}) is None
    assert checker.get_denial_reason("write_file", {"file_path": "README.md"}) is None
    assert (
        checker.get_denial_reason("read_file", {"file_path": "settings.json"}) is None
    )
    assert checker.get_denial_reason("read_file", {"file_path": ".git/config"}) is None


def test_permission_path_allowlist_only_applies_to_local_file_tools(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    checker = PermissionChecker(
        PermissionSettings(
            auto_allow=["remote_lookup"],
            path_allowlist=["./src"],
        ),
        workspace,
    )

    assert checker.get_denial_reason("read_file", {"file_path": "README.md"}) is None
    assert checker.get_denial_reason("remote_lookup", {"path": "README.md"}) is None


def test_command_text_keeps_catastrophic_commands_blocked_even_in_bypass_mode(
    tmp_path: Path,
) -> None:
    checker = PermissionChecker(
        PermissionSettings(auto_allow=["terminal.exec"]),
        tmp_path,
    )
    context = checker.build_context(mode="bypass", source="test")

    assert (
        checker.get_denial_reason(
            "terminal.exec",
            {"command": "echo ok", "cwd": str(tmp_path)},
            context=context,
        )
        is None
    )
    from backend.permissions.checker import check_catastrophic_command

    allowed, denial = check_catastrophic_command("mkfs /dev/sda")
    assert allowed is False
    assert "filesystem format" in denial


def test_auto_mode_keeps_mcp_confirmation_floor() -> None:
    checker = PermissionChecker(PermissionSettings())
    auto_context = checker.build_context(mode="auto", source="test")

    assert (
        checker.check("mcp__websearch__search", context=auto_context)
        == PermissionLevel.CONFIRM
    )
    assert (
        checker.check("mcp__github__create_issue", context=auto_context)
        == PermissionLevel.CONFIRM
    )


def test_task_manager_tracks_completion_failure_and_cancellation() -> None:
    from backend.tasks.manager import TaskManager

    async def _exercise() -> None:
        manager = TaskManager()

        async def _ok() -> str:
            await asyncio.sleep(0)
            return "done"

        async def _boom() -> str:
            await asyncio.sleep(0)
            raise RuntimeError("boom")

        ok_task = manager.create("agent.run", _ok())
        failed_task = manager.create("agent.run", _boom())
        cancelled_task = manager.create("agent.run", asyncio.sleep(10))

        await asyncio.sleep(0)
        manager.cancel(cancelled_task.id)

        await asyncio.gather(
            manager.wait(ok_task.id),
            manager.wait(failed_task.id),
            manager.wait(cancelled_task.id),
            return_exceptions=True,
        )
        await asyncio.sleep(0)

        assert manager.get(ok_task.id).status == "completed"
        assert manager.get(ok_task.id).result == "done"
        assert manager.get(failed_task.id).status == "failed"
        assert "boom" in (manager.get(failed_task.id).error or "")
        assert manager.get(cancelled_task.id).status == "cancelled"


def test_cancel_pending_approvals_emits_cancelled_event_and_clears_state() -> None:
    async def _exercise() -> None:
        runtime = _ApprovalRuntime()
        future = asyncio.get_running_loop().create_future()
        runtime.turn_wait_state.pending_approvals["approval-1"] = future
        runtime.turn_wait_state.pending_approval_payloads["approval-1"] = {
            "type": "approval_request",
            "conversation_id": "conv_cancel_pending",
        }
        runtime.approval_diff_cache["approval-1"] = {"files": []}

        cancelled = await runtime._cancel_pending_approvals(reason="user_interrupted")

        assert cancelled == ["approval-1"]
        assert future.cancelled()
        assert runtime.turn_wait_state.pending_approvals == {}
        assert runtime.turn_wait_state.pending_approval_payloads == {}
        assert runtime.approval_diff_cache == {}
        assert runtime.sent_events[-1].type == "approval.cancelled"
        assert runtime.sent_events[-1].data == {
            "request_ids": ["approval-1"],
            "reason": "user_interrupted",
            "conversation_id": "conv_cancel_pending",
        }

    asyncio.run(_exercise())


def test_task_manager_cancel_all_and_wait_drains_wrapped_coroutines() -> None:
    from backend.tasks.manager import TaskManager

    async def _exercise() -> None:
        manager = TaskManager()
        cancelled = asyncio.Event()

        async def _blocking() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        first = manager.create("agent.run", _blocking())
        second = manager.create("background", _blocking())
        await asyncio.sleep(0)

        count = await manager.cancel_all_and_wait()

        assert count == 2
        assert cancelled.is_set()
        assert first.task is not None and first.task.done()
        assert second.task is not None and second.task.done()
        assert manager.summary()["running"] == 0
        assert manager.summary()["cancelled"] == 2

    asyncio.run(_exercise())


def test_cancel_pending_approvals_clears_payload_without_future() -> None:
    async def _exercise() -> None:
        runtime = _ApprovalRuntime()
        runtime.turn_wait_state.pending_approval_payloads["ask-1"] = {
            "type": "ask_user",
            "tool_call_id": "ask-1",
            "conversation_id": "conv_cancel_ask",
        }

        cancelled = await runtime._cancel_pending_approvals(reason="user_interrupted")

        assert cancelled == ["ask-1"]
        assert runtime.turn_wait_state.pending_approvals == {}
        assert runtime.turn_wait_state.pending_approval_payloads == {}
        assert runtime.sent_events[-1].data == {
            "request_ids": ["ask-1"],
            "reason": "user_interrupted",
            "conversation_id": "conv_cancel_ask",
        }

    asyncio.run(_exercise())


def test_cancel_pending_approvals_can_target_one_conversation() -> None:
    async def _exercise() -> None:
        runtime = _ApprovalRuntime()
        future_a = asyncio.get_running_loop().create_future()
        future_b = asyncio.get_running_loop().create_future()
        runtime.turn_wait_state.pending_approvals["approval-a"] = future_a
        runtime.turn_wait_state.pending_approvals["approval-b"] = future_b
        runtime.turn_wait_state.pending_approval_payloads["approval-a"] = {
            "type": "approval_request",
            "conversation_id": "conv_a12345",
        }
        runtime.turn_wait_state.pending_approval_payloads["approval-b"] = {
            "type": "approval_request",
            "conversation_id": "conv_b12345",
        }

        cancelled = await runtime._cancel_pending_approvals(
            reason="user_interrupted",
            conversation_id="conv_a12345",
        )

        assert cancelled == ["approval-a"]
        assert future_a.cancelled()
        assert not future_b.cancelled()
        assert list(runtime.turn_wait_state.pending_approvals) == ["approval-b"]
        assert list(runtime.turn_wait_state.pending_approval_payloads) == ["approval-b"]
        assert runtime.sent_events[-1].data["request_ids"] == ["approval-a"]
        assert runtime.sent_events[-1].data["conversation_id"] == "conv_a12345"

    asyncio.run(_exercise())


def test_approval_handler_emits_cancelled_event_on_timeout(monkeypatch) -> None:
    async def _timeout(_future: asyncio.Future, *, timeout: float) -> dict[str, object]:
        raise asyncio.TimeoutError

    monkeypatch.setattr("backend.ws.approval_runtime.asyncio.wait_for", _timeout)

    async def _exercise() -> None:
        runtime = _ApprovalRuntime()
        runtime.turn_wait_state.pending_approval_payloads["approval-timeout"] = {
            "type": "approval_request",
            "conversation_id": "conv_timeout",
            "tool_name": "run_command",
            "args": {"command": "npm test"},
        }
        runtime.approval_diff_cache["approval-timeout"] = {"files": []}

        result = await runtime.approval_handler("approval-timeout")

        assert result == {
            "action": "reject",
            "guidance": "approval timed out after 300 seconds",
        }
        assert runtime.turn_wait_state.pending_approvals == {}
        assert runtime.turn_wait_state.pending_approval_payloads == {}
        assert runtime.approval_diff_cache == {}
        assert runtime.sent_events[-1].type == "approval.cancelled"
        assert runtime.sent_events[-1].data == {
            "request_ids": ["approval-timeout"],
            "reason": "approval_timeout",
            "conversation_id": "conv_timeout",
        }

    asyncio.run(_exercise())


def test_cancelled_approval_wait_emits_one_terminal_event() -> None:
    async def _exercise() -> None:
        runtime = _ApprovalRuntime()
        runtime.turn_wait_state.pending_approval_payloads["approval-cancelled"] = {
            "type": "approval_request",
            "conversation_id": "conv_cancelled",
            "tool_name": "run_command",
            "args": {"command": "npm test"},
        }

        task = asyncio.create_task(runtime.approval_handler("approval-cancelled"))
        for _ in range(20):
            if "approval-cancelled" in runtime.turn_wait_state.pending_approvals:
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert [event.type for event in runtime.sent_events].count(
            "approval.cancelled"
        ) == 1
        assert runtime.sent_events[-1].data == {
            "request_ids": ["approval-cancelled"],
            "reason": "approval_wait_cancelled",
            "conversation_id": "conv_cancelled",
        }

        cancelled = await runtime._cancel_pending_approvals(reason="run_cancelled")
        assert cancelled == []
        assert [event.type for event in runtime.sent_events].count(
            "approval.cancelled"
        ) == 1

    asyncio.run(_exercise())


def test_websocket_session_initializes_workspace_context_before_starting_file_watcher() -> (
    None
):
    source = Path("backend/ws/session_lifecycle.py").read_text(encoding="utf-8")

    workspace_context_index = source.index("self._workspace_context:")
    start_file_watcher_index = source.index("def start_file_watcher")

    assert workspace_context_index < start_file_watcher_index


def test_workspace_file_watcher_schedules_changes_on_captured_event_loop(
    monkeypatch,
) -> None:
    from backend.workspace.file_watcher import WorkspaceFileWatcher

    fallback_loop = object()
    monkeypatch.setattr(asyncio, "get_event_loop", lambda: fallback_loop)

    watcher = WorkspaceFileWatcher(
        workspace_root=Path.cwd(),
        on_change=lambda path, event_type: None,
    )

    scheduled: list[tuple[object, object]] = []

    def _fake_run_coroutine_threadsafe(coro, loop):
        scheduled.append((coro, loop))
        return SimpleNamespace()

    monkeypatch.setattr(
        asyncio, "run_coroutine_threadsafe", _fake_run_coroutine_threadsafe
    )

    handler = watcher._create_handler()
    handler.on_any_event(
        SimpleNamespace(
            is_directory=False,
            src_path=str(Path.cwd() / "demo.txt"),
            event_type="modified",
        )
    )

    assert len(scheduled) == 1
    coroutine, loop = scheduled[0]
    assert loop is fallback_loop
    coroutine.close()


def test_workspace_file_watcher_reports_directory_move_destination(
    monkeypatch, tmp_path
) -> None:
    from backend.workspace.file_watcher import WorkspaceFileWatcher

    watcher = WorkspaceFileWatcher(
        workspace_root=tmp_path,
        on_change=lambda path, event_type: None,
    )
    scheduled: list[object] = []

    def _fake_run_coroutine_threadsafe(coro, loop):
        scheduled.append(coro)
        return SimpleNamespace()

    monkeypatch.setattr(
        asyncio, "run_coroutine_threadsafe", _fake_run_coroutine_threadsafe
    )
    handler = watcher._create_handler()
    handler.on_any_event(
        SimpleNamespace(
            is_directory=True,
            src_path=str(tmp_path / "old-dir"),
            dest_path=str(tmp_path / "new-dir"),
            event_type="moved",
        )
    )

    assert len(scheduled) == 2
    for coroutine in scheduled:
        coroutine.close()


def test_workspace_file_watcher_ignores_runtime_data_directory() -> None:
    from backend.workspace.file_watcher import WorkspaceFileWatcher

    workspace_root = Path.cwd()
    watcher = WorkspaceFileWatcher(
        workspace_root=workspace_root,
        on_change=lambda path, event_type: None,
    )

    assert watcher._should_ignore(
        workspace_root / "data" / "conversations" / "session.meta.json"
    )
    assert not watcher._should_ignore(workspace_root / "src" / "app.py")


def test_task_manager_emits_change_callback_for_lifecycle_updates() -> None:
    from backend.tasks.manager import TaskManager

    async def _exercise() -> int:
        change_count = 0

        def _on_change() -> None:
            nonlocal change_count
            change_count += 1

        manager = TaskManager(on_change=_on_change)

        async def _ok() -> str:
            await asyncio.sleep(0)
            return "done"

        managed = manager.create("agent.run", _ok())
        await manager.wait(managed.id)
        await asyncio.sleep(0)
        return change_count

    changes = asyncio.run(_exercise())
    assert changes >= 2

    asyncio.run(_exercise())


def test_task_manager_prunes_terminal_tasks_and_reports_summary() -> None:
    from backend.tasks.manager import TaskManager

    async def _exercise() -> None:
        manager = TaskManager(max_tasks=2, terminal_task_ttl_seconds=None)

        async def _ok(label: str) -> str:
            await asyncio.sleep(0)
            return label

        first = manager.create("agent.run", _ok("first"))
        await manager.wait(first.id)
        second = manager.create("agent.run", _ok("second"))
        await manager.wait(second.id)

        summary = manager.summary()
        assert summary["completed"] == 2
        assert summary["total"] == 2

        third = manager.create("agent.run", _ok("third"))
        await manager.wait(third.id)
        await asyncio.sleep(0)

        assert manager.get(first.id) is None
        assert manager.get(second.id) is not None
        assert manager.get(third.id) is not None

        post_summary = manager.summary()
        assert post_summary["total"] == 2
        assert post_summary["completed"] == 2

    asyncio.run(_exercise())


def test_user_command_normalizes_control_response_request_id() -> None:
    command = UserCommand.from_ws_message(
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "requestId": "req_123",
                "response": {"action": "accept"},
            },
        }
    )

    assert command.type == "control_response"
    assert command.data["response"]["request_id"] == "req_123"


def test_user_command_normalizes_control_cancel_request_id() -> None:
    command = UserCommand.from_ws_message(
        {
            "type": "control_cancel_request",
            "requestId": "req_cancel_1",
        }
    )

    assert command.type == "control_cancel_request"
    assert command.data["request_id"] == "req_cancel_1"


def test_control_response_normalizer_maps_no_to_reject() -> None:
    runtime = _ApprovalRuntime()

    request_id, payload = runtime._normalize_control_response(
        {
            "request_id": "req_no",
            "response": {
                "subtype": "success",
                "response": {"action": "no"},
            },
        }
    )

    assert request_id == "req_no"
    assert payload["action"] == "reject"


def test_control_response_normalizer_maps_boolean_actions() -> None:
    runtime = _ApprovalRuntime()

    approve_request_id, approve_payload = runtime._normalize_control_response(
        {
            "request_id": "req_true",
            "response": {
                "subtype": "success",
                "response": {"action": True},
            },
        }
    )
    reject_request_id, reject_payload = runtime._normalize_control_response(
        {
            "request_id": "req_false",
            "response": {
                "subtype": "success",
                "response": {"action": False},
            },
        }
    )

    assert approve_request_id == "req_true"
    assert approve_payload["action"] == "approve"
    assert reject_request_id == "req_false"
    assert reject_payload["action"] == "reject"


def test_task_manager_ttl_prunes_old_terminal_tasks() -> None:
    from backend.tasks.manager import TaskManager

    async def _exercise() -> None:
        manager = TaskManager(max_tasks=8, terminal_task_ttl_seconds=0)

        async def _ok() -> str:
            await asyncio.sleep(0)
            return "done"

        task = manager.create("agent.run", _ok())
        await manager.wait(task.id)
        await asyncio.sleep(0)

        manager.prune()
        assert manager.get(task.id) is None

    asyncio.run(_exercise())


def test_websocket_modules_send_only_through_transport_boundary() -> None:
    roots = [
        Path("backend/ws/agent_runner.py"),
        Path("backend/ws/command_handlers.py"),
        Path("backend/ws/handler.py"),
    ]
    direct_sends: list[tuple[str, str]] = []
    for path in roots:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "self.ws.send_json" not in line:
                continue
            direct_sends.append((path.as_posix(), line.strip()))

    assert direct_sends == []


def test_event_filter_hides_adapter_deltas_but_keeps_tool_progress() -> None:
    assert not should_emit_event(
        AgentEvent(type="tool_call_start", data={"id": "tc_1", "name": "read_file"})
    )
    assert not should_emit_event(
        AgentEvent(
            type="tool_call_delta", data={"id": "tc_1", "partial_arguments": '{"path"'}
        )
    )
    assert should_emit_event(
        AgentEvent.progress(
            "Read README.md",
            stage="tool",
            id="tool:tc_1",
            tool_call_id="tc_1",
            tool_name="read_file",
        )
    )

    reasoning = AgentEvent.thinking_chunk(
        "process note",
        source="model_preamble",
        visibility="timeline",
    )
    assert should_emit_event(reasoning)
    assert reasoning.type == "thinking_delta"
    assert reasoning.data["source"] == "model_preamble"
    assert reasoning.data["visibility"] == "timeline"


def test_agent_event_preserves_tool_evidence_metadata() -> None:
    event = AgentEvent.tool_result(
        id="fetch_1",
        summary="Fetched source",
        source_url="https://example.test/weather",
        extraction_status="ok",
        content_preview="Beijing 18.3C southwest wind",
        evidence_type="fetched",
    )

    assert event.type == "tool_result"
    assert event.data["source_url"] == "https://example.test/weather"
    assert event.data["extraction_status"] == "ok"
    assert event.data["content_preview"] == "Beijing 18.3C southwest wind"
    assert event.data["evidence_type"] == "fetched"


def test_agent_runner_transcript_uses_blocks_for_reasoning_only() -> None:
    source = Path("backend/ws/agent_runner.py").read_text(encoding="utf-8")

    assert "AgentTurnState" in source
    assert 'assistant_message["thinking"]' not in source
    assert "self.query_engine.submit(" in source
    assert 'event.type == "tool_call_start"' not in source
    assert 'event.type == "tool_call_delta"' not in source
    assert "assistant_draft_blocks" not in source
    assert "def _append_text_block" not in source
    assert "def _replace_tool_call_record" not in source


def test_agent_runner_emits_source_citations_for_fetched_web_evidence() -> None:
    source = Path("backend/ws/agent_runner.py").read_text(encoding="utf-8")
    turn_state_source = Path("backend/agent/turn_state.py").read_text(encoding="utf-8")

    assert "citation.add" in source
    assert "record_source_citation" in source
    assert "data.get('evidence_type')" in turn_state_source
    assert "'fetched'" in turn_state_source
    assert "_citations" in turn_state_source
    assert 'assistant_message["citations"]' in source


def test_frontend_websocket_hook_consumes_only_stable_tool_lifecycle_events() -> None:
    source = Path("frontend/src.v2/hooks/useWebSocket.ts").read_text(encoding="utf-8")

    assert 'case "tool_call_start"' not in source
    assert 'case "tool_call_delta"' not in source
    assert 'case "text_draft_chunk"' not in source
    assert 'case "text_draft_discard"' not in source
    assert 'case "text_draft_commit"' not in source
    assert 'case "text_draft_replace"' not in source
    assert "reduceToolCallDelta" not in source
    assert "reduceToolCallEarlyStart" not in source
    assert "draftStreamBuffers" not in source


def test_websocket_command_handlers_do_not_inline_command_safety_policy() -> None:
    source = Path("backend/ws/command_handlers.py").read_text(encoding="utf-8")

    assert "dangerous_patterns" not in source
    assert "Command contains a dangerous pattern" not in source


def test_websocket_command_handlers_use_declarative_registration() -> None:
    from backend.ws.command_handlers import SessionCommandHandlersMixin
    from backend.ws.handlers import HANDLERS
    from backend.ws.events import CLIENT_COMMAND_TYPES

    command_names = list(HANDLERS)
    source = Path("backend/ws/command_handlers.py").read_text(encoding="utf-8")
    handlers_source = Path("backend/ws/handlers/__init__.py").read_text(
        encoding="utf-8"
    )

    assert len(command_names) == len(set(command_names))
    assert set(command_names).issubset(CLIENT_COMMAND_TYPES)
    assert hasattr(SessionCommandHandlersMixin, "_register_command_handlers")
    assert "register_domain_handlers(self)" in source
    assert "partial(fn, session)" in handlers_source
    assert "COMMAND_HANDLERS" not in source
    assert "SessionDiffCommandHandlersMixin" not in source
    assert "SessionPreviewCommandHandlersMixin" not in source
    assert "_handle_diff_git_working_tree" not in source
    assert "_handle_preview_launch_start" not in source


def test_permission_rules_do_not_inline_command_safety_policy() -> None:
    source = Path("backend/permissions/rules.py").read_text(encoding="utf-8")

    assert "check_command_safety" not in source
    assert "dangerous_patterns" not in source
    assert "rm -rf" not in source


def test_tool_execution_has_no_heuristic_web_call_gate() -> None:
    source = Path("backend/agent/tool_execution.py").read_text(encoding="utf-8")

    assert "tool_guardrails" not in source
    assert "guardrail_controller" not in source
    assert "web_guard_reason" not in source
    assert "web_search_guard_result" not in source
    assert "backend.agent.policies.slot_filling" not in source


def test_frontend_message_list_has_no_legacy_renderer_escape_hatch() -> None:
    source = Path("frontend/src.v2/chat/MessageList.tsx").read_text(encoding="utf-8")

    assert "legacyMessageUi" not in source
    assert "AssistantMessage" not in source
    assert "projectMessagesToTurns" in source


def test_tool_execution_projects_activity_kind_on_tool_start() -> None:
    source = Path("backend/agent/tool_events.py").read_text(encoding="utf-8")

    assert "activity_kind=projection.activity_kind" in source


def test_tool_batch_execution_routes_control_tools_through_router() -> None:
    source = Path("backend/agent/tool_batch_execution.py").read_text(encoding="utf-8")
    router_source = Path("backend/agent/control_tools.py").read_text(encoding="utf-8")

    assert "from backend.agent.control_tools import" in source
    assert "ControlToolRouter" in source
    assert "def execute_serial" in source
    assert 'tc.name == "ask_user"' not in source
    assert 'tc.name == "load_skill"' not in source
    assert 'tc.name == "unload_skill"' not in source
    assert 'tc.name == "list_skills"' not in source
    assert "ask_user" in router_source
    assert "load_skill" not in router_source
    assert "unload_skill" not in router_source
    assert "list_skills" not in router_source


def test_tool_execution_does_not_rewrite_model_arguments() -> None:
    source = Path("backend/agent/tool_execution.py").read_text(encoding="utf-8")
    contracts_source = Path("backend/tools/contracts.py").read_text(encoding="utf-8")

    assert "ToolArgRepairEngine" not in source
    assert "default_args" not in source
    assert "default_args" not in contracts_source


def test_base_tool_keeps_cc_style_per_input_capability_hooks() -> None:
    source = Path("backend/tools/base.py").read_text(encoding="utf-8")
    execution_source = Path("backend/agent/tool_batch_execution.py").read_text(
        encoding="utf-8"
    )

    assert "def validate_input(" in source
    assert "def check_permission(" in source
    assert "def is_read_only(" in source
    assert "def is_concurrency_safe(" in source
    assert "tool.is_concurrency_safe(tc.arguments)" in execution_source


def test_build_context_derives_the_approval_policy_from_the_mode() -> None:
    """The mode already implies an approval policy; hard-coding one forked it.

    ``build_context`` returned "on-request" for every mode, so every caller other
    than the agent bootstrap ran ``separate approval policy``/``bypass`` as if it were
    ``default`` — a second source of truth for the same decision.
    """
    from backend.config_requirements import permission_mode_requirements

    checker = PermissionChecker(PermissionSettings())
    for mode in ("plan", "confirm", "auto", "bypass"):
        expected = permission_mode_requirements(mode)[0]
        assert (
            checker.build_context(mode=mode, source="test").approval_policy == expected
        ), mode
    # An explicit policy from managed requirements still wins.
    assert (
        checker.build_context(
            mode="auto", source="test", approval_policy="on-request"
        ).approval_policy
        == "on-request"
    )


def test_visible_tool_schemas_never_advertise_a_tool_the_chokepoint_denies(
    tmp_path,
) -> None:
    """Schema visibility must use the authorizing decision, not the raw level.

    ``get_schemas`` filtered on ``check()`` while the tool chokepoint authorizes
    with ``evaluate()``, which turns any "ask" into a deny when
    ``approval_policy == "never"``. In separate approval policy the model was handed write_file
    and run_command it could never call.
    """
    from backend.artifact.store import ArtifactStore
    from backend.permissions.checker import evaluate_permission_decision
    from backend.services.tool_registry_factory import build_tool_registry

    checker = PermissionChecker(PermissionSettings(), tmp_path)
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    for mode in ("confirm", "bypass"):
        context = checker.build_context(mode=mode, source="test")
        visible = {
            schema["function"]["name"]
            for schema in registry.get_schemas(
                permission_checker=checker, permission_context=context
            )
        }
        for name in visible:
            decision = evaluate_permission_decision(
                checker, name, context=context, tool=registry.get_tool(name)
            )
            assert decision.decision != "deny", (mode, name)

    confirm_context = checker.build_context(mode="confirm", source="test")
    confirm_visible = {
        schema["function"]["name"]
        for schema in registry.get_schemas(
            permission_checker=checker, permission_context=confirm_context
        )
    }
    assert "read_file" in confirm_visible
