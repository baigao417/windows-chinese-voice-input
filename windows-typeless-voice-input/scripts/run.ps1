param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (!(Test-Path $VenvPython)) {
    throw "Virtualenv not found. Run .\scripts\install.ps1 first."
}

$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = Join-Path $Root "src"
& $VenvPython -m win_voice_input @Args
exit $LASTEXITCODE
