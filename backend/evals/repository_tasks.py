"""Run coding agents against real repositories with mechanical judges.

This module deliberately does not grade prose or ask another model whether a
change "looks right". A task is prepared in an isolated checkout, its broken
baseline is proved, an arbitrary agent driver receives the task prompt, and
independent commands/file invariants decide the outcome.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


_DEFAULT_IGNORED_CHANGES = [
    ".minicode/plans/**",
    ".minicode/todos/**",
]
_EVAL_VENV_NAME = ".minicode-eval-venv"
_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_PYTEST_FILE_RE = re.compile(r"^[A-Za-z0-9_.\\/-]+\.py$")
_PYTEST_NODE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$"
)


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be an array of strings")
    return [str(item) for item in value]


def portable_pytest_selectors(values: list[str], *, limit: int | None = None) -> list[str]:
    """Return stable pytest selectors from sometimes noisy SWE-bench metadata.

    Parameter ids can contain platform-specific paths, commas, quotes, and even
    fragments of captured pytest output in older SWE-bench exports. Selecting
    the parent test function keeps the judge portable while still exercising
    every parameter case for that test.
    """

    selectors: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip().replace("\\", "/")
        file_path, separator, node = value.partition("::")
        if not _PYTEST_FILE_RE.fullmatch(file_path):
            continue
        candidate = file_path
        if separator:
            parent_node = re.sub(r"\[.*$", "", node).strip()
            if _PYTEST_NODE_RE.fullmatch(parent_node):
                candidate = f"{file_path}::{parent_node}"
        if candidate in seen:
            continue
        seen.add(candidate)
        selectors.append(candidate)
        if limit is not None and len(selectors) >= limit:
            break
    return selectors


@dataclass(frozen=True)
class RepositorySource:
    local_path: str = ""
    git_url: str = ""
    revision: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "RepositorySource":
        if isinstance(value, str):
            path = value.strip()
            if not path:
                raise ValueError("source cannot be empty")
            return cls(local_path=path)
        if not isinstance(value, dict):
            raise ValueError("source must be a local path string or a git source object")
        git_url = str(value.get("git") or "").strip()
        revision = str(value.get("revision") or "").strip().lower()
        if not git_url or not _FULL_GIT_SHA_RE.fullmatch(revision):
            raise ValueError("git source requires git and a full 40-character revision SHA")
        return cls(git_url=git_url, revision=revision)

    @property
    def is_git(self) -> bool:
        return bool(self.git_url)


@dataclass(frozen=True)
class CommandSpec:
    argv: list[str]
    timeout_seconds: float = 300.0
    expected_exit_codes: list[int] = field(default_factory=lambda: [0])
    name: str = ""
    env: dict[str, str] = field(default_factory=dict)
    output_limit_chars: int = 100_000

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, field_name: str) -> "CommandSpec":
        if not isinstance(value, dict):
            raise ValueError(f"{field_name} must be an object")
        argv = _string_list(value.get("argv"), field_name=f"{field_name}.argv")
        if not argv:
            raise ValueError(f"{field_name}.argv cannot be empty")
        exit_codes = value.get("expected_exit_codes", [0])
        if not isinstance(exit_codes, list) or not exit_codes or not all(isinstance(code, int) for code in exit_codes):
            raise ValueError(f"{field_name}.expected_exit_codes must be a non-empty integer array")
        raw_env = value.get("env", {})
        if not isinstance(raw_env, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in raw_env.items()
        ):
            raise ValueError(f"{field_name}.env must be an object of string values")
        return cls(
            argv=argv,
            timeout_seconds=max(1.0, float(value.get("timeout_seconds", 300))),
            expected_exit_codes=list(exit_codes),
            name=str(value.get("name") or ""),
            env=dict(raw_env),
            output_limit_chars=max(10_000, min(10_000_000, int(value.get("output_limit_chars", 100_000)))),
        )


@dataclass(frozen=True)
class FileJudge:
    path: str
    contains: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    must_exist: bool = True

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, index: int) -> "FileJudge":
        path = str(value.get("path") or "").strip()
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            raise ValueError(f"file_judges[{index}].path must be a workspace-relative path")
        return cls(
            path=path,
            contains=_string_list(value.get("contains", []), field_name=f"file_judges[{index}].contains"),
            excludes=_string_list(value.get("excludes", []), field_name=f"file_judges[{index}].excludes"),
            must_exist=bool(value.get("must_exist", True)),
        )


@dataclass(frozen=True)
class RepositoryTask:
    task_id: str
    title: str
    prompt: str
    source: RepositorySource
    python_version: str = ""
    env: dict[str, str] = field(default_factory=dict)
    setup: list[CommandSpec] = field(default_factory=list)
    baseline: list[CommandSpec] = field(default_factory=list)
    judges: list[CommandSpec] = field(default_factory=list)
    file_judges: list[FileJudge] = field(default_factory=list)
    forbidden_changes: list[str] = field(default_factory=list)
    ignored_changes: list[str] = field(default_factory=lambda: list(_DEFAULT_IGNORED_CHANGES))
    require_diff: bool = True
    agent_verify_command: str = ""
    agent_verify_timeout_seconds: float = 300.0
    agent_min_subagents: int = 0
    agent_min_parallel_subagents: int = 0
    agent_max_subagents: int = 0
    agent_timeout_seconds: float = 1800.0
    baseline_patch: str = ""
    seeds: tuple[int, ...] = (0,)
    minimum_pass_rate: float = 1.0
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RepositoryTask":
        if not isinstance(value, dict):
            raise ValueError("task manifest must be an object")
        task_id = str(value.get("id") or "").strip()
        prompt = str(value.get("prompt") or "").strip()
        if not task_id or not prompt or value.get("source") is None:
            raise ValueError("task manifest requires non-empty id, prompt, and source")
        source = RepositorySource.from_value(value.get("source"))

        def commands(key: str) -> list[CommandSpec]:
            raw = value.get(key, [])
            if not isinstance(raw, list):
                raise ValueError(f"{key} must be an array")
            return [CommandSpec.from_dict(item, field_name=f"{key}[{index}]") for index, item in enumerate(raw)]

        raw_file_judges = value.get("file_judges", [])
        if not isinstance(raw_file_judges, list):
            raise ValueError("file_judges must be an array")
        raw_env = value.get("env", {})
        if not isinstance(raw_env, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in raw_env.items()
        ):
            raise ValueError("env must be an object of string values")
        min_subagents = max(0, int(value.get("agent_min_subagents", 0)))
        min_parallel_subagents = max(
            0,
            int(value.get("agent_min_parallel_subagents", 0)),
        )
        max_subagents = max(0, int(value.get("agent_max_subagents", 0)))
        if min_parallel_subagents > min_subagents:
            raise ValueError("agent_min_parallel_subagents cannot exceed agent_min_subagents")
        if max_subagents and max_subagents < min_subagents:
            raise ValueError("agent_max_subagents cannot be lower than agent_min_subagents")
        raw_seeds = value.get("seeds", [0])
        if (
            not isinstance(raw_seeds, list)
            or not raw_seeds
            or not all(isinstance(seed, int) and not isinstance(seed, bool) for seed in raw_seeds)
        ):
            raise ValueError("seeds must be a non-empty array of integers")
        seeds = tuple(dict.fromkeys(raw_seeds))
        minimum_pass_rate = float(value.get("minimum_pass_rate", 1.0))
        if not 0 < minimum_pass_rate <= 1:
            raise ValueError("minimum_pass_rate must be greater than 0 and at most 1")
        tags = tuple(_string_list(value.get("tags", []), field_name="tags"))
        python_version = str(value.get("python_version") or "").strip()
        if python_version and not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", python_version):
            raise ValueError("python_version must be a numeric Python version such as 3.8")
        return cls(
            task_id=task_id,
            title=str(value.get("title") or task_id),
            prompt=prompt,
            source=source,
            python_version=python_version,
            env=dict(raw_env),
            setup=commands("setup"),
            baseline=commands("baseline"),
            judges=commands("judges"),
            file_judges=[FileJudge.from_dict(item, index=index) for index, item in enumerate(raw_file_judges)],
            forbidden_changes=_string_list(value.get("forbidden_changes", []), field_name="forbidden_changes"),
            ignored_changes=_string_list(
                value.get("ignored_changes", _DEFAULT_IGNORED_CHANGES),
                field_name="ignored_changes",
            ),
            require_diff=bool(value.get("require_diff", True)),
            agent_verify_command=str(value.get("agent_verify_command") or "").strip(),
            agent_verify_timeout_seconds=max(
                1.0,
                float(value.get("agent_verify_timeout_seconds", 300)),
            ),
            agent_min_subagents=min_subagents,
            agent_min_parallel_subagents=min_parallel_subagents,
            agent_max_subagents=max_subagents,
            agent_timeout_seconds=max(1.0, float(value.get("agent_timeout_seconds", 1800))),
            baseline_patch=str(value.get("baseline_patch") or ""),
            seeds=seeds,
            minimum_pass_rate=minimum_pass_rate,
            tags=tags,
        )


@dataclass
class JudgeResult:
    name: str
    passed: bool
    detail: str = ""
    exit_code: int | None = None
    duration_ms: int = 0
    output: str = ""


@dataclass
class AgentRunMetrics:
    agent: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    tool_call_count: int = 0
    tool_failure_count: int = 0
    invalid_search_count: int = 0
    recovery_count: int = 0
    provider_elapsed_ms: int = 0
    cost_usd: float = 0.0
    terminal_status: str = ""
    terminal_reason: str = ""
    iterations: int = 0
    verification_failure_count: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    tool_error_kinds: dict[str, int] = field(default_factory=dict)


@dataclass
class FailureAttribution:
    category: str = ""
    detail: str = ""
    confidence: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class EvalReport:
    task_id: str
    passed: bool
    workspace: str
    started_at_ms: int
    duration_ms: int
    baseline_proved_broken: bool
    agent_exit_code: int | None
    agent_output: str
    changed_files: list[str]
    tracked_patch: str
    judges: list[JudgeResult]
    infrastructure_error: str = ""
    seed: int = 0
    agent_metrics: AgentRunMetrics = field(default_factory=AgentRunMetrics)
    failure_attribution: FailureAttribution = field(default_factory=FailureAttribution)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AggregateEvalReport:
    task_id: str
    passed: bool
    pass_rate: float
    minimum_pass_rate: float
    seeds: list[int]
    reports: list[EvalReport]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_repository_task(path: Path) -> RepositoryTask:
    return RepositoryTask.from_dict(json.loads(path.read_text(encoding="utf-8")))


def build_agent_task_prompt(task: RepositoryTask) -> str:
    """Expose evaluation-owned constraints without prescribing a workflow."""

    sections = [task.prompt]
    contract: list[str] = []
    if task.forbidden_changes:
        immutable = "\n".join(f"- {path}" for path in task.forbidden_changes)
        contract.append(
            "The following evaluator-owned files are immutable. Do not edit, "
            f"delete, rename, or replace them:\n{immutable}"
        )
    if task.agent_verify_command:
        contract.append(
            "Before completing, run this mechanical verification command and "
            f"fix any failure:\n{task.agent_verify_command}"
        )
    if contract:
        sections.append(
            "Repository evaluation contract:\n"
            "Fix the implementation rather than weakening the evaluation.\n"
            + "\n\n".join(contract)
        )
    return "\n\n".join(sections)


def parse_agent_run_metrics(output: str, *, fallback_agent: str = "") -> AgentRunMetrics:
    """Read the stable final metrics envelope emitted by every eval driver."""

    summary: dict[str, Any] = {}
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") not in {"eval.driver.summary", "eval.external.summary"}:
            continue
        data = payload.get("data")
        if isinstance(data, dict):
            summary = data

    usage = summary.get("usage") if isinstance(summary.get("usage"), dict) else {}
    tool_calls = summary.get("tool_calls")
    tool_statuses = summary.get("tool_result_statuses")
    raw_tool_calls = summary.get("tool_calls")
    raw_tool_error_kinds = summary.get("tool_error_kinds")
    tool_call_count = int(summary.get("tool_call_count") or 0)
    if not tool_call_count and isinstance(tool_calls, dict):
        tool_call_count = sum(int(value or 0) for value in tool_calls.values())
    tool_failure_count = int(summary.get("tool_failure_count") or 0)
    if not tool_failure_count and isinstance(tool_statuses, dict):
        tool_failure_count = sum(
            int(value or 0)
            for status, value in tool_statuses.items()
            if str(status) not in {"success", "partial"}
        )
    loop_metrics = summary.get("loop_metrics") if isinstance(summary.get("loop_metrics"), dict) else {}
    return AgentRunMetrics(
        agent=str(summary.get("agent") or fallback_agent),
        model=str(summary.get("model") or ""),
        input_tokens=int(usage.get("input_tokens") or summary.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or summary.get("output_tokens") or 0),
        cache_read_input_tokens=int(
            usage.get("cache_read_input_tokens")
            or usage.get("cached_input_tokens")
            or summary.get("cache_read_input_tokens")
            or 0
        ),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        tool_call_count=tool_call_count or int(loop_metrics.get("tool_call_count") or 0),
        tool_failure_count=tool_failure_count,
        invalid_search_count=int(summary.get("invalid_search_count") or 0),
        recovery_count=int(summary.get("recovery_count") or 0),
        provider_elapsed_ms=int(summary.get("provider_elapsed_ms") or loop_metrics.get("elapsed_ms") or 0),
        cost_usd=float(summary.get("cost_usd") or 0.0),
        terminal_status=str(summary.get("terminal_status") or ""),
        terminal_reason=str(summary.get("terminal_reason") or ""),
        iterations=int(summary.get("iterations") or loop_metrics.get("iterations") or 0),
        verification_failure_count=int(summary.get("verification_failure_count") or 0),
        tool_calls={
            str(name): int(count or 0)
            for name, count in raw_tool_calls.items()
        } if isinstance(raw_tool_calls, dict) else {},
        tool_error_kinds={
            str(name): int(count or 0)
            for name, count in raw_tool_error_kinds.items()
        } if isinstance(raw_tool_error_kinds, dict) else {},
    )


def parse_agent_driver_error(output: str) -> str:
    """Return a structured driver bootstrap error, if one was emitted.

    Eval drivers write a single JSON object before exiting when they cannot
    construct the runtime at all (missing credentials, malformed configuration,
    unavailable executable, and similar harness failures). Treating that as a
    model failure corrupts pass/failure attribution because no model call took
    place.
    """

    error = ""
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = payload.get("driver_error")
        if isinstance(value, str) and value.strip():
            error = value.strip()
    return error


def classify_failure_attribution(
    *,
    passed: bool,
    infrastructure_error: str,
    metrics: AgentRunMetrics,
    changed_files: list[str],
    judges: list[JudgeResult],
) -> FailureAttribution:
    """Classify a failed run from explicit runtime and judge evidence only."""
    if passed:
        return FailureAttribution()

    evidence: list[str] = []
    if infrastructure_error:
        evidence.append(infrastructure_error)
        if infrastructure_error.startswith("agent provider failed"):
            return FailureAttribution(
                category="provider_api_failure",
                detail="Provider failed before useful model execution.",
                confidence="high",
                evidence=evidence,
            )
        return FailureAttribution(
            category="evaluation_environment_error",
            detail="Repository setup or evaluator infrastructure failed.",
            confidence="high",
            evidence=evidence,
        )

    provider_reasons = {
        "api",
        "api_error",
        "auth",
        "billing",
        "blocked",
        "model",
        "stream_error",
    }
    budget_reasons = {
        "max_iterations",
        "max_tool_calls",
        "max_turn_seconds",
        "timeout",
    }
    if metrics.terminal_reason in provider_reasons:
        evidence.append(f"terminal_reason={metrics.terminal_reason}")
        return FailureAttribution(
            category="provider_api_failure",
            detail="The provider or model stream ended the run.",
            confidence="high",
            evidence=evidence,
        )
    if metrics.terminal_reason == "budget_exceeded":
        evidence.extend(
            [
                "terminal_reason=budget_exceeded",
                f"input_tokens={metrics.input_tokens}",
            ]
        )
        return FailureAttribution(
            category="context_growth",
            detail="The accumulated context exhausted the runtime token budget.",
            confidence="high",
            evidence=evidence,
        )
    if metrics.terminal_reason in budget_reasons:
        evidence.extend(
            [
                f"terminal_reason={metrics.terminal_reason}",
                f"iterations={metrics.iterations}",
                f"tool_calls={metrics.tool_call_count}",
            ]
        )
        return FailureAttribution(
            category="timeout_or_budget",
            detail="The run reached a mechanical execution boundary before the judges passed.",
            confidence="high",
            evidence=evidence,
        )

    unavailable_tool_errors = {
        name: count
        for name, count in metrics.tool_error_kinds.items()
        if name in {"not_found", "disabled", "permission_denied", "unsupported"}
    }
    if unavailable_tool_errors:
        evidence.append(f"tool_error_kinds={unavailable_tool_errors}")
        return FailureAttribution(
            category="tool_missing_or_hidden",
            detail="A required tool was unavailable or denied during execution.",
            confidence="high",
            evidence=evidence,
        )

    if metrics.verification_failure_count > 0:
        evidence.append(
            f"verification_failure_count={metrics.verification_failure_count}"
        )
        return FailureAttribution(
            category="verification_failure",
            detail="The agent's configured mechanical verification failed and was not repaired.",
            confidence="high",
            evidence=evidence,
        )

    failed_judges = [judge.name for judge in judges if not judge.passed]
    evidence.append(f"changed_files={len(changed_files)}")
    if failed_judges:
        evidence.append(f"failed_judges={failed_judges}")
    return FailureAttribution(
        category="model_reasoning_or_implementation_error",
        detail=(
            "The run changed the repository but the mechanical judges still failed."
            if changed_files
            else "The model did not produce a judge-satisfying repository change."
        ),
        confidence="medium",
        evidence=evidence,
    )


class RepositoryTaskRunner:
    """Prepare an isolated checkout, invoke an agent, and execute all judges."""

    def __init__(
        self,
        *,
        output_root: Path | None = None,
        keep_workspace: bool = False,
        source_cache_root: Path | None = None,
    ) -> None:
        self.output_root = output_root
        self.keep_workspace = keep_workspace
        self.source_cache_root = (
            source_cache_root
            or Path.home() / ".cache" / "minicode" / "repository-evals"
        ).expanduser().resolve()

    @staticmethod
    def _remove_tree(path: Path) -> None:
        """Remove an evaluation tree, including read-only Git object files."""
        target = path.resolve()
        if not target.exists():
            return

        def _make_writable(func: Any, item: str, _exc_info: Any) -> None:
            try:
                os.chmod(item, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
                func(item)
            except OSError:
                pass

        for attempt in range(4):
            try:
                shutil.rmtree(target, onerror=_make_writable)
            except OSError:
                pass
            if not target.exists():
                return
            if attempt < 3:
                time.sleep(0.15 * (attempt + 1))
        raise OSError(f"evaluation workspace cleanup failed: {target}")

    @staticmethod
    def _run_command(
        spec: CommandSpec,
        cwd: Path,
        *,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
        use_isolated_python: bool = True,
    ) -> JudgeResult:
        started = time.monotonic()
        name = spec.name or " ".join(spec.argv)
        argv = list(spec.argv)
        executable = Path(argv[0]).name.lower() if argv else ""
        if executable in {"python", "python.exe", "python3", "python3.exe"}:
            if use_isolated_python and env and env.get("VIRTUAL_ENV"):
                scripts_dir = Path(env["VIRTUAL_ENV"]) / ("Scripts" if os.name == "nt" else "bin")
                candidate = scripts_dir / ("python.exe" if os.name == "nt" else "python")
                if not candidate.is_file():
                    return JudgeResult(
                        name=name,
                        passed=False,
                        detail=f"isolated Python executable is missing: {candidate}",
                    )
                argv[0] = str(candidate)
            elif not Path(argv[0]).is_absolute():
                # The agent driver is part of MiniCode, not part of the target
                # repository. Keep it on the host interpreter even though its
                # child tools inherit the target repository's virtualenv.
                argv[0] = sys.executable
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.PIPE if stdin is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**(env or os.environ), **spec.env},
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
                start_new_session=os.name != "nt",
            )
            stdout, stderr = process.communicate(input=stdin, timeout=spec.timeout_seconds)
            output = (stdout + stderr)[-spec.output_limit_chars:]
            return JudgeResult(
                name=name,
                passed=process.returncode in spec.expected_exit_codes,
                exit_code=process.returncode,
                duration_ms=int((time.monotonic() - started) * 1000),
                output=output,
                detail=f"expected exit code in {spec.expected_exit_codes}",
            )
        except subprocess.TimeoutExpired as exc:
            if process is not None and process.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                        timeout=10,
                    )
                else:
                    try:
                        os.killpg(process.pid, 9)
                    except OSError:
                        process.kill()
            try:
                stdout, stderr = process.communicate(timeout=5) if process is not None else ("", "")
            except (subprocess.TimeoutExpired, OSError):
                if process is not None and process.poll() is None:
                    process.kill()
                stdout, stderr = ("", "")
            output = "".join(
                part for part in (*((exc.stdout, exc.stderr)), stdout, stderr)
                if isinstance(part, str)
            )[-spec.output_limit_chars:]
            return JudgeResult(
                name=name,
                passed=False,
                detail=f"timed out after {spec.timeout_seconds:g}s",
                duration_ms=int((time.monotonic() - started) * 1000),
                output=output,
            )
        except OSError as exc:
            return JudgeResult(name=name, passed=False, detail=str(exc))

    @staticmethod
    def _copy_source(source: Path, destination: Path) -> None:
        if not source.is_dir():
            raise ValueError(f"task source does not exist or is not a directory: {source}")
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".pytest_cache"),
        )
        subprocess.run(["git", "init", "-q"], cwd=destination, check=True)
        exclude_file = destination / ".git" / "info" / "exclude"
        with exclude_file.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n__pycache__/\n*.py[cod]\n.pytest_cache/\n.coverage\n{_EVAL_VENV_NAME}/\n"
            )
        subprocess.run(["git", "add", "-A"], cwd=destination, check=True)
        subprocess.run(
            ["git", "-c", "user.name=MiniCode Eval", "-c", "user.email=eval@localhost", "commit", "-qm", "eval baseline"],
            cwd=destination,
            check=True,
        )

    @staticmethod
    def _git_run(argv: list[str], *, cwd: Path | None = None, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"git {' '.join(argv)} failed: {detail}")
        return completed

    def _copy_git_source(self, source: RepositorySource, destination: Path) -> None:
        cache_key = hashlib.sha256(source.git_url.encode("utf-8")).hexdigest()[:24]
        cache = self.source_cache_root / cache_key
        self.source_cache_root.mkdir(parents=True, exist_ok=True)
        if not (cache / ".git").is_dir():
            if cache.exists():
                self._remove_tree(cache)
            self._git_run(["clone", "--filter=blob:none", "--no-checkout", source.git_url, str(cache)])
        else:
            self._git_run(["remote", "set-url", "origin", source.git_url], cwd=cache)
        self._git_run(["fetch", "--no-tags", "origin", source.revision], cwd=cache)
        self._git_run(["checkout", "--detach", "--force", source.revision], cwd=cache)
        cached_head = self._git_run(["rev-parse", "HEAD"], cwd=cache).stdout.strip().lower()
        if cached_head != source.revision:
            raise RuntimeError(
                f"cached source revision mismatch: expected {source.revision}, got {cached_head}"
            )
        self._git_run(["clone", "--quiet", "--no-checkout", str(cache), str(destination)])
        self._git_run(["checkout", "--detach", "--force", source.revision], cwd=destination)
        workspace_head = self._git_run(["rev-parse", "HEAD"], cwd=destination).stdout.strip().lower()
        if workspace_head != source.revision:
            raise RuntimeError(
                f"workspace revision mismatch: expected {source.revision}, got {workspace_head}"
            )
        self._git_run(["config", "user.name", "MiniCode Eval"], cwd=destination)
        self._git_run(["config", "user.email", "eval@localhost"], cwd=destination)

    @staticmethod
    def _exclude_runtime_files(workspace: Path) -> None:
        exclude_file = workspace / ".git" / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_file.read_text(encoding="utf-8", errors="replace") if exclude_file.exists() else ""
        entry = f"{_EVAL_VENV_NAME}/"
        if entry not in existing.splitlines():
            with exclude_file.open("a", encoding="utf-8") as handle:
                handle.write(f"\n{entry}\n")

    @staticmethod
    def _isolated_task_environment(
        workspace: Path,
        env: dict[str, str],
        *,
        python_version: str = "",
    ) -> dict[str, str]:
        """Create a disposable Python environment for repository setup and tools."""

        venv_dir = workspace / _EVAL_VENV_NAME
        uv = shutil.which("uv") if python_version else None
        argv = (
            [uv, "venv", "--python", python_version, "--seed", str(venv_dir)]
            if uv
            else [sys.executable, "-m", "venv", str(venv_dir)]
        )
        completed = subprocess.run(
            argv,
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            runtime = f"Python {python_version}" if python_version else "host Python"
            raise RuntimeError(f"unable to create isolated evaluation environment with {runtime}: {detail}")
        scripts_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
        python_executable = scripts_dir / ("python.exe" if os.name == "nt" else "python")
        if not python_executable.is_file():
            raise RuntimeError(f"isolated Python executable is missing: {python_executable}")
        version_check = subprocess.run(
            [str(python_executable), "-c", "import platform; print(platform.python_version())"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        actual_version = version_check.stdout.strip()
        if version_check.returncode != 0 or (
            python_version and not actual_version.startswith(f"{python_version}.")
        ):
            raise RuntimeError(
                f"isolated Python version mismatch: requested={python_version or 'host'}, "
                f"actual={actual_version or 'unknown'}"
            )
        isolated = dict(env)
        isolated.pop("PYTHONHOME", None)
        isolated["VIRTUAL_ENV"] = str(venv_dir)
        isolated["PYTHONNOUSERSITE"] = "1"
        isolated["PATH"] = str(scripts_dir) + os.pathsep + isolated.get("PATH", "")
        return isolated

    def _prepare_source(
        self,
        source: RepositorySource,
        destination: Path,
        *,
        manifest_dir: Path | None,
    ) -> None:
        if source.is_git:
            self._copy_git_source(source, destination)
            return
        local = Path(source.local_path).expanduser()
        if not local.is_absolute() and manifest_dir is not None:
            local = manifest_dir / local
        self._copy_source(local.resolve(), destination)

    @classmethod
    def _apply_baseline_patch(cls, workspace: Path, patch: str) -> None:
        if not patch:
            return
        cls._git_run(["apply", "--whitespace=nowarn", "-"], cwd=workspace, stdin=patch)

    @staticmethod
    def _commit_prepared_baseline(workspace: Path) -> None:
        """Seal setup/injected defects so only the agent's edits are judged."""

        subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=workspace,
            check=False,
        )
        if staged.returncode == 1:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=MiniCode Eval",
                    "-c",
                    "user.email=eval@localhost",
                    "commit",
                    "-qm",
                    "prepared broken baseline",
                ],
                cwd=workspace,
                check=True,
            )
        elif staged.returncode != 0:
            raise RuntimeError("unable to inspect prepared evaluation baseline")

    @staticmethod
    def _changed_files(workspace: Path, *, ignored_changes: list[str] | None = None) -> list[str]:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        entries = result.stdout.decode("utf-8", errors="replace").split("\0")
        changed: list[str] = []
        for entry in entries:
            if not entry:
                continue
            path = entry[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            changed.append(path.replace("\\", "/"))
        ignored = ignored_changes or []
        return sorted(
            path
            for path in set(changed)
            if not RepositoryTaskRunner._matches_forbidden(path, ignored)
        )

    @staticmethod
    def _tracked_patch(workspace: Path, *, limit_chars: int = 2_000_000) -> str:
        result = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            return ""
        patch = result.stdout
        if len(patch) <= limit_chars:
            return patch
        head = (limit_chars * 2) // 3
        tail = limit_chars - head
        omitted = len(patch) - head - tail
        return f"{patch[:head]}\n... [{omitted} patch chars omitted] ...\n{patch[-tail:]}"

    @staticmethod
    def _matches_forbidden(path: str, patterns: list[str]) -> bool:
        from fnmatch import fnmatch

        return any(fnmatch(path, pattern) or path == pattern.rstrip("/") for pattern in patterns)

    def run(
        self,
        task: RepositoryTask,
        *,
        agent_argv: list[str],
        manifest_dir: Path | None = None,
        seed: int | None = None,
        agent_name: str = "",
    ) -> EvalReport:
        started_at_ms = int(time.time() * 1000)
        started = time.monotonic()
        root = (self.output_root or Path(tempfile.mkdtemp(prefix="minicode-eval-"))).resolve()
        root.mkdir(parents=True, exist_ok=True)
        resolved_seed = task.seeds[0] if seed is None else int(seed)
        workspace = root / f"{task.task_id}-seed-{resolved_seed}-{started_at_ms}"
        judges: list[JudgeResult] = []
        baseline_proved_broken = not task.baseline
        agent_exit_code: int | None = None
        agent_output = ""
        changed_files: list[str] = []
        tracked_patch = ""
        infrastructure_error = ""
        try:
            self._prepare_source(task.source, workspace, manifest_dir=manifest_dir)
            self._exclude_runtime_files(workspace)
            self._apply_baseline_patch(workspace, task.baseline_patch)
            task_env = {
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                **task.env,
            }
            if task.setup:
                task_env = self._isolated_task_environment(
                    workspace,
                    task_env,
                    python_version=task.python_version,
                )
            for spec in task.setup:
                result = self._run_command(spec, workspace, env=task_env)
                judges.append(JudgeResult(name=f"setup: {result.name}", passed=result.passed, detail=result.detail, exit_code=result.exit_code, duration_ms=result.duration_ms, output=result.output))
                if not result.passed:
                    raise RuntimeError(f"setup failed: {result.name}")
            self._commit_prepared_baseline(workspace)

            baseline_results = [self._run_command(spec, workspace, env=task_env) for spec in task.baseline]
            baseline_proved_broken = bool(baseline_results) and any(not item.passed for item in baseline_results)
            judges.extend(
                JudgeResult(
                    name=f"baseline must fail: {item.name}",
                    passed=not item.passed,
                    detail="task is invalid if its baseline already passes",
                    exit_code=item.exit_code,
                    duration_ms=item.duration_ms,
                    output=item.output,
                )
                for item in baseline_results
            )
            if task.baseline and not baseline_proved_broken:
                raise RuntimeError("all baseline checks passed; the task does not prove a pre-existing defect")

            agent_spec = CommandSpec(
                argv=list(agent_argv),
                timeout_seconds=task.agent_timeout_seconds,
                expected_exit_codes=[0],
                name="agent execution",
                output_limit_chars=2_000_000,
            )
            agent_env = dict(task_env)
            agent_env.update(
                {
                    "MINICODE_EVAL_TASK_ID": task.task_id,
                    "MINICODE_EVAL_WORKSPACE": str(workspace),
                    "MINICODE_EVAL_REQUIRE_DIFF": "1" if task.require_diff else "0",
                    "MINICODE_EVAL_MIN_SUBAGENTS": str(task.agent_min_subagents),
                    "MINICODE_EVAL_MIN_PARALLEL_SUBAGENTS": str(
                        task.agent_min_parallel_subagents
                    ),
                    "MINICODE_EVAL_MAX_SUBAGENTS": str(task.agent_max_subagents),
                    "MINICODE_EVAL_AGENT_TIMEOUT_SECONDS": str(task.agent_timeout_seconds),
                    "MINICODE_EVAL_SEED": str(resolved_seed),
                }
            )
            agent_result = self._run_command(
                agent_spec,
                workspace,
                stdin=build_agent_task_prompt(task),
                env=agent_env,
                use_isolated_python=False,
            )
            agent_exit_code = agent_result.exit_code
            agent_output = agent_result.output
            agent_metrics = parse_agent_run_metrics(agent_output, fallback_agent=agent_name)
            judges.append(agent_result)
            driver_error = parse_agent_driver_error(agent_output)
            if not agent_result.passed and driver_error:
                infrastructure_error = (
                    "agent driver failed before model execution: " + driver_error
                )
            if (
                not agent_result.passed
                and not infrastructure_error
                and agent_metrics.terminal_status == "failed"
                and agent_metrics.terminal_reason
                in {
                    "api",
                    "api_error",
                    "auth",
                    "billing",
                    "blocked",
                    "incomplete_tool_stream",
                    "model",
                    "stream_error",
                }
                and agent_metrics.tool_call_count == 0
                and agent_metrics.input_tokens == 0
                and agent_metrics.output_tokens == 0
            ):
                infrastructure_error = (
                    "agent provider failed before model execution: "
                    f"{agent_metrics.terminal_reason}"
                )

            changed_files = self._changed_files(workspace, ignored_changes=task.ignored_changes)
            tracked_patch = self._tracked_patch(workspace)
            judges.append(JudgeResult(name="workspace diff", passed=bool(changed_files) or not task.require_diff, detail=f"{len(changed_files)} changed files"))
            forbidden = [path for path in changed_files if self._matches_forbidden(path, task.forbidden_changes)]
            judges.append(JudgeResult(name="forbidden changes", passed=not forbidden, detail=", ".join(forbidden)))

            for file_judge in task.file_judges:
                target = workspace / file_judge.path
                exists = target.is_file()
                content = target.read_text(encoding="utf-8", errors="replace") if exists else ""
                missing = [needle for needle in file_judge.contains if needle not in content]
                present = [needle for needle in file_judge.excludes if needle in content]
                passed = exists == file_judge.must_exist and not missing and not present
                judges.append(
                    JudgeResult(
                        name=f"file invariant: {file_judge.path}",
                        passed=passed,
                        detail=f"exists={exists}; missing={missing}; forbidden_present={present}",
                    )
                )
            judges.extend(self._run_command(spec, workspace, env=task_env) for spec in task.judges)
        except Exception as exc:
            infrastructure_error = str(exc)
            agent_metrics = parse_agent_run_metrics(agent_output, fallback_agent=agent_name)

        passed = not infrastructure_error and baseline_proved_broken and bool(judges) and all(item.passed for item in judges)
        failure_attribution = classify_failure_attribution(
            passed=passed,
            infrastructure_error=infrastructure_error,
            metrics=agent_metrics,
            changed_files=changed_files,
            judges=judges,
        )
        report = EvalReport(
            task_id=task.task_id,
            passed=passed,
            workspace=str(workspace),
            started_at_ms=started_at_ms,
            duration_ms=int((time.monotonic() - started) * 1000),
            baseline_proved_broken=baseline_proved_broken,
            agent_exit_code=agent_exit_code,
            agent_output=agent_output,
            changed_files=changed_files,
            tracked_patch=tracked_patch,
            judges=judges,
            infrastructure_error=infrastructure_error,
            seed=resolved_seed,
            agent_metrics=agent_metrics,
            failure_attribution=failure_attribution,
        )
        report_path = root / f"{task.task_id}-seed-{resolved_seed}-{started_at_ms}.json"
        report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        if not self.keep_workspace and workspace.exists():
            try:
                self._remove_tree(workspace)
            except OSError as exc:
                infrastructure_error = infrastructure_error or str(exc)
                report.infrastructure_error = infrastructure_error
                report.passed = False
            if not workspace.exists():
                report.workspace = ""
        return report

    def run_all_seeds(
        self,
        task: RepositoryTask,
        *,
        agent_argv: list[str],
        manifest_dir: Path | None = None,
        agent_name: str = "",
    ) -> AggregateEvalReport:
        root = (self.output_root or Path(tempfile.mkdtemp(prefix="minicode-eval-"))).resolve()
        root.mkdir(parents=True, exist_ok=True)
        seed_runner = self
        if self.output_root is None:
            seed_runner = RepositoryTaskRunner(
                output_root=root,
                keep_workspace=self.keep_workspace,
                source_cache_root=self.source_cache_root,
            )
        reports = [
            seed_runner.run(
                task,
                agent_argv=agent_argv,
                manifest_dir=manifest_dir,
                seed=seed,
                agent_name=agent_name,
            )
            for seed in task.seeds
        ]
        pass_rate = sum(report.passed for report in reports) / len(reports)
        aggregate = AggregateEvalReport(
            task_id=task.task_id,
            passed=pass_rate >= task.minimum_pass_rate,
            pass_rate=pass_rate,
            minimum_pass_rate=task.minimum_pass_rate,
            seeds=list(task.seeds),
            reports=reports,
        )
        (root / f"{task.task_id}-aggregate.json").write_text(
            json.dumps(aggregate.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return aggregate
