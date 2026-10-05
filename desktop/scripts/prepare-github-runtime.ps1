$ErrorActionPreference = "Stop"
$version = "2.102.0"
$archiveSha256 = "ae64e556ecc240b200f7eba60d550e4bb60d78e860e69dd88c449405b86067f4"
$desktopRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$runtimeRoot = Join-Path $desktopRoot "github-runtime"
$githubCommand = Join-Path $runtimeRoot "bin\gh.exe"

if (Test-Path -LiteralPath $githubCommand -PathType Leaf) {
    $reportedVersion = (& $githubCommand --version | Select-Object -First 1)
    if ($LASTEXITCODE -eq 0 -and $reportedVersion.StartsWith("gh version $version ")) {
        Write-Output "GitHub CLI $version is ready at $githubCommand"
        exit 0
    }
}

$cacheRoot = Join-Path $desktopRoot "..\.tmp\github-cli"
$archive = Join-Path $cacheRoot "gh_${version}_windows_amd64.zip"
New-Item -ItemType Directory -Force -Path $cacheRoot, $runtimeRoot | Out-Null
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    $downloadUrl = "https://github.com/cli/cli/releases/download/v$version/gh_${version}_windows_amd64.zip"
    curl.exe -L --fail --silent --show-error -o $archive $downloadUrl
    if ($LASTEXITCODE -ne 0) { throw "Could not download official GitHub CLI $version" }
}

if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $archiveSha256) {
    throw "Official GitHub CLI archive failed SHA-256 verification"
}
Expand-Archive -LiteralPath $archive -DestinationPath $runtimeRoot -Force
& $githubCommand --version
if ($LASTEXITCODE -ne 0) { throw "Prepared GitHub CLI could not start" }
Write-Output "Prepared official GitHub CLI $version at $githubCommand"
