"""Receiving-thread environment assembly shared by every query entry point."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from backend.agent.environment_selection import (
    EnvironmentConfig, EnvironmentManagerLease, ThreadEnvironmentConfigDraft,
    ThreadEnvironmentDefaults, ThreadEnvironments, TurnEnvironmentRequest,
    TurnEnvironmentSelection, default_thread_environment_requests,
)
from backend.permissions.environment_scope import capture_environment_permission_profile
from backend.runtime_env import ShellEnvironmentPolicy


def thread_environment_defaults(permission, config_stack) -> ThreadEnvironmentDefaults:
    config = config_stack.effective_config()
    windows_mode = config.get("windows", {}).get("sandbox")
    windows_type, windows_level = {
        None: ("none", "disabled"),
        "elevated": ("windowsRestrictedToken", "elevated"),
        "unelevated": ("windowsRestrictedToken", "restricted-token"),
        "mxc": ("windowsMxc", "disabled"),
    }[windows_mode]
    permission_facts = replace(permission, captured_mcp_permission_profile=None,
        captured_mcp_approval_store=None, captured_exec_policy=None, environment_scope=None)
    draft = ThreadEnvironmentConfigDraft(permission_facts, config_stack,
        allow_login_shell=config_stack.requirements.value_for("allow_login_shell", config.get("allow_login_shell", True)),
        shell_environment_policy=ShellEnvironmentPolicy.from_mapping(config.get("shell_environment_policy")),
        windows_sandbox_type=windows_type, windows_sandbox_level=windows_level,
        use_legacy_landlock=bool(config.get("features", {}).get("use_legacy_landlock", False)),
        exec_policy=None, mcp_policy=None,
        network_policy=None)
    return ThreadEnvironmentDefaults(draft, windows_type)


async def resolve_environment_config(selection, config, environment, info) -> EnvironmentConfig:
    if isinstance(config, EnvironmentConfig):
        return config
    profile = await capture_environment_permission_profile(config.permission, config.config_stack,
        selection.cwd, config.workspace_roots, info.get("platformOs"), environment.filesystem,
        user_home_uri=info.get("userHomeDir"),
        temporary_directory_uris=(tuple(info["temporaryDirectories"])
            if info.get("temporaryDirectories") is not None else None),
        windows_sandbox_type=config.windows_sandbox_type, windows_sandbox_level=config.windows_sandbox_level,
        use_legacy_landlock=config.use_legacy_landlock)
    return EnvironmentConfig(profile, config.workspace_roots,
        allow_login_shell=config.allow_login_shell, shell_environment_policy=config.shell_environment_policy,
        windows_sandbox_type=config.windows_sandbox_type, windows_sandbox_level=config.windows_sandbox_level,
        use_legacy_landlock=config.use_legacy_landlock, exec_policy=config.exec_policy,
        mcp_policy=config.mcp_policy, network_policy=config.network_policy,
        selected_capability_roots=config.selected_capability_roots)


async def prepare_thread_environments(*, manager, defaults, cwd_uri: str,
        workspace_root_uris: tuple[str, ...], owner: ThreadEnvironments | None = None,
        requests: tuple[TurnEnvironmentRequest, ...] | None = None, inherited=None,
        manager_lease: EnvironmentManagerLease | None = None,
        selected_capability_roots: tuple[dict, ...] = ()) -> ThreadEnvironments:
    if owner is None:
        owner = ThreadEnvironments(manager, defaults, resolve_config=resolve_environment_config,
            inherited=inherited, manager_lease=manager_lease.clone() if manager_lease is not None else None,
            selected_capability_roots=selected_capability_roots)
        if inherited is None and requests is None:
            requests = default_thread_environment_requests(manager, cwd_uri, workspace_root_uris)
    else:
        await owner.set_active_thread_defaults(defaults)
    if requests is not None:
        await owner.update_selections(TurnEnvironmentSelection.from_request(request) for request in requests)
    return owner


async def capture_conversation_environments(*, environment_manager, execution_registry,
        conversation_repository, conversation_id: str, permission_context, config_stack,
        cwd_uri: str, workspace_root_uris: tuple[str, ...],
        requests: tuple[TurnEnvironmentRequest, ...] | None = None,
        selected_capability_roots: tuple[dict, ...] = (), require_existing_attachment: bool = False):
    """Capture the same store/thread owner used by WS, REST and the editor.

    Inputs come from the receiving host. Persisted prompts and context snapshots
    cannot carry a registered executor, Ready state or live lease into this API.
    The caller must close the returned snapshot after its final borrower.
    """
    store_identity = await asyncio.to_thread(conversation_repository.store_instance_id)
    runtime = execution_registry.thread_runtime(store_identity, conversation_id)
    defaults = thread_environment_defaults(permission_context, config_stack)
    async with runtime.environment_lock:
        if require_existing_attachment:
            from backend.environments.errors import ExecutorProtocolError

            if runtime.thread_environments is None or not any(
                    selection.cwd == cwd_uri for selection in runtime.thread_environments.selections()):
                raise ExecutorProtocolError("The conversation's original execution environment is not attached to its recorded workspace.")
        runtime.thread_environments = await prepare_thread_environments(
            manager=environment_manager, defaults=defaults, cwd_uri=cwd_uri,
            workspace_root_uris=workspace_root_uris, owner=runtime.thread_environments,
            requests=requests, selected_capability_roots=selected_capability_roots)
        return await runtime.thread_environments.snapshot()


async def prepare_run_environments(run_context, *, agent_session, permission_context,
        config_stack, workspace_root, conversation_id: str):
    manager = run_context.environment_manager
    if manager is None:
        return None
    cwd_uri = run_context.execution_cwd_uri or Path(workspace_root if workspace_root is not None else Path.cwd()).as_uri()
    roots = (cwd_uri,) if run_context.execution_cwd_uri or workspace_root is not None else ()
    defaults = thread_environment_defaults(permission_context, config_stack)
    runtime = run_context.environment_thread_runtime
    lock = runtime.environment_lock if runtime is not None else run_context.environment_lock
    async with lock:
        existing = runtime.thread_environments if runtime is not None else run_context.thread_environments
        if run_context.execution_cwd_uri:
            from backend.environments.errors import ExecutorProtocolError

            if existing is None or not existing.selections() or existing.selections()[0].cwd != cwd_uri:
                raise ExecutorProtocolError("The conversation's original execution environment is not attached to its recorded workspace.")
        owner = await prepare_thread_environments(manager=manager, defaults=defaults,
            cwd_uri=cwd_uri, workspace_root_uris=roots, owner=existing,
            requests=run_context.environment_requests, inherited=run_context.inherited_turn_environments,
            manager_lease=run_context.environment_manager_lease,
            selected_capability_roots=run_context.selected_capability_roots)
        run_context.environment_requests = None
        run_context.thread_environments = owner
        if runtime is not None:
            runtime.thread_environments = owner
            runtime.retain_turn(asyncio.current_task(), run_context.lifecycle_cleanup_tasks)
            runtime.mcp_app_runtime = run_context.mcp_app_runtime
        elif agent_session is not None:
            agent_session.thread_environments = owner
            agent_session.owns_thread_environments = not run_context.retain_thread_environments
        if run_context.inherited_turn_environments is not None:
            await run_context.inherited_turn_environments.aclose()
            run_context.inherited_turn_environments = None
        return await run_context.capture_turn_environments()


async def refresh_run_environment_capabilities(run_context, *, permission_context, config_stack,
        feature_flags, registry, artifact_store, workspace_root, conversation_id: str,
        turn_id: str, agent_session=None):
    if run_context.environment_manager is None:
        return None
    snapshot = await prepare_run_environments(run_context, agent_session=agent_session,
        permission_context=permission_context, config_stack=config_stack,
        workspace_root=workspace_root, conversation_id=conversation_id)
    from backend.mcp.environment_runtime import prepare_environment_capabilities

    previous = run_context.environment_capabilities
    prepared = await prepare_environment_capabilities(turn_environments=snapshot,
        environment_manager=run_context.environment_manager,
        host_mcp_manager=run_context.environment_host_mcp_manager,
        host_skill_manager=run_context.environment_host_skill_manager,
        feature_flags=feature_flags, thread_id=conversation_id, turn_id=turn_id,
        session_source=run_context.session_source, isolated=run_context.isolated,
        is_subagent=run_context.is_subagent, local_cwd=Path(workspace_root or Path.cwd()).as_uri(),
        selected_capability_roots=snapshot.selected_capability_roots, previous=previous,
        restriction_product="codex")
    try:
        prepared.validate_required_skills()
        prepared.bind_registry(registry, artifact_store)
    except BaseException:
        await prepared.aclose()
        raise
    run_context.environment_capabilities = prepared
    run_context.mcp_manager = prepared.mcp_manager
    run_context.skill_manager = prepared.skill_manager
    if previous is not None:
        run_context.environment_retirement_tasks.add(previous.retire(run_context))
    return prepared
