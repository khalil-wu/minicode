!macro customInstall
  DetailPrint "Installing MiniCode Windows sandbox accounts, ACLs, and WFP filters"
  ; The desktop shell's owner can differ from the elevated installer account.
  System::Call 'user32::GetShellWindow() p.r0'
  System::Call 'user32::GetWindowThreadProcessId(p r0, *i .r1) i.r2'
  StrCmp $1 0 0 +2
  Abort "MiniCode setup requires a current interactive desktop shell."
  ExecWait '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\windows-sandbox\setup-desktop-user-sandbox.ps1" -ShellProcessId $1 -UserDataFolderName "${APP_PACKAGE_NAME}"' $0
  DetailPrint "Windows sandbox setup exit code: $0"
  IntCmp $0 0 +2
  Abort "MiniCode Windows sandbox setup failed (exit code $0)"
!macroend
