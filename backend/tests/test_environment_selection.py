"""Thread authority and original leases through the real executor transport."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import sys

import pytest
import pytest_asyncio

from backend.agent.environment_selection import (
    EnvironmentConfig, EnvironmentConfigState, FailedTurnEnvironment,
    ThreadEnvironmentDefaults, ThreadEnvironments, TurnEnvironmentRequest,
    TurnEnvironmentSelection, default_thread_environment_requests,
)
from backend.environments.manager import EnvironmentManager
from backend.permissions.context import PermissionContext
from backend.sandbox.policy import FileSystemSandboxPolicy, PermissionProfile
from backend.tests.test_environments import PEER, configure


async def capture_config(selection, config, environment, peer_info):
    assert environment.client.connection.connected and environment.client.session_id == "owned-stdio-session"
    return config


class Case:
    def __init__(self, manager, cwd):
        self.manager, self.cwd = manager, cwd
        self.owners, self.snapshots = [], []
        self.defaults = ThreadEnvironmentDefaults(
            EnvironmentConfig(PermissionProfile.disabled(), (cwd,)), "disabled")

    def owner(self, *, defaults=None, inherited=None, resolver=capture_config):
        owner = ThreadEnvironments(self.manager, defaults or self.defaults,
            resolve_config=resolver, inherited=inherited)
        self.owners.append(owner)
        return owner

    def selection(self, name="test", *, config=None, cwd=None):
        path = cwd or self.cwd
        return TurnEnvironmentSelection.from_request(TurnEnvironmentRequest(name, path, (path,),
            config or EnvironmentConfigState.from_thread()))

    async def snapshot(self, owner):
        snapshot = await owner.snapshot()
        self.snapshots.append(snapshot)
        return snapshot


@pytest_asyncio.fixture
async def environment_case(tmp_path):
    entries = [{"id": name, "program": sys.executable, "args": [str(PEER)],
        "cwd": str(tmp_path), "skills": {"required": [f"{name}-required"]}}
        for name in ("test", "other")]
    configure(tmp_path, entries, default="other")
    case = Case(EnvironmentManager(tmp_path), tmp_path.as_uri())
    try:
        yield case
    finally:
        for owner in case.owners:
            await owner.aclose()
        for snapshot in case.snapshots:
            await snapshot.aclose()
        await case.manager.close()


@pytest.mark.asyncio
async def test_default_order_captures_all_actual_executors_without_host_metadata(environment_case):
    case = environment_case
    requests = default_thread_environment_requests(case.manager, case.cwd, (case.cwd,))
    assert [item.environment_id for item in requests] == ["other", "test"]
    owner = case.owner()
    await owner.update_selections(TurnEnvironmentSelection.from_request(item) for item in requests)
    snapshot = await case.snapshot(owner)
    assert [item.selection.environment_id for item in snapshot.turn_environments()] == ["other", "test"]
    assert [item.config.kind for item in snapshot.all_selections()] == ["FromThread", "FromThread"]
    assert [item.skill_names for item in snapshot.required_skills()] == [("other-required",), ("test-required",)]
    ready = snapshot.primary()
    assert ready.peer_info["providerId"] == "owned-stdio-test-peer"
    assert ready.permission_scope.platform_os is None and ready.permission_scope.user_home_uri is None
    assert ready.permission_scope.sandbox_context()["permissions"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_pending_primary_roots_and_failed_requirements_do_not_authorize_full_access(environment_case):
    case = environment_case
    pending_cwd = case.cwd + "/pending"
    owner = case.owner()
    await owner.update_selections((case.selection(config=EnvironmentConfigState.pending(), cwd=pending_cwd),
        case.selection("other")))
    captured = await case.snapshot(owner)
    assert captured.primary().selection.environment_id == "other"
    assert captured.primary_workspace_root_uris() == (pending_cwd,)
    assert not captured.has_full_access("never", PermissionProfile.disabled())
    assert len(captured.starting()) == 1
    await owner.environment_failed(case.selection(cwd=pending_cwd), "Owner rejected this attachment")
    failed = await case.snapshot(owner)
    assert isinstance(failed.environments[0], FailedTurnEnvironment)
    assert failed.environments[0].error == "Owner rejected this attachment"
    assert len(failed.required_skills()) == 2 and len(failed.inheritable_selections()) == 1
    assert not failed.has_full_access("never", PermissionProfile.disabled())


@pytest.mark.asyncio
async def test_same_id_replacement_and_child_keep_the_original_object_until_last_lease(environment_case):
    case = environment_case
    owner = case.owner()
    await owner.update_selections((case.selection(),))
    captured = await case.snapshot(owner)
    original = captured.primary().environment
    replacement = case.manager.upsert({"id": "test", "program": "unavailable-replacement-program"})
    child = case.owner(inherited=captured)
    child_snapshot = await case.snapshot(child)
    assert child_snapshot.primary().environment is original
    assert replacement is not original and not original.closed
    await owner.aclose()
    await captured.aclose()
    assert not original.closed
    await child.aclose()
    assert not original.closed
    await child_snapshot.aclose()
    assert original.closed
    assert original.client.connection.transport.child.returncode == 0
    assert replacement.client.connection is None


@pytest.mark.asyncio
async def test_child_reinfers_thread_config_but_keeps_owner_config(environment_case):
    case = environment_case
    owner_config = replace(case.defaults.common, allow_login_shell=False)
    owner = case.owner()
    await owner.update_selections((case.selection(), case.selection("other", config=EnvironmentConfigState.ready(owner_config))))
    parent = await case.snapshot(owner)
    child_profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted(()))
    child_defaults = replace(case.defaults, common=replace(case.defaults.common, permission_profile=child_profile))
    child = case.owner(defaults=child_defaults, inherited=parent)
    snapshot = await case.snapshot(child)
    assert snapshot.turn_environments()[0].config.permission_profile == child_profile
    assert snapshot.turn_environments()[0].config_origin == "Thread"
    assert snapshot.turn_environments()[1].config == owner_config
    assert snapshot.turn_environments()[1].config_origin == "Owner"
    assert not snapshot.has_full_access("never", PermissionProfile.disabled())


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_failure", [False, True])
async def test_child_follows_original_pending_result_through_its_receiving_owner(environment_case, owner_failure):
    case = environment_case
    owner = case.owner()
    selection = case.selection(config=EnvironmentConfigState.pending())
    await owner.update_selections((selection,))
    parent = await case.snapshot(owner)
    child = case.owner(inherited=parent)
    before = await case.snapshot(child)
    assert before.starting()[0].selection.config.kind == "Pending"
    if owner_failure:
        await owner.environment_failed(selection, "The original owner could not supply config")
    else:
        await owner.environment_ready(selection, case.defaults.common)
    async with asyncio.timeout(5):
        while child.selections()[0].config.kind == "Pending":
            await asyncio.sleep(0)
    after = await case.snapshot(child)
    if owner_failure:
        assert isinstance(after.environments[0], FailedTurnEnvironment)
        assert after.environments[0].error == "The original owner could not supply config"
    else:
        assert after.primary().environment is parent.starting()[0].environment
        assert after.primary().config_origin == "Owner"
        assert after.primary().config == case.defaults.common
    await asyncio.gather(*(item.resolution for item in before.starting()), return_exceptions=True)
    refreshed = before.refresh_readiness()
    assert isinstance(refreshed.environments[0], FailedTurnEnvironment) == owner_failure


@pytest.mark.asyncio
async def test_old_snapshot_only_advances_its_startup_and_does_not_adopt_new_selection(environment_case):
    case = environment_case
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting_config(selection, config, environment, peer_info):
        entered.set()
        await release.wait()
        return config

    owner = case.owner(resolver=waiting_config)
    await owner.update_selections((case.selection(),))
    old = owner.snapshot_now()
    case.snapshots.append(old)
    await entered.wait()
    owner.resolve_config = capture_config
    await owner.update_selections((case.selection("other"),))
    current = await case.snapshot(owner)
    release.set()
    await old.starting()[0].wait_until_ready()
    refreshed = old.refresh_readiness()
    assert refreshed.primary().selection.environment_id == "test"
    assert current.primary().selection.environment_id == "other"
    assert refreshed.primary().environment is not current.primary().environment


@pytest.mark.asyncio
async def test_configuration_updates_do_not_widen_owner_authority_or_accept_stale_workspace(environment_case):
    case = environment_case
    owner = case.owner()
    selected = case.selection(config=EnvironmentConfigState.ready(case.defaults.common))
    await owner.update_selections((selected,))
    with pytest.raises(ValueError, match="owner-provided"):
        await owner.update_selections((case.selection(),))
    with pytest.raises(ValueError, match="requested workspace"):
        await owner.environment_ready(case.selection(cwd=case.cwd + "/other-workspace"), case.defaults.common)
    roots = ({"id": "wrong-root", "location": {"type": "environment", "environmentId": "other", "path": case.cwd}},)
    with pytest.raises(ValueError, match="belong to environment"):
        await owner.environment_ready(selected, replace(case.defaults.common, selected_capability_roots=roots))
    assert owner.selections() == (selected,)


@pytest.mark.asyncio
async def test_failed_connection_retains_selection_and_required_skill_with_no_local_fallback(environment_case):
    case = environment_case
    case.manager.upsert({"id": "test", "program": "unavailable-original-executor", "skills": {"required": ["must-remain-required"]}})
    owner = case.owner()
    await owner.update_selections((case.selection(),))
    snapshot = await case.snapshot(owner)
    assert snapshot.primary() is None and snapshot.local() is None
    assert snapshot.all_selections()[0].config.kind == "FromThread"
    assert snapshot.required_skills()[0].skill_names == ("must-remain-required",)
    assert snapshot.environments[0].error == str(case.manager.get_environment("test").client.failure)
    assert not snapshot.has_full_access("never", PermissionProfile.disabled())


@pytest.mark.asyncio
async def test_owner_roots_and_thread_profile_are_frozen_at_the_actual_step(environment_case):
    case = environment_case
    roots = ({"id": "selected-root", "location": {"type": "environment", "environmentId": "other", "path": case.cwd}},)
    owner_config = replace(case.defaults.common, selected_capability_roots=roots)
    owner = case.owner()
    await owner.update_selections((case.selection(), case.selection("other", config=EnvironmentConfigState.ready(owner_config))))
    before = await case.snapshot(owner)
    narrow = PermissionProfile.managed(FileSystemSandboxPolicy.restricted(()))
    await owner.set_active_thread_defaults(replace(case.defaults, common=replace(case.defaults.common, permission_profile=narrow)))
    after = await case.snapshot(owner)
    assert before.turn_environments()[1].config.selected_capability_roots == roots
    assert after.turn_environments()[1].config.selected_capability_roots == roots
    assert after.primary().config.selected_capability_roots == ()
    assert before.primary().environment.selected_capability_roots() == ()
    assert before.primary().permission_scope.permission_profile.enforcement.value == "disabled"
    assert after.primary().permission_scope.permission_profile == narrow
    permission = PermissionContext(environment_scope=before.primary().permission_scope)
    assert permission.environment_scope.environment_id == "test"


@pytest.mark.asyncio
async def test_new_pending_cycle_does_not_reuse_the_first_owner_configuration(environment_case):
    case = environment_case
    owner = case.owner()
    selected = case.selection(config=EnvironmentConfigState.pending())
    await owner.update_selections((selected,))
    await owner.environment_ready(selected, case.defaults.common)
    first = await case.snapshot(owner)
    await owner.update_selections((selected,))
    pending = await case.snapshot(owner)
    assert pending.primary() is None and len(pending.starting()) == 1
    child = case.owner(inherited=pending)
    child_before = await case.snapshot(child)
    assert child_before.primary() is None
    narrowed = replace(case.defaults.common, allow_login_shell=False)
    await owner.environment_ready(selected, narrowed)
    async with asyncio.timeout(5):
        while child.selections()[0].config.kind == "Pending":
            await asyncio.sleep(0)
    child_after = await case.snapshot(child)
    assert child_after.primary().config == narrowed
    assert first.primary().config.allow_login_shell


@pytest.mark.asyncio
async def test_failed_snapshot_keeps_evidence_without_retaining_a_retired_executor(environment_case):
    case = environment_case
    original = case.manager.upsert({"id": "test", "program": "unavailable-failed-binding"})
    owner = case.owner()
    await owner.update_selections((case.selection(),))
    failed = await case.snapshot(owner)
    assert isinstance(failed.environments[0], FailedTurnEnvironment)
    case.manager.upsert({"id": "test", "program": sys.executable, "args": [str(PEER)]})
    await owner.update_selections((case.selection(),))
    ready = await case.snapshot(owner)
    assert ready.primary().environment is not original
    assert original.closed
    assert failed.all_selections()[0].config.kind == "FromThread"


@pytest.mark.asyncio
async def test_snapshot_clone_is_an_independent_lease_while_readiness_view_is_same_capture(environment_case):
    case = environment_case
    owner = case.owner()
    await owner.update_selections((case.selection(),))
    snapshot = await case.snapshot(owner)
    original = snapshot.primary().environment
    cloned = snapshot.clone()
    case.snapshots.append(cloned)
    view = snapshot.refresh_readiness()
    assert original.borrowers == 3  # thread + original capture + cloned capture
    case.manager.upsert({"id": "test", "program": "unused-replacement"})
    await owner.aclose()
    await view.aclose()
    assert not original.closed and cloned.primary().environment is original
    await snapshot.aclose()
    assert original.borrowers == 1
    await cloned.aclose()
    assert original.closed


@pytest.mark.asyncio
async def test_target_config_read_error_is_a_failed_attachment_with_its_real_evidence(environment_case):
    case = environment_case

    async def read_target_config(selection, config, environment, peer_info):
        await environment.filesystem.read_file(selection.cwd + "/missing-target-config",
            sandbox={"permissions": {"type": "disabled"}, "cwd": selection.cwd,
                "workspaceRoots": list(config.workspace_roots), "windowsSandboxLevel": "disabled",
                "useLegacyLandlock": False})
        return config

    owner = case.owner(resolver=read_target_config)
    await owner.update_selections((case.selection(),))
    snapshot = await case.snapshot(owner)
    assert isinstance(snapshot.environments[0], FailedTurnEnvironment)
    assert "missing-target-config" in snapshot.environments[0].error
    assert snapshot.required_skills()[0].skill_names == ("test-required",)
    assert not snapshot.has_full_access("never", PermissionProfile.disabled())


@pytest.mark.asyncio
async def test_configured_none_has_no_implicit_local_attachment(tmp_path):
    configure(tmp_path, [{"id": "test", "program": "unstarted-executor"}], default="none")
    async with EnvironmentManager(tmp_path) as manager:
        cwd = tmp_path.as_uri()
        defaults = ThreadEnvironmentDefaults(EnvironmentConfig(PermissionProfile.disabled(), (cwd,)), "disabled")
        async with ThreadEnvironments(manager, defaults, resolve_config=capture_config) as owner:
            requests = default_thread_environment_requests(manager, cwd, (cwd,))
            assert requests == ()
            await owner.update_selections(())
            async with await owner.snapshot() as snapshot:
                assert snapshot.environments == () and snapshot.local() is None
                assert snapshot.has_full_access("never", PermissionProfile.disabled())
                assert not snapshot.has_full_access("on-request", PermissionProfile.disabled())
                assert manager.get_environment("test").client.connection is None


@pytest.mark.asyncio
@pytest.mark.parametrize("rules", [{}, {"prefix_rules": []},
    {"prefix_rules": [{"pattern": [{"token": "git"}], "decision": "allow"}]}])
async def test_owner_ready_exec_rules_are_rejected_by_the_actual_source_requirements_parser(environment_case, rules):
    from backend.permissions.native_execpolicy import NativeExecPolicyError, NativeExecPolicyProcessError

    case = environment_case
    owner = case.owner()
    configured = replace(case.defaults.common, exec_policy=rules)
    with pytest.raises((NativeExecPolicyError, NativeExecPolicyProcessError)):
        await owner.update_selections((case.selection(config=EnvironmentConfigState.ready(configured)),))
    assert owner.selections() == ()
    assert case.manager.get_environment("test").client.connection is None


@pytest.mark.asyncio
async def test_owner_ready_retains_exact_requirements_shape_and_thread_draft_has_none(environment_case):
    from backend.agent.environment_runtime import thread_environment_defaults
    from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack

    case = environment_case
    owner = case.owner()
    rules = {"prefix_rules": [{"pattern": [{"token": "git"}], "decision": "forbidden"}]}
    configured = replace(case.defaults.common, exec_policy=rules)
    await owner.update_selections((case.selection(config=EnvironmentConfigState.ready(configured)),))
    snapshot = await case.snapshot(owner)
    assert snapshot.primary().config.exec_policy == rules
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file="owned-config.json"),
        {"exec_policy": {"prefix_rules": [{"decision": "allow"}]}}),))
    assert thread_environment_defaults(PermissionContext(), stack).common.exec_policy is None
