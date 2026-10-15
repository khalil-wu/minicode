param([string]$Cargo = "", [string]$Python = "python", [string]$OutputDirectory = "")
$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
if (-not $Cargo) {
    $Cargo = Join-Path $env:USERPROFILE ".rustup/toolchains/1.95.0-x86_64-pc-windows-msvc/bin/cargo.exe"
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repoRoot "backend/permissions/resources/execpolicy/x86_64-pc-windows-msvc"
}
& $Python (Join-Path $PSScriptRoot "prepare-execpolicy-runtime.py") --cargo $Cargo --output-directory $OutputDirectory
if ($LASTEXITCODE -ne 0) { throw "Pinned Source execpolicy runtime preparation failed" }
