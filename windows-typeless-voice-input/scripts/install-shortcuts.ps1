[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Startup,
    [ValidateSet("control-panel", "clipboard-only-daemon", "daily-daemon")]
    [string]$StartupMode = "control-panel"
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$ControlPanelScript = Join-Path $Root "scripts\control-panel.ps1"
$RunScript = Join-Path $Root "scripts\run.ps1"
$Desktop = [Environment]::GetFolderPath("Desktop")
$StartMenu = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
$StartupFolder = [Environment]::GetFolderPath("Startup")

function New-Shortcut {
    param(
        [string]$Path,
        [string]$Target,
        [string]$Arguments,
        [string]$WorkingDirectory,
        [string]$Description
    )
    if ($PSCmdlet.ShouldProcess($Path, "Create shortcut")) {
        $Shell = New-Object -ComObject WScript.Shell
        $Shortcut = $Shell.CreateShortcut($Path)
        $Shortcut.TargetPath = $Target
        $Shortcut.Arguments = $Arguments
        $Shortcut.WorkingDirectory = $WorkingDirectory
        $Shortcut.Description = $Description
        $Shortcut.IconLocation = "$env:SystemRoot\System32\SHELL32.dll,24"
        $Shortcut.Save()
    }
    Write-Host "Shortcut: $Path"
}

$PowerShell = (Get-Command powershell.exe).Source
$BaseArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$ControlPanelScript`""
$DailySafeArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$RunScript`" --daily"

New-Shortcut `
    -Path (Join-Path $Desktop "Windows Typeless Voice Input.lnk") `
    -Target $PowerShell `
    -Arguments $BaseArgs `
    -WorkingDirectory $Root `
    -Description "Open Windows Typeless Voice Input control panel"

New-Shortcut `
    -Path (Join-Path $StartMenu "Windows Typeless Voice Input.lnk") `
    -Target $PowerShell `
    -Arguments $BaseArgs `
    -WorkingDirectory $Root `
    -Description "Open Windows Typeless Voice Input control panel"

New-Shortcut `
    -Path (Join-Path $Desktop "Windows Typeless Voice Input - Daily Safe.lnk") `
    -Target $PowerShell `
    -Arguments $DailySafeArgs `
    -WorkingDirectory $Root `
    -Description "Start Windows Typeless Voice Input safe daily daemon"

New-Shortcut `
    -Path (Join-Path $StartMenu "Windows Typeless Voice Input - Daily Safe.lnk") `
    -Target $PowerShell `
    -Arguments $DailySafeArgs `
    -WorkingDirectory $Root `
    -Description "Start Windows Typeless Voice Input safe daily daemon"

if ($Startup) {
    if ($StartupMode -eq "daily-daemon") {
        $StartupArgs = $DailySafeArgs
        $Description = "Start Windows Typeless Voice Input safe daily daemon"
    } elseif ($StartupMode -eq "clipboard-only-daemon") {
        $StartupArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$RunScript`" --clipboard-only"
        $Description = "Start Windows Typeless Voice Input clipboard-only daemon"
    } else {
        $StartupArgs = $BaseArgs
        $Description = "Open Windows Typeless Voice Input control panel at login"
    }
    New-Shortcut `
        -Path (Join-Path $StartupFolder "Windows Typeless Voice Input.lnk") `
        -Target $PowerShell `
        -Arguments $StartupArgs `
        -WorkingDirectory $Root `
        -Description $Description
} else {
    Write-Host "Startup shortcut not enabled. Re-run with -Startup to create it."
}
