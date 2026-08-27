"""Executable-extension capability discovery outside the harness kernel."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from backend.config import load_config_layer_stack
from backend.llm.model_registry import ModelRegistry, ModelRuntime
from backend.services.plugin_settings_service import (
    get_plugin_snapshot,
    load_enabled_plugin_extension_sources,
)

from .loader import ExtensionLoader, resolve_extension_entries
from .types import LoadExtensionsResult


@dataclass(frozen=True, slots=True)
class LoadedLifecycleCapability:
    """One unpublished extension candidate and its private resources."""

    runtime: Any | None
    result: LoadExtensionsResult
    loader: ExtensionLoader
    model_runtime: ModelRuntime
    model_registry: ModelRegistry
    fingerprint: str
    workspace_key: str
    project_trusted: bool

    def discard(self, reason: str) -> None:
        if self.runtime is not None:
            self.runtime.invalidate(reason)
        self.model_runtime.retire()


class ExtensionCapabilitySource:
    """Load extension capabilities without owning session lifecycle.

    This source understands MiniCode plugin declarations and extension loading.
    Publication, rollback, retirement, and shutdown remain owned by MiniCode's
    conversation-scoped generation state.
    """

    def __init__(
        self,
        *,
        session_owner: str,
        owner_id: str,
        workspace_root: Path | None,
        project_trusted: bool,
        on_model_change: Callable[[Any, str, str], None],
    ) -> None:
        self.session_owner = str(session_owner or "")
        self.owner_id = str(owner_id or "")
        self.workspace_root = workspace_root
        self.project_trusted = bool(project_trusted)
        self.on_model_change = on_model_change
        self._config_stack: Any | None = None
        self._plugin_snapshot: dict[str, Any] | None = None

    @staticmethod
    def _workspace_key(workspace_root: Path | None) -> str:
        if workspace_root is None:
            return ""
        try:
            return str(Path(workspace_root).expanduser().resolve())
        except OSError:
            return ""

    def fingerprint(self) -> str:
        cwd = self.workspace_root or Path.cwd()
        config_stack = load_config_layer_stack(cwd=cwd)
        snapshot = get_plugin_snapshot(config_stack=config_stack)
        self._config_stack = config_stack
        self._plugin_snapshot = dict(snapshot)
        return "|".join(
            (
                self._workspace_key(self.workspace_root),
                "trusted" if self.project_trusted else "untrusted",
                str(snapshot.get("fingerprint") or ""),
            )
        )
    async def load(self, *, clear_cache: bool = False) -> LoadedLifecycleCapability:
        loader_cwd = self.workspace_root or Path.cwd()
        workspace_key = self._workspace_key(self.workspace_root)
        config_stack = self._config_stack
        plugin_snapshot = self._plugin_snapshot
        if config_stack is None or plugin_snapshot is None:
            config_stack = load_config_layer_stack(cwd=loader_cwd)
            plugin_snapshot = dict(get_plugin_snapshot(config_stack=config_stack))
            self._config_stack = config_stack
            self._plugin_snapshot = plugin_snapshot
        plugin_fingerprint = str(plugin_snapshot.get("fingerprint") or "")
        fingerprint = "|".join(
            (
                workspace_key,
                "trusted" if self.project_trusted else "untrusted",
                plugin_fingerprint,
            )
        )

        user_roots: list[Path] = []
        managed_roots: list[Path] = []
        plugin_paths: list[Path] = []
        source_scopes: dict[str | Path, Any] = {}
        source_metadata: dict[str | Path, dict[str, Any]] = {}
        source_errors: list[dict[str, str]] = []
        for source in load_enabled_plugin_extension_sources(config_stack=config_stack):
            raw_root = str(source.get("plugin_root") or "").strip()
            if raw_root:
                roots = managed_roots if bool(source.get("managed")) else user_roots
                root = Path(raw_root).expanduser().absolute()
                root_key = os.path.normcase(str(root))
                if all(os.path.normcase(str(existing)) != root_key for existing in roots):
                    roots.append(root)
            error = str(source.get("error") or "").strip()
            raw_path = str(source.get("path") or "").strip()
            if error:
                source_errors.append(
                    {
                        "path": raw_path or str(source.get("source_path") or "<plugin>"),
                        "error": error,
                    }
                )
                continue
            if not raw_path:
                continue
            entries = resolve_extension_entries(Path(raw_path))
            if not entries:
                source_errors.append(
                    {
                        "path": raw_path,
                        "error": "declared extension directory has no loadable entry",
                    }
                )
                continue
            for entry in entries:
                plugin_paths.append(entry)
                scope = str(source.get("scope") or "user")
                metadata = {
                    "origin": "plugin",
                    "plugin_id": str(source.get("plugin_id") or ""),
                    "marketplace": str(source.get("marketplace") or ""),
                    "plugin_root": raw_root,
                    "manifest_path": str(source.get("source_path") or ""),
                    "declaration": str(source.get("declaration") or ""),
                    "plugin_fingerprint": str(
                        source.get("plugin_fingerprint") or plugin_fingerprint
                    ),
                }
                source_scopes[str(entry)] = scope
                source_scopes[entry] = scope
                source_metadata[str(entry)] = metadata
                source_metadata[entry] = metadata

        loader = ExtensionLoader(
            cwd=loader_cwd,
            cache_namespace=(
                f"conversation:{self.session_owner}:{self.owner_id}"
            ),
            additional_user_roots=user_roots,
            additional_managed_roots=managed_roots,
        )
        if clear_cache:
            loader.clear_cache()
        standard_paths = loader.discover(
            project_root=self.workspace_root or loader_cwd,
        )
        paths: list[Path] = []
        seen_paths: set[str] = set()
        for path in [*standard_paths, *plugin_paths]:
            try:
                key = os.path.normcase(str(path.resolve(strict=False)))
            except OSError:
                key = os.path.normcase(str(path.absolute()))
            if key in seen_paths:
                continue
            seen_paths.add(key)
            paths.append(path)

        model_runtime = ModelRuntime(on_change=self.on_model_change)
        model_registry = ModelRegistry(model_runtime)
        try:
            result = await loader.load(
                paths,
                source_scopes=source_scopes,
                source_metadata=source_metadata,
                project_trusted=self.project_trusted,
                mode="rpc",
                bind_provider_sink=model_registry,
            )
        except BaseException:
            model_runtime.retire()
            raise
        result.errors.extend(source_errors)
        return LoadedLifecycleCapability(
            runtime=result.runner,
            result=result,
            loader=loader,
            model_runtime=model_runtime,
            model_registry=model_registry,
            fingerprint=fingerprint,
            workspace_key=workspace_key,
            project_trusted=self.project_trusted,
        )


__all__ = ["ExtensionCapabilitySource", "LoadedLifecycleCapability"]
