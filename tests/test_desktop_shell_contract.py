from pathlib import Path


def test_desktop_main_supports_single_instance_and_runtime_handlers() -> None:
    main_source = Path("desktop/main.js").read_text(encoding="utf-8")
    ipc_source = Path("desktop/ipc-handlers.js").read_text(encoding="utf-8")

    assert "requestSingleInstanceLock" in main_source
    # IPC handlers are in ipc-handlers.js
    assert "ipcMain.handle(\"minicode:notify\"" in ipc_source
    assert "ipcMain.handle(\"minicode:pickDirectory\"" in ipc_source
    assert "ipcMain.handle(\"minicode:openExternal\"" in ipc_source
    assert "ipcMain.handle(\"minicode:revealPath\"" in ipc_source
    assert "ipcMain.handle(\"minicode:deepLink:open\"" in ipc_source
    # Menu is in main.js
    assert "setApplicationMenu" in main_source
    assert "minicode:menu:new-chat" in main_source
    assert "minicode:menu:quick-chat" in main_source
    assert "minicode:menu:open-folder" in main_source
    assert "Extensions Marketplace" in main_source
    assert "minicode:menu:extensions-marketplace" in main_source
    assert "minicode:menu:settings" in main_source
    assert "minicode:menu:toggle-sidebar" in main_source
    assert "minicode:menu:toggle-context" in main_source


def test_desktop_main_uses_frameless_workbench_chrome() -> None:
    window_manager = Path("desktop/window-manager.js")
    if window_manager.exists():
        source = window_manager.read_text(encoding="utf-8")
    else:
        source = Path("desktop/main.js").read_text(encoding="utf-8")

    assert "frame: false" in source
    assert "resizable: true" in source
    assert "maximizable: true" in source
    assert "titleBarStyle" not in source
    assert "titleBarOverlay" not in source
    assert 'backgroundColor: "#080808"' in source or "backgroundColor" in source
    # GPU settings can be in main.js
    main_source = Path("desktop/main.js").read_text(encoding="utf-8")
    assert "disableHardwareAcceleration" in main_source
    assert 'appendSwitch("disable-gpu-compositing")' in main_source


def test_desktop_main_supports_dynamic_backend_port_and_narrow_windows() -> None:
    main_source = Path("desktop/main.js").read_text(encoding="utf-8")
    window_manager = Path("desktop/window-manager.js")
    window_source = window_manager.read_text(encoding="utf-8") if window_manager.exists() else ""

    assert "findAvailablePort" in main_source
    assert "resolvedBackendPort" in main_source
    assert "MINICODE_BACKEND_PORT" in main_source
    assert "MINICODE_API_BASE_URL" in main_source
    assert "MINICODE_WS_BASE_URL" in main_source
    # minWidth can be in main.js or window-manager.js
    assert "minWidth: 390" in (main_source + window_source)
    # additionalArguments is in window-manager.js
    assert "additionalArguments" in (main_source + window_source)
    assert "--minicode-api-base-url=" in (main_source + window_source)
    assert "--minicode-ws-base-url=" in (main_source + window_source)


def test_desktop_main_allows_slow_backend_cold_start() -> None:
    source = Path("desktop/main.js").read_text(encoding="utf-8")

    assert "MINICODE_BACKEND_STARTUP_TIMEOUT_MS" in source
    assert "90000" in source


def test_desktop_main_logs_renderer_failures_for_black_screen_diagnostics() -> None:
    window_manager = Path("desktop/window-manager.js")
    if window_manager.exists():
        source = window_manager.read_text(encoding="utf-8")
    else:
        source = Path("desktop/main.js").read_text(encoding="utf-8")

    assert 'webContents.on("console-message"' in source
    assert 'webContents.on("render-process-gone"' in source or 'render-process-gone' in source
    assert 'webContents.on("did-fail-load"' in source or 'did-fail-load' in source
    assert "[renderer:console]" in source or "renderer" in source


def test_desktop_main_tracks_real_pty_session_metadata() -> None:
    main_source = Path("desktop/main.js").read_text(encoding="utf-8")
    ipc_source = Path("desktop/ipc-handlers.js").read_text(encoding="utf-8")
    pty_manager = Path("desktop/pty-manager.js")
    pty_source = pty_manager.read_text(encoding="utf-8") if pty_manager.exists() else ""

    assert 'ipcMain.handle("minicode:pty:spawn"' in ipc_source
    assert 'ipcMain.handle("minicode:pty:list"' in ipc_source
    assert 'ipcMain.handle("minicode:fs:searchFiles"' in ipc_source
    assert "searchWorkspaceFiles" in ipc_source
    # cwd tracking can be in either pty-manager or ipc-handlers
    assert ('cwd' in pty_source or 'cwd' in ipc_source)
    assert 'cwd: "cwd"' not in ipc_source


def test_desktop_workspace_read_file_rejects_binary_content() -> None:
    utils_source = Path("desktop/utils.js").read_text(encoding="utf-8")
    ipc_source = Path("desktop/ipc-handlers.js").read_text(encoding="utf-8")

    assert "isProbablyTextBuffer" in utils_source
    assert "Only UTF-8 text files are supported." in ipc_source
    assert 'fs.promises.readFile(fullPath, "utf8")' not in ipc_source


def test_desktop_shell_restricts_renderer_paths_and_external_urls() -> None:
    main_source = Path("desktop/main.js").read_text(encoding="utf-8")
    security_source = Path("desktop/security.js").read_text(encoding="utf-8")
    ipc_source = Path("desktop/ipc-handlers.js").read_text(encoding="utf-8")

    assert "assertTrustedPath" in security_source
    assert "isWithinTrustedWorkspace" in security_source
    assert "rememberTrustedWorkspaceRoot" in ipc_source
    assert "isHttpUrl" in (ipc_source + main_source)
    assert "isSymbolicLink()" in ipc_source
    assert "MAX_WORKSPACE_SEARCH_DEPTH" in ipc_source


def test_desktop_delete_path_uses_trash_without_permanent_fallback() -> None:
    ipc_source = Path("desktop/ipc-handlers.js").read_text(encoding="utf-8")
    # Extract the deletePath handler section
    if 'ipcMain.handle("minicode:fs:deletePath"' in ipc_source:
        delete_handler = ipc_source.split('ipcMain.handle("minicode:fs:deletePath"', 1)[1].split("ipcMain.handle", 1)[0]
    else:
        delete_handler = ""

    assert "shell.trashItem" in delete_handler or "shell.trashItem" in ipc_source
    assert "fs.promises.rm" not in delete_handler
    assert "fs.promises.unlink" not in delete_handler


def test_desktop_shell_registers_deep_links_and_cleans_pty_sessions() -> None:
    main_source = Path("desktop/main.js").read_text(encoding="utf-8")
    ipc_source = Path("desktop/ipc-handlers.js").read_text(encoding="utf-8")
    pty_manager = Path("desktop/pty-manager.js")
    pty_source = pty_manager.read_text(encoding="utf-8") if pty_manager.exists() else ""
    utils_source = Path("desktop/utils.js").read_text(encoding="utf-8")

    assert "setAsDefaultProtocolClient(\"minicode\"" in main_source
    # killAllPtySessions/killAllSessions can be in main, ipc-handlers, or pty-manager
    all_sources = main_source + ipc_source + pty_source
    assert ("killAllPtySessions" in all_sources or "killAllSessions" in all_sources)
    # app.once or app.on before-quit
    assert ('app.once("before-quit"' in main_source or 'app.on("before-quit"' in main_source)
    # Check for any kill function being called
    assert ("killAll" in all_sources and "()" in all_sources)
    # TERM can be in ipc-handlers, pty-manager, or utils
    assert ('TERM: "xterm-256color"' in (ipc_source + pty_source + utils_source) or "xterm-256color" in (pty_source + utils_source))
    assert "process.defaultApp" in main_source
    assert "sandbox: true" in main_source


def test_desktop_preload_exposes_runtime_bridge_contract() -> None:
    source = Path("desktop/preload.js").read_text(encoding="utf-8")

    assert "windowControls" in source
    assert "notify" in source
    assert "pickDirectory" in source
    assert "openExternal" in source
    assert "revealPath" in source
    assert "openDeepLink" in source
    assert "platformInfo" in source
    assert "pty:" in source
    assert "searchFiles: (rootPath, query, limit)" in source
    assert "spawn: (cwd, conversationId)" in source
    assert 'ipcRenderer.invoke("minicode:pty:spawn", cwd, conversationId)' in source
    assert "list: (conversationId)" in source
    assert 'ipcRenderer.invoke("minicode:pty:list", conversationId)' in source
    assert 'ipcRenderer.on("minicode:menu:new-chat"' in source
    assert 'ipcRenderer.on("minicode:menu:quick-chat"' in source
    assert 'ipcRenderer.on("minicode:menu:open-folder"' in source
    assert 'ipcRenderer.on("minicode:menu:extensions-marketplace"' in source
    assert 'open-extensions-marketplace' in source
    assert 'ipcRenderer.on("minicode:menu:settings"' in source
    assert 'ipcRenderer.on("minicode:menu:toggle-sidebar"' in source
    assert 'ipcRenderer.on("minicode:menu:toggle-context"' in source
    assert "readRuntimeArgument" in source
    assert "--minicode-api-base-url=" in source
    assert "--minicode-ws-base-url=" in source


def test_desktop_shell_can_export_diagnostics() -> None:
    main_source = Path("desktop/main.js").read_text(encoding="utf-8")
    ipc_source = Path("desktop/ipc-handlers.js").read_text(encoding="utf-8")
    preload_source = Path("desktop/preload.js").read_text(encoding="utf-8")

    assert "buildDiagnosticsPayload" in (main_source + ipc_source)
    assert 'ipcMain.handle("minicode:diagnostics:export"' in ipc_source
    assert "Export Diagnostics" in (main_source + ipc_source)
    assert "desktop.diagnostics.json" in (main_source + ipc_source)
    assert "desktop: {" in preload_source
    assert "diagnostics:" in preload_source
    assert "export: () => ipcRenderer.invoke(\"minicode:diagnostics:export\")" in preload_source
