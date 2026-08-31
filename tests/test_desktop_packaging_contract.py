import json
from pathlib import Path


def test_desktop_package_has_windows_builder_scripts() -> None:
    package_json = json.loads(Path("desktop/package.json").read_text(encoding="utf-8"))
    scripts = package_json.get("scripts", {})
    build = package_json.get("build", {})
    build_resources_dir = Path("desktop") / build.get("directories", {}).get("buildResources", "build")

    assert "dist:win" in scripts
    assert "pack:dir" in scripts
    assert "electron-builder" in json.dumps(package_json)
    assert package_json.get("author")
    assert build.get("appId")
    assert build.get("icon") == "build/icon.ico"
    assert build.get("win", {}).get("target")
    assert "nsis" in json.dumps(build).lower()
    assert build_resources_dir.exists()
    assert (Path("desktop/build/icon.ico")).exists()


def test_desktop_package_includes_required_shell_modules_and_sidecar() -> None:
    package_json = json.loads(Path("desktop/package.json").read_text(encoding="utf-8"))
    build_files = set(package_json.get("build", {}).get("files", []))

    for required in {
        "backend-sidecar.js",
        "cdp-bridge.js",
        "crash-reporter.js",
        "ipc-handlers.js",
        "pty-manager.js",
        "security.js",
        "utils.js",
        "window-manager.js",
    }:
        assert required in build_files

    assert "crash-reporter.test.js" in package_json.get("scripts", {}).get("test:unit", "")


def test_pdf_frame_csp_allows_only_local_backend_frames() -> None:
    index = Path("frontend/index.html").read_text(encoding="utf-8")
    csp = index.split('Content-Security-Policy" content="', 1)[1].split('"', 1)[0]
    frame_policy = next(part.strip() for part in csp.split(";") if part.strip().startswith("frame-src"))

    assert "http://localhost:*" in frame_policy
    assert "http://127.0.0.1:*" in frame_policy
    assert "https:" not in frame_policy
    assert "http:" not in frame_policy.split()


def test_release_workflow_requires_signed_update_bundle() -> None:
    workflow = Path(".github/workflows/release-windows.yml").read_text(encoding="utf-8")

    assert "Get-AuthenticodeSignature" in workflow
    assert "latest.yml" in workflow
    assert "*.blockmap" in workflow
    assert "SHA256SUMS.txt" in workflow
    assert "MINICODE_UPDATE_FEED_URL" in workflow


def test_desktop_startup_failure_surface_exists_and_is_wired() -> None:
    main_source = Path("desktop/main.js").read_text(encoding="utf-8")
    ipc_handlers = Path("desktop/ipc-handlers.js").read_text(encoding="utf-8")
    error_html = Path("desktop/startup-error.html")
    error_preload = Path("desktop/startup-error-preload.js")

    assert "startup-error.html" in main_source
    assert "createStartupFailureWindow" in main_source
    assert 'ipcMain.handle("minicode:startup:retry"' in ipc_handlers
    assert 'ipcMain.handle("minicode:startup:quit"' in ipc_handlers
    assert 'ipcMain.handle("minicode:startup:getState", withStartupSender("minicode:startup:getState"' in ipc_handlers
    assert "startupFailureState,\n    getDesktopLogPath" not in ipc_handlers
    assert error_html.exists()
    assert error_preload.exists()

    page_source = error_html.read_text(encoding="utf-8")
    assert "Retry startup" in page_source
    assert "Quit MiniCode" in page_source


def test_frontend_build_defaults_to_file_safe_relative_assets() -> None:
    vite_config = Path("frontend/vite.config.ts").read_text(encoding="utf-8")

    assert "process.env.MINICODE_VITE_RELATIVE_BASE" in vite_config
    assert 'base: useRelativeBase ? "./" : "./"' in vite_config
    assert 'base: useRelativeBase ? "./" : "/"' not in vite_config


def test_desktop_diagnostics_declares_private_beta_safety_defaults() -> None:
    main_source = Path("desktop/main.js").read_text(encoding="utf-8")

    assert 'channel: "windows_private_beta"' in main_source
    assert 'defaultPermissionMode: "confirm"' in main_source
    assert 'backendPermissionMode: "confirm"' in main_source
    assert 'networkAccess: "tool_layer_approval_required"' in main_source
    assert 'windowsSandbox: "docker_workspace_container_fail_closed"' in main_source
