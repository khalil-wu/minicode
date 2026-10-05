[CmdletBinding(DefaultParameterSetName = "Installer")]
param(
  [Parameter(Mandatory = $true)][int]$ShellProcessId,
  [Parameter(Mandatory = $true, ParameterSetName = "Installer")][string]$UserDataFolderName,
  [Parameter(Mandatory = $true, ParameterSetName = "Desktop")][string]$SandboxHome,
  [string]$RuntimePath = (Join-Path $PSScriptRoot "codex.exe")
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
if ($PSCmdlet.ParameterSetName -eq "Installer") {
  $folders = Get-ItemProperty -LiteralPath "Registry::HKEY_USERS\$resolvedSid\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders" -Name AppData -ErrorAction Stop
  $UserDataRoot = Join-Path $folders.AppData $UserDataFolderName
  $SandboxHome = Join-Path $UserDataRoot "data\windows-sandbox"
}
& $RuntimePath sandbox setup --elevated --user $account --codex-home $SandboxHome
if ($LASTEXITCODE -ne 0) {
  throw "Desktop-user Windows sandbox setup failed (exit code $LASTEXITCODE)."
}
