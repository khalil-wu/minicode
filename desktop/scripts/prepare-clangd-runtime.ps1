$ErrorActionPreference = "Stop"
$taskClangdVersion = "23.1.0"
$taskDesktopRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$taskRuntimeRoot = Join-Path $taskDesktopRoot "language-services\clangd"
$taskClangdCommand = Join-Path $taskRuntimeRoot "clangd_$taskClangdVersion\bin\clangd.exe"
if (Test-Path -LiteralPath $taskClangdCommand -PathType Leaf) {
    & $taskClangdCommand --version
    if ($LASTEXITCODE -eq 0) { exit 0 }
}
$taskCacheRoot = Join-Path $taskDesktopRoot "..\.tmp\clangd"
$taskArchive = Join-Path $taskCacheRoot "clangd-windows-$taskClangdVersion.zip"
New-Item -ItemType Directory -Force -Path $taskCacheRoot, $taskRuntimeRoot | Out-Null
if (-not (Test-Path -LiteralPath $taskArchive -PathType Leaf)) {
    curl.exe -L --fail --silent --show-error -o $taskArchive "https://github.com/clangd/clangd/releases/download/$taskClangdVersion/clangd-windows-$taskClangdVersion.zip"
    if ($LASTEXITCODE -ne 0) { throw "Could not download official clangd $taskClangdVersion" }
}
Expand-Archive -LiteralPath $taskArchive -DestinationPath $taskRuntimeRoot -Force
& $taskClangdCommand --version
if ($LASTEXITCODE -ne 0) { throw "Prepared clangd could not start" }
