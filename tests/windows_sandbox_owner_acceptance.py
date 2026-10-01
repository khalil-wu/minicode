"""Explicit live owner acceptance; never collected/run by pytest or normal builds."""
from __future__ import annotations

import argparse
import ctypes as c
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.sandbox.windows_native import PrivateDesktop

OLD_FILTERS = [
    "95d20d3a711451bf87a902c15b275378", "d51a616a61f055b2849f114419a498d9",
    "9ebad511017c525898ab42dcaba0477b", "4957f061275858e9939f65f07abb12ec",
    "da80b32340ad5b108d9e37e1c8ed6013", "f31060053138559e868cb2b0e1531e87",
    "c95b3c5cf3055c8b9a2664b1ca85e5df", "d7f2804095c25cde957d3b8d75d60210",
    "f27b3fc6175552e2b47049713f246c80", "8c89c8aa64bf590a8584f8214af0d6d5",
    "77daf944448458a3b562bde035d68310", "636833e99e455ea7ab129754b7ac1b5d",
    "ef9b83976e4653f4a138e38500d00ac4", "55b3cd86d00753218f392c8a8cd53330",
]
OLD_RULES = ["minicode_sandbox_offline_" + name for name in (
    "block_outbound", "block_inbound", "block_loopback_tcp", "block_loopback_udp", "allow_loopback_proxy",
)]


def run(argv, *, timeout=45):
    result = subprocess.run([str(a) for a in argv], capture_output=True, timeout=timeout,
                            encoding="utf-8", env={k: v for k, v in os.environ.items() if k != "CODEX_HOME"})
    return {"argv": [str(a) for a in argv[:6]], "exit": result.returncode,
            "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def powershell(script):
    exe = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = run([exe, "-NoProfile", "-NonInteractive", "-Command",
                  "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);" + script])
    assert result["exit"] == 0, result
    return json.loads(result["stdout"])


class GUID(c.Structure):
    _fields_ = [("data1", c.c_uint32), ("data2", c.c_uint16), ("data3", c.c_uint16), ("data4", c.c_ubyte * 8)]


class Blob(c.Structure):
    _fields_ = [("size", c.c_uint32), ("data", c.c_void_p)]


class ValueUnion(c.Union):
    _fields_ = [("number", c.c_uint64), ("pointer", c.c_void_p)]


class Value(c.Structure):
    _fields_ = [("type", c.c_uint32), ("value", ValueUnion)]


class Condition(c.Structure):
    _fields_ = [("field", GUID), ("match", c.c_uint32), ("value", Value)]


class Display(c.Structure):
    _fields_ = [("name", c.c_wchar_p), ("description", c.c_wchar_p)]


class FilterPrefix(c.Structure):
    # Only the documented prefix through filterCondition is dereferenced.
    _fields_ = [("key", GUID), ("display", Display), ("flags", c.c_uint32),
                ("provider", c.POINTER(GUID)), ("data", Blob), ("layer", GUID),
                ("sublayer", GUID), ("weight", Value), ("count", c.c_uint32),
                ("conditions", c.POINTER(Condition))]


def guid(value):
    return str(uuid.UUID(bytes_le=c.string_at(c.byref(value), 16)))


def wfp(keys):
    dll = c.WinDLL("fwpuclnt")
    dll.FwpmEngineOpen0.argtypes = [c.c_wchar_p, c.c_uint32, c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p)]
    dll.FwpmEngineOpen0.restype = c.c_uint32
    dll.FwpmFilterGetByKey0.argtypes = [c.c_void_p, c.POINTER(GUID), c.POINTER(c.POINTER(FilterPrefix))]
    dll.FwpmFilterGetByKey0.restype = c.c_uint32
    dll.FwpmFreeMemory0.argtypes = [c.POINTER(c.c_void_p)]
    dll.FwpmEngineClose0.argtypes = [c.c_void_p]
    engine = c.c_void_p()
    assert dll.FwpmEngineOpen0(None, 0xFFFFFFFF, None, None, c.byref(engine)) == 0
    result = {}
    try:
        for key in keys:
            g = GUID.from_buffer_copy(uuid.UUID(key).bytes_le)
            value = c.POINTER(FilterPrefix)()
            status = dll.FwpmFilterGetByKey0(engine, c.byref(g), c.byref(value))
            if status in (0x80320003, 0x80320008):
                result[str(uuid.UUID(key))] = None
                continue
            assert status == 0, (key, hex(status))
            try:
                conditions = []
                for index in range(value.contents.count):
                    item = value.contents.conditions[index]
                    condition = {"field": guid(item.field), "match": item.match, "type": item.value.type}
                    if item.value.type == 14:  # SDK FWP_SECURITY_DESCRIPTOR_TYPE / sd byte blob
                        blob = c.cast(item.value.value.pointer, c.POINTER(Blob)).contents
                        condition["sd_sha256"] = hashlib.sha256(c.string_at(blob.data, blob.size)).hexdigest()
                    else:
                        # Only the active uint8/uint16 member belongs to these specs;
                        # the unused union bytes are not filter state.
                        assert item.value.type in (1, 2)
                        condition["value"] = item.value.value.number & (255 if item.value.type == 1 else 65535)
                    conditions.append(condition)
                result[str(uuid.UUID(key))] = {"name": value.contents.display.name,
                    "provider": guid(value.contents.provider.contents), "sublayer": guid(value.contents.sublayer),
                    "conditions": conditions}
            finally:
                memory = c.cast(value, c.c_void_p)
                dll.FwpmFreeMemory0(c.byref(memory))
    finally:
        dll.FwpmEngineClose0(engine)
    return result


def snapshot(home, accounts, rules, filters):
    quoted_accounts = ",".join("'" + name.replace("'", "''") + "'" for name in accounts)
    quoted_rules = ",".join("'" + name.replace("'", "''") + "'" for name in rules)
    metadata = powershell(
        "$ErrorActionPreference='Stop';$policy=New-Object -ComObject HNetCfg.FwPolicy2;"
        "$accounts=@(" + quoted_accounts + ")|ForEach-Object { $u=Get-LocalUser -Name $_;"
        "@{name=$u.Name;sid=$u.SID.Value;enabled=$u.Enabled;password_last_set=$u.PasswordLastSet.ToUniversalTime().ToString('o')} };"
        "$rules=@(" + quoted_rules + ")|ForEach-Object {try{$r=$policy.Rules.Item($_);"
        "@{name=$r.Name;users=$r.LocalUserAuthorizedList;enabled=$r.Enabled;ports=$r.RemotePorts;addresses=$r.RemoteAddresses}}"
        "catch{if($_.Exception.HResult -ne -2147024894){throw}}};"
        "@{accounts=@($accounts);rules=@($rules)}|ConvertTo-Json -Depth 5 -Compress"
    )
    file = home / ".sandbox-secrets/sandbox_users.json"
    metadata["credential_sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
    metadata["wfp"] = wfp(filters)
    return metadata


def identity(runtime, home):
    response = run([runtime, "sandbox", "identity", "--codex-home", home])
    assert response["exit"] == 0, response
    value = json.loads(response["stdout"])
    assert value["schema"] == 3
    return value


def execute(runtime, home, owner, workspace, port, *, online=False):
    account = owner["online_username" if online else "offline_username"]
    desktop = PrivateDesktop.create(account)
    env = {"SYSTEMROOT": os.environ["SYSTEMROOT"], "WINDIR": os.environ["SYSTEMROOT"],
           "PATH": str(Path(os.environ["SYSTEMROOT"]) / "System32"), "TEMP": str(workspace), "TMP": str(workspace)}
    profile = {"type": "managed", "file_system": {"type": "restricted", "entries": [
        {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"},
        {"path": {"type": "path", "path": str(workspace)}, "access": "write"}]},
        "network": "enabled" if online else "restricted"}
    script = ("[Console]::WriteLine([Security.Principal.WindowsIdentity]::GetCurrent().Name);"
              "[IO.File]::WriteAllText((Join-Path $env:TEMP 'owner-result.txt'),'owner-ok');"
              "$tcp=[Net.Sockets.TcpClient]::new();try{$task=$tcp.ConnectAsync('127.0.0.1'," + str(port) + ");"
              "if(!$task.Wait(1500)){throw 'connect timed out'};[Console]::WriteLine('CONNECTED')}"
              "catch{[Console]::WriteLine('BLOCKED')}finally{$tcp.Dispose()}")
    shell = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    args = [runtime, "--run-as-windows-sandbox", "--codex-home", home, "--command-cwd", workspace,
            "--permission-profile", json.dumps(profile), "--env-json", json.dumps(env),
            "--windows-sandbox-level", "elevated", "--windows-sandbox-private-desktop-name", desktop.name,
            "--workspace-root", workspace, "--write-roots-json", json.dumps([str(workspace)]),
            "--read-roots-include-platform-defaults", "--read-roots-json", "[]",
            "--", shell, "-NoProfile", "-NonInteractive", "-Command", script]
    try:
        result = run(args)
    finally:
        desktop.close()
    assert result["exit"] == 0, result
    assert account.lower() in result["stdout"].lower(), result
    assert ("CONNECTED" if online else "BLOCKED") in result["stdout"], result
    assert (workspace / "owner-result.txt").read_text() == "owner-ok"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--legacy-home", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    args = parser.parse_args()
    runtime, legacy, root = (p.resolve() for p in (args.runtime, args.legacy_home, args.root))
    assert not root.exists(), "acceptance root must be new, never repair a machine home blindly"
    manifest = json.loads((runtime.parent / "runtime.json").read_text(encoding="utf-8"))
    assert manifest["patch_version"] == 3
    expected = identity(runtime, legacy)
    assert {expected["offline_username"], expected["online_username"]}.isdisjoint({"MiniCodeSbxOffline", "MiniCodeSbxOnline"})
    assert set(expected["wfp_filters"]).isdisjoint(str(uuid.UUID(key)) for key in OLD_FILTERS)
    receipts = {}
    def save():
        ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
        ledger["live_acceptance"] = receipts
        args.ledger.write_text(json.dumps(ledger, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    old = snapshot(legacy, ["MiniCodeSbxOffline", "MiniCodeSbxOnline"], OLD_RULES, OLD_FILTERS)
    receipts["legacy_before"] = old
    rejection = run([runtime, "sandbox", "setup", "--elevated", "--current-user", "--codex-home", legacy])
    assert rejection["exit"] != 0 and "legacy" in rejection["stderr"], rejection
    receipts["legacy_rejection"] = rejection
    assert snapshot(legacy, ["MiniCodeSbxOffline", "MiniCodeSbxOnline"], OLD_RULES, OLD_FILTERS) == old
    save()
    root.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); listener.listen(20)
        port = listener.getsockname()[1]
        owners = {}
        for label in ("A", "B"):
            home, workspace = root / ("home-" + label), root / ("workspace-" + label)
            home.mkdir(); workspace.mkdir()
            owner = identity(runtime, home)
            owners[label] = owner
            setup = run([runtime, "sandbox", "setup", "--elevated", "--current-user", "--codex-home", home])
            receipts[label] = {"identity": owner, "setup": setup}
            assert setup["exit"] == 0, setup
            receipts[label]["offline_execute"] = execute(runtime, home, owner, workspace, port)
            receipts[label]["snapshot"] = snapshot(home, [owner["offline_username"], owner["online_username"]], owner["firewall_rules"], owner["wfp_filters"])
            assert all(receipts[label]["snapshot"]["wfp"].values())
            assert snapshot(legacy, ["MiniCodeSbxOffline", "MiniCodeSbxOnline"], OLD_RULES, OLD_FILTERS) == old
            save()
        a, b = owners["A"], owners["B"]
        assert set(a["wfp_filters"]).isdisjoint(b["wfp_filters"])
        assert snapshot(root / "home-A", [a["offline_username"], a["online_username"]], a["firewall_rules"], a["wfp_filters"]) == receipts["A"]["snapshot"]
        receipts["A"]["after_B_execute"] = execute(runtime, root / "home-A", a, root / "workspace-A", port)
        receipts["B"]["online_execute"] = execute(runtime, root / "home-B", b, root / "workspace-B", port, online=True)
        repair = run([runtime, "sandbox", "setup", "--elevated", "--current-user", "--codex-home", root / "home-A"])
        receipts["A"]["repair"] = repair
        assert repair["exit"] == 0, repair
        receipts["A"]["after_repair_execute"] = execute(runtime, root / "home-A", a, root / "workspace-A", port)
        assert snapshot(root / "home-B", [b["offline_username"], b["online_username"]], b["firewall_rules"], b["wfp_filters"]) == receipts["B"]["snapshot"]
        cleanup = run([runtime, "sandbox", "cleanup", "--elevated", "--current-user", "--codex-home", root / "home-A"])
        receipts["A"]["cleanup"] = cleanup
        assert cleanup["exit"] == 0, cleanup
        assert not any(wfp(a["wfp_filters"]).values())
        receipts["B"]["after_A_cleanup_execute"] = execute(runtime, root / "home-B", b, root / "workspace-B", port)
        assert snapshot(root / "home-B", [b["offline_username"], b["online_username"]], b["firewall_rules"], b["wfp_filters"]) == receipts["B"]["snapshot"]
        receipts["legacy_after"] = snapshot(legacy, ["MiniCodeSbxOffline", "MiniCodeSbxOnline"], OLD_RULES, OLD_FILTERS)
        assert receipts["legacy_after"] == old
        receipts["status"] = "passed"
        save()
    print(json.dumps({"status": "passed", "root": str(root), "retained_home": str(root / 'home-B')}, ensure_ascii=True))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Elevated CLI stdout is not attached to the agent's console. Preserve
        # the actual failure at this entry boundary, then propagate it unchanged.
        ledger_path = Path(sys.argv[sys.argv.index("--ledger") + 1])
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger.setdefault("live_attempt_errors", []).append(traceback.format_exc())
        ledger_path.write_text(json.dumps(ledger, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        raise
