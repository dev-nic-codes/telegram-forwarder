$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Missing .venv. Create it with: py -3.12 -m venv .venv"
}
& $python run.py
exit $LASTEXITCODE
