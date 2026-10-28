param([string]$PythonCommand = "python")
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $PythonCommand -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 'KMLink requires Python 3.11 or later.')"
    if ($LASTEXITCODE -ne 0) { throw "Install Python 3.11 or later, or pass -PythonCommand with its full path." }
    & $PythonCommand -m venv (Join-Path $ProjectRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create the project virtual environment." }
}
& $VenvPython -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 'Remove/recreate this project virtual environment with Python 3.11 or later.')"
if ($LASTEXITCODE -ne 0) { throw "The existing virtual environment uses an unsupported Python version." }
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not update pip in the project virtual environment." }
& $VenvPython -m pip install -e "${ProjectRoot}[build]"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Check the network and the error above." }
Write-Host "KMLink is ready. Run scripts\run.ps1 to open it."
