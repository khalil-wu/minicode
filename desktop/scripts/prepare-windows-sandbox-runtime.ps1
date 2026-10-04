$ErrorActionPreference = "Stop"
$version = "0.158.0-alpha.2.1"
$archiveHash = "e0a5e3ba9421f3f3f46836974d0396bbd8782721b4913b614ff0267911e0a3ee"
$desktopRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$repoRoot = [IO.Path]::GetFullPath((Join-Path $desktopRoot ".."))
$destinationRoot = Join-Path $desktopRoot "windows-sandbox-runtime"
$cacheRoot = Join-Path $repoRoot ".tmp"
$archive = Join-Path $cacheRoot "codex-rust-v$version.zip"
$buildRoot = Join-Path $cacheRoot "minicode-windows-sandbox-v$version"
$sourceRoot = Join-Path $buildRoot "codex-rust-v$version"
$manifestPath = Join-Path $destinationRoot "runtime.json"
$runtimeFiles = @("codex.exe", "codex-command-runner.exe", "codex-windows-sandbox-setup.exe")
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$sourceFingerprint = (python (Join-Path $PSScriptRoot "patch-windows-sandbox-source.py") --fingerprint | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not fingerprint native sandbox source inputs" }

function FileSha256([string]$file) {
    $stream = [IO.File]::OpenRead($file)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

if (Test-Path -LiteralPath $manifestPath) {
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    $ready = $manifest.identity -eq "minicode" -and $manifest.upstream_version -eq $version -and $manifest.patch_version -eq 3 -and $manifest.input_sha256 -eq $sourceFingerprint -and $manifest.source_sha256 -eq $archiveHash -and $manifest.owner_namespace -eq "windows-sid+canonical-home-v3" -and $manifest.reported_version -eq "minicode-windows-sandbox $version owner-v3" -and $null -eq $manifest.offline_account -and $null -eq $manifest.online_account
    foreach ($name in @("LICENSE.openai-codex", "NOTICE.openai-codex")) {
        $ready = $ready -and (Test-Path -LiteralPath (Join-Path $destinationRoot $name) -PathType Leaf)
    }
    foreach ($name in $runtimeFiles) {
        $file = Join-Path $destinationRoot $name
        $expected = $manifest.files.$name
        $ready = $ready -and (Test-Path -LiteralPath $file -PathType Leaf)
        if ($ready) {
            $ready = (FileSha256 $file) -eq $expected
        }
    }
    if ($ready) {
        Write-Output "MiniCode Windows sandbox runtime is ready at $destinationRoot"
        exit 0
    }
}

New-Item -ItemType Directory -Force -Path $cacheRoot, $destinationRoot | Out-Null
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    $url = "https://codeload.github.com/openai/codex/zip/refs/tags/rust-v$version"
    curl.exe -L --fail --silent --show-error --max-time 900 -o $archive $url
    if ($LASTEXITCODE -ne 0) { throw "Could not download pinned Codex source archive" }
}
if ((FileSha256 $archive) -ne $archiveHash) {
    throw "Pinned Codex source archive failed SHA-256 verification"
}

$identityMarker = Join-Path $sourceRoot "codex-rs\.minicode-windows-identity.json"
if (-not (Test-Path -LiteralPath $identityMarker)) {
    $resolvedCache = [IO.Path]::GetFullPath($cacheRoot).TrimEnd('\') + '\'
    $resolvedBuild = [IO.Path]::GetFullPath($buildRoot)
    if (-not $resolvedBuild.StartsWith($resolvedCache, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Windows sandbox build directory escaped the workspace cache"
    }
    if (Test-Path -LiteralPath $resolvedBuild) { throw "Unmarked source cache exists; refusing to replace local source: $resolvedBuild" }
    New-Item -ItemType Directory -Force -Path $resolvedBuild | Out-Null
    python -c "import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" $archive $resolvedBuild
    if ($LASTEXITCODE -ne 0) { throw "Could not extract pinned Codex source" }
    python (Join-Path $PSScriptRoot "patch-windows-sandbox-source.py") --source $sourceRoot
    if ($LASTEXITCODE -ne 0) { throw "Could not apply MiniCode Windows sandbox identity" }
}
python (Join-Path $PSScriptRoot "patch-windows-sandbox-source.py") --source $sourceRoot
if ($LASTEXITCODE -ne 0) { throw "Could not apply MiniCode owner namespace" }

$cargo = Get-Command cargo.exe -ErrorAction Stop
$previousTarget = $env:CARGO_TARGET_DIR
$env:CARGO_TARGET_DIR = Join-Path $buildRoot "target"
Push-Location (Join-Path $sourceRoot "codex-rs")
try {
    & $cargo.Source build --offline --release -p minicode-sandbox-launcher -p codex-windows-sandbox --bin minicode-sandbox-launcher --bin codex-command-runner --bin codex-windows-sandbox-setup
    if ($LASTEXITCODE -ne 0) { throw "MiniCode Windows sandbox Rust build failed" }
} finally {
    Pop-Location
    $env:CARGO_TARGET_DIR = $previousTarget
}

$releaseDir = Join-Path $buildRoot "target\release"
$outputs = @{
    "codex.exe" = "minicode-sandbox-launcher.exe"
    "codex-command-runner.exe" = "codex-command-runner.exe"
    "codex-windows-sandbox-setup.exe" = "codex-windows-sandbox-setup.exe"
}
foreach ($name in $runtimeFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $releaseDir $outputs[$name]) -PathType Leaf)) {
        throw "Native sandbox build did not produce $name"
    }
}
foreach ($name in @("LICENSE", "NOTICE")) {
    if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot $name) -PathType Leaf)) {
        throw "Pinned native sandbox source is missing $name"
    }
}
foreach ($name in $runtimeFiles) {
    Copy-Item -LiteralPath (Join-Path $releaseDir $outputs[$name]) -Destination (Join-Path $destinationRoot $name) -Force
}
Copy-Item -LiteralPath (Join-Path $sourceRoot "LICENSE") -Destination (Join-Path $destinationRoot "LICENSE.openai-codex") -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot "NOTICE") -Destination (Join-Path $destinationRoot "NOTICE.openai-codex") -Force
$reported = (& (Join-Path $destinationRoot "codex.exe") --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $reported -ne "minicode-windows-sandbox $version owner-v3") {
    throw "Bundled runtime reported an unexpected identity: $reported"
}
$hashes = @{}
foreach ($name in $runtimeFiles) {
    $hashes[$name] = FileSha256 (Join-Path $destinationRoot $name)
}
$manifest = @{
    identity = "minicode"
    upstream_version = $version
    source_sha256 = $archiveHash
    patch_version = 3
    owner_namespace = "windows-sid+canonical-home-v3"
    input_sha256 = $sourceFingerprint
    files = $hashes
    reported_version = $reported
} | ConvertTo-Json
[IO.File]::WriteAllText($manifestPath, $manifest + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Write-Output "Prepared $reported at $destinationRoot"
