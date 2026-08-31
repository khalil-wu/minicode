import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_desktop_dev_scripts_force_utf8_console_and_python_output() -> None:
    package_json = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    scripts = package_json["scripts"]

    for script_name in ("dev", "start", "start:client", "start:client:devfrontend"):
        script = scripts[script_name].lower()
        assert "chcp 65001" in script
        assert "pythonutf8=1" in script
        assert "pythonioencoding=utf-8" in script


def test_desktop_manual_launchers_force_utf8() -> None:
    ps1 = (ROOT / "desktop" / "run-desktop.ps1").read_text(encoding="utf-8").lower()
    bat = (ROOT / "desktop" / "run-desktop.bat").read_text(encoding="utf-8").lower()

    assert "chcp.com 65001" in ps1
    assert "pythonutf8" in ps1
    assert "pythonioencoding" in ps1
    assert "chcp 65001" in bat
    assert "pythonutf8=1" in bat
    assert "pythonioencoding=utf-8" in bat


def test_desktop_backend_sidecar_decodes_utf8_incrementally() -> None:
    sidecar = (ROOT / "desktop" / "backend-sidecar.js").read_text(encoding="utf-8")

    assert 'require("node:string_decoder")' in sidecar
    assert 'new StringDecoder("utf8")' in sidecar
    assert 'PYTHONUTF8: "1"' in sidecar
    assert 'PYTHONIOENCODING: "utf-8"' in sidecar


def test_desktop_pty_has_no_child_process_fallback_needing_manual_decoding() -> None:
    """node-pty is the single authoritative terminal path.

    The child_process pipe fallback -- the thing that needed its own
    ``StringDecoder`` because it received raw Buffers -- was deliberately
    removed: a missing/broken node-pty runtime is now an explicit startup
    failure instead of a silently degraded pipe session with different
    semantics. node-pty defaults to ``encoding: "utf8"`` and hands back
    already-decoded strings (via the socket's own boundary-safe decoder), so
    this module must not reintroduce manual chunk decoding.
    """
    pty_manager = (ROOT / "desktop" / "pty-manager.js").read_text(encoding="utf-8")

    assert "pty.spawn(" in pty_manager
    assert "ptyProcess.onData(" in pty_manager
    # Fail closed rather than degrade to a pipe session.
    assert "node-pty module unavailable" in pty_manager
    # A raw Buffer -> string conversion would split multi-byte UTF-8 sequences
    # at chunk boundaries; neither it nor a hand-rolled decoder may come back.
    assert '.toString("utf8")' not in pty_manager
    assert "StringDecoder" not in pty_manager
