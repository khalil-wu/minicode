param([string]$Cargo = "", [string]$Python = "python", [string]$SourceCheckout = "", [string]$OutputDirectory = "")
$ErrorActionPreference = 'Stop'
$networkRepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if (-not $Cargo) { $Cargo = Join-Path $env:USERPROFILE '.rustup/toolchains/1.99.1-x86_64-pc-windows-msvc/bin/cargo.exe' }
if (-not $SourceCheckout) { $SourceCheckout = Join-Path $networkRepoRoot '.tmp/codex-src' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $networkRepoRoot 'backend/network/resources/x86_64-pc-windows-msvc' }
& $Python (Join-Path $PSScriptRoot 'prepare-network-runtime.py') --cargo $Cargo --source-checkout $SourceCheckout --build-directory (Join-Path $networkRepoRoot '.tmp/native-network-release') --output-directory $OutputDirectory
if ($LASTEXITCODE -ne 0) { throw 'Pinned Source network runtime preparation failed' }
