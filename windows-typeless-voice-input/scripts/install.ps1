param(
    [switch]$RecreateVenv
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Venv = Join-Path $Root ".venv"
$Python = "python"

Write-Host "[1/4] Project root: $Root"

if ($RecreateVenv -and (Test-Path $Venv)) {
    $ResolvedVenv = (Resolve-Path -LiteralPath $Venv).Path
    $ExpectedVenv = [System.IO.Path]::GetFullPath((Join-Path $Root ".venv"))
    $Item = Get-Item -LiteralPath $Venv
    if ($ResolvedVenv -ne $ExpectedVenv -or ($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "Refusing to delete a redirected or unexpected virtualenv."
    }
    Write-Host "[2/4] Removing old venv: $Venv"
    Remove-Item -LiteralPath $Venv -Recurse -Force
}

if (!(Test-Path $Venv)) {
    Write-Host "[2/4] Creating venv..."
    & $Python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Virtualenv creation failed." }
} else {
    Write-Host "[2/4] Reusing venv: $Venv"
}

$VenvPython = Join-Path $Venv "Scripts\python.exe"
Write-Host "[3/4] Installing Python dependencies..."
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
& $VenvPython -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

$Config = Join-Path $Root "config.json"
$ConfigExample = Join-Path $Root "config.example.json"
if (!(Test-Path $Config)) {
    Copy-Item -LiteralPath $ConfigExample -Destination $Config
    Write-Host "[4/4] Created config.json from config.example.json"
} else {
    Write-Host "[4/4] config.json already exists"
}

if (!(Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "Ollama not found in PATH. Install it only when you want experimental local rewrite:"
    Write-Host "  winget install Ollama.Ollama"
    Write-Host "  ollama pull qwen2.5:3b"
    Write-Host "  ollama pull qwen3:4b"
}

Write-Host ""
Write-Host "Done. Start with:"
Write-Host "  .\scripts\run.ps1 --dry-run"
Write-Host "  .\scripts\run.ps1"
