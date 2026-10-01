!macro customInstall
  DetailPrint "Installing MiniCode Windows sandbox accounts, ACLs, and WFP filters"
  ; Per-machine install uses all-users shell variables. Sandbox state follows
  ; Electron's current-user userData, never the shared ProgramData directory.
  SetShellVarContext current
  StrCpy $R8 "$APPDATA\minicode-desktop\data\windows-sandbox"
  SetShellVarContext all
  ExecWait '"$INSTDIR\resources\windows-sandbox\codex.exe" sandbox setup --elevated --current-user --codex-home "$R8"' $0
  DetailPrint "Windows sandbox setup exit code: $0"
  IntCmp $0 0 +2
  Abort "MiniCode Windows sandbox setup failed (exit code $0)"
!macroend
