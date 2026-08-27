"""Safe Python module/factory loader for MiniCode executable extensions."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import logging
import os
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from backend.agent.markdown_scopes import get_minicode_config_home_dir
from backend.managed_settings import default_minicode_managed_dir

from .runtime import (
    ExtensionAPI,
    ExtensionEventBus,
    ExtensionRunner,
    ExtensionRuntime,
    _maybe_await,
)
from .trust import ExtensionTrustPolicy
from .types import (
    Extension,
    ExtensionFactory,
    ExtensionScope,
    ExtensionSource,
    LoadExtensionsResult,
)

logger = logging.getLogger(__name__)


_FACTORY_NAMES = (
    "extension",
    "register",
    "create_extension",
    "EXTENSION_FACTORY",
)


class ExtensionModuleCache:
    """Process-local factory cache partitioned by runtime owner and cwd.

    MiniCode hosts multiple websocket runtimes in one process, so executable
    module caches are partitioned by runtime owner and cwd. Clearing one
    partition cannot invalidate another workspace's modules or share mutable
    module globals across conversations.
    """

    def __init__(self) -> None:
        # ``cwd`` records the last touched partition and ``generation`` counts
        # explicit invalidations for diagnostics.
        self.cwd: str | None = None
        self.generation = 0
        self._generation_serial = 0
        self._partitions: dict[
            tuple[str, str],
            dict[str, Any],
        ] = {}

    @staticmethod
    def _resolved_cwd(cwd: Path) -> str:
        return os.path.normcase(str(cwd.expanduser().resolve()))

    @classmethod
    def _partition_key(
        cls,
        cwd: Path,
        cache_namespace: str | None,
    ) -> tuple[str, str]:
        resolved = cls._resolved_cwd(cwd)
        namespace = str(cache_namespace or resolved)
        return namespace, resolved

    def _new_generation(self) -> int:
        self._generation_serial += 1
        return self._generation_serial

    def _partition(
        self,
        cwd: Path,
        cache_namespace: str | None,
    ) -> dict[str, Any]:
        key = self._partition_key(cwd, cache_namespace)
        partition = self._partitions.get(key)
        if partition is None:
            partition = {
                "generation": self._new_generation(),
                "factories": {},
                "module_names": set(),
            }
            self._partitions[key] = partition
        self.cwd = key[1]
        return partition

    def token(
        self,
        cwd: Path,
        cache_namespace: str | None = None,
    ) -> tuple[str, int]:
        partition = self._partition(cwd, cache_namespace)
        return self._resolved_cwd(cwd), int(partition["generation"])

    def generation_for(
        self,
        cwd: Path,
        cache_namespace: str | None = None,
    ) -> int:
        return int(self._partition(cwd, cache_namespace)["generation"])

    def get(
        self,
        path: Path,
        cwd: Path,
        cache_namespace: str | None = None,
    ) -> Callable[..., Any] | None:
        partition = self._partition(cwd, cache_namespace)
        factories = partition["factories"]
        return factories.get(os.path.normcase(str(path.resolve())))

    def put(
        self,
        path: Path,
        cwd: Path,
        factory: Callable[..., Any],
        module_name: str,
        *,
        cache_namespace: str | None = None,
        expected_generation: int | None = None,
    ) -> None:
        partition = self._partition(cwd, cache_namespace)
        if (
            expected_generation is not None
            and int(partition["generation"]) != int(expected_generation)
        ):
            # MiniCode rejects a load completion whose cache generation changed
            # while work was in progress.  Never repopulate a freshly-cleared
            # partition with a stale factory.
            self._purge_module_names({module_name})
            return
        factories = partition["factories"]
        module_names = partition["module_names"]
        factories[os.path.normcase(str(path.resolve()))] = factory
        module_names.add(module_name)

    @staticmethod
    def _purge_module_names(module_names: set[str]) -> None:
        for name in tuple(module_names):
            sys.modules.pop(name, None)
            for loaded_name in tuple(sys.modules):
                if loaded_name.startswith(f"{name}."):
                    sys.modules.pop(loaded_name, None)

    def clear(
        self,
        *,
        cwd: Path | None = None,
        cache_namespace: str | None = None,
    ) -> None:
        if cwd is None and cache_namespace is None:
            partitions = tuple(self._partitions.values())
            self._partitions.clear()
        else:
            keys: list[tuple[str, str]] = []
            if cwd is not None:
                keys.append(self._partition_key(cwd, cache_namespace))
            else:
                namespace = str(cache_namespace or "")
                keys.extend(
                    key for key in self._partitions if key[0] == namespace
                )
            partitions = tuple(
                partition
                for key in keys
                if (partition := self._partitions.pop(key, None)) is not None
            )
        for partition in partitions:
            self._purge_module_names(set(partition["module_names"]))
        self.cwd = None
        self.generation += 1


_GLOBAL_CACHE = ExtensionModuleCache()


def clear_extension_cache(
    cwd: Path | str | None = None,
    *,
    cache_namespace: str | None = None,
) -> int:
    """Clear one owner/cwd partition, or every partition when omitted."""

    _GLOBAL_CACHE.clear(
        cwd=Path(cwd).expanduser() if cwd is not None else None,
        cache_namespace=cache_namespace,
    )
    return _GLOBAL_CACHE.generation


def extension_cache_generation(
    cwd: Path | str | None = None,
    *,
    cache_namespace: str | None = None,
) -> int:
    if cwd is None:
        return _GLOBAL_CACHE.generation
    return _GLOBAL_CACHE.generation_for(
        Path(cwd).expanduser(),
        cache_namespace,
    )


def _resolve_factory(module: ModuleType) -> Callable[..., Any] | None:
    for name in _FACTORY_NAMES:
        candidate = getattr(module, name, None)
        if callable(candidate) or (
            hasattr(candidate, "setup") and callable(getattr(candidate, "setup", None))
        ):
            return candidate
    # A module with a single public callable is convenient for tiny extensions,
    # but do not guess from imported callables (which could register the wrong
    # function).  Only inspect names declared by the module itself.
    declared = getattr(module, "__dict__", {})
    candidates = [
        value
        for name, value in declared.items()
        if not name.startswith("_")
        and callable(value)
        and getattr(value, "__module__", None) == module.__name__
    ]
    return candidates[0] if len(candidates) == 1 else None


def _manifest_entries(directory: Path) -> list[Path]:
    """Resolve MiniCode extension manifests and Python package entrypoints."""

    entries: list[Path] = []
    for manifest_name in ("minicode-extension.json",):
        manifest = directory / manifest_name
        if not manifest.is_file():
            continue
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            configured = (
                payload.get("extensions") if isinstance(payload, Mapping) else None
            )
            if isinstance(configured, list):
                for raw in configured:
                    if not isinstance(raw, str) or not raw.strip():
                        continue
                    candidate = (directory / raw).absolute()
                    resolved_candidate = candidate.resolve(strict=False)
                    if (
                        resolved_candidate.is_file()
                        and resolved_candidate.suffix in {".py", ".pyw"}
                    ):
                        try:
                            resolved_candidate.relative_to(directory.resolve())
                        except ValueError:
                            continue
                        entries.append(candidate)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue

    pyproject = directory / "pyproject.toml"
    if pyproject.is_file():
        try:
            payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            tool = payload.get("tool", {})
            config = tool.get("minicode", {}) if isinstance(tool, Mapping) else {}
            configured = (
                config.get("extensions") if isinstance(config, Mapping) else None
            )
            if isinstance(configured, list):
                for raw in configured:
                    if not isinstance(raw, str) or not raw.strip():
                        continue
                    candidate = (directory / raw).absolute()
                    resolved_candidate = candidate.resolve(strict=False)
                    if (
                        resolved_candidate.is_file()
                        and resolved_candidate.suffix in {".py", ".pyw"}
                    ):
                        try:
                            resolved_candidate.relative_to(directory.resolve())
                        except ValueError:
                            continue
                        entries.append(candidate)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            pass

    index = directory / "__init__.py"
    if index.is_file():
        entries.append(index.absolute())
    return _dedupe_paths(entries)


def discover_extensions_in_dir(directory: Path | str) -> list[Path]:
    """Discover direct Python files and one-level package entrypoints.

    This explicit discovery rule avoids recursively importing
    every Python file in a package (which would make accidental activation and
    trust review impossible).
    """

    directory = Path(directory).expanduser()
    if not directory.is_dir():
        return []
    discovered: list[Path] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_file() and entry.suffix in {".py", ".pyw"}:
            discovered.append(entry.absolute())
            continue
        if entry.is_dir():
            manifest = _manifest_entries(entry)
            if manifest:
                discovered.extend(manifest)
            elif (entry / "__init__.py").is_file():
                discovered.append((entry / "__init__.py").absolute())
    return _dedupe_paths(discovered)


def resolve_extension_entries(path: Path | str) -> list[Path]:
    """Resolve one explicitly declared file/package/directory resource.

    MiniCode manifests may point at either a module entry or a directory. Keep
    this resolution primitive public so plugin resource loading and the
    standard-path loader apply the exact same non-recursive discovery rule.
    """

    candidate = Path(path).expanduser().absolute()
    if candidate.is_dir():
        entries = _manifest_entries(candidate)
        return entries or discover_extensions_in_dir(candidate)
    return [candidate]


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        try:
            original = path.expanduser().absolute()
            resolved = original.resolve(strict=False)
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        result.append(original)
    return result


class ExtensionLoader:
    """Load trusted Python extension modules and invoke their factories."""

    def __init__(
        self,
        *,
        cwd: Path | str | None = None,
        trust_policy: ExtensionTrustPolicy | None = None,
        use_cache: bool = True,
        cache_namespace: str | None = None,
        additional_user_roots: Sequence[Path | str] = (),
        additional_managed_roots: Sequence[Path | str] = (),
        event_bus: ExtensionEventBus | None = None,
        runtime_actions: Mapping[str, Callable[..., Any]] | None = None,
        context_actions: Mapping[str, Callable[..., Any]] | None = None,
    ) -> None:
        self.cwd = Path(cwd or Path.cwd()).expanduser().resolve()
        self.cache_namespace = str(cache_namespace or self.cwd)
        if trust_policy is None:
            user_root = get_minicode_config_home_dir()
            self.trust_policy = ExtensionTrustPolicy(
                cwd=self.cwd,
                project_root=self.cwd,
                user_roots=(
                    user_root / "extensions",
                    *additional_user_roots,
                ),
                managed_roots=(
                    default_minicode_managed_dir() / "extensions",
                    *additional_managed_roots,
                ),
            )
        else:
            if additional_user_roots or additional_managed_roots:
                raise ValueError(
                    "additional extension roots cannot be combined with an explicit trust policy"
                )
            self.trust_policy = trust_policy
        self.use_cache = bool(use_cache)
        self.event_bus = event_bus or ExtensionEventBus()
        self.runtime_actions = dict(runtime_actions or {})
        self.context_actions = dict(context_actions or {})
        self._last_result: LoadExtensionsResult | None = None

    @property
    def generation(self) -> int:
        return _GLOBAL_CACHE.generation_for(self.cwd, self.cache_namespace)

    def clear_cache(self) -> int:
        clear_extension_cache(
            self.cwd,
            cache_namespace=self.cache_namespace,
        )
        return self.generation

    def discover(
        self,
        configured_paths: Sequence[str | Path] = (),
        *,
        project_root: Path | str | None = None,
        user_config_dir: Path | str | None = None,
    ) -> list[Path]:
        project = Path(project_root or self.cwd).expanduser().resolve()
        if user_config_dir is None:
            user_config = get_minicode_config_home_dir()
        else:
            user_config = Path(user_config_dir).expanduser().resolve()
        candidates: list[Path] = []

        # Project discovery remains separate so trust is checked before import.
        candidates.extend(
            discover_extensions_in_dir(project / ".minicode" / "extensions")
        )
        candidates.extend(discover_extensions_in_dir(user_config / "extensions"))

        for raw in configured_paths:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = self.cwd / path
            # Keep the lexical path until the trust policy inspects it.  An
            # early resolve erased symlink components and let an explicitly
            # configured link evade the pre-import trust check.
            path = path.absolute()
            candidates.extend(resolve_extension_entries(path))
        return _dedupe_paths(candidates)

    async def load(
        self,
        paths: Sequence[str | Path] = (),
        *,
        runtime: ExtensionRuntime | None = None,
        event_bus: ExtensionEventBus | None = None,
        source_scopes: Mapping[str | Path, ExtensionScope] | None = None,
        source_metadata: Mapping[str | Path, Mapping[str, Any]] | None = None,
        project_trusted: bool | None = None,
        mode: str = "print",
        ui_context: Any = None,
        bind_tool_registry: Any | None = None,
        bind_command_registry: Any | None = None,
        bind_provider_sink: Any | None = None,
    ) -> LoadExtensionsResult:
        source_scopes = source_scopes or {}
        source_metadata = source_metadata or {}
        # Establish cwd/generation before selecting a reusable runtime.
        _GLOBAL_CACHE.token(self.cwd, self.cache_namespace)
        if project_trusted is not None:
            self.trust_policy.project_trusted = bool(project_trusted)
        resolved_event_bus = event_bus or self.event_bus
        if runtime is not None and runtime.generation != self.generation:
            # A runtime from a previous cache generation is stale by
            # construction.  Do not let a caller accidentally rebind old
            # registrations into a newly loaded module set.
            runtime.invalidate(
                "Extension runtime generation is stale after cache clear/reload."
            )
            resolved_runtime = ExtensionRuntime(
                generation=self.generation, actions=self.runtime_actions
            )
        else:
            resolved_runtime = runtime or ExtensionRuntime(
                generation=self.generation, actions=self.runtime_actions
            )
        runner = ExtensionRunner(
            runtime=resolved_runtime,
            cwd=self.cwd,
            event_bus=resolved_event_bus,
            mode=mode,  # type: ignore[arg-type]
            ui_context=ui_context,
            context_actions=self.context_actions,
        )
        extensions: list[Extension] = []
        errors: list[dict[str, str]] = []

        for raw_path in paths:
            path_label = str(raw_path)
            try:
                path = Path(raw_path).expanduser()
                if not path.is_absolute():
                    path = self.cwd / path
                path = path.absolute()
                requested_scope = source_scopes.get(str(raw_path)) or source_scopes.get(
                    path
                )
                decision = self.trust_policy.assert_allowed(
                    path, source_scope=requested_scope
                )
                path = decision.resolved_path or path.resolve(strict=False)
                raw_metadata = (
                    source_metadata.get(str(raw_path))
                    or source_metadata.get(path)
                    or {}
                )
                metadata = (
                    dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
                )
                source = ExtensionSource(
                    path=path_label,
                    resolved_path=str(path),
                    scope=decision.scope,
                    trusted=decision.trusted,
                    origin=str(metadata.get("origin") or "local"),
                    marketplace=(
                        str(metadata["marketplace"])
                        if metadata.get("marketplace")
                        else None
                    ),
                    plugin_id=(
                        str(metadata["plugin_id"])
                        if metadata.get("plugin_id")
                        else None
                    ),
                    metadata=metadata,
                )
                factory, module_name = self._load_factory(path)
                if factory is None:
                    raise TypeError(
                        f"extension module does not export a factory function: {path_label}"
                    )
                extension = await self._load_factory_into_extension(
                    factory,
                    path_label,
                    path,
                    source,
                    runner,
                )
                extensions.append(extension)
                # The cache owns module lifetime.  The module name is kept in
                # the loader cache rather than mutating the frozen provenance
                # object; callers can inspect ``sys.modules`` only for
                # diagnostics, never for trust decisions.
            except Exception as exc:
                errors.append({"path": path_label, "error": str(exc)})

        runner.extensions = extensions
        if bind_provider_sink is not None:
            runner.bind_provider_sink(bind_provider_sink)
            errors.extend(dict(item) for item in resolved_runtime.provider_diagnostics)
        if bind_tool_registry is not None:
            runner.bind_tool_registry(bind_tool_registry)
        if bind_command_registry is not None:
            runner.bind_command_registry(bind_command_registry)

        result = LoadExtensionsResult(
            extensions=extensions,
            errors=errors,
            runtime=resolved_runtime,
            generation=self.generation,
            runner=runner,
        )
        runner._last_result = result  # type: ignore[attr-defined]
        self._last_result = result
        return result

    async def load_factory(
        self,
        factory: ExtensionFactory | Any,
        *,
        extension_path: str = "<inline>",
        scope: ExtensionScope = "temporary",
        trusted: bool = True,
        runtime: ExtensionRuntime | None = None,
        event_bus: ExtensionEventBus | None = None,
        mode: str = "print",
        ui_context: Any = None,
    ) -> LoadExtensionsResult:
        _GLOBAL_CACHE.token(self.cwd, self.cache_namespace)
        if runtime is not None and runtime.generation != self.generation:
            runtime.invalidate(
                "Extension runtime generation is stale after cache clear/reload."
            )
            resolved_runtime = ExtensionRuntime(
                generation=self.generation, actions=self.runtime_actions
            )
        else:
            resolved_runtime = runtime or ExtensionRuntime(
                generation=self.generation, actions=self.runtime_actions
            )
        runner = ExtensionRunner(
            runtime=resolved_runtime,
            cwd=self.cwd,
            event_bus=event_bus or self.event_bus,
            mode=mode,  # type: ignore[arg-type]
            ui_context=ui_context,
            context_actions=self.context_actions,
        )
        source = ExtensionSource(
            path=extension_path,
            resolved_path=extension_path,
            scope=scope,
            trusted=trusted,
            origin="inline",
        )
        try:
            extension = await self._load_factory_into_extension(
                factory, extension_path, Path(extension_path), source, runner
            )
            runner.extensions = [extension]
            result = LoadExtensionsResult(
                [extension], [], resolved_runtime, self.generation, runner
            )
        except Exception as exc:
            result = LoadExtensionsResult(
                [],
                [{"path": extension_path, "error": str(exc)}],
                resolved_runtime,
                self.generation,
                runner,
            )
        runner._last_result = result  # type: ignore[attr-defined]
        self._last_result = result
        return result

    async def _load_factory_into_extension(
        self,
        factory: Any,
        path_label: str,
        resolved_path: Path,
        source: ExtensionSource,
        runner: ExtensionRunner,
    ) -> Extension:
        extension = Extension(
            path=path_label, resolved_path=str(resolved_path), source=source
        )
        api = ExtensionAPI(runner, extension)
        if hasattr(factory, "setup") and callable(getattr(factory, "setup")):
            await _maybe_await(factory.setup(api))
        elif callable(factory):
            await _maybe_await(factory(api))
        else:
            raise TypeError(f"extension factory is not callable: {path_label}")
        return extension

    def _load_factory(
        self, path: Path
    ) -> tuple[Callable[..., Any] | Any | None, str | None]:
        if not path.is_file():
            raise FileNotFoundError(f"extension path does not exist: {path}")
        if path.suffix not in {".py", ".pyw"}:
            raise ValueError(
                f"unsupported extension module type: {path.suffix or '<none>'}"
            )
        if self.use_cache:
            cached = _GLOBAL_CACHE.get(path, self.cwd, self.cache_namespace)
            if cached is not None:
                return cached, None

        load_generation = self.generation
        digest = hashlib.sha256(
            f"{self.cache_namespace}:{path.resolve()}:{load_generation}".encode(
                "utf-8"
            )
        ).hexdigest()[:20]
        module_name = f"_minicode_extension_{digest}"
        # Remove a stale module name if a test reuses a generation after a
        # failed import.  A unique name prevents Python's import cache from
        # returning an old module after reload.
        self._purge_module_name(module_name)
        spec_kwargs: dict[str, Any] = {}
        if path.name == "__init__.py":
            # Preserve relative imports for one-level Python extension
            # packages while keeping the package name generation-isolated.
            spec_kwargs["submodule_search_locations"] = [str(path.parent)]
        spec = importlib.util.spec_from_file_location(
            module_name, str(path), **spec_kwargs
        )
        if spec is None:
            raise ImportError(f"could not create import spec for extension: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            # Execute fresh source directly instead of delegating to the
            # bytecode loader.  Python's timestamp-based ``.pyc`` validation
            # can otherwise reuse a stale module when a reload happens twice
            # within one filesystem timestamp tick (a common editor workflow).
            source = path.read_bytes()
            code = compile(source, str(path), "exec", dont_inherit=True)
            exec(code, module.__dict__)
        except Exception:
            sys.modules.pop(module_name, None)
            raise

        factory = self._resolve_factory(module)
        if factory is None:
            return None, module_name
        if self.use_cache:
            _GLOBAL_CACHE.put(
                path,
                self.cwd,
                factory,
                module_name,
                cache_namespace=self.cache_namespace,
                expected_generation=load_generation,
            )
        return factory, module_name

    @staticmethod
    def _purge_module_name(module_name: str) -> None:
        """Remove a generation-isolated package and all loaded submodules."""

        sys.modules.pop(module_name, None)
        for loaded_name in tuple(sys.modules):
            if loaded_name.startswith(f"{module_name}."):
                sys.modules.pop(loaded_name, None)

    @staticmethod
    def _resolve_factory(module: ModuleType) -> Callable[..., Any] | Any | None:
        for name in (
            "extension",
            "register",
            "create_extension",
            "EXTENSION_FACTORY",
        ):
            candidate = getattr(module, name, None)
            if callable(candidate) or (
                hasattr(candidate, "setup")
                and callable(getattr(candidate, "setup", None))
            ):
                return candidate
        return None

    async def discover_and_load(
        self,
        configured_paths: Sequence[str | Path] = (),
        *,
        project_root: Path | str | None = None,
        user_config_dir: Path | str | None = None,
        **kwargs: Any,
    ) -> LoadExtensionsResult:
        paths = self.discover(
            configured_paths,
            project_root=project_root,
            user_config_dir=user_config_dir,
        )
        return await self.load(paths, **kwargs)


async def load_extensions(
    paths: Sequence[str | Path],
    cwd: Path | str,
    *,
    trust_policy: ExtensionTrustPolicy | None = None,
    **kwargs: Any,
) -> LoadExtensionsResult:
    """Load an explicit set of MiniCode extension paths."""

    return await ExtensionLoader(cwd=cwd, trust_policy=trust_policy).load(
        paths, **kwargs
    )


async def load_extensions_cached(
    paths: Sequence[str | Path],
    cwd: Path | str,
    *,
    trust_policy: ExtensionTrustPolicy | None = None,
    **kwargs: Any,
) -> LoadExtensionsResult:
    """Load extensions through the owner-partitioned module cache."""

    return await ExtensionLoader(
        cwd=cwd, trust_policy=trust_policy, use_cache=True
    ).load(paths, **kwargs)


async def load_extension_from_factory(
    factory: ExtensionFactory | Any,
    cwd: Path | str,
    *,
    runtime: ExtensionRuntime | None = None,
    event_bus: ExtensionEventBus | None = None,
    extension_path: str = "<inline>",
    scope: ExtensionScope = "temporary",
    trusted: bool = True,
    **kwargs: Any,
) -> LoadExtensionsResult:
    """Load one inline MiniCode extension factory."""

    loader = ExtensionLoader(cwd=cwd, event_bus=event_bus)
    return await loader.load_factory(
        factory,
        extension_path=extension_path,
        scope=scope,
        trusted=trusted,
        runtime=runtime,
        event_bus=event_bus,
        **kwargs,
    )


async def discover_and_load_extensions(
    configured_paths: Sequence[str | Path],
    cwd: Path | str,
    *,
    user_config_dir: Path | str | None = None,
    trust_policy: ExtensionTrustPolicy | None = None,
    **kwargs: Any,
) -> LoadExtensionsResult:
    """Discover MiniCode standard locations and load their extensions."""

    loader = ExtensionLoader(cwd=cwd, trust_policy=trust_policy)
    return await loader.discover_and_load(
        configured_paths, user_config_dir=user_config_dir, **kwargs
    )


__all__ = [
    "ExtensionLoader",
    "ExtensionModuleCache",
    "clear_extension_cache",
    "discover_extensions_in_dir",
    "discover_and_load_extensions",
    "extension_cache_generation",
    "load_extension_from_factory",
    "load_extensions",
    "load_extensions_cached",
    "resolve_extension_entries",
]
