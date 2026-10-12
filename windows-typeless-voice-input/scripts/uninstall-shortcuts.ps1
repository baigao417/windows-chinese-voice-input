[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
$Desktop = [Environment]::GetFolderPath("Desktop")
$StartMenu = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
$StartupFolder = [Environment]::GetFolderPath("Startup")
$Targets = @(
    (Join-Path $Desktop "Windows Typeless Voice Input.lnk"),
    (Join-Path $Desktop "Windows Typeless Voice Input - Daily Safe.lnk"),
    (Join-Path $StartMenu "Windows Typeless Voice Input.lnk"),
    (Join-Path $StartMenu "Windows Typeless Voice Input - Daily Safe.lnk"),
    (Join-Path $StartupFolder "Windows Typeless Voice Input.lnk")
)

foreach ($Path in $Targets) {
    if (Test-Path $Path) {
        if ($PSCmdlet.ShouldProcess($Path, "Remove shortcut")) {
            Remove-Item -LiteralPath $Path -Force
        }
        Write-Host "Removed: $Path"
    } else {
        Write-Host "Missing: $Path"
    }
}
