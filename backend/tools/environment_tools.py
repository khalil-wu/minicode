"""Read-only local environment discovery tools."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema


class DetectPythonEnvironmentTool(BaseTool):
    """Discover Python interpreters, virtual environments, and conda envs."""

    name = "detect_python_environment"
    result_kind = "environment"
    activity_kind = "genericTool"
    display_label = "Detect Python environment"
    description = (
        "Inspect local Python environments before installing Python dependencies. "
        "Use this before pip/conda/mamba/uv installs, especially for large packages "
        "such as torch, tensorflow, jax, opencv, or CUDA-related wheels. It checks "
        "the current interpreter, workspace .venv/venv folders, PATH interpreters, "
        "and conda/miniconda environments, and can test whether requested packages "
        "are already importable."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    should_defer = True
    search_hint = "python env conda miniconda venv virtualenv pip packages torch dependency install"
    workspace_path_fields = ("cwd",)

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "package_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional Python import/package names to check, e.g. ['torch', 'torchvision'].",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Workspace directory to scan for .venv, venv, env, and .conda folders.",
                    },
                },
            },
        )

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="environment.inspect",
            toolset="default",
            exposure="deferred",
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        packages = _normalize_packages(args.get("package_names"))
        workspace = _resolve_workspace(args.get("cwd"), context)
        deadline_timeout = _remaining_deadline_seconds(context)
        candidates = await asyncio.to_thread(_discover_python_candidates, workspace)
        # SECURITY: never execute an interpreter that lives inside the workspace.
        # A hostile repo can ship .venv/bin/python (or Scripts/python.exe) that is
        # actually a payload; probing runs it, and this tool is AUTO/read_only so
        # it would fire with no confirmation -> arbitrary code execution just by
        # pointing the agent at a repo. Trusted out-of-workspace interpreters
        # (sys.executable, PATH, conda in home/system) are still probed; workspace
        # interpreters are reported as detected-but-not-executed. Running one is a
        # deliberate act the model must do via run_command (sandboxed + gated).
        envs = await asyncio.gather(*(
            asyncio.to_thread(_probe_python, path, packages, deadline_timeout)
            if not _is_within_workspace(path, workspace)
            else asyncio.sleep(0, result=_detected_not_probed(path))
            for path in candidates
        ))
        conda = await asyncio.to_thread(_discover_conda, deadline_timeout)

        if context is not None and hasattr(context, "metadata"):
            context.metadata["python_environment_checked"] = True
            context.metadata["python_environment_packages"] = packages

        found_packages = {
            pkg: [
                env["path"]
                for env in envs
                if env.get("packages", {}).get(pkg, {}).get("available")
            ]
            for pkg in packages
        }
        payload = {
            "workspace": str(workspace) if workspace else "",
            "current_python": sys.executable,
            "python_candidates": envs,
            "conda": conda,
            "requested_packages": packages,
            "found_packages": found_packages,
            "guidance": _guidance(packages, found_packages, conda),
        }
        return ToolResult(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            evidence_type="environment",
            display_summary=_summary(packages, found_packages, envs, conda),
            result_kind="environment",
        )


def _normalize_packages(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    packages: list[str] = []
    for item in value:
        name = str(item or "").strip()
        if name and name.replace("_", "").replace("-", "").replace(".", "").isalnum():
            packages.append(name.replace("-", "_"))
    return packages


def _remaining_deadline_seconds(context: Any) -> float | None:
    """Return the enclosing turn budget without inventing a tool timeout."""
    deadline = getattr(context, "deadline_monotonic", None) if context is not None else None
    if deadline is None:
        return None
    try:
        return max(0.0, float(deadline) - asyncio.get_running_loop().time())
    except (TypeError, ValueError, RuntimeError):
        return None


def _resolve_workspace(value: Any, context: Any) -> Path | None:
    raw = str(value or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    workspace_root = getattr(context, "workspace_root", None) if context else None
    return Path(workspace_root).expanduser().resolve() if workspace_root else None


def _is_within_workspace(path: Path, workspace: Path | None) -> bool:
    """True when ``path`` resolves inside ``workspace`` (an untrusted tree).

    Both sides are already resolved (``_resolve_workspace`` resolves the root;
    ``_discover_python_candidates`` resolves each candidate), so this is a pure
    prefix check with no extra I/O. Symlinks were collapsed by resolve(), so a
    workspace symlink pointing outside is treated by its real location.
    """
    if workspace is None:
        return False
    try:
        path.relative_to(workspace)
        return True
    except ValueError:
        return False


def _detected_not_probed(path: Path) -> dict[str, Any]:
    """Report a workspace-local interpreter without executing it (see execute)."""
    return {
        "path": str(path),
        "ok": False,
        "error": (
            "Interpreter is inside the workspace and was not executed for safety. "
            "Use run_command to run it explicitly (sandboxed and permission-gated)."
        ),
        "in_workspace": True,
    }


def _discover_python_candidates(workspace: Path | None) -> list[Path]:
    seen: set[str] = set()
    candidates: list[Path] = []

    def add(path: str | Path | None) -> None:
        if not path:
            return
        candidate = Path(path).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError:
            return
        key = os.path.normcase(str(resolved))
        if key in seen or not resolved.exists():
            return
        seen.add(key)
        candidates.append(resolved)

    add(sys.executable)
    for name in ("python", "python3", "py"):
        add(shutil.which(name))

    if workspace:
        for env_name in (".venv", "venv", "env", ".conda"):
            env_dir = workspace / env_name
            if os.name == "nt":
                add(env_dir / "Scripts" / "python.exe")
            else:
                add(env_dir / "bin" / "python")

    for conda_env in _conda_env_paths():
        if os.name == "nt":
            add(Path(conda_env) / "python.exe")
        else:
            add(Path(conda_env) / "bin" / "python")

    return candidates


def _probe_python(path: Path, packages: list[str], timeout: float | None = None) -> dict[str, Any]:
    script = (
        "import importlib.util, json, platform, sys; "
        f"pkgs={packages!r}; "
        "print(json.dumps({"
        "'executable': sys.executable, "
        "'version': sys.version.split()[0], "
        "'implementation': platform.python_implementation(), "
        "'prefix': sys.prefix, "
        "'base_prefix': getattr(sys, 'base_prefix', ''), "
        "'packages': {p: {'available': importlib.util.find_spec(p) is not None} for p in pkgs}"
        "}))"
    )
    try:
        completed = subprocess.run(
            [str(path), "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        return {"path": str(path), "ok": False, "error": str(exc)}
    if completed.returncode != 0:
        return {
            "path": str(path),
            "ok": False,
            "error": (completed.stderr or completed.stdout).strip()[:500],
        }
    try:
        payload = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "path": str(path),
        "ok": True,
        "version": payload.get("version", ""),
        "implementation": payload.get("implementation", ""),
        "prefix": payload.get("prefix", ""),
        "base_prefix": payload.get("base_prefix", ""),
        "packages": payload.get("packages", {}),
    }


def _discover_conda(timeout: float | None = None) -> dict[str, Any]:
    executables = [p for p in (shutil.which("conda"), shutil.which("mamba"), shutil.which("micromamba")) if p]
    roots = _common_conda_roots()
    envs = _conda_env_paths(timeout)
    return {
        "executables": executables,
        "common_roots": [str(root) for root in roots if root.exists()],
        "envs": [str(env) for env in envs],
    }


def _common_conda_roots() -> list[Path]:
    home = Path.home()
    roots = [
        home / "miniconda3",
        home / "anaconda3",
        home / "mambaforge",
        home / "micromamba",
    ]
    if os.name == "nt":
        roots.extend([
            Path(os.environ.get("USERPROFILE", str(home))) / "Miniconda3",
            Path(os.environ.get("USERPROFILE", str(home))) / "Anaconda3",
            Path("C:/ProgramData/Miniconda3"),
            Path("C:/ProgramData/Anaconda3"),
        ])
    return roots


def _conda_env_paths(timeout: float | None = None) -> list[Path]:
    envs: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return
        key = os.path.normcase(str(resolved))
        if key not in seen and resolved.exists():
            seen.add(key)
            envs.append(resolved)

    conda = shutil.which("conda")
    if conda:
        try:
            completed = subprocess.run(
                [conda, "env", "list", "--json"],
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            if completed.returncode == 0:
                for raw in json.loads(completed.stdout or "{}").get("envs", []):
                    add(Path(str(raw)))
        except Exception:
            pass

    for root in _common_conda_roots():
        add(root)
        envs_dir = root / "envs"
        if envs_dir.exists():
            for child in envs_dir.iterdir():
                if child.is_dir():
                    add(child)
    return envs


def _summary(packages: list[str], found: dict[str, list[str]], envs: list[dict[str, Any]], conda: dict[str, Any]) -> str:
    if packages:
        hits = sum(1 for paths in found.values() if paths)
        return f"Checked {len(envs)} Python interpreters and {len(conda.get('envs', []))} conda envs; found {hits}/{len(packages)} requested packages."
    return f"Checked {len(envs)} Python interpreters and {len(conda.get('envs', []))} conda envs."


def _guidance(packages: list[str], found: dict[str, list[str]], conda: dict[str, Any]) -> str:
    if packages and all(found.get(pkg) for pkg in packages):
        return "Requested packages already exist in at least one interpreter. Prefer that interpreter/environment over installing again."
    if conda.get("envs"):
        return "Conda environments exist. Prefer selecting an existing env or asking before installing new large packages."
    return "No matching packages were found. Ask before installing large dependencies or creating a new environment."
