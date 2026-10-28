$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Run scripts\setup.ps1 first."
}
& $VenvPython -m kmlink
if ($LASTEXITCODE -ne 0) { throw "KMLink exited with code $LASTEXITCODE." }
