"""Receiving-thread selections and captured executor attachments.

The object boundaries follow Codex Source ``environment_selection.rs``. Caller
requests contain data; only a receiving thread borrows a registered executor.
An execution snapshot retains that exact object across registration changes.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
import logging
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote, urlsplit
import weakref

from backend.environments.errors import ExecutorClosed, ExecutorError, ExecutorProtocolError
from backend.environments.filesystem import file_uri
from backend.environments.manager import Environment, EnvironmentLease, EnvironmentManager, LocalEnvironment
from backend.permissions.environment_scope import EnvironmentPermissionScope, executor_windows_sandbox_selection
from backend.runtime_env import ShellEnvironmentPolicy
from backend.sandbox.policy import PermissionProfile, SandboxEnforcement


EnvironmentConfigKind = Literal["FromThread", "Pending", "Ready", "Failed"]
EnvironmentConfigOrigin = Literal["Thread", "Owner"]
ExecutionEnvironment = Environment | LocalEnvironment
logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from backend.config_layers import ConfigLayerStack
    from backend.permissions.context import PermissionContext, ToolExecutionContext


@dataclass(slots=True)
class _EnvironmentManagerOwner:
    manager: EnvironmentManager
    references: int = 0
    close_task: asyncio.Task | None = None


class EnvironmentManagerLease:
    """SDK host ownership; children keep the same manager until their last release."""

    def __init__(self, owner: _EnvironmentManagerOwner):
        self._owner = owner
        self.released = False
        owner.references += 1

    @classmethod
    def own(cls, manager: EnvironmentManager) -> EnvironmentManagerLease:
        return cls(_EnvironmentManagerOwner(manager))

    @property
    def manager(self) -> EnvironmentManager:
        return self._owner.manager

    def clone(self) -> EnvironmentManagerLease:
        if self.released:
            raise ExecutorClosed("SDK execution host owner is closed")
        return EnvironmentManagerLease(self._owner)

    async def aclose(self) -> None:
        if self.released:
            return
        self.released = True
        self._owner.references -= 1
        if self._owner.references == 0:
            self._owner.close_task = asyncio.create_task(self.manager.close(), name="sdk-environment-host-close")
            await asyncio.shield(self._owner.close_task)


@dataclass(frozen=True, slots=True)
class EnvironmentConfig:
    permission_profile: PermissionProfile
    workspace_roots: tuple[str, ...]
    allow_login_shell: bool = True
    shell_environment_policy: ShellEnvironmentPolicy = field(default_factory=ShellEnvironmentPolicy)
    windows_sandbox_level: str = "disabled"
    windows_sandbox_type: str = "disabled"
    use_legacy_landlock: bool = False
    exec_policy: Mapping[str, Any] | None = None
    mcp_policy: Mapping[str, Any] | None = None
    network_policy: Mapping[str, Any] | None = None
    selected_capability_roots: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "shell_environment_policy", deepcopy(self.shell_environment_policy))
        for name in ("exec_policy", "mcp_policy", "network_policy", "selected_capability_roots"):
            object.__setattr__(self, name, deepcopy(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class ThreadEnvironmentConfigDraft:
    """Accepted thread facts awaiting the selected executor's path domain."""

    permission: PermissionContext
    config_stack: ConfigLayerStack
    workspace_roots: tuple[str, ...] = ()
    allow_login_shell: bool = True
    shell_environment_policy: ShellEnvironmentPolicy = field(default_factory=ShellEnvironmentPolicy)
    windows_sandbox_level: str = "disabled"
    windows_sandbox_type: str = "disabled"
    use_legacy_landlock: bool = False
    exec_policy: Mapping[str, Any] | None = None
    mcp_policy: Mapping[str, Any] | None = None
    network_policy: Mapping[str, Any] | None = None
    selected_capability_roots: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        for name in ("permission", "config_stack", "shell_environment_policy", "exec_policy",
                "mcp_policy", "network_policy", "selected_capability_roots"):
            object.__setattr__(self, name, deepcopy(getattr(self, name)))


EnvironmentConfigInput = EnvironmentConfig | ThreadEnvironmentConfigDraft


@dataclass(frozen=True, slots=True)
class EnvironmentConfigState:
    kind: EnvironmentConfigKind
    value: EnvironmentConfig | None = None
    error: str | None = None

    @classmethod
    def from_thread(cls) -> EnvironmentConfigState:
        return cls("FromThread")

    @classmethod
    def pending(cls) -> EnvironmentConfigState:
        return cls("Pending")

    @classmethod
    def ready(cls, config: EnvironmentConfig) -> EnvironmentConfigState:
        return cls("Ready", value=config)

    @classmethod
    def failed(cls, error: str) -> EnvironmentConfigState:
        return cls("Failed", error=error)


@dataclass(frozen=True, slots=True)
class TurnEnvironmentRequest:
    environment_id: str
    cwd: str
    workspace_roots: tuple[str, ...]
    config: EnvironmentConfigState = field(default_factory=EnvironmentConfigState.from_thread)

    def __post_init__(self) -> None:
        file_uri(self.cwd)
        for root in self.workspace_roots:
            file_uri(root)


@dataclass(frozen=True, slots=True)
class TurnEnvironmentSelection:
    environment_id: str
    cwd: str
    workspace_roots: tuple[str, ...]
    config: EnvironmentConfigState

    @classmethod
    def from_request(cls, request: TurnEnvironmentRequest) -> TurnEnvironmentSelection:
        return cls(request.environment_id, request.cwd, request.workspace_roots, request.config)

    def into_request(self) -> TurnEnvironmentRequest:
        return TurnEnvironmentRequest(self.environment_id, self.cwd, self.workspace_roots, self.config)

    def same_attachment(self, other: TurnEnvironmentSelection) -> bool:
        return (self.environment_id, self.cwd, self.workspace_roots) == (
            other.environment_id, other.cwd, other.workspace_roots)


@dataclass(frozen=True, slots=True)
class EnvironmentSkillRequirements:
    environment_id: str
    skill_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ThreadEnvironmentDefaults:
    common: EnvironmentConfigInput
    local_windows_sandbox_type: str

    def for_selection(self, selection: TurnEnvironmentSelection) -> EnvironmentConfigInput:
        return replace(self.common, workspace_roots=selection.workspace_roots,
            selected_capability_roots=(),
            windows_sandbox_type=(self.local_windows_sandbox_type
                if selection.environment_id == "local" else self.common.windows_sandbox_type))


def default_thread_environment_requests(manager: EnvironmentManager, cwd: str,
        workspace_roots: tuple[str, ...]) -> tuple[TurnEnvironmentRequest, ...]:
    return tuple(TurnEnvironmentRequest(environment_id, cwd, workspace_roots)
        for environment_id in manager.environment_ids())


def validate_environment_requests(manager: EnvironmentManager,
        requests: Iterable[TurnEnvironmentRequest]) -> None:
    seen = set()
    for request in requests:
        parsed = urlsplit(request.cwd)
        native = unquote(parsed.path)
        if len(native) >= 3 and native[0] == "/" and native[1].isalpha() and native[2] == ":":
            native = native[1:]
        if parsed.netloc:
            native = "//" + parsed.netloc + native
        if len(native.encode("utf-8")) > 8 * 1024:
            raise ValueError("turn environment working directory exceeds the maximum size")
        if request.environment_id in seen:
            raise ValueError(f"duplicate turn environment id `{request.environment_id}`")
        seen.add(request.environment_id)
        if manager.get_environment(request.environment_id) is None:
            raise ValueError(f"unknown turn environment id `{request.environment_id}`")


def validate_environment_configs(selections: Iterable[TurnEnvironmentSelection]) -> None:
    for selection in selections:
        if selection.config.kind != "Ready":
            continue
        config = selection.config.value
        if config.network_policy is not None and selection.environment_id == "local":
            raise ValueError("attachment-owned network policy requires a remote executor")
        if len(config.selected_capability_roots) > 256:
            raise ValueError("environment readiness contains more than 256 selected capability roots")
        root_ids = set()
        for root in config.selected_capability_roots:
            root_id = root["id"]
            location = root["location"]
            if (not root_id.strip() or root_id in root_ids or location["type"] != "environment"
                    or location["environmentId"] != selection.environment_id):
                raise ValueError("selected capability roots must have unique non-empty IDs and belong to "
                    f"environment `{selection.environment_id}`")
            file_uri(location["path"])
            root_ids.add(root_id)


def _input_selection(selection: TurnEnvironmentSelection,
        origin: EnvironmentConfigOrigin) -> TurnEnvironmentSelection:
    return replace(selection, config=EnvironmentConfigState.from_thread()) if origin == "Thread" else selection


def _normalize(selection: TurnEnvironmentSelection, defaults: ThreadEnvironmentDefaults
        ) -> tuple[TurnEnvironmentSelection, EnvironmentConfigOrigin]:
    if selection.config.kind == "FromThread":
        return selection, "Thread"
    return selection, "Owner"


@dataclass(frozen=True, slots=True)
class _ConfigurationResult:
    config: EnvironmentConfig | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedEnvironment:
    config: EnvironmentConfig
    peer_info: dict[str, Any]


ConfigResolver = Callable[[TurnEnvironmentSelection, EnvironmentConfigInput,
    ExecutionEnvironment, Mapping[str, Any]], Awaitable[EnvironmentConfig]]


@dataclass(frozen=True, slots=True)
class _CapturedEnvironment:
    selection: TurnEnvironmentSelection
    config_origin: EnvironmentConfigOrigin
    lease: EnvironmentLease
    resolution: asyncio.Task[_ResolvedEnvironment]
    owner_config_result: asyncio.Future[_ConfigurationResult] | None
    registration_roots: tuple[dict[str, Any], ...]

    def clone(self) -> _CapturedEnvironment:
        return replace(self, lease=self.lease.clone())


@dataclass(frozen=True, slots=True)
class TurnEnvironment:
    selection: TurnEnvironmentSelection
    config_origin: EnvironmentConfigOrigin
    lease: EnvironmentLease
    peer_info: dict[str, Any]

    @property
    def environment(self) -> ExecutionEnvironment:
        return self.lease.environment

    @property
    def config(self) -> EnvironmentConfig:
        return self.selection.config.value

    @property
    def is_remote(self) -> bool:
        return isinstance(self.environment, Environment)

    @property
    def shell(self) -> Mapping[str, str] | None:
        shell = self.peer_info.get("shell")
        return shell if shell is not None and shell["name"] in {"zsh", "bash", "powershell", "sh", "cmd"} else None

    @property
    def permission_scope(self) -> EnvironmentPermissionScope:
        config = self.config
        return EnvironmentPermissionScope(
            environment_id=self.selection.environment_id, permission_profile=config.permission_profile,
            cwd_uri=self.selection.cwd, workspace_root_uris=config.workspace_roots,
            platform_os=self.peer_info.get("platformOs"),
            user_home_uri=self.peer_info.get("userHomeDir"),
            temporary_directory_uris=(tuple(self.peer_info["temporaryDirectories"])
                if self.peer_info.get("temporaryDirectories") is not None else None),
            windows_sandbox_level=executor_windows_sandbox_selection(
                config.windows_sandbox_type, config.windows_sandbox_level, self.selection.cwd),
            use_legacy_landlock=config.use_legacy_landlock,
        )

    def input_selection(self) -> TurnEnvironmentSelection:
        return _input_selection(self.selection, self.config_origin)


@dataclass(frozen=True, slots=True)
class StartingTurnEnvironment:
    selection: TurnEnvironmentSelection
    config_origin: EnvironmentConfigOrigin
    lease: EnvironmentLease
    resolution: asyncio.Task[_ResolvedEnvironment]
    owner_config_result: asyncio.Future[_ConfigurationResult] | None

    @property
    def environment(self) -> ExecutionEnvironment:
        return self.lease.environment

    async def wait_until_ready(self) -> None:
        await asyncio.shield(self.resolution)

    def owner_configuration(self) -> asyncio.Future[_ConfigurationResult] | None:
        return self.owner_config_result if self.selection.config.kind == "Pending" else None


@dataclass(frozen=True, slots=True)
class FailedTurnEnvironment:
    selection: TurnEnvironmentSelection
    required_skills: tuple[str, ...]
    error: str
    config_origin: EnvironmentConfigOrigin


TurnEnvironmentState = TurnEnvironment | StartingTurnEnvironment | FailedTurnEnvironment


def _state(captured: _CapturedEnvironment | FailedTurnEnvironment) -> TurnEnvironmentState:
    if isinstance(captured, FailedTurnEnvironment):
        return captured
    selection = captured.selection
    if selection.config.kind == "Failed":
        return FailedTurnEnvironment(_input_selection(selection, captured.config_origin),
            captured.lease.environment.required_skills, selection.config.error, captured.config_origin)
    if not captured.resolution.done():
        return StartingTurnEnvironment(selection, captured.config_origin, captured.lease,
            captured.resolution, captured.owner_config_result)
    if captured.resolution.cancelled():
        return FailedTurnEnvironment(_input_selection(selection, captured.config_origin),
            captured.lease.environment.required_skills, "environment configuration was canceled", captured.config_origin)
    error = captured.resolution.exception()
    if error is not None:
        return FailedTurnEnvironment(_input_selection(selection, captured.config_origin),
            captured.lease.environment.required_skills, str(error), captured.config_origin)
    resolved = captured.resolution.result()
    config = (replace(resolved.config, selected_capability_roots=captured.registration_roots)
        if captured.config_origin == "Thread" else resolved.config)
    return TurnEnvironment(replace(selection, config=EnvironmentConfigState.ready(config)),
        captured.config_origin, captured.lease, deepcopy(resolved.peer_info))


class _SnapshotResources:
    def __init__(self, captured: tuple[_CapturedEnvironment | FailedTurnEnvironment, ...],
            manager_lease: EnvironmentManagerLease | None = None):
        self.captured = captured
        self.manager_lease = manager_lease
        self.close_task: asyncio.Task[None] | None = None

    async def _close(self) -> None:
        results = await asyncio.gather(*(item.lease.aclose() for item in self.captured
            if isinstance(item, _CapturedEnvironment)), return_exceptions=True)
        errors = [result for result in results if isinstance(result, Exception)]
        if self.manager_lease is not None:
            try:
                await self.manager_lease.aclose()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("captured environment leases did not close", errors)

    async def aclose(self) -> None:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="turn-environment-snapshot-close")
        await asyncio.shield(self.close_task)


class TurnEnvironmentSnapshot:
    """One capture and its readiness views; ``clone`` creates another borrower."""

    def __init__(self, captured: tuple[_CapturedEnvironment | FailedTurnEnvironment, ...] = (), *,
            resources: _SnapshotResources | None = None, selected_capability_roots: tuple[dict[str, Any], ...] = ()):
        self._captured = captured
        self._resources = resources if resources is not None else _SnapshotResources(captured)
        self.environments = tuple(_state(item) for item in captured)
        self.selected_capability_roots = deepcopy(selected_capability_roots)

    def refresh_readiness(self) -> TurnEnvironmentSnapshot:
        return TurnEnvironmentSnapshot(self._captured, resources=self._resources,
            selected_capability_roots=self.selected_capability_roots)

    def clone(self) -> TurnEnvironmentSnapshot:
        captured = tuple(item.clone() if isinstance(item, _CapturedEnvironment) else item for item in self._captured)
        manager_lease = self._resources.manager_lease.clone() if self._resources.manager_lease is not None else None
        return TurnEnvironmentSnapshot(captured, resources=_SnapshotResources(captured, manager_lease),
            selected_capability_roots=self.selected_capability_roots)

    def turn_environments(self) -> tuple[TurnEnvironment, ...]:
        return tuple(item for item in self.environments if isinstance(item, TurnEnvironment))

    def starting(self) -> tuple[StartingTurnEnvironment, ...]:
        return tuple(item for item in self.environments if isinstance(item, StartingTurnEnvironment))

    def primary(self) -> TurnEnvironment | None:
        return next(iter(self.turn_environments()), None)

    def primary_workspace_root_uris(self) -> tuple[str, ...]:
        return self.environments[0].selection.workspace_roots if self.environments else ()

    def local(self) -> TurnEnvironment | None:
        return next((item for item in self.turn_environments()
            if isinstance(item.environment, LocalEnvironment)), None)

    def single_local_environment(self) -> TurnEnvironment | None:
        if self.starting():
            return None
        ready = self.turn_environments()
        return ready[0] if len(ready) == 1 and not ready[0].is_remote else None

    def primary_filesystem(self) -> Any | None:
        primary = self.primary()
        return primary.environment.filesystem if primary is not None else None

    def to_selections(self) -> tuple[TurnEnvironmentSelection, ...]:
        return tuple(item.input_selection() for item in self.turn_environments())

    def ready_environment_handles(self) -> dict[str, ExecutionEnvironment]:
        return {item.selection.environment_id: item.environment for item in self.turn_environments()}

    def captured_environments(self) -> dict[str, ExecutionEnvironment | None]:
        return {item.selection.environment_id: item.environment if isinstance(item, TurnEnvironment) else None
            for item in self.environments if not isinstance(item, FailedTurnEnvironment)}

    def all_selections(self) -> tuple[TurnEnvironmentSelection, ...]:
        return tuple(item.input_selection() if isinstance(item, TurnEnvironment)
            else _input_selection(item.selection, item.config_origin) for item in self.environments)

    def inheritable_selections(self) -> tuple[TurnEnvironmentSelection, ...]:
        return tuple(item.input_selection() if isinstance(item, TurnEnvironment)
            else _input_selection(item.selection, item.config_origin)
            for item in self.environments if not isinstance(item, FailedTurnEnvironment))

    def required_skills(self) -> tuple[EnvironmentSkillRequirements, ...]:
        requirements = []
        for item in self.environments:
            names = item.required_skills if isinstance(item, FailedTurnEnvironment) else item.environment.required_skills
            if names:
                requirements.append(EnvironmentSkillRequirements(item.selection.environment_id, tuple(names)))
        return tuple(requirements)

    def has_full_access(self, approval_policy: str, thread_profile: PermissionProfile) -> bool:
        if approval_policy != "never":
            return False
        current = self.refresh_readiness()
        if not current.environments:
            return thread_profile.enforcement is SandboxEnforcement.DISABLED
        return all(isinstance(item, TurnEnvironment)
            and item.config.permission_profile.enforcement is SandboxEnforcement.DISABLED
            for item in current.environments)

    def for_environment(self, environment_id: str) -> TurnEnvironment:
        for item in self.environments:
            if item.selection.environment_id != environment_id:
                continue
            if isinstance(item, TurnEnvironment):
                return item
            if isinstance(item, FailedTurnEnvironment):
                raise ExecutorProtocolError(item.error)
            raise ExecutorProtocolError(f"environment `{environment_id}` is still starting")
        raise ExecutorProtocolError(f"environment `{environment_id}` is not selected on this thread")

    async def aclose(self) -> None:
        await self._resources.aclose()

    async def __aenter__(self) -> TurnEnvironmentSnapshot:
        return self

    async def __aexit__(self, *_args) -> None:
        await self.aclose()


def select_turn_environment(snapshot: TurnEnvironmentSnapshot, environment_id: str | None = None, *,
        stable_environment_tools: bool = True,
        legacy_unavailable_message: str = "No usable execution environment is available.") -> TurnEnvironment:
    ready = snapshot.primary() if environment_id is None else next((environment
        for environment in snapshot.turn_environments() if environment.selection.environment_id == environment_id), None)
    if ready is not None:
        return ready
    if environment_id is not None and (not stable_environment_tools or not any(
            selection.environment_id == environment_id for selection in snapshot.all_selections())):
        raise ValueError(f"unknown turn environment id `{environment_id}`")
    raise ValueError("No usable execution environment is available. Wait for an environment to become available before using this tool."
        if stable_environment_tools else legacy_unavailable_message)


def bind_tool_execution_environment(context: ToolExecutionContext,
        environment: TurnEnvironment) -> ToolExecutionContext:
    attachment = replace(environment, lease=environment.lease.clone())
    return replace(context, turn_environment=attachment, environment_id=attachment.selection.environment_id,
        permission=replace(context.permission, environment_scope=attachment.permission_scope))


async def close_tool_execution_environment(context: ToolExecutionContext) -> None:
    await context.turn_environment.lease.aclose()


@dataclass(slots=True)
class _ThreadBinding:
    selection: TurnEnvironmentSelection
    config_origin: EnvironmentConfigOrigin
    lease: EnvironmentLease
    resolution: asyncio.Task[_ResolvedEnvironment]
    pending_completion: asyncio.Future[_ConfigurationResult] | None
    owner_config_result: asyncio.Future[_ConfigurationResult] | None
    thread_config: EnvironmentConfigInput | None

    def capture(self) -> _CapturedEnvironment | FailedTurnEnvironment:
        captured = _CapturedEnvironment(self.selection, self.config_origin, self.lease,
            self.resolution, self.owner_config_result, self.lease.environment.selected_capability_roots())
        state = _state(captured)
        return state if isinstance(state, FailedTurnEnvironment) else captured.clone()


class ThreadEnvironments:
    """The receiving session's accepted selections and original executor leases."""

    @classmethod
    def retarget(cls, manager, defaults, *, resolve_config, attachment: TurnEnvironment,
            cwd: str, workspace_roots: tuple[str, ...]):
        """Open a new thread on a captured executor without a registry lookup."""
        request = TurnEnvironmentRequest(attachment.selection.environment_id, cwd, workspace_roots,
            attachment.input_selection().config)
        requested = TurnEnvironmentSelection.from_request(request)
        if requested.config.kind == "Ready":
            requested = replace(requested, config=EnvironmentConfigState.ready(
                replace(requested.config.value, workspace_roots=workspace_roots)))
        owner = cls(manager, defaults, resolve_config=resolve_config,
            selected_capability_roots=attachment.config.selected_capability_roots)
        selection, origin = _normalize(requested, defaults)
        owner._bindings = (owner._start(selection, origin, attachment.lease.clone(), cached_info=attachment.peer_info),)
        return owner

    def __init__(self, manager: EnvironmentManager, defaults: ThreadEnvironmentDefaults, *,
            resolve_config: ConfigResolver, inherited: TurnEnvironmentSnapshot | None = None,
            non_blocking_snapshots: bool = False, manager_lease: EnvironmentManagerLease | None = None,
            selected_capability_roots: tuple[dict[str, Any], ...] = ()):
        self.manager = manager
        self.defaults = defaults
        self.resolve_config = resolve_config
        self.non_blocking_snapshots = non_blocking_snapshots
        self.manager_lease = manager_lease
        self.selected_capability_roots = deepcopy(selected_capability_roots)
        self._bindings: tuple[_ThreadBinding, ...] = ()
        self.project_source_cwd: str | None = None
        self._follow_tasks: set[asyncio.Task[None]] = set()
        self._resolution_tasks: set[asyncio.Task[_ResolvedEnvironment]] = set()
        self._retirement_tasks: list[asyncio.Task[None]] = []
        self._close_task: asyncio.Task[None] | None = None
        self.closed = False
        if inherited is not None:
            if self.manager_lease is None and inherited._resources.manager_lease is not None:
                self.manager_lease = inherited._resources.manager_lease.clone()
            self.selected_capability_roots = deepcopy(inherited.selected_capability_roots)
            bindings = []
            for captured, state in zip(inherited._captured, inherited.environments):
                if isinstance(state, FailedTurnEnvironment):
                    continue
                selection, origin = _normalize(_input_selection(state.selection, captured.config_origin), defaults)
                cached_info = state.peer_info if isinstance(state, TurnEnvironment) else None
                binding = self._start(selection, origin, captured.lease.clone(), cached_info=cached_info,
                    owner_config_result=captured.owner_config_result)
                bindings.append(binding)
                if isinstance(state, StartingTurnEnvironment) and state.owner_configuration() is not None:
                    self._follow_configuration(binding, state.owner_configuration())
            self._bindings = tuple(bindings)

    def _follow_configuration(self, binding: _ThreadBinding,
            original: asyncio.Future[_ConfigurationResult]) -> None:
        owner = weakref.ref(self)
        selection = _input_selection(binding.selection, binding.config_origin)

        async def follow() -> None:
            result = await asyncio.shield(original)
            current = owner()
            if current is None or current.closed:
                return
            if result.error is not None:
                await current.environment_failed(selection, result.error, inherited=True)
            else:
                try:
                    await current.environment_ready(selection, result.config, inherited=True)
                except (ExecutorError, ValueError) as error:
                    await current.environment_failed(selection, str(error), inherited=True)

        task = asyncio.create_task(follow(), name=f"inherited-environment-config-{selection.environment_id}")
        self._follow_tasks.add(task)
        task.add_done_callback(self._follow_tasks.discard)

    def _start(self, selection: TurnEnvironmentSelection, origin: EnvironmentConfigOrigin,
            lease: EnvironmentLease, *, cached_info: dict[str, Any] | None = None,
            owner_config_result: asyncio.Future[_ConfigurationResult] | None = None,
            pending_completion: asyncio.Future[_ConfigurationResult] | None = None) -> _ThreadBinding:
        if selection.config.kind == "Pending" and pending_completion is None:
            pending_completion = asyncio.get_running_loop().create_future()
        if owner_config_result is None:
            owner_config_result = pending_completion
        resolver_lease = lease.clone()
        resolve_config = self.resolve_config
        thread_config = self.defaults.for_selection(selection) if origin == "Thread" else None

        async def resolve() -> _ResolvedEnvironment:
            try:
                state = selection.config
                if state.kind == "Failed":
                    raise ExecutorProtocolError(state.error)
                environment = resolver_lease.environment
                await environment.wait_until_ready()
                if state.kind == "Pending":
                    result = await asyncio.shield(pending_completion)
                    if result.error is not None:
                        raise ExecutorProtocolError(result.error)
                    config = result.config
                elif state.kind == "FromThread":
                    config = thread_config
                else:
                    config = state.value
                if cached_info is not None:
                    info = cached_info
                elif isinstance(environment, Environment):
                    try:
                        info = environment.client.environment_info
                        if info is None:
                            info = await environment.info()
                    except Exception as error:
                        # Source keeps optional executor facts absent when info fails.
                        logger.warning("Failed to get info for environment `%s`: %s", selection.environment_id, error)
                        info = {}
                else:
                    info = await environment.info()
                if info.get("shell") is not None and info["shell"]["name"] not in {"zsh", "bash", "powershell", "sh", "cmd"}:
                    logger.warning("Failed to resolve shell for environment `%s`: unknown environment shell `%s`",
                        selection.environment_id, info["shell"]["name"])
                resolved_config = await resolve_config(_input_selection(selection, origin), config, environment, info)
                return _ResolvedEnvironment(resolved_config, deepcopy(info))
            finally:
                await resolver_lease.aclose()

        task = asyncio.create_task(resolve(), name=f"environment-selection-{selection.environment_id}")
        self._resolution_tasks.add(task)

        def resolved(done: asyncio.Task[_ResolvedEnvironment]) -> None:
            self._resolution_tasks.discard(done)
            if not done.cancelled():
                done.exception()  # The same result remains the captured startup evidence.

        task.add_done_callback(resolved)
        return _ThreadBinding(selection, origin, lease, task, pending_completion, owner_config_result, thread_config)

    def selections(self) -> tuple[TurnEnvironmentSelection, ...]:
        return tuple(_input_selection(item.selection, item.config_origin) for item in self._bindings)

    async def update_selections(self, selections: Iterable[TurnEnvironmentSelection]) -> None:
        if self.closed:
            raise ExecutorClosed("thread environment owner is closed")
        selected = tuple(selections)
        validate_environment_requests(self.manager, (item.into_request() for item in selected))
        validate_environment_configs(selected)
        from backend.async_cleanup import to_thread_cancel_safe
        from backend.permissions.native_execpolicy import NativeExecPolicyRuntime

        for selection in selected:
            if (selection.config.kind == "Ready" and selection.config.value.exec_policy is not None
                    and not any(item.selection == selection for item in self._bindings)):
                await to_thread_cancel_safe(NativeExecPolicyRuntime().invoke, {
                    "operation": "inspect", "sources": [],
                    "requirements_rules": selection.config.value.exec_policy,
                    "ignore_allow_prefix_rules": False})
        for selection in selected:
            if selection.config.kind == "FromThread" and any(
                    item.selection.environment_id == selection.environment_id and item.config_origin == "Owner"
                    for item in self._bindings):
                raise ValueError(f"environment `{selection.environment_id}` requires owner-provided configuration")
        previous = self._bindings
        next_bindings = []
        completions = []
        for requested in selected:
            selection, origin = _normalize(requested, self.defaults)
            old = next((item for item in previous if item.selection.same_attachment(selection)), None)
            failed = old is not None and (old.selection.config.kind == "Failed" or
                (old.resolution.done() and (old.resolution.cancelled() or old.resolution.exception() is not None)))
            thread_config = self.defaults.for_selection(selection) if origin == "Thread" else None
            if (old is not None and not failed and old.selection == selection and old.config_origin == origin
                    and old.thread_config == thread_config):
                next_bindings.append(old)
                continue
            restarting_pending = old is not None and selection.config.kind == "Pending" and old.selection.config.kind != "Pending"
            owner_result = old.owner_config_result if old is not None and not restarting_pending else None
            completion = (old.pending_completion
                if old is not None and old.selection.config.kind == "Pending" and selection.config.kind == "Pending" else None)
            if old is not None and old.pending_completion is not None and not old.pending_completion.done():
                if selection.config.kind == "Ready":
                    completions.append((old.pending_completion, _ConfigurationResult(config=selection.config.value)))
                elif selection.config.kind == "Failed":
                    completions.append((old.pending_completion, _ConfigurationResult(error=selection.config.error)))
            if old is not None and not failed:
                cached = old.resolution.result().peer_info if old.resolution.done() and not old.resolution.cancelled() and old.resolution.exception() is None else None
                binding = self._start(selection, origin, old.lease.clone(), cached_info=cached,
                    owner_config_result=owner_result, pending_completion=completion)
            else:
                binding = self._start(selection, origin, self.manager.borrow(selection.environment_id),
                    owner_config_result=owner_result, pending_completion=completion)
            next_bindings.append(binding)
        self._bindings = tuple(next_bindings)
        for future, result in completions:
            future.set_result(result)
        retained = {id(item) for item in next_bindings}
        retiring = []
        for old in previous:
            if id(old) in retained:
                continue
            if not any(item.selection.same_attachment(old.selection) for item in next_bindings):
                self._cancel_pending(old)
            retiring.append(old.lease)
        if retiring:
            task = asyncio.create_task(self._release_leases(tuple(retiring)), name="retired-thread-environment-leases")
            self._retirement_tasks.append(task)
            await asyncio.shield(task)

    @staticmethod
    async def _release_leases(leases: tuple[EnvironmentLease, ...]) -> None:
        results = await asyncio.gather(*(lease.aclose() for lease in leases), return_exceptions=True)
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("thread environment leases did not close", errors)

    @staticmethod
    def _cancel_pending(binding: _ThreadBinding) -> None:
        future = binding.pending_completion
        if future is not None and not future.done():
            future.set_result(_ConfigurationResult(error="environment configuration was canceled"))

    async def set_active_thread_defaults(self, defaults: ThreadEnvironmentDefaults) -> None:
        self.defaults = defaults
        await self.update_selections(self.selections())

    async def environment_ready(self, selection: TurnEnvironmentSelection, config: EnvironmentConfig,
            *, inherited: bool = False) -> None:
        await self._update_configuration(selection, EnvironmentConfigState.ready(config), inherited=inherited)

    async def environment_failed(self, selection: TurnEnvironmentSelection, error: str,
            *, inherited: bool = False) -> None:
        await self._update_configuration(selection, EnvironmentConfigState.failed(error), inherited=inherited)

    async def _update_configuration(self, selection: TurnEnvironmentSelection,
            state: EnvironmentConfigState, *, inherited: bool) -> None:
        current = self.selections()
        index = next((index for index, item in enumerate(current) if item.same_attachment(selection)
            and (not inherited or item.config.kind == "Pending")), None)
        if index is None:
            if inherited:
                return
            raise ValueError(f"environment `{selection.environment_id}` is not selected on this thread with the requested workspace")
        await self.update_selections((*current[:index], replace(current[index], config=state), *current[index + 1:]))

    def snapshot_now(self) -> TurnEnvironmentSnapshot:
        if self.closed:
            raise ExecutorClosed("thread environment owner is closed")
        captured = tuple(item.capture() for item in self._bindings)
        manager_lease = (self.manager_lease.clone() if self.manager_lease is not None
            and any(isinstance(item, _CapturedEnvironment) for item in captured) else None)
        return TurnEnvironmentSnapshot(captured, resources=_SnapshotResources(captured, manager_lease),
            selected_capability_roots=self.selected_capability_roots)

    async def snapshot(self) -> TurnEnvironmentSnapshot:
        captured = self.snapshot_now()
        try:
            if not self.non_blocking_snapshots:
                for starting in captured.starting():
                    if starting.selection.config.kind != "Pending":
                        # Result errors become Failed entries in this same capture.
                        await asyncio.wait({starting.resolution})
            refreshed = captured.refresh_readiness()
            entries = tuple(state if isinstance(state, FailedTurnEnvironment) else item
                for item, state in zip(captured._captured, refreshed.environments))
            failed_leases = tuple(item.lease for item, state in zip(captured._captured, refreshed.environments)
                if isinstance(item, _CapturedEnvironment) and isinstance(state, FailedTurnEnvironment))
            if failed_leases:
                # The private startup capture has not escaped this method. A
                # Failed result retains evidence, not its temporary Arc/lease.
                captured._resources.captured = entries
                task = asyncio.create_task(self._release_leases(failed_leases), name="failed-capture-environment-leases")
                self._retirement_tasks.append(task)
                await asyncio.shield(task)
                if not any(isinstance(item, _CapturedEnvironment) for item in entries) and captured._resources.manager_lease is not None:
                    await captured._resources.manager_lease.aclose()
                    captured._resources.manager_lease = None
            return TurnEnvironmentSnapshot(entries, resources=captured._resources,
                selected_capability_roots=captured.selected_capability_roots)
        except BaseException:
            await captured.aclose()
            raise

    async def _close(self) -> None:
        self.closed = True
        bindings, self._bindings = self._bindings, ()
        for binding in bindings:
            self._cancel_pending(binding)
        for task in tuple(self._follow_tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._follow_tasks), return_exceptions=True)
        await asyncio.gather(*tuple(self._resolution_tasks), return_exceptions=True)
        releases = [*self._retirement_tasks,
            asyncio.create_task(self._release_leases(tuple(binding.lease for binding in bindings)),
                name="remaining-thread-environment-leases")]
        results = await asyncio.gather(*releases, return_exceptions=True)
        errors = [result for result in results if isinstance(result, Exception)]
        if self.manager_lease is not None:
            try:
                await self.manager_lease.aclose()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("thread environment leases did not close", errors)

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="thread-environments-close")
        await asyncio.shield(self._close_task)

    async def __aenter__(self) -> ThreadEnvironments:
        return self

    async def __aexit__(self, *_args) -> None:
        await self.aclose()
