param(
  [string]$PythonVersion = "3.11.9"
)

$ErrorActionPreference = "Stop"
$desktopRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $desktopRoot "python-runtime"
$requirements = Join-Path $desktopRoot "requirements-sidecar.lock"
$stamp = Join-Path $runtimeRoot ".minicode-runtime"
$desktopFull = [System.IO.Path]::GetFullPath($desktopRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
$runtimeFull = [System.IO.Path]::GetFullPath($runtimeRoot)
if (-not $runtimeFull.StartsWith($desktopFull + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Refusing to manage a Python runtime outside the desktop directory: $runtimeFull"
}
if (-not (Test-Path -LiteralPath $requirements)) {
  throw "Sidecar requirements lock is missing: $requirements"
}

$sha256 = [System.Security.Cryptography.SHA256]::Create()
$stream = [System.IO.File]::OpenRead($requirements)
try {
  $requirementsHash = ([System.BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
} finally {
  $stream.Dispose()
  $sha256.Dispose()
}
$expectedStamp = "$PythonVersion`n$requirementsHash"

if ((Test-Path -LiteralPath (Join-Path $runtimeRoot "python.exe")) -and (Test-Path -LiteralPath $stamp)) {
  if ((Get-Content -Raw -LiteralPath $stamp).Trim() -eq $expectedStamp.Trim()) {
    Write-Host "Bundled Python runtime is current."
    exit 0
  }
}

$downloadRoot = Join-Path $env:TEMP "minicode-python-runtime"
New-Item -ItemType Directory -Force -Path $downloadRoot | Out-Null
$archive = Join-Path $downloadRoot "python-$PythonVersion-embed-amd64.zip"
$url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"

if (-not (Test-Path -LiteralPath $archive)) {
  Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive
}
if (Test-Path -LiteralPath $runtimeRoot) {
  Remove-Item -LiteralPath $runtimeRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
Expand-Archive -LiteralPath $archive -DestinationPath $runtimeRoot -Force

$pth = Get-ChildItem -LiteralPath $runtimeRoot -Filter "python*._pth" | Select-Object -First 1
if (-not $pth) { throw "Embedded Python ._pth file was not found." }
$pthContent = Get-Content -LiteralPath $pth.FullName
$pthContent = $pthContent | ForEach-Object { if ($_ -eq "#import site") { "import site" } else { $_ } }
if ($pthContent -notcontains "Lib\site-packages") { $pthContent += "Lib\site-packages" }
if ($pthContent -notcontains "..") { $pthContent += ".." }
if ($pthContent -notcontains "..\..") { $pthContent += "..\.." }
Set-Content -LiteralPath $pth.FullName -Value $pthContent -Encoding ascii

$getPip = Join-Path $downloadRoot "get-pip.py"
if (-not (Test-Path -LiteralPath $getPip)) {
  Invoke-WebRequest -UseBasicParsing -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
}
& (Join-Path $runtimeRoot "python.exe") $getPip --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "Failed to bootstrap pip into the embedded runtime." }
& (Join-Path $runtimeRoot "python.exe") -m pip install --disable-pip-version-check --no-warn-script-location -r $requirements
if ($LASTEXITCODE -ne 0) { throw "Failed to install sidecar dependencies." }

Set-Content -LiteralPath $stamp -Value $expectedStamp -Encoding ascii
Write-Host "Bundled Python $PythonVersion runtime prepared at $runtimeRoot"
