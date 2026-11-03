param(
  [string]$PythonVersion = "3.11.9"
)

$ErrorActionPreference = "Stop"
$runtimeLayoutVersion = "2"
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

function Get-FileSha256([string]$Path) {
  $sha256 = [System.Security.Cryptography.SHA256]::Create()
  $stream = [System.IO.File]::OpenRead($Path)
  try {
    return ([System.BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
  } finally {
    $stream.Dispose()
    $sha256.Dispose()
  }
}
$requirementsHash = Get-FileSha256 $requirements
$expectedStamp = "$runtimeLayoutVersion`n$PythonVersion`n$requirementsHash"

function Assert-WithinRuntime([string]$Path, [switch]$AllowRoot) {
  $candidate = [System.IO.Path]::GetFullPath($Path)
  $runtimePrefix = $runtimeFull.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
  $isRoot = [string]::Equals($candidate, $runtimeFull, [System.StringComparison]::OrdinalIgnoreCase)
  if (-not (($AllowRoot -and $isRoot) -or $candidate.StartsWith($runtimePrefix, [System.StringComparison]::OrdinalIgnoreCase))) {
    throw "Refusing to remove a path outside the bundled Python runtime: $candidate"
  }
  $ancestor = $candidate
  while ($ancestor.StartsWith($desktopFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or [string]::Equals($ancestor, $desktopFull, [StringComparison]::OrdinalIgnoreCase)) {
    if (Test-Path -LiteralPath $ancestor) {
      $item = Get-Item -LiteralPath $ancestor -Force
      if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to manage a runtime path through a reparse point: $ancestor"
      }
    }
    if ([string]::Equals($ancestor, $desktopFull, [StringComparison]::OrdinalIgnoreCase)) { break }
    $ancestor = Split-Path -Parent $ancestor
  }
  if (Test-Path -LiteralPath $candidate) {
    $candidate = (Resolve-Path -LiteralPath $candidate).ProviderPath
    if (-not (($AllowRoot -and [string]::Equals($candidate, $runtimeFull, [StringComparison]::OrdinalIgnoreCase)) -or $candidate.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase))) {
      throw "Resolved runtime target escaped the managed directory: $candidate"
    }
  }
  return $candidate
}

function Remove-RuntimeBuildArtifacts {
  $checkedRuntime = Assert-WithinRuntime $runtimeRoot -AllowRoot
  $directories = Get-ChildItem -LiteralPath $checkedRuntime -Recurse -Force -Directory | Where-Object {
    $_.Name -in @("__pycache__", ".pytest_cache", ".ruff_cache", "test", "tests")
  } | Sort-Object { $_.FullName.Length } -Descending

  foreach ($directory in $directories) {
    $target = Assert-WithinRuntime $directory.FullName
    if (Test-Path -LiteralPath $target) {
      Remove-Item -LiteralPath $target -Recurse -Force
    }
  }

  $compiledFiles = Get-ChildItem -LiteralPath $runtimeRoot -Recurse -Force -File | Where-Object {
    $_.Extension -in @(".pyc", ".pyo")
  }
  foreach ($file in $compiledFiles) {
    $target = Assert-WithinRuntime $file.FullName
    Remove-Item -LiteralPath $target -Force
  }
}

if ((Test-Path -LiteralPath (Join-Path $runtimeRoot "python.exe")) -and (Test-Path -LiteralPath $stamp)) {
  if ((Get-Content -Raw -LiteralPath $stamp).Trim() -eq $expectedStamp.Trim()) {
    Remove-RuntimeBuildArtifacts
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
  $target = Assert-WithinRuntime $runtimeRoot -AllowRoot
  Remove-Item -LiteralPath $target -Recurse -Force
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

# pip is bootstrapped from its pinned wheel rather than get-pip.py: the current
# get-pip.py runs pip with argv[0] "pip", which trips pip's own refusal to
# modify itself on Windows.
$pipVersion = "26.3"
$pipWheelSha256 = "f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13"
$pipWheel = Join-Path $downloadRoot "pip-$pipVersion-py3-none-any.whl"
if (-not (Test-Path -LiteralPath $pipWheel)) {
  Invoke-WebRequest -UseBasicParsing -Uri "https://files.pythonhosted.org/packages/07/25/ee8c38ae72d66b844ce8eb9711ba905dba21ca1a8a0c960057c41cd23337/pip-$pipVersion-py3-none-any.whl" -OutFile $pipWheel
}
if ((Get-FileSha256 $pipWheel) -ne $pipWheelSha256) { throw "Pinned pip wheel failed SHA-256 verification" }
& (Join-Path $runtimeRoot "python.exe") -c "import sys, runpy; sys.path.insert(0, sys.argv[1]); sys.argv = ['pip-bootstrap', 'install', '--disable-pip-version-check', '--no-warn-script-location', sys.argv[1]]; runpy.run_module('pip', run_name='__main__', alter_sys=True)" $pipWheel
if ($LASTEXITCODE -ne 0) { throw "Failed to bootstrap pip into the embedded runtime." }
& (Join-Path $runtimeRoot "python.exe") -m pip install --disable-pip-version-check --no-warn-script-location -r $requirements
if ($LASTEXITCODE -ne 0) { throw "Failed to install sidecar dependencies." }

Remove-RuntimeBuildArtifacts
Set-Content -LiteralPath $stamp -Value $expectedStamp -Encoding ascii
Write-Host "Bundled Python $PythonVersion runtime prepared at $runtimeRoot"
