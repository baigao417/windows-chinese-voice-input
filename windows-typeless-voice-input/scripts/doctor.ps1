param(
    [switch]$ApiSmoke
)

$ErrorActionPreference = "Continue"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$ConfigPath = Join-Path $Root "config.json"
if (!(Test-Path $ConfigPath)) {
    $ConfigPath = Join-Path $Root "config.example.json"
}

function Resolve-ConfigRelativePath {
    param([string]$PathValue)
    if (!$PathValue) { return $null }
    if ([System.IO.Path]::IsPathRooted($PathValue)) { return $PathValue }
    return (Join-Path $Root $PathValue)
}

function Test-ApiKeySource {
    param(
        [object]$Section,
        [string]$Label
    )
    if (!$Section) {
        Write-Host "FAIL $Label config section missing"
        return
    }
    $EnvName = $Section.api_key_env
    if ($EnvName -and [Environment]::GetEnvironmentVariable($EnvName)) {
        Write-Host "OK  $Label API key env is set: $EnvName"
        return
    }
    if ($Section.secrets_file) {
        $SecretCandidate = Resolve-ConfigRelativePath $Section.secrets_file
        $SecretsPath = Resolve-Path -Path $SecretCandidate -ErrorAction SilentlyContinue
        if ($SecretsPath) {
            $Secrets = Get-Content -Raw $SecretsPath | ConvertFrom-Json
            $Provider = $Section.secrets_provider
            $KeyName = $Section.secrets_key
            $KeyValue = $Secrets.$Provider.$KeyName
            if ($KeyValue) {
                Write-Host "OK  $Label API key found in secrets file: $($Section.secrets_file)"
            } else {
                Write-Host "FAIL $Label API key missing in secrets file provider/key: $Provider/$KeyName"
            }
        } else {
            Write-Host "FAIL $Label secrets file not found: $($Section.secrets_file)"
        }
        return
    }
    Write-Host "FAIL $Label no API key source configured"
}

Write-Host "== Windows Typeless Voice Input Doctor =="
Write-Host "Root: $Root"

Write-Host "`n[1] Python venv"
if (Test-Path $VenvPython) {
    Write-Host "OK  $VenvPython"
    & $VenvPython --version
} else {
    Write-Host "FAIL venv missing. Run: .\scripts\install.ps1"
}

Write-Host "`n[2] Python packages"
if (Test-Path $VenvPython) {
@'
import importlib
mods = ["sounddevice", "keyboard", "pyperclip", "pyautogui", "faster_whisper", "ctranslate2", "numpy"]
for m in mods:
    try:
        mod = importlib.import_module(m)
        print("OK  ", m, getattr(mod, "__version__", ""))
    except Exception as e:
        print("FAIL", m, repr(e))
'@ | & $VenvPython -
}

Write-Host "`n[2.5] STT provider config"
try {
    $Config = Get-Content -Raw $ConfigPath | ConvertFrom-Json
    Write-Host "Engine: $($Config.stt.engine)"
    Write-Host "Model : $($Config.stt.model)"
    Test-ApiKeySource $Config.stt "STT"
} catch {
    Write-Host "WARN cannot check STT config: $($_.Exception.Message)"
}

Write-Host "`n[3] Audio input devices"
if (Test-Path $VenvPython) {
    & (Join-Path $Root "scripts\run.ps1") --list-devices
}

Write-Host "`n[4] Configured LLM provider"
try {
    if (!$Config) {
        $Config = Get-Content -Raw $ConfigPath | ConvertFrom-Json
    }
    $Llm = $Config.llm
    Write-Host "Enabled : $($Llm.enabled)"
    Write-Host "Provider: $($Llm.provider)"
    Write-Host "Model   : $($Llm.model)"
    if ($Llm.fallback_model) {
        Write-Host "Fallback: $($Llm.fallback_model)"
    }

    if (!$Llm.enabled) {
        Write-Host "WARN LLM rewrite disabled"
    } elseif ($Llm.provider -eq "openai-compatible") {
        Test-ApiKeySource $Llm "LLM"
    } elseif ($Llm.provider -eq "ollama") {
        $OllamaCommand = Get-Command ollama -ErrorAction SilentlyContinue
        if ($OllamaCommand) {
            $OllamaPath = $OllamaCommand.Source
            Write-Host "OK  ollama in PATH: $OllamaPath"
        } else {
            $Candidate = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
            if (Test-Path $Candidate) {
                $OllamaPath = $Candidate
                Write-Host "WARN ollama not in PATH, but found: $OllamaPath"
            } else {
                $OllamaPath = $null
                Write-Host "FAIL ollama.exe not found. Install: winget install Ollama.Ollama"
            }
        }
        if ($OllamaPath) {
            & $OllamaPath --version
        }
        try {
            $Tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 5
            Write-Host "OK  http://127.0.0.1:11434/api/tags"
            $Names = @($Tags.models | ForEach-Object { $_.name })
            $Model = $Llm.model
            if (($Names -contains $Model) -or ($Names -contains "$Model:latest")) {
                Write-Host "OK  model installed: $Model"
            } else {
                Write-Host "FAIL model not installed: $Model"
                if ($OllamaPath) {
                    Write-Host "Install it:"
                    Write-Host "  & `"$OllamaPath`" pull $Model"
                }
            }
        } catch {
            Write-Host "FAIL Ollama server not reachable: $($_.Exception.Message)"
            if ($OllamaPath) {
                Write-Host "Try starting it manually: & `"$OllamaPath`" serve"
            }
        }
    } else {
        Write-Host "WARN unsupported LLM provider in doctor: $($Llm.provider)"
    }
} catch {
    Write-Host "WARN cannot check LLM config: $($_.Exception.Message)"
}

Write-Host "`n[5] Dry run"
if (Test-Path $VenvPython) {
    & (Join-Path $Root "scripts\run.ps1") --dry-run
}

Write-Host "`n[6] Optional API smoke"
if ($ApiSmoke) {
    if (Test-Path $VenvPython) {
        & (Join-Path $Root "scripts\run.ps1") --api-smoke
    }
} else {
    Write-Host "SKIP API smoke. Run .\scripts\doctor.ps1 -ApiSmoke to check provider connectivity without inference calls."
}
