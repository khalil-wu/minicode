from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from backend.config import PermissionSettings, permission_settings_from_config
from backend.artifact.store import ArtifactStore
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.grants import (
    PermissionGrantResponse, PermissionGrantStore, current_granted_permissions,
    intersect_permission_profiles, merge_permission_profiles,
    normalize_permission_profile, normalize_permission_response,
    permission_context_with_grants, permission_profile_allows_path,
    permission_profile_payload, sandbox_policy_with_grants,
)
from backend.sandbox.policy import (
    AdditionalPermissionProfile, FileSystemAccessMode, FileSystemPath,
    FileSystemPermissions, FileSystemSandboxEntry, FileSystemSandboxPolicy,
    NetworkPermissions, PermissionProfile, SandboxPolicy,
)
from backend.sandbox.result import SandboxResult
from backend.tools.apply_patch import ApplyPatchTool
from backend.tools.base import PermissionLevel
from backend.tools.command_tool import RunCommandTool
from backend.tools.request_permissions import RequestPermissionsTool
from backend.tools.write_file import WriteFileTool


def profile(tmp_path, payload):
    return normalize_permission_profile(payload, cwd=tmp_path, workspace_roots=(tmp_path,))


def entry(path, access):
    return {"path": {"type": "path", "path": str(path)}, "access": access}


def context_for(tmp_path, **kwargs):
    return ToolExecutionContext(PermissionContext(mode="confirm", workspace_root=tmp_path),
        workspace_root=tmp_path, conversation_id="chat", session_id="session",
        turn_id="turn", tool_call_id="permission-call", **kwargs)


def test_relative_paths_normalize_lexically_and_detach_request(tmp_path):
    payload = {"network": {"enabled": True}, "file_system": {"write": ["../outside", "../outside"], "read": ["reader"]}}
    normalized = profile(tmp_path, payload)
    payload["file_system"]["write"].append("../unapproved")
    assert permission_profile_payload(normalized) == {"network": {"enabled": True}, "file_system": {
        "read": [str(tmp_path / "reader")], "write": [str(tmp_path.parent / "outside")],
    }}


@pytest.mark.parametrize("requested,granted,expected", [(True, True, True), (True, False, False), (False, True, False)])
def test_network_intersection_is_grant_only(tmp_path, requested, granted, expected):
    result = intersect_permission_profiles(profile(tmp_path, {"network": {"enabled": requested}}),
        profile(tmp_path, {"network": {"enabled": granted}}), cwd=tmp_path)
    assert (result.network is not None) == expected


def test_client_cannot_expand_path_or_read_access(tmp_path):
    requested = profile(tmp_path, {"file_system": {"read": ["reader"], "write": ["writer/child"]}})
    granted = profile(tmp_path, {"file_system": {"write": ["reader", "writer", "writer/child/narrow", "../outside"], "read": ["reader/sub"]}})
    accepted = permission_profile_payload(intersect_permission_profiles(requested, granted, cwd=tmp_path))
    assert accepted["file_system"] == {"read": [str(tmp_path / "reader/sub")], "write": [str(tmp_path / "writer/child/narrow")]}


def test_child_deny_survives_parent_path_grant_and_relative_glob(tmp_path):
    requested = profile(tmp_path, {"file_system": {"entries": [entry(tmp_path / "granted", "write"), entry(tmp_path / "granted/secret", "deny"),
        {"path": {"type": "glob_pattern", "pattern": "granted/*.key"}, "access": "deny"}], "glob_scan_max_depth": 2}})
    granted = profile(tmp_path, {"file_system": {"write": ["granted"]}})
    accepted = intersect_permission_profiles(requested, granted, cwd=tmp_path)
    assert not permission_profile_allows_path(accepted, tmp_path / "granted/secret/file", cwd=tmp_path, workspace_root=tmp_path)
    assert not permission_profile_allows_path(accepted, tmp_path / "granted/token.key", cwd=tmp_path, workspace_root=tmp_path)
    assert permission_profile_allows_path(accepted, tmp_path / "granted/code.py", cwd=tmp_path, workspace_root=tmp_path, write=True)
    assert accepted.file_system.glob_scan_max_depth == 2


def test_symbolic_project_roots_materialize_for_session_reuse(tmp_path):
    requested = profile(tmp_path, {"file_system": {"entries": [{"path": {"type": "special", "value": {"kind": "project_roots", "subpath": "child"}}, "access": "write"}]}})
    granted = profile(tmp_path, {"file_system": {"write": ["child/narrow"]}})
    accepted = intersect_permission_profiles(requested, granted, cwd=tmp_path, workspace_roots=(tmp_path,))
    assert accepted.file_system.entries == (FileSystemSandboxEntry(FileSystemPath.path(tmp_path / "child/narrow"), FileSystemAccessMode.WRITE),)


def test_symbolic_broader_grant_is_not_clipped_to_requested_child(tmp_path):
    requested = profile(tmp_path, {"file_system": {"write": ["child"]}})
    granted = profile(tmp_path, {"file_system": {"entries": [{"path": {"type": "special", "value": {"kind": "project_roots"}}, "access": "write"}]}})
    assert intersect_permission_profiles(requested, granted, cwd=tmp_path, workspace_roots=(tmp_path,)).is_empty


def test_tmpdir_permission_uses_captured_environment_roots_without_system_fallback(tmp_path):
    symbolic = profile(tmp_path, {"file_system": {"entries": [{"path": {"type": "special", "value": {"kind": "tmpdir"}}, "access": "write"}]}})
    granted = profile(tmp_path, {"file_system": {"write": ["first", "second", "unlisted"]}})
    accepted = intersect_permission_profiles(symbolic, granted, cwd=tmp_path,
        temporary_directories=(tmp_path / "first", tmp_path / "second"))
    assert permission_profile_payload(accepted)["file_system"]["write"] == [str(tmp_path / "first"), str(tmp_path / "second")]
    assert intersect_permission_profiles(symbolic, granted, cwd=tmp_path, temporary_directories=()).is_empty


def test_globs_cannot_grant_read_or_write(tmp_path):
    with pytest.raises(ValueError, match="glob entries may only deny"):
        profile(tmp_path, {"file_system": {"entries": [{"path": {"type": "glob_pattern", "pattern": "**/*"}, "access": "write"}]}})


def test_denies_without_accepted_grants_are_dropped(tmp_path):
    req = profile(tmp_path, {"file_system": {"entries": [entry(tmp_path / "allowed", "write"), entry(tmp_path / "secret", "deny")]}})
    grant = profile(tmp_path, {"file_system": {"entries": [entry(tmp_path / "outside", "write"), entry(tmp_path / "secret", "deny")]}})
    assert intersect_permission_profiles(req, grant, cwd=tmp_path).is_empty


def test_request_denied_parent_cannot_be_reopened_by_narrower_entry(tmp_path):
    req = profile(tmp_path, {"file_system": {"entries": [entry(tmp_path, "write"), entry(tmp_path / "secret", "deny"), entry(tmp_path / "secret/public", "write")]}})
    granted = profile(tmp_path, {"file_system": {"write": ["secret/public"]}})
    assert intersect_permission_profiles(req, granted, cwd=tmp_path).is_empty


def test_subset_never_inspects_or_substitutes_symlink_target(tmp_path, monkeypatch):
    req = profile(tmp_path, {"file_system": {"write": ["link"]}})
    granted = profile(tmp_path, {"file_system": {"write": ["target"]}})
    with monkeypatch.context() as lexical:
        lexical.setattr(Path, "resolve", lambda *args, **kwargs: pytest.fail("Source subset normalization is lexical"))
        assert intersect_permission_profiles(req, granted, cwd=tmp_path).is_empty
        accepted = intersect_permission_profiles(req, profile(tmp_path, {"file_system": {"write": ["link/child"]}}), cwd=tmp_path)
        assert accepted.file_system.entries[0].path.value == tmp_path / "link/child"


@pytest.mark.parametrize("pattern,target,expected", [
    ("*.key", "child/token.key", False),
    ("*.key", "token.key", True),
    ("**/*.key", "token.key", True),
    ("**/*.key", "child/deeper/token.key", True),
    ("**/*.{key,pem}", "child/token.pem", True),
    ("broken[", "broken[", True),
    ("broken[", "other", False),
    ("*{,.key}", "code", False),
    ("*{,.key}", "code.key", True),
])
def test_source_deny_glob_separator_recursive_and_literal_class(tmp_path, pattern, target, expected):
    req = profile(tmp_path, {"file_system": {"entries": [entry(tmp_path, "write"), {"path": {"type": "glob_pattern", "pattern": pattern}, "access": "deny"}]}})
    grant = profile(tmp_path, {"file_system": {"write": [target]}})
    assert intersect_permission_profiles(req, grant, cwd=tmp_path).is_empty == expected


@pytest.mark.parametrize("pattern", ["{unclosed", "[z-a]"])
def test_invalid_deny_glob_uses_source_read_deny_fail_closed_semantics(tmp_path, pattern):
    req = profile(tmp_path, {"file_system": {"entries": [entry(tmp_path, "write"), {"path": {"type": "glob_pattern", "pattern": pattern}, "access": "deny"}]}})
    assert intersect_permission_profiles(req, profile(tmp_path, {"file_system": {"write": ["code"]}}), cwd=tmp_path).is_empty


def test_strict_review_cannot_create_a_session_grant(tmp_path):
    req = profile(tmp_path, {"network": {"enabled": True}})
    response = normalize_permission_response(req, {"permissions": {"network": {"enabled": True}}, "scope": "session", "strict_auto_review": True}, cwd=tmp_path)
    assert response == PermissionGrantResponse()


def test_session_turn_environment_lifetime_and_strict_state(tmp_path):
    store = PermissionGrantStore()
    network = profile(tmp_path, {"network": {"enabled": True}})
    files = profile(tmp_path, {"file_system": {"write": ["writer"]}})
    store.begin_turn("one")
    store.record(PermissionGrantResponse(network, "session"), "local", "one")
    store.record(PermissionGrantResponse(files, "turn", True), "local", "one")
    store.record(PermissionGrantResponse(files, "session"), "remote", "one")
    assert store.granted("local", "one") == merge_permission_profiles(network, files)
    assert store.granted("remote", "one") == files
    assert store.strict_auto_review_enabled("one")
    store.end_turn("one")
    assert store.granted("local", "two") == network
    assert not store.strict_auto_review_enabled("one")
    assert store.granted("missing", "two").is_empty
    assert PermissionGrantStore().granted("local", "one").is_empty


@pytest.mark.parametrize("policy,granular", [("never", None), ("granular", {"sandbox_approval": True, "rules": True, "mcp_elicitations": True, "request_permissions": False})])
def test_source_policy_cannot_ask_returns_empty_without_emitting(tmp_path, policy, granular):
    async def scenario():
        async def unexpected(*args):
            pytest.fail("policy cannot emit an approval request")
        ctx = context_for(tmp_path, emit_event=unexpected, approval_handler=unexpected)
        ctx.permission = replace(ctx.permission, approval_policy=policy, granular_approval=granular)
        result = await RequestPermissionsTool().execute({"permissions": {"network": {"enabled": True}}}, ctx)
        assert json.loads(result.content) == {"permissions": {"network": None, "file_system": None}, "scope": "turn"}
        assert current_granted_permissions(ctx).is_empty
    asyncio.run(scenario())


def test_approval_subset_is_frozen_and_consumed_by_real_patch(tmp_path):
    async def scenario():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        accepted_root = tmp_path / "accepted"
        events = []
        async def emit(event_type, data):
            events.append((event_type, data))
            # This transport object is not the authority held by the request.
            data["permissions"]["file_system"]["write"].append(str(tmp_path / "injected"))
        async def approve(call_id):
            assert call_id == "permission-call"
            return {"action": "approve", "permissions": {"file_system": {"write": [str(accepted_root), str(tmp_path / "injected")]}}, "scope": "turn"}
        ctx = context_for(workspace, emit_event=emit, approval_handler=approve)
        result = await RequestPermissionsTool().execute({"permissions": {"file_system": {"write": ["../accepted"]}}}, ctx)
        assert not result.is_error
        assert events[0][1]["subtype"] == "request_permissions"
        assert events[0][1]["environment_id"] == "local"
        assert json.loads(result.content)["permissions"]["file_system"]["write"] == [str(accepted_root)]
        ctx.permission = permission_context_with_grants(ctx.permission, ctx)
        patch_args = {"patch": "*** Begin Patch\n*** Add File: ../accepted/code.py\n+print('approved')\n*** End Patch"}
        checker = PermissionChecker(permission_settings_from_config({}), workspace)
        assert checker.evaluate("apply_patch", patch_args, context=ctx.permission, tool=ApplyPatchTool()).decision == "allow"
        ctx.emit_event = None
        applied = await ApplyPatchTool().execute(patch_args, ctx)
        assert not applied.is_error, applied.content
        assert (accepted_root / "code.py").read_text() == "print('approved')\n"
        ctx.permission_grants.end_turn("turn")
        denied = await ApplyPatchTool().execute({"patch": "*** Begin Patch\n*** Add File: ../accepted/later.py\n+unapproved\n*** End Patch"}, ctx)
        assert denied.is_error and not (accepted_root / "later.py").exists()
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["reject", "cancel"])
def test_denial_and_cancellation_do_not_record_grants(tmp_path, action):
    async def scenario():
        async def emit(*args):
            pass
        async def answer(call_id):
            return {"action": action, "permissions": {"network": {"enabled": True}}, "scope": "session"}
        ctx = context_for(tmp_path, emit_event=emit, approval_handler=answer)
        result = await RequestPermissionsTool().execute({"permissions": {"network": {"enabled": True}}}, ctx)
        assert current_granted_permissions(ctx).is_empty
        assert result.is_error == (action == "cancel")
    asyncio.run(scenario())


def test_interruption_cancels_existing_waiter_and_cannot_record_late_response(tmp_path):
    async def scenario():
        cancel = asyncio.Event()
        started = asyncio.Event()
        ended = asyncio.Event()
        async def emit(*args):
            pass
        async def await_answer(call_id):
            started.set()
            try:
                await asyncio.Future()
            finally:
                ended.set()
        ctx = context_for(tmp_path, emit_event=emit, approval_handler=await_answer, cancel_event=cancel)
        task = asyncio.create_task(RequestPermissionsTool().execute({"permissions": {"network": {"enabled": True}}}, ctx))
        await started.wait()
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ended.is_set() and current_granted_permissions(ctx).is_empty
    asyncio.run(scenario())


def test_unknown_environment_cannot_borrow_primary_authority(tmp_path):
    result = asyncio.run(RequestPermissionsTool().execute({"environment_id": "remote", "permissions": {"network": {"enabled": True}}}, context_for(tmp_path)))
    assert result.is_error and "Unknown environment id" in result.content


def test_user_explicit_static_rules_survive_identical_default_routing(tmp_path):
    ctx = context_for(tmp_path)
    ctx.permission_grants.record(PermissionGrantResponse(profile(tmp_path, {"network": {"enabled": True}})), "local", "turn")
    permission = permission_context_with_grants(ctx.permission, ctx)
    command = RunCommandTool(None)
    args = {"command": "echo approved"}
    builtin = PermissionChecker(permission_settings_from_config({}), tmp_path)
    explicit = PermissionChecker(permission_settings_from_config({"permissions": {"require_confirm": ["run_command"]}}), tmp_path)
    assert builtin.check("run_command", args, context=permission, tool=command) == PermissionLevel.AUTO
    assert explicit.check("run_command", args, context=permission, tool=command) == PermissionLevel.CONFIRM
    assert PermissionChecker(PermissionSettings(), tmp_path).check("run_command", args, context=permission, tool=command) == PermissionLevel.CONFIRM


def test_grants_do_not_cover_escalation_or_typed_file_tools(tmp_path):
    outside = tmp_path.parent / "granted-outside"
    grant = profile(tmp_path, {"file_system": {"write": [str(outside)]}})
    ctx = replace(context_for(tmp_path).permission, additional_permissions=grant)
    checker = PermissionChecker(permission_settings_from_config({}), tmp_path)
    assert checker.check("run_command", {"command": "echo permission", "with_escalated_permissions": True}, context=ctx, tool=RunCommandTool(None)) == PermissionLevel.CONFIRM
    typed = checker.evaluate("write_file", {"file_path": str(outside / "plain.py"), "content": "no"}, context=ctx, tool=WriteFileTool())
    assert not typed.capability_allowed


def test_patch_read_grant_does_not_authorize_outside_write(tmp_path):
    grant = profile(tmp_path, {"file_system": {"read": ["../readonly"]}})
    permission = replace(context_for(tmp_path).permission, additional_permissions=grant)
    args = {"patch": "*** Begin Patch\n*** Add File: ../readonly/a.py\n+unapproved\n*** End Patch"}
    checker = PermissionChecker(permission_settings_from_config({}), tmp_path)
    assert not checker.evaluate("apply_patch", args, context=permission, tool=ApplyPatchTool()).capability_allowed


def test_policy_overlay_preserves_denies_and_unbounded_glob_scan(tmp_path):
    deny = FileSystemSandboxEntry(FileSystemPath.glob(str(tmp_path / "*.key")), FileSystemAccessMode.DENY)
    base_fs = FileSystemSandboxPolicy.restricted((deny,), glob_scan_max_depth=2)
    base = SandboxPolicy(permission_profile=PermissionProfile.managed(base_fs))
    ctx = context_for(tmp_path)
    grant = AdditionalPermissionProfile(FileSystemPermissions((deny, FileSystemSandboxEntry(FileSystemPath.path(tmp_path), FileSystemAccessMode.WRITE)), None), NetworkPermissions(True))
    ctx.permission_grants.record(PermissionGrantResponse(grant), "local", "turn")
    effective = sandbox_policy_with_grants(base, ctx)
    assert effective.permission_profile.file_system.glob_scan_max_depth is None
    assert effective.permission_profile.file_system.entries.count(deny) == 1
    assert effective.resolve(cwd=tmp_path).allow_network
    assert not effective.resolve(cwd=tmp_path).resolve_access(tmp_path / "token.key").can_read


@pytest.mark.parametrize("exit_code", [0, 3])
def test_foreground_hook_gets_raw_actual_output_without_result_envelope(tmp_path, exit_code):
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    result = tool._foreground_result(SandboxResult("actual stdout\n", "actual stderr\n", exit_code),
        "echo observed", 10, context_for(tmp_path), SandboxPolicy.bypass(), False, {}, None)
    assert result.runtime_metadata["hook_output"] == "actual stdout\nactual stderr\n"
    assert "Exit code:" in result.content
    assert "Exit code:" not in result.runtime_metadata["hook_output"]


def test_foreground_hook_uses_same_model_output_truncation_preview(tmp_path):
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    result = tool._foreground_result(SandboxResult("BEGIN\n" + "data\n" * 80 + "END\n", "", 0),
        "produce long output", 10, context_for(tmp_path), SandboxPolicy.bypass(), False, {}, 50)
    assert result.runtime_metadata["hook_output"] == result.artifact_preview
    assert "Exit code:" not in result.runtime_metadata["hook_output"]
    assert "Output view limited" not in result.runtime_metadata["hook_output"]
