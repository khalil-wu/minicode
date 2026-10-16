"""Pinned PathUri vectors and captured executor permission projection."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.environments import EnvironmentManager
from backend.permissions.context import PermissionContext
from backend.permissions.environment_scope import (
    EnvironmentPermissionScope, capture_environment_permission_profile,
    executor_permission_profile, executor_windows_sandbox_selection,
    permission_profile_from_executor_wire,
)
from backend.sandbox.policy import (
    FileSystemAccessMode as Access, FileSystemPath, FileSystemSandboxEntry as Entry,
    FileSystemSandboxPolicy, FileSystemSpecialPath as Special, NetworkSandboxPolicy,
    PermissionProfile, SandboxEnforcement,
)
from backend.workspace.executor_paths import ExecutorPath


@pytest.mark.parametrize("base,value,expected", [
    ("file:///workspace/src", "../tests/test.rs", "file:///workspace/tests/test.rs"),
    ("file:///workspace/src", "/", "file:///"),
    ("file:///workspace/src", "../../../../tmp", "file:///tmp"),
    ("file:///workspace/src", "100%/a?#", "file:///workspace/src/100%25/a%3F%23"),
    ("file:///C:/workspace/src", r"..\tests/file.rs", "file:///C:/workspace/tests/file.rs"),
    ("file:///C:/workspace/src", r"../tests\file.rs", "file:///C:/workspace/tests/file.rs"),
    ("file:///C:/base/dir", r"\Windows", "file:///C:/Windows"),
    ("file://server/share/base/dir", r"\Windows", "file://server/share/Windows"),
    ("file:///C:/base/dir", r"..\..\..\Windows", "file:///C:/Windows"),
    ("file://server/share/base/dir", r"..\..\..\Windows", "file://server/share/Windows"),
    ("file:///C:/base", "c:tmp", "file:///C:/base/tmp"),
    ("file:///C%3a/base", "c:tmp", "file:///C%3a/base/tmp"),
    ("file:///C:/base/dir", r"C:..\tmp", "file:///C:/base/tmp"),
    ("file:///C:/base", "C:", "file:///C:/base"),
    ("file:///C:/base", "D:/tmp", "file:///D:/tmp"),
    ("file:///C:/base", "C:/", "file:///C:/"),
    ("file:///C:/", ".", "file:///C:"),
])
def test_source_join_vectors(base, value, expected):
    # Source uses the base URI convention, independently from the controller.
    actual = ExecutorPath.resolve(value, cwd_uri=base, platform_os="linux")
    assert actual.uri == expected


@pytest.mark.parametrize("value", ["D:tmp", "D:..\\tmp", "abc\0file"])
def test_other_drive_relative_and_null_retain_source_errors(value):
    with pytest.raises(ValueError):
        ExecutorPath.resolve(value, cwd_uri="file:///C:/base", platform_os="windows")


@pytest.mark.parametrize("target,root,contained", [
    ("file:///workspace/plugin/assets", "file:///workspace/plugin", True),
    ("file:///workspace/plugin-other/assets", "file:///workspace/plugin", False),
    ("file:///C:/PLUGINS/foo/assets", "file:///c:/plugins/foo", True),
    ("file:///C:/PLUGINS/%C3%84/assets", "file:///C:/plugins/%C3%A4", False),
    ("file:///C:/plugins/ǈ/assets", "file:///C:/plugins/Ǉ", False),
    ("file:///C:/project/secret", "file:///%63%3A/project", False),
    ("file:///C:/root/a%2Fb", "file:///C:/root", False),
    ("file:///C:/root/..%5Coutside", "file:///C:/root", False),
    ("file:///root/a%2Fb", "file:///root", False),
    ("file:///tmp/%FF/inside", "file:///tmp/%FF", True),
    ("file://localhost/workspace/src", "file:///workspace", True),
])
def test_source_segment_containment_vectors(target, root, contained):
    assert ExecutorPath.parse(target).is_relative_to(ExecutorPath.parse(root)) is contained


def test_uri_identity_ascii_only_and_lossless_posix_names():
    left = ExecutorPath.parse("file:///c:/ROOT/a%20file")
    right = ExecutorPath.parse("file:///C%3A/root/a file")
    assert left == right and hash(left) == hash(right)
    path = ExecutorPath.parse("file:///tmp/non-utf8-%FF")
    assert path.uri == "file:///tmp/non-utf8-%FF" and path.native_bytes() == b"/tmp/non-utf8-\xff"
    assert path.name == "non-utf8-%FF"
    assert ExecutorPath.from_native(b"/tmp/non-utf8-\xff", convention="posix") == path
    assert ExecutorPath.parse("file://localhost/workspace").uri == "file:///workspace"


@pytest.mark.parametrize("uri", ["file:///C:/", "file:///C:", "file://server/share", "file://server/share/", "file:///"])
def test_root_parent_and_legacy_native_are_stable(uri):
    root = ExecutorPath.parse(uri)
    assert root.parent == root
    if root.convention == "windows" and not uri.startswith("file://server"):
        assert root.native == "C:\\"


@pytest.mark.parametrize("native,convention", [("/C:/posix", "posix"), ("/tmp/a\0b", "posix"), ("C:\\a\ud800b", "windows")])
def test_unrepresentable_native_paths_preserve_source_opaque_bytes(native, convention):
    path = ExecutorPath.from_native(native, convention=convention)
    assert path.uri.startswith("file:///%00/bad/path/")
    assert path.convention == convention and path.parent == path
    with pytest.raises(ValueError, match="opaque"):
        path.joinpath("descendant")


def test_home_expansion_uses_executor_home_and_foreign_local_conversion_is_rejected():
    value = ExecutorPath.resolve("~/src", cwd_uri="file:///remote/project", platform_os="windows", user_home_uri="file:///remote/home")
    assert value.uri == "file:///remote/home/src"
    with pytest.raises(ValueError, match="home"):
        ExecutorPath.resolve("~/src", cwd_uri="file:///remote/project", platform_os="linux")
    foreign = "file:///remote/project" if os.name == "nt" else "file:///C:/project"
    with pytest.raises(ValueError, match="foreign"):
        ExecutorPath.parse(foreign).to_local_path()


@pytest.mark.parametrize("profile_type", ["disabled", "external", "restricted", "unrestricted"])
def test_source_permission_profile_wire_roundtrip(profile_type):
    source = {"type": profile_type} if profile_type == "disabled" else {"type": profile_type, "network": "restricted"}
    if profile_type in {"restricted", "unrestricted"}:
        fs = {"type": profile_type}
        if profile_type == "restricted":
            fs.update(entries=[
                {"path": {"type": "path", "path": "file:///C:/project"}, "access": "write", "missing_path_behavior": "skip"},
                {"path": {"type": "glob_pattern", "pattern": r"C:\project\*.secret"}, "access": "deny"},
                {"path": {"type": "special", "value": {"kind": "project_roots", "subpath": "src"}}, "access": "read"},
                {"path": {"type": "special", "value": {"kind": "unknown", "path": ":future", "subpath": "metadata"}}, "access": "read"},
            ], glob_scan_max_depth=6)
        source = {"type": "managed", "network": "restricted", "file_system": fs}
    assert executor_permission_profile(permission_profile_from_executor_wire(source)) == source


@pytest.mark.parametrize("cwd,level,backend,expected", [
    ("file:///C:/project", "disabled", "none", "restricted-token"),
    ("file:///C:/project", "elevated", "windowsRestrictedToken", "elevated"),
    ("file:///C:/project", "disabled", "windowsMxc", "mxc"),
    ("file:///remote/project", "elevated", "windowsMxc", "disabled"),
])
def test_source_windows_executor_selection(cwd, level, backend, expected):
    assert executor_windows_sandbox_selection(backend, level, cwd) == expected


@pytest.mark.parametrize("windows,slash_deny,other_deny,expected", [(False, False, False, True), (False, True, False, False), (True, True, False, True), (True, False, True, False)])
def test_full_read_fact_uses_selected_uri_convention(windows, slash_deny, other_deny, expected):
    entries = [Entry(FileSystemPath.special(Special.ROOT), Access.READ)]
    if slash_deny:
        entries.append(Entry(FileSystemPath.special(Special.SLASH_TMP), Access.DENY))
    if other_deny:
        entries.append(Entry(FileSystemPath.glob("*.secret"), Access.DENY))
    scope = EnvironmentPermissionScope("target", PermissionProfile.managed(FileSystemSandboxPolicy.restricted(entries)),
        "file:///C:/project" if windows else "file:///remote/project", (), None)
    assert scope.has_full_disk_read_access() is expected


@pytest.mark.parametrize("selector,legacy", [(FileSystemPath("path", "file:///remote/project"), False), (FileSystemPath.special(Special.PROJECT_ROOTS), True), (FileSystemPath.glob("*.secret"), True), (FileSystemPath.glob("/remote/*.secret"), False)])
def test_scope_preserves_new_policy_context_and_only_needed_legacy_fields(selector, legacy):
    profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted([Entry(selector, Access.DENY if selector.kind == "glob_pattern" else Access.READ)]))
    value = EnvironmentPermissionScope("target", profile, "file:///remote/project", ("file:///remote/project",), "linux").sandbox_context()
    assert value["policyContext"] == {"cwd": "file:///remote/project", "workspaceRoots": ["file:///remote/project"]}
    assert ("cwd" in value) is legacy and ("workspaceRoots" in value) is legacy


def config_stack(tmp_path, value=None):
    return ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "config.toml")), value or {}),))


class NoPolicyIO:
    async def walk(self, *args, **kwargs):
        pytest.fail("A fixed permission path must not scan the controller or executor")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,enforcement", [("confirm", SandboxEnforcement.MANAGED), ("plan", SandboxEnforcement.MANAGED), ("bypass", SandboxEnforcement.DISABLED)])
async def test_actual_thread_profile_capture_never_resolves_executor_paths_on_controller(tmp_path, monkeypatch, mode, enforcement):
    stack = config_stack(tmp_path)
    permission = PermissionContext(mode=mode)
    with monkeypatch.context() as guard:
        guard.setattr(Path, "resolve", lambda *_args, **_kwargs: pytest.fail("Controller Path.resolve used for target authority"))
        profile = await capture_environment_permission_profile(permission, stack, "file:///remote/project",
            ("file:///remote/project", "file:///remote/other"), "linux", NoPolicyIO())
    assert profile.enforcement is enforcement
    if mode == "confirm":
        assert any(entry.path.value is Special.PROJECT_ROOTS and entry.access is Access.WRITE for entry in profile.file_system.entries)
    if mode == "plan":
        assert not any(entry.access is Access.WRITE for entry in profile.file_system.entries)


@pytest.mark.asyncio
async def test_target_read_denies_and_home_are_captured_without_host_home(tmp_path):
    permission = PermissionContext(mode="confirm", filesystem_constraints={"allowlist": ["~/src"], "denylist": ["private/*.secret"]})
    profile = await capture_environment_permission_profile(permission, config_stack(tmp_path), "file:///remote/project",
        ("file:///remote/project",), "linux", NoPolicyIO(), user_home_uri="file:///remote/user")
    assert any(entry.path.value == "file:///remote/user/src" and entry.access is Access.WRITE for entry in profile.file_system.entries)
    assert any(entry.path.value == "/remote/project/private/*.secret" and entry.access is Access.DENY for entry in profile.file_system.entries)


@pytest.mark.asyncio
async def test_fine_grained_network_requirements_are_not_pretended_to_be_enforced(tmp_path):
    from backend.network.native import NativeNetworkRuntime
    from backend.network.policy import capture_thread_network_policy

    stack = config_stack(tmp_path, {"sandbox": {"enabled": True, "network": {"allowedDomains": ["example.invalid"]}}})
    profile = await capture_environment_permission_profile(PermissionContext(mode="confirm"), stack,
        "file:///remote/project", ("file:///remote/project",), "linux", NoPolicyIO())
    assert profile.enforcement is SandboxEnforcement.MANAGED
    assert profile.network is NetworkSandboxPolicy.RESTRICTED
    scope = EnvironmentPermissionScope("target", profile, "file:///remote/project", ("file:///remote/project",), "linux")
    assert scope.process_sandbox_context(enforce_managed_network=True) is not None
    draft = capture_thread_network_policy(stack)
    directory = os.environ.get("MINICODE_NETWORK_RUNTIME")
    native = NativeNetworkRuntime(directory=Path(directory) if directory else None)
    try:
        result = await native.inspect({"controller": draft.controller, "requirements": draft.requirements,
            "environmentPolicy": None, "permissionProfile": executor_permission_profile(profile),
            "platformOs": "linux", "windowsSandbox": "disabled", "execPolicySources": []})
        assert result["enabled"]
        assert result["effectivePolicy"]["requiresProxy"]
        assert result["effectivePolicy"]["domains"] == {"example.invalid": "allow"}
    finally:
        await native.aclose()


class GrantPolicyIO:
    def __init__(self, entries=(), *, metadata=None, error=None, truncated=False, errors=()):
        self.entries = tuple(entries)
        self.metadata = metadata
        self.error = error
        self.truncated = truncated
        self.errors = list(errors)
        self.calls = []

    async def walk(self, path, options, *, sandbox):
        self.calls.append(("walk", path, sandbox))
        assert options["followDirectorySymlinks"] is False
        if self.error is not None:
            raise self.error
        return {"entries": [{"path": path} for path in self.entries], "errors": self.errors, "truncated": self.truncated}

    async def get_metadata(self, path, *, sandbox):
        self.calls.append(("metadata", path, sandbox))
        if self.error is not None:
            raise self.error
        return self.metadata


async def captured_grants(tmp_path, cwd, pattern, filesystem):
    permission = PermissionContext(mode="confirm", filesystem_constraints={"allowlist": [pattern], "denylist": ["private/**"]})
    profile = await capture_environment_permission_profile(permission, config_stack(tmp_path), cwd, (cwd,),
        "linux", filesystem)
    return tuple(entry.path.value for entry in profile.file_system.entries
        if entry.path.kind == "path" and entry.access is Access.WRITE)


@pytest.mark.asyncio
@pytest.mark.parametrize("cwd,pattern,prefix,entries,expected", [
    ("file:///remote/project", "src/**/*.py", "file:///remote/project/src", (
        "file:///remote/project/src/a.py", "file:///remote/project/src/deep/nested/b.py",
        "file:///remote/project/src/no.txt", "file:///remote/project/src/.hidden/a.py",
        "file:///remote/project/src/.private.py"), (
        "file:///remote/project/src/a.py", "file:///remote/project/src/deep/nested/b.py")),
    ("file:///C:/workspace", r"C:\*.txt", "file:///C:", (
        "file:///C:/ONE.TXT", "file:///C:/workspace/a.txt", "file:///C:/.secret.txt"), ("file:///C:/ONE.TXT",)),
    ("file:///remote/project", "packages/*/src/**", "file:///remote/project/packages", (
        "file:///remote/project/packages/one/src", "file:///remote/project/packages/one/src/a.py",
        "file:///remote/project/packages/one/docs", "file:///remote/project/packages/.private/src"), (
        "file:///remote/project/packages/one/src", "file:///remote/project/packages/one/src/a.py")),
    ("file:///remote/project", "file:///remote/%FF/*.py", "file:///remote/%FF", (
        "file:///remote/%FF/a.py", "file:///remote/%EF%BF%BD/a.py"), ("file:///remote/%FF/a.py",)),
    ("file:///remote/project", "src/**/.config/[ab]?.py", "file:///remote/project/src", (
        "file:///remote/project/src/.config/a1.py", "file:///remote/project/src/deep/.config/b2.py",
        "file:///remote/project/src/.hidden/.config/a1.py", "file:///remote/project/src/.config/c3.py"), (
        "file:///remote/project/src/.config/a1.py", "file:///remote/project/src/deep/.config/b2.py")),
])
async def test_grant_globs_use_target_segments_and_never_promote_wildcard_static_prefix(tmp_path, cwd, pattern, prefix, entries, expected):
    filesystem = GrantPolicyIO(entries)
    grants = await captured_grants(tmp_path, cwd, pattern, filesystem)
    assert grants == expected
    assert len(filesystem.calls) == 1 and filesystem.calls[0][:2] == ("walk", prefix)
    sandbox = filesystem.calls[0][2]
    assert sandbox["policyContext"]["cwd"] == cwd
    assert any(entry["access"] == "deny" for entry in sandbox["permissions"]["file_system"]["entries"])


@pytest.mark.asyncio
@pytest.mark.parametrize("directory,expected", [(True, ("file:///C:",)), (False, ())])
async def test_concrete_subtree_grant_checks_target_metadata_without_walking_denied_children(tmp_path, directory, expected):
    filesystem = GrantPolicyIO(metadata={"isDirectory": directory})
    actual = await captured_grants(tmp_path, "file:///C:/workspace", "C:/**", filesystem)
    assert tuple(ExecutorPath.parse(uri) for uri in actual) == tuple(ExecutorPath.parse(uri) for uri in expected)
    assert filesystem.calls[0][0] == "metadata" and len(filesystem.calls) == 1
    assert filesystem.calls[0][1] == "file:///C:"
    assert ExecutorPath.parse(filesystem.calls[0][1]).native == "C:\\"


@pytest.mark.asyncio
@pytest.mark.parametrize("truncated,errors", [(True, []), (False, [{"path": "file:///remote/project/src/private", "message": "denied"}])])
async def test_incomplete_executor_grant_scan_cannot_become_a_partial_authority(tmp_path, truncated, errors):
    filesystem = GrantPolicyIO(("file:///remote/project/src/a.py",), truncated=truncated, errors=errors)
    with pytest.raises(ValueError, match="not complete"):
        await captured_grants(tmp_path, "file:///remote/project", "src/**/*.py", filesystem)


@pytest.mark.asyncio
@pytest.mark.parametrize("tree", [False, True])
async def test_optional_missing_grant_is_distinct_from_real_executor_error(tmp_path, tree):
    from backend.environments.errors import ExecutorRpcError

    pattern = "absent/**" if tree else "absent/*.py"
    for missing in (FileNotFoundError("absent"), ExecutorRpcError(-32004, "not found")):
        assert await captured_grants(tmp_path, "file:///remote/project", pattern, GrantPolicyIO(error=missing)) == ()
    error = ExecutorRpcError(-32003, "sandbox denied the policy scan")
    with pytest.raises(ExecutorRpcError) as caught:
        await captured_grants(tmp_path, "file:///remote/project", pattern, GrantPolicyIO(error=error))
    assert caught.value is error


@pytest.mark.parametrize("windows", [False, True])
def test_executor_access_uses_source_specificity_and_independent_non_root_denies(windows, monkeypatch):
    cwd = "file:///C:/project" if windows else "file:///remote/project"
    entries = [Entry(FileSystemPath.special(Special.ROOT), Access.READ),
        Entry(FileSystemPath.special(Special.PROJECT_ROOTS), Access.WRITE),
        Entry(FileSystemPath("path", cwd + "/private"), Access.DENY),
        Entry(FileSystemPath("path", cwd + "/private/allowed"), Access.WRITE),
        Entry(FileSystemPath("path", cwd + "/readonly"), Access.READ)]
    scope = EnvironmentPermissionScope("target", PermissionProfile.managed(FileSystemSandboxPolicy.restricted(entries)), cwd, (cwd,), "linux")
    with monkeypatch.context() as guard:
        guard.setattr(Path, "resolve", lambda *_a, **_kw: pytest.fail("executor access resolved on the controller"))
        assert scope.resolve_access("src/app.py") is Access.WRITE
        assert scope.resolve_access("readonly/app.py") is Access.READ
        assert scope.resolve_access("private/allowed/app.py") is Access.DENY
        assert scope.resolve_access("../outside") is Access.READ
        assert not scope.has_full_disk_read_access() and not scope.has_full_disk_write_access()


@pytest.mark.parametrize("narrow,override,expected", [(Access.READ, False, False), (Access.READ, True, True), (Access.DENY, True, False)])
def test_full_disk_write_fact_keeps_source_same_target_override_precedence(narrow, override, expected):
    path = FileSystemPath("path", "file:///C:/readonly")
    entries = [Entry(FileSystemPath.special(Special.ROOT), Access.WRITE), Entry(path, narrow)]
    if override:
        entries.append(Entry(path, Access.WRITE))
    scope = EnvironmentPermissionScope("target", PermissionProfile.managed(FileSystemSandboxPolicy.restricted(entries)),
        "file:///C:/project", (), "linux")
    assert scope.has_full_disk_write_access() is expected


def test_target_permission_grants_normalize_intersect_and_materialize_without_host_paths(monkeypatch):
    from backend.permissions.grants import normalize_permission_profile, normalize_permission_response, permission_profile_payload

    cwd = ExecutorPath.parse("file:///C:/remote/project", platform_os="linux")
    requested_payload = {"file_system": {"read": ["../shared"], "write": ["../shared/build"]}, "network": {"enabled": True}}
    with monkeypatch.context() as guard:
        guard.setattr(Path, "resolve", lambda *_a, **_kw: pytest.fail("permission grant resolved on controller"))
        requested = normalize_permission_profile(requested_payload, cwd=cwd, workspace_roots=(cwd,))
        response = normalize_permission_response(requested, {"permissions": {"file_system": {
            "read": ["C:/remote/shared/docs"], "write": ["C:/remote", "C:/remote/shared/build/output"]},
            "network": {"enabled": True}}, "scope": "turn"}, cwd=cwd, workspace_roots=(cwd,), temporary_directories=())
        assert permission_profile_payload(response.permissions) == {"file_system": {
            "read": ["file:///C:/remote/shared/docs"], "write": ["file:///C:/remote/shared/build/output"]},
            "network": {"enabled": True}}
        scope = EnvironmentPermissionScope("target", PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
            Entry(FileSystemPath.special(Special.ROOT), Access.READ)])), cwd.uri, (cwd.uri,), "linux")
        effective = scope.with_additional_permissions(response.permissions)
        assert effective.resolve_access("../shared/build/output/a") is Access.WRITE
        assert effective.resolve_access("../shared/other") is Access.READ
        assert effective.permission_profile.network is NetworkSandboxPolicy.ENABLED


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("MINICODE_SOURCE_EXECUTOR"), reason="requires explicit owned Source exec-server peer")
async def test_captured_explicit_source_wire_context_is_received_by_actual_source_executor(tmp_path):
    codex_home = tmp_path / "owned-codex-home"
    codex_home.mkdir()
    (tmp_path / "environments.toml").write_text("include_local=false\ndefault='target'\n[[environments]]\nid='target'\nprogram="
        + json.dumps(os.environ["MINICODE_SOURCE_EXECUTOR"])
        + "\nargs=['exec-server','--listen','stdio','--concurrent-requests','4']\ncwd=" + json.dumps(str(tmp_path))
        + "\n[environments.env]\nCODEX_HOME=" + json.dumps(str(codex_home)) + "\n", encoding="utf-8")
    manager = EnvironmentManager(tmp_path)
    try:
        environment = manager.get_environment("target")
        info = await environment.info()
        target = tmp_path / "scope-roundtrip.txt"
        scope = EnvironmentPermissionScope("target", PermissionProfile.disabled(), tmp_path.as_uri(), (tmp_path.as_uri(),), info.get("platformOs"))
        await environment.filesystem.write_file(target.as_uri(), b"actual-source-permission-context", sandbox=scope.sandbox_context())
        assert await environment.filesystem.read_file(target.as_uri(), sandbox=scope.sandbox_context()) == b"actual-source-permission-context"
    finally:
        await manager.close()
