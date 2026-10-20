$ErrorActionPreference = 'Stop'
$source = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'dictation-keyboard.cs') -Raw
Add-Type -TypeDefinition $source -ReferencedAssemblies System.Web.Extensions
[MiniCode.DictationKeyboard]::Run()
