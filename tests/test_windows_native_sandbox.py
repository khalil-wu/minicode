from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from backend.sandbox.policy import (
    FileSystemAccessMode,
    FileSystemPath,
    FileSystemSandboxEntry,
    FileSystemSandboxPolicy,
    FileSystemSpecialPath,
    PermissionProfile,
    SandboxPolicy,
)
from backend.sandbox.runner import SandboxRunner
from backend.sandbox import windows_native


_SID = "S-1-5-21-1-2-3-1000"
_INSPECT = windows_native.inspect_runtime_owner


def test_native_profile_encodes_the_effective_exact_path_grant(tmp_path):
    metadata = tmp_path / ".git"
    entries = [
        FileSystemSandboxEntry(FileSystemPath.path(metadata), FileSystemAccessMode.READ),
        FileSystemSandboxEntry(FileSystemPath.path(metadata), FileSystemAccessMode.WRITE),
    ]
    policy = SandboxPolicy(workspace_root=tmp_path, permission_profile=PermissionProfile.managed(FileSystemSandboxPolicy.restricted(entries)))
    encoded = windows_native._permission_profile(policy.resolve(cwd=tmp_path), tmp_path)
    assert encoded["file_system"]["entries"] == [{"path": {"type": "path", "path": str(metadata.resolve())}, "access": "write"}]
    entries.append(FileSystemSandboxEntry(FileSystemPath.path(metadata), FileSystemAccessMode.DENY))
    denied = SandboxPolicy(workspace_root=tmp_path, permission_profile=PermissionProfile.managed(FileSystemSandboxPolicy.restricted(entries)))
    assert windows_native._permission_profile(denied.resolve(cwd=tmp_path), tmp_path)["file_system"]["entries"][0]["access"] == "deny"


def test_native_private_temp_does_not_create_deny_write_for_a_readable_peer(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    peer = tmp_path / "peer"
    peer.mkdir()
    policy = SandboxPolicy(workspace_root=workspace, permission_profile=PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
        FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),
        FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.TMPDIR), FileSystemAccessMode.WRITE),
        FileSystemSandboxEntry(FileSystemPath.path(workspace), FileSystemAccessMode.WRITE),
        FileSystemSandboxEntry(FileSystemPath.path(peer), FileSystemAccessMode.READ),
    ])))
    captured = {}
    def prepare(command, **kwargs):
        captured.update(kwargs)
        args = [
            "not-launched", "--permission-profile", json.dumps(windows_native._permission_profile(kwargs["resolved"], kwargs["cwd"])),
            "--write-roots-json", json.dumps([str(workspace)]),
        ]
        return args, SimpleNamespace(close=lambda: None), tmp_path / "not-created"
    monkeypatch.setattr(windows_native, "prepare_command", prepare)
    runner = SandboxRunner(policy)
    runner._wrap_command("git status", SimpleNamespace(backend="windows-elevated-wfp"), cwd=workspace)
    assert peer not in captured["deny_write_paths"]
    assert workspace / ".git" in captured["deny_write_paths"]


def test_native_launcher_scope_follows_permission_changes_and_ignores_temp_nonce(tmp_path):
    workspace = tmp_path / "workspace"
    metadata = workspace / ".git"
    entries = [
        FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),
        FileSystemSandboxEntry(FileSystemPath.path(workspace), FileSystemAccessMode.WRITE),
    ]
    read_policy = SandboxPolicy(workspace_root=workspace, permission_profile=PermissionProfile.managed(FileSystemSandboxPolicy.restricted(entries)))
    write_policy = SandboxPolicy(workspace_root=workspace, permission_profile=PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
        *entries, FileSystemSandboxEntry(FileSystemPath.path(metadata), FileSystemAccessMode.WRITE),
    ])))

    def launch_env(policy, private_temp, writable, deny_write):
        args = [
            "native", "--permission-profile", json.dumps(windows_native._permission_profile(policy.resolve(cwd=workspace), workspace)),
            "--write-roots-json", json.dumps([*writable, str(private_temp.resolve())]),
            "--deny-write-paths-json", json.dumps(deny_write),
        ]
        return windows_native.command_launcher_env(args, private_temp)

    original = launch_env(read_policy, tmp_path / "temp-a", [str(workspace)], [str(metadata)])
    assert original == launch_env(read_policy, tmp_path / "temp-b", [str(workspace)], [str(metadata)])
    assert original != launch_env(write_policy, tmp_path / "temp-c", [str(workspace), str(metadata)], [])
    runner = SandboxRunner(SandboxPolicy(
        workspace_root=workspace,
        permission_profile=read_policy.permission_profile,
        env_overrides={windows_native.POLICY_SCOPE_ENV: "workload-override"},
    ))
    assert windows_native.POLICY_SCOPE_ENV not in runner._build_env()
    runner._windows_launcher_env = original
    assert runner._build_env()[windows_native.POLICY_SCOPE_ENV] == original[windows_native.POLICY_SCOPE_ENV]


def _identity(home: Path) -> dict:
    digest = hashlib.sha256(b"minicode.windows-sandbox.owner.v3\0" + _SID.encode("ascii")
        + b"\0" + str(home.resolve()).replace("\\", "/").encode("utf-8").lower()).digest()
    label = base64.b32encode(digest[:10]).decode("ascii").lower()
    return {"schema": 3, "owner_id": digest.hex(), "user_sid": _SID, "home": str(home.resolve()),
            "offline_username": f"MC{label}O", "online_username": f"MC{label}N",
            "group": f"MiniCodeSandboxUsers-{digest.hex()}"}


def _records(home: Path, marker: str | None = None) -> None:
    owner = _identity(home)
    (home / ".sandbox-secrets/sandbox_users.json").write_text(json.dumps({
        "version": 6, "owner_id": owner["owner_id"],
        "offline": {"username": owner["offline_username"], "password": "ciphertext"},
        "online": {"username": owner["online_username"], "password": "ciphertext"},
    }), encoding="utf-8")
    (home / ".sandbox/setup_marker.json").write_text(
        marker if marker is not None else json.dumps({"version": 6, **owner}), encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def owner_boundary(monkeypatch):
    # Exercise the Windows adapter's mocked native boundary without changing
    # the host platform used by pathlib, subprocesses or other modules.
    monkeypatch.setattr(windows_native, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(windows_native, "_current_user_sid", lambda: _SID)
    monkeypatch.setattr(windows_native, "inspect_runtime_owner", lambda executable, home: _identity(home))


def test_other_platforms_refuse_windows_runtime_before_native_inspection(monkeypatch):
    monkeypatch.setattr(windows_native, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(windows_native, "inspect_runtime_owner", lambda *_args: pytest.fail("A Windows runtime was inspected on another host"))
    runtime, reason = windows_native.discover_runtime()
    assert runtime is None and reason == "the native Windows sandbox is Windows-only"


def _home(tmp_path: Path, *, marker: str | None = None) -> tuple[Path, Path]:
    home = tmp_path / "native-home"
    secrets = home / ".sandbox-secrets"
    sandbox = home / ".sandbox"
    secrets.mkdir(parents=True)
    sandbox.mkdir()
    _records(home, marker)
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    return home, executable


@pytest.mark.parametrize("marker", ["", "[]", "null"])
def test_invalid_setup_marker_requires_repair(tmp_path, monkeypatch, marker):
    home, executable = _home(tmp_path, marker=marker)
    monkeypatch.setenv("MINICODE_WINDOWS_SANDBOX_HOME", str(home))
    monkeypatch.setattr(windows_native, "_candidate_executables", lambda: iter([executable]))

    runtime, reason = windows_native.discover_runtime()

    assert runtime is None
    assert "interrupted" in reason


def test_default_minicode_home_does_not_claim_an_interrupted_setup(tmp_path, monkeypatch):
    home, executable = _home(tmp_path, marker="")
    monkeypatch.delenv("MINICODE_WINDOWS_SANDBOX_HOME", raising=False)
    state = tmp_path / "state"
    monkeypatch.setattr(windows_native, "DATA_ROOT", state)
    (state).mkdir()
    home.rename(state / "windows-sandbox")
    monkeypatch.setattr(windows_native, "_candidate_executables", lambda: iter([executable]))

    runtime, reason = windows_native.discover_runtime()

    assert runtime is None
    assert "interrupted" in reason


def test_packaged_resource_runtime_uses_minicode_state_home(tmp_path, monkeypatch):
    state = tmp_path / "state"
    home, _unused_executable = _home(tmp_path)
    state.mkdir()
    home.rename(state / "windows-sandbox")
    _records(state / "windows-sandbox")
    resources = tmp_path / "resources"
    executable = resources / "windows-sandbox" / "codex.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    monkeypatch.delenv("MINICODE_WINDOWS_SANDBOX_HOME", raising=False)
    monkeypatch.delenv("MINICODE_WINDOWS_SANDBOX_EXECUTABLE", raising=False)
    monkeypatch.setenv("MINICODE_APP_RESOURCES_DIR", str(resources))
    monkeypatch.setattr(windows_native, "DATA_ROOT", state)

    runtime, reason = windows_native.discover_runtime()

    assert reason == ""
    assert runtime is not None
    assert runtime.home == (state / "windows-sandbox").resolve()
    assert runtime.executable == executable.resolve()


@pytest.mark.parametrize("structured", [False, True])
def test_native_command_projects_environment_and_policy(tmp_path, monkeypatch, structured):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "runtime-home"
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    owner = _identity(runtime_home)
    runtime = windows_native.WindowsNativeRuntime(executable, runtime_home,
        owner["offline_username"], owner["online_username"], owner["owner_id"], _SID, owner["group"])
    desktop = SimpleNamespace(name="MiniCodeSandboxDesktop-" + "a" * 32, close=lambda: None)
    monkeypatch.setattr(windows_native, "discover_runtime", lambda: (runtime, ""))
    monkeypatch.setattr(windows_native.PrivateDesktop, "create", lambda _account: desktop)
    monkeypatch.setattr(windows_native, "STATE_ROOT", tmp_path / "state")
    profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
        FileSystemSandboxEntry(
            FileSystemPath.special(FileSystemSpecialPath.ROOT),
            FileSystemAccessMode.READ,
        ),
        FileSystemSandboxEntry(FileSystemPath.path(workspace), FileSystemAccessMode.WRITE),
    ]))
    policy = SandboxPolicy(workspace_root=workspace, permission_profile=profile)
    resolved = policy.resolve(cwd=workspace)

    args, returned_desktop, private_temp = windows_native.prepare_command(
        "python -V",
        cwd=workspace,
        resolved=resolved,
        env={
            "PATH": "fixture",
            "OPENAI_API_KEY": "must-not-be-added",
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "CODEX_WINDOWS_SANDBOX_PROXY_PORTS": "7897",
            windows_native.POLICY_SCOPE_ENV: "workload-override",
        },
        workspace_roots=(workspace,),
        deny_read_paths=(),
        deny_write_paths=(workspace / ".git",),
        argv=["python", "-c", 'print("literal")', "one two", 'a"b'] if structured else None,
    )

    assert returned_desktop is desktop
    assert private_temp.is_dir()
    wire_profile = json.loads(args[args.index("--permission-profile") + 1])
    child_env = json.loads(args[args.index("--env-json") + 1])
    assert wire_profile["network"] == "restricted"
    assert wire_profile["file_system"]["entries"][0] == {
        "path": {"type": "special", "value": {"kind": "root"}},
        "access": "read",
    }
    assert str(workspace.resolve()) in json.loads(args[args.index("--write-roots-json") + 1])
    assert "--read-roots-json" not in args
    assert child_env["OPENAI_API_KEY"] == "must-not-be-added"
    assert "HTTP_PROXY" not in child_env
    assert "CODEX_WINDOWS_SANDBOX_PROXY_PORTS" not in child_env
    assert windows_native.POLICY_SCOPE_ENV not in child_env
    assert child_env["TEMP"] == str(private_temp)
    if structured:
        assert args[args.index("--") + 1:] == ["python", "-c", 'print("literal")', "one two", 'a"b']
    else:
        script = args[args.index("-Command") + 1]
        assert "Get-ChildItem -LiteralPath $minicodePrivateTemp" in script
        assert "Remove-Item -Recurse -Force" in script
    assert json.loads(args[args.index("--deny-write-paths-json") + 1]) == [
        str((workspace / ".git").resolve())
    ]


def test_runner_prefers_native_wfp_when_runtime_is_ready(tmp_path, monkeypatch):
    import backend.sandbox.runner as runner_module

    monkeypatch.setattr(runner_module, "sys", SimpleNamespace(platform="win32"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    monkeypatch.setattr(
        windows_native,
        "discover_runtime",
        lambda: (SimpleNamespace(), ""),
    )

    profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
        FileSystemSandboxEntry(
            FileSystemPath.special(FileSystemSpecialPath.ROOT),
            FileSystemAccessMode.READ,
        ),
        FileSystemSandboxEntry(FileSystemPath.path(workspace), FileSystemAccessMode.WRITE),
    ]))
    capability = SandboxRunner(SandboxPolicy(
        workspace_root=workspace, permission_profile=profile,
    )).capability(cwd=workspace)

    assert capability.backend == "windows-elevated-wfp"
    assert capability.filesystem_isolated
    assert capability.network_isolated
    assert capability.protected_paths_isolated


def test_discovery_validates_current_sid_home_and_exact_group(tmp_path, monkeypatch):
    home, executable = _home(tmp_path)
    monkeypatch.setenv("MINICODE_WINDOWS_SANDBOX_HOME", str(home))
    monkeypatch.setattr(windows_native, "_candidate_executables", lambda: iter([executable]))
    runtime, reason = windows_native.discover_runtime()
    assert reason == ""
    assert runtime.owner_id == _identity(home)["owner_id"]
    assert runtime.user_sid == _SID
    assert runtime.group == _identity(home)["group"]


@pytest.mark.parametrize("field,value", [
    ("schema", 2), ("owner_id", "0" * 64), ("user_sid", "S-1-5-21-1-2-3-2000"),
    ("home", "C:/different-home"), ("group", "MiniCodeSandboxUsers"),
    ("offline_username", "MiniCodeSbxOffline"), ("online_username", "MiniCodeSbxOnline"),
])
def test_discovery_rejects_foreign_runtime_receipt(tmp_path, monkeypatch, field, value):
    home, executable = _home(tmp_path)
    owner = {**_identity(home), field: value}
    monkeypatch.setenv("MINICODE_WINDOWS_SANDBOX_HOME", str(home))
    monkeypatch.setattr(windows_native, "_candidate_executables", lambda: iter([executable]))
    monkeypatch.setattr(windows_native, "inspect_runtime_owner", lambda *_: owner)
    runtime, reason = windows_native.discover_runtime()
    assert runtime is None
    assert "current Windows SID and canonical home" in reason


@pytest.mark.parametrize("record", [".sandbox-secrets/sandbox_users.json", ".sandbox/setup_marker.json"])
def test_discovery_rejects_copied_state_receipts(tmp_path, monkeypatch, record):
    home, executable = _home(tmp_path)
    path = home / record
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["owner_id"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("MINICODE_WINDOWS_SANDBOX_HOME", str(home))
    monkeypatch.setattr(windows_native, "_candidate_executables", lambda: iter([executable]))
    assert windows_native.discover_runtime()[0] is None


def test_legacy_home_is_rejected_before_any_runtime_process(tmp_path, monkeypatch):
    home, executable = _home(tmp_path)
    path = home / ".sandbox-secrets/sandbox_users.json"
    old = json.dumps({"version": 5, "offline": {"username": "MiniCodeSbxOffline", "password": "ciphertext"},
                      "online": {"username": "MiniCodeSbxOnline", "password": "ciphertext"}})
    path.write_text(old, encoding="utf-8")
    monkeypatch.setenv("MINICODE_WINDOWS_SANDBOX_HOME", str(home))
    monkeypatch.setattr(windows_native, "inspect_runtime_owner", lambda *_: pytest.fail("must not launch a runtime"))
    runtime, reason = windows_native.discover_runtime()
    assert runtime is None and "legacy" in reason
    assert path.read_text(encoding="utf-8") == old


def test_inspection_uses_only_read_only_native_abi(tmp_path, monkeypatch):
    home, executable = _home(tmp_path)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=windows_native._RUNTIME_VERSION if len(calls) == 1 else json.dumps(_identity(home)))
    monkeypatch.setattr(windows_native.subprocess, "run", run)
    assert _INSPECT(executable, home)["owner_id"] == _identity(home)["owner_id"]
    assert [c[0] for c in calls] == [[str(executable), "--version"],
        [str(executable), "sandbox", "identity", "--codex-home", str(home)]]
    assert all(c[1]["check"] and c[1]["encoding"] == "utf-8" for c in calls)


def test_inspection_refuses_v2_runtime_without_identity_or_setup(tmp_path, monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout="minicode-windows-sandbox 0.158.0-alpha.2.1")
    monkeypatch.setattr(windows_native.subprocess, "run", run)
    with pytest.raises(ValueError, match="incompatible"):
        _INSPECT(tmp_path / "codex.exe", tmp_path)
    assert calls == [[str(tmp_path / "codex.exe"), "--version"]]


def test_prepare_status_does_not_claim_legacy_runtime_ready(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("native_prepare", Path(__file__).resolve().parents[1] / "scripts/prepare_windows_native_sandbox.py")
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    home = tmp_path / "home"
    owner = _identity(home)
    legacy = SimpleNamespace(offline_username="MiniCodeSbxOffline", online_username="MiniCodeSbxOnline")
    monkeypatch.setattr(prepare, "_identity", lambda *_: owner)
    monkeypatch.setattr(windows_native, "discover_runtime", lambda: (legacy, ""))
    status = prepare._status(home, tmp_path / "codex.exe")
    assert status["ready"] is False
    assert status["identity"]["group"] == owner["group"]


def test_prepare_grants_only_exact_queried_owner_group(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("native_prepare_group", Path(__file__).resolve().parents[1] / "scripts/prepare_windows_native_sandbox.py")
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    home = tmp_path / "home"
    runtime = tmp_path / "codex.exe"
    owner = _identity(home)
    calls = []
    monkeypatch.setattr(prepare, "_runtime_path", lambda _: runtime)
    monkeypatch.setattr(prepare, "_identity", lambda *_: owner)
    monkeypatch.setattr(prepare, "_status", lambda *_: {"ready": True})
    monkeypatch.setattr(prepare, "sys", SimpleNamespace(
        platform="win32",
        executable=str(Path.home() / "minicode-test-python/python.exe"),
    ))
    monkeypatch.setattr("sys.argv", ["prepare", "--target-home", str(home)])
    monkeypatch.setattr(prepare.subprocess, "run", lambda argv, **_: calls.append(argv))
    assert prepare.main() == 0
    assert calls[0] == [str(runtime), "sandbox", "setup", "--elevated", "--current-user", "--codex-home", str(home)]
    assert calls[1][0] == "icacls.exe"
    assert calls[1][3] == f"{owner['group']}:(OI)(CI)(RX)"
