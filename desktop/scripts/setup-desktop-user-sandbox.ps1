param(
  [Parameter(Mandatory = $true)][int]$ShellProcessId,
  [Parameter(Mandatory = $true)][string]$UserDataFolderName
)

$ErrorActionPreference = "Stop"
$sessionId = (Get-Process -Id $PID).SessionId
$desktopShell = Get-CimInstance Win32_Process -Filter "ProcessId = $ShellProcessId" -ErrorAction Stop
if (-not $desktopShell -or $desktopShell.SessionId -ne $sessionId) {
  throw "The desktop shell does not belong to this installation's interactive session."
}
$owner = Invoke-CimMethod -InputObject $desktopShell -MethodName GetOwner -ErrorAction Stop
$ownerSid = Invoke-CimMethod -InputObject $desktopShell -MethodName GetOwnerSid -ErrorAction Stop
if ($owner.ReturnValue -ne 0 -or $ownerSid.ReturnValue -ne 0) {
  throw "Could not resolve the current desktop shell owner."
}
$account = "$($owner.Domain)\$($owner.User)"
$accountIdentity = New-Object Security.Principal.NTAccount -ArgumentList $account
$resolvedSid = $accountIdentity.Translate([Security.Principal.SecurityIdentifier]).Value
if ($resolvedSid -ne $ownerSid.Sid) {
  throw "The desktop shell account and SID disagree."
}
$folders = Get-ItemProperty -LiteralPath "Registry::HKEY_USERS\$resolvedSid\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders" -Name AppData -ErrorAction Stop
$userDataRoot = Join-Path $folders.AppData $UserDataFolderName
$sandboxHome = Join-Path $userDataRoot "data\windows-sandbox"
$runtime = Join-Path $PSScriptRoot "codex.exe"
& $runtime sandbox setup --elevated --user $account --codex-home $sandboxHome
if ($LASTEXITCODE -ne 0) {
  throw "Desktop-user Windows sandbox setup failed (exit code $LASTEXITCODE)."
}
