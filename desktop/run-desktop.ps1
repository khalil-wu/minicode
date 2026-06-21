$ErrorActionPreference = "Stop"

$utf8NoBom = New-Object System.Text.UTF8Encoding $false
try {
  [Console]::OutputEncoding = $utf8NoBom
  [Console]::InputEncoding = $utf8NoBom
  $OutputEncoding = $utf8NoBom
} catch {
  # Some launchers do not attach a real console; env vars below still keep
  # Python/Node subprocess output UTF-8.
}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
if ($IsWindows -or $env:OS -eq "Windows_NT") {
  chcp.com 65001 | Out-Null
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")

Write-Host "[MiniCode Desktop] Repo root: $repoRoot"

function Stop-ExistingMiniCodeDesktop {
  $desktopElectron = Join-Path $repoRoot "desktop\node_modules\electron\dist\electron.exe"
  if (-not (Test-Path $desktopElectron)) {
    return
  }

  $resolvedElectron = (Resolve-Path $desktopElectron).Path
  $currentPid = $PID
  $processes = Get-CimInstance Win32_Process -Filter "name = 'electron.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
      $_.ProcessId -ne $currentPid -and
      $_.ExecutablePath -and
      ([string]::Equals($_.ExecutablePath, $resolvedElectron, [System.StringComparison]::OrdinalIgnoreCase))
    }

  if (-not $processes) {
    return
  }

  Write-Host "[MiniCode Desktop] Closing existing desktop instance before rebuilding frontend assets..."
  foreach ($proc in $processes) {
    try {
      Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
    } catch {
      Write-Warning "Failed to stop existing Electron process $($proc.ProcessId): $($_.Exception.Message)"
    }
  }
  Start-Sleep -Milliseconds 500
}

function Invoke-Step {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Description,
    [Parameter(Mandatory = $true)]
    [scriptblock]$Action
  )

  Write-Host "[MiniCode Desktop] $Description"
  & $Action
  if ($LASTEXITCODE -ne 0) {
    throw "Step failed: $Description (exit code: $LASTEXITCODE)"
  }
}

# The Electron shell loads Vite's hashed dynamic chunks from frontend/dist.
# Rebuilding while an old desktop window is alive deletes chunks that the old
# renderer may still lazy-load, which turns into a white screen. Close this
# project's previous Electron instance before touching dist.
Stop-ExistingMiniCodeDesktop

# Ensure frontend dependencies are installed.
if (-not (Test-Path (Join-Path $repoRoot "frontend\node_modules"))) {
  Invoke-Step -Description "Installing frontend dependencies..." -Action {
    npm --prefix "$repoRoot\frontend" install
  }
}

# Ensure desktop dependencies are installed.
if (-not (Test-Path (Join-Path $repoRoot "desktop\node_modules\electron\package.json"))) {
  Invoke-Step -Description "Installing desktop dependencies..." -Action {
    npm --prefix "$repoRoot\desktop" install
  }
}

# Ensure backend PDF/document parsing dependencies are present in the same
# Python environment used by the managed desktop sidecar.
$pythonCommand = if ($env:MINICODE_PYTHON) { $env:MINICODE_PYTHON } elseif ($IsWindows -or $env:OS -eq "Windows_NT") { "py" } else { "python3" }
& $pythonCommand -c "import importlib.util, sys; sys.exit(0 if (importlib.util.find_spec('pymupdf') or importlib.util.find_spec('fitz')) and importlib.util.find_spec('docx') else 1)"
if ($LASTEXITCODE -ne 0) {
  Invoke-Step -Description "Installing backend document parsing dependencies..." -Action {
    & $pythonCommand -m pip install -e "${repoRoot}[docparse]"
  }
}

# Build frontend assets used by Electron shell.
Invoke-Step -Description "Building frontend..." -Action {
  $env:MINICODE_VITE_RELATIVE_BASE = "1"
  npm --prefix "$repoRoot\frontend" run build
  Remove-Item Env:MINICODE_VITE_RELATIVE_BASE -ErrorAction SilentlyContinue
}

# Start desktop client (Electron + managed backend sidecar).
Invoke-Step -Description "Launching desktop client..." -Action {
  npm --prefix "$repoRoot\desktop" run start
}
