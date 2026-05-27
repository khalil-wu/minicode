"""Session utility mixin for WebSocketSession."""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, TYPE_CHECKING

logger = logging.getLogger(__name__)

from backend.agent.message import AgentEvent
from backend.config import get_available_models, get_llm_provider

if TYPE_CHECKING:
    pass


class SessionCommandHandlersMixin:
    """Shared session utilities used by flat websocket handlers."""

    def _register_command_handlers(self) -> None:
        from backend.commands.slash_commands import register_all_slash_commands
        from backend.ws.handlers import register_domain_handlers

        register_all_slash_commands(self.command_registry)
        register_domain_handlers(self)

    # ── Skill toggle ─────────────────────────────────────

    async def _toggle_skill(self, skill_name: str, *, activate: bool) -> None:
        if not self.skill_manager or not skill_name:
            await self._send_event(AgentEvent.error("Skills are unavailable", recoverable=True))
            return
        success = self.skill_manager.activate(skill_name) if activate else self.skill_manager.deactivate(skill_name)
        if success:
            await self._send_event(
                AgentEvent(
                    type="skill_activated" if activate else "skill_deactivated",
                    data={"skill_name": skill_name},
                )
            )
        else:
            await self._send_event(
                AgentEvent.error(
                    f"Skill '{skill_name}' {'activate' if activate else 'deactivate'} failed",
                    recoverable=True,
                )
            )

    # ── LLM model selection ──────────────────────────────

    async def _set_selected_model(self, model: str, *, manual_override: bool) -> None:
        normalized = model.strip()
        if not normalized:
            return
        self.provider = get_llm_provider()
        refreshed_models = get_available_models(self.provider)
        self.available_models = list(refreshed_models)
        if normalized not in self.available_models:
            self.available_models.insert(0, normalized)
        self.selected_model = normalized
        self._model_override_active = manual_override

        from backend.llm.model_registry import create_session_llm

        self.llm = create_session_llm(self.config, model_override=self.selected_model)
        self.context_builder._llm = self.llm

    async def _send_llm_state(self) -> None:
        await self._send_ws_payload(
            {
                "type": "llm.model.updated",
                "provider": self.provider,
                "model": self.selected_model,
                "current_model": self.selected_model,
                "available_models": self.available_models,
                "working_directory": str(self._current_workspace_root()),
            },
            log_context="llm.model.updated",
        )

    # ── Workspace utilities ──────────────────────────────

    async def _create_isolated_conversation_worktree(self, conversation: Any) -> Any | None:
        from backend.workspace.worktree import WorktreeManager

        base_root = self._main_worktree_root(Path(conversation.workspace_root or self._current_workspace_root()))
        try:
            manager = WorktreeManager(base_root)
        except Exception as exc:
            await self._send_event(
                AgentEvent.error(f"Git isolation unavailable for this workspace: {exc}", recoverable=True)
            )
            return conversation

        worktree_root = base_root / ".claude" / "worktrees"
        worktree_path = worktree_root / conversation.id
        branch = f"minicode/{conversation.id}"
        try:
            worktree_root.mkdir(parents=True, exist_ok=True)
            created = manager.create_worktree(worktree_path, branch=branch, new_branch=True)
        except Exception as exc:
            await self._send_event(
                AgentEvent.error(f"Failed to create isolated Git worktree: {exc}", recoverable=True)
            )
            return conversation

        if not created:
            await self._send_event(
                AgentEvent.error("Failed to create isolated Git worktree", recoverable=True)
            )
            return conversation

        updated = self.conversation_repo.update_workspace_binding(
            conversation.id,
            workspace_root=str(worktree_path),
            git_branch=branch,
            worktree_path=str(worktree_path),
            git_isolated=True,
        )
        await self._send_event(
            AgentEvent(
                type="system_notice",
                data={
                    "content": (
                        "Created an isolated workspace for this session. "
                        "Edits in this session are separated from your main checkout until you review or merge them."
                    )
                },
            )
        )
        return updated or conversation

    async def _switch_workspace_for_conversation(self, conversation: Any, *, announce: bool) -> None:
        workspace_path = str(
            getattr(conversation, "worktree_path", "")
            or getattr(conversation, "workspace_root", "")
            or ""
        ).strip()
        if not workspace_path:
            return

        if self._workspace_context:
            current_root = str(self._workspace_context.root_path).strip()
            from backend.workspace.path_utils import normalize_project_import_path
            try:
                target_root = str(normalize_project_import_path(workspace_path)).strip()
                if current_root.lower() == target_root.lower() or os.path.normpath(current_root) == os.path.normpath(target_root):
                    return
            except Exception:
                pass

        await self._activate_workspace_path(workspace_path, announce=announce)

    async def _activate_workspace_path(self, path_str: str, *, announce: bool = False) -> bool:
        from backend.workspace.context import WorkspaceContext
        from backend.workspace.path_utils import normalize_project_import_path
        from backend.workspace.recent_projects import RecentProjectStore
        from backend.workspace.state import set_active_workspace_root

        project_path = normalize_project_import_path(path_str)
        if not project_path.exists() or not project_path.is_dir():
            await self._send_event(
                AgentEvent.error(f"Session workspace does not exist: {path_str}", recoverable=True)
            )
            return False

        try:
            ctx = WorkspaceContext(project_path)
            metadata = await ctx.initialize()
            self._workspace_context = ctx
            set_active_workspace_root(project_path)
            restart_file_watcher = getattr(self, "_restart_file_watcher", None)
            if callable(restart_file_watcher):
                restart_file_watcher(project_path)
            RecentProjectStore().add(
                path=str(project_path),
                name=metadata.name,
                project_type=metadata.project_type,
            )
            if announce:
                await self._send_ws_payload(
                    {
                        "type": "workspace.imported",
                        "project": ctx.to_dict(),
                        "summary": ctx.get_project_summary()[:3000],
                        "file_count": metadata.file_count,
                    },
                    log_context="workspace.imported",
                )
            return True
        except Exception as exc:
            await self._send_event(
                AgentEvent.error(f"Failed to switch session workspace: {exc}", recoverable=True)
            )
            return False

    def _git_branch_for(self, path: Path) -> str:
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=path,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=5,
                check=True,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _main_worktree_root(self, path: Path) -> Path:
        root = path.resolve()
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=5,
                check=True,
            )
        except Exception:
            return root

        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                candidate = Path(line[9:].strip()).resolve()
                if (candidate / ".git").is_dir():
                    return candidate
                return candidate
        return root

    def _is_path_within(self, path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
            return True
        except ValueError:
            return False

    def _resolve_workspace_cwd(self, cwd: str | None = None) -> Path:
        workspace_root = self._current_workspace_root().resolve()
        candidate = Path(cwd).expanduser().resolve() if cwd else workspace_root
        if not self._is_path_within(candidate, workspace_root):
            raise ValueError(f"CWD must stay inside workspace: {workspace_root}")
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError(f"CWD does not exist or is not a directory: {candidate}")
        return candidate

    def _resolve_requested_workspace(self, requested_workspace: str | None = None) -> Path:
        workspace_root = self._current_workspace_root().resolve()
        if not requested_workspace:
            return workspace_root
        requested = Path(requested_workspace).expanduser().resolve()
        if not self._is_path_within(requested, workspace_root):
            raise ValueError(f"Workspace must stay inside current session workspace: {workspace_root}")
        if not requested.exists() or not requested.is_dir():
            raise ValueError(f"Workspace does not exist or is not a directory: {requested}")
        return requested

    def _validate_git_relative_path(self, path: str) -> str:
        value = str(path or "").replace("\\", "/").strip()
        candidate = Path(value)
        if not value or candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("Git path must be a relative path inside the workspace")
        return value

    def _worktree_has_local_changes(self, path: Path) -> bool:
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=path,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=5,
                check=True,
            )
            return bool(result.stdout.strip())
        except Exception:
            return True
