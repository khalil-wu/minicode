"""Model context prepared through the captured Source execution attachments."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from xml.sax.saxutils import escape, quoteattr

from backend.agent.environment_selection import FailedTurnEnvironment, StartingTurnEnvironment, TurnEnvironment
from backend.environments.errors import ExecutorError, ExecutorRpcError
from backend.permissions.environment_scope import executor_permission_profile
from backend.workspace.executor_paths import ExecutorPath
from backend.workspace.executor_service import not_found


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnvironmentProjectInstructions:
    text: str
    directory: str | None = None

ENVIRONMENTS_INSTRUCTIONS = """<environments_instructions>
## Execution environments
Execution environments are separate machines or workspaces with their own files, shell, and installed capabilities. `<environment_context>` lists the environments selected for this task.

An environment marked `starting` is not yet usable. Its files, commands, AGENTS.md instructions, skills, plugins, and MCP tools may become available when startup completes.

Wait only when the current task needs that environment. Continue using tools that are already available for unrelated work.
</environments_instructions>"""


def _filesystem(environment) -> str:
    scope = environment.permission_scope
    profile = executor_permission_profile(scope.permission_profile)
    roots = "".join(f"<root>{escape(ExecutorPath.parse(root).native)}</root>" for root in scope.workspace_root_uris)
    filesystem = profile.get("file_system")
    kind = profile["type"]
    if filesystem is None:
        body = '<file_system type="unrestricted" />' if kind == "disabled" else '<file_system type="external" />'
    elif filesystem["type"] == "unrestricted":
        body = '<file_system type="unrestricted" />'
    else:
        entries = []
        special_names = {"root": ":root", "minimal": ":minimal", "project_roots": ":workspace_roots",
            "tmpdir": ":tmpdir", "slash_tmp": ":slash_tmp"}
        for entry in filesystem["entries"]:
            selector = entry["path"]
            access = entry["access"]
            deny = ' escalatable="false"' if access == "deny" else ""
            if selector["type"] == "special" and selector["value"]["kind"] == "project_roots":
                for root_uri in scope.workspace_root_uris:
                    root = ExecutorPath.parse(root_uri)
                    subpath = selector["value"].get("subpath")
                    if subpath is not None:
                        root = root.joinpath(subpath)
                    entries.append(f'<entry access={quoteattr(access)}{deny}><path>{escape(root.native)}</path></entry>')
                continue
            if selector["type"] == "path":
                path = f"<path>{escape(ExecutorPath.parse(selector['path']).native)}</path>"
            elif selector["type"] == "glob_pattern":
                path = f"<glob>{escape(selector['pattern'])}</glob>"
            else:
                special = selector["value"]
                value = special["path"] if special["kind"] == "unknown" else special_names[special["kind"]]
                if special.get("subpath"):
                    value += "/" + special["subpath"]
                path = f"<special>{escape(value)}</special>"
            entries.append(f'<entry access={quoteattr(access)}{deny}>{path}</entry>')
        depth = filesystem.get("glob_scan_max_depth")
        scan = f' glob_scan_max_depth="{depth}"' if depth is not None else ""
        body = (f'<file_system type="restricted"{scan}>' + "".join(dict.fromkeys(entries)) + "</file_system>"
            if entries or depth is not None else '<file_system type="restricted" />')
    return ("  <filesystem>" + (f"<workspace_roots>{roots}</workspace_roots>" if roots else "")
        + f'<permission_profile type={quoteattr(kind)}>{body}</permission_profile></filesystem>')


def render_environment_context(snapshot, *, current_date: str, timezone: str) -> str:
    primary = snapshot.primary()
    entries = sorted(snapshot.environments, key=lambda item: item.selection.environment_id)
    lines = ["<environment_context>"]
    legacy = len(entries) == 1 and isinstance(entries[0], TurnEnvironment)
    if not legacy and entries:
        lines.append("  <environments>")
    error_budget = 512
    for item in entries:
        indent = "  " if legacy else "      "
        if not legacy:
            selected = f' primary="{str(item is primary).lower()}"' if len(entries) > 1 else ""
            lines.append(f'    <environment id={quoteattr(item.selection.environment_id)}{selected}>')
        lines.append(f"{indent}<cwd>{escape(ExecutorPath.parse(item.selection.cwd).native)}</cwd>")
        if isinstance(item, StartingTurnEnvironment):
            lines.append(f"{indent}<status>starting</status>")
        elif isinstance(item, FailedTurnEnvironment):
            lines.append(f"{indent}<status>failed</status>")
            detail = item.error.encode("utf-8")[:min(256, error_budget)].decode("utf-8", errors="ignore")
            error_budget -= len(detail.encode("utf-8"))
            lines.append(f"{indent}<error>{escape(detail)}</error>")
        elif isinstance(item, TurnEnvironment):
            shell = item.shell
            if shell is not None:
                lines.append(f"{indent}<shell>{escape(shell['name'])}</shell>")
            if item.peer_info.get("platformOs"):
                lines.append(f"{indent}<platform>{escape(item.peer_info['platformOs'])}</platform>")
        if not legacy:
            lines.append("    </environment>")
    if not legacy and entries:
        lines.append("  </environments>")
    lines.extend((f"  <current_date>{escape(current_date)}</current_date>", f"  <timezone>{escape(timezone)}</timezone>"))
    if primary is not None:
        lines.append(_filesystem(primary))
    lines.append("</environment_context>")
    return "\n".join(lines)


async def load_environment_project_instructions(snapshot, *, project_root_markers=None,
        fallback_filenames=(), max_bytes=32 * 1024) -> EnvironmentProjectInstructions:
    """agents_md.rs: ready environments, override-first discovery, shared budget."""
    remaining = max_bytes
    documents = []
    markers = (".git",) if project_root_markers is None else project_root_markers
    for environment in snapshot.turn_environments():
        if remaining == 0:
            break
        environment_remaining = remaining
        scope = environment.permission_scope
        filesystem = environment.environment.filesystem
        sandbox = None if scope.has_full_disk_read_access() else scope.sandbox_context()
        cwd = ExecutorPath.parse(environment.selection.cwd, platform_os=scope.platform_os)
        names = ["AGENTS.override.md", "AGENTS.md"]
        for name in fallback_filenames:
            if (not name or name in {".", ".."} or any(char in name for char in ("/", "\0"))
                    or cwd.convention == "windows" and any(char in name for char in ("\\", ":"))):
                continue
            if name not in names:
                names.append(name)
        try:
            directories, cursor, root = [cwd], cwd, None
            while markers:
                for marker in markers:
                    try:
                        await filesystem.get_metadata(cursor.joinpath(marker).uri, sandbox=sandbox)
                        root = cursor
                        break
                    except (OSError, ExecutorRpcError):
                        continue  # Source FindUpErrorPolicy::Ignore only governs root-marker probes.
                if root is not None or cursor.parent == cursor:
                    break
                cursor = cursor.parent
                directories.append(cursor)
            directories = list(reversed(directories)) if root is not None else [cwd]
            paths = []
            for directory in directories:
                for name in names:
                    candidate = directory.joinpath(name)
                    try:
                        metadata = await filesystem.get_metadata(candidate.uri, sandbox=sandbox)
                    except (OSError, ExecutorRpcError) as error:
                        if not_found(error):
                            continue
                        raise
                    if metadata["isFile"]:
                        paths.append(candidate)
                        break
            entries = []
            for path in paths:
                if environment_remaining == 0:
                    break
                try:
                    data = await filesystem.read_file(path.uri, sandbox=sandbox)
                except (OSError, ExecutorRpcError) as error:
                    if not_found(error):
                        continue
                    raise
                data = data[:environment_remaining]
                text = data.decode("utf-8", errors="replace")
                if text.strip():
                    entries.append(text)
                    environment_remaining -= len(data)
            if entries:
                documents.append((environment.selection.environment_id, cwd.native, "\n\n".join(entries)))
                remaining = max(0, remaining - sum(len(entry.encode("utf-8")) for entry in entries))
        except (OSError, ExecutorRpcError) as error:
            if sandbox is not None:
                raise
            logger.error("error trying to find AGENTS.md docs in environment `%s`: %s", environment.selection.environment_id, error)
    if len(documents) < 2:
        return EnvironmentProjectInstructions(documents[0][2], documents[0][1]) if documents else EnvironmentProjectInstructions("")
    return EnvironmentProjectInstructions("\n\n".join(f"for `{identity}` with root {cwd}\n\n{text}" for identity, cwd, text in documents))


async def build_environment_git_status(context) -> str:
    from backend.agent.prompting import _format_git_status_context
    from backend.workspace.executor_git import run_executor_git_metadata
    from backend.workspace.executor_service import ExecutorWorkspaceService

    async with ExecutorWorkspaceService(context) as service:
        async def git(*args):
            from backend.async_cleanup import retain_cleanup_task

            operation = asyncio.create_task(run_executor_git_metadata(service, args))
            retain_cleanup_task(operation, context.pending_cleanup_tasks)
            if context.run_context.retain_model is not None:
                context.run_context.retain_model(context.llm, operation)
            result = await operation
            return result.stdout.decode("utf-8", errors="replace").strip() if result.returncode == 0 else None

        try:
            if await git("rev-parse", "--is-inside-work-tree") != "true":
                return ""
            branch, head, user, status, log = await asyncio.gather(git("branch", "--show-current"),
                git("symbolic-ref", "refs/remotes/origin/HEAD"), git("config", "user.name"),
                git("status", "--short"), git("log", "--oneline", "-n", "5"))
        except (OSError, TimeoutError, ExecutorError) as error:
            logger.warning("Git prompt snapshot unavailable in environment `%s`: %s", context.environment_id, error)
            return f"The Git status snapshot is unavailable for execution environment `{context.environment_id}`: {error}"
        if status is None:
            return ""
        return _format_git_status_context(branch=branch or "", main_branch=head.rsplit("/", 1)[-1] if head else "main",
            user_name=user or "", status=status, log=log or "")
