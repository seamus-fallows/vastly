#Requires -Version 5.1
<#
.SYNOPSIS
    Installs the Vastly PowerShell module.

.DESCRIPTION
    Copies module files to $env:USERPROFILE\.vastly\, optionally adds an
    Import-Module line to $PROFILE, and creates a default config file if
    one doesn't already exist.

    Safe to re-run: module files are overwritten with the latest version,
    user config is never touched, and $PROFILE is checked for duplicates.

.EXAMPLE
    .\Install.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- Paths ---
$RepoRoot    = $PSScriptRoot
$InstallDir  = Join-Path $env:USERPROFILE '.vastly'
$ConfigPath  = Join-Path $env:USERPROFILE '.vastly.json'
$TemplatePath = Join-Path $RepoRoot '.vastly.template.json'
$ImportLine  = "Import-Module `"$InstallDir\Vastly.psd1`""

$ModuleFiles = @(
    'Vastly.psd1'
    'Vastly.psm1'
    'setup-remote.sh'
    '.vastly.template.json'
)

# --- Step 1: Copy module files ---
Write-Host ''
Write-Host 'Installing Vastly...' -ForegroundColor Cyan
Write-Host ''

if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Write-Host "  Created $InstallDir"
}

foreach ($file in $ModuleFiles) {
    $src = Join-Path $RepoRoot $file
    $dst = Join-Path $InstallDir $file

    if (-not (Test-Path $src)) {
        Write-Warning "  Source file not found, skipping: $src"
        continue
    }

    Copy-Item -Path $src -Destination $dst -Force
    Write-Host "  Copied $file"
}

# --- Step 2: Prompt to add to $PROFILE ---
Write-Host ''
$addToProfile = Read-Host 'Add to $PROFILE so commands are always available? [Y/n]'

if ($addToProfile -eq '' -or $addToProfile -match '^[Yy]') {
    # Ensure the $PROFILE directory exists
    $profileDir = Split-Path $PROFILE -Parent
    if (-not (Test-Path $profileDir)) {
        New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
        Write-Host "  Created profile directory: $profileDir"
    }

    # Create $PROFILE if it doesn't exist
    if (-not (Test-Path $PROFILE)) {
        New-Item -ItemType File -Path $PROFILE -Force | Out-Null
        Write-Host "  Created profile: $PROFILE"
    }

    # Check for existing Import-Module line before appending
    $profileContent = Get-Content $PROFILE -Raw -ErrorAction SilentlyContinue
    if ($profileContent -and $profileContent.Contains($ImportLine)) {
        Write-Host "  `$PROFILE already contains the Import-Module line. No changes made."
    }
    else {
        Add-Content -Path $PROFILE -Value "`n$ImportLine"
        Write-Host "  Added Import-Module line to `$PROFILE"
    }
}
else {
    Write-Host '  Skipped. You can always add it manually later:'
    Write-Host "    $ImportLine" -ForegroundColor DarkGray
}

# --- Step 3: Create default config if missing ---
Write-Host ''
if (Test-Path $ConfigPath) {
    Write-Host "  Config already exists at $ConfigPath -- not modified."
}
elseif (Test-Path $TemplatePath) {
    Copy-Item -Path $TemplatePath -Destination $ConfigPath
    Write-Host "  Created default config at $ConfigPath"
    Write-Host '  Edit this file to set your IDE, SSH key, port forwards, etc.'
}
else {
    Write-Warning "  Template not found at $TemplatePath -- skipping config creation."
}

# --- Done ---
Write-Host ''
Write-Host 'Done!' -ForegroundColor Green
Write-Host "  Restart your terminal or run: $ImportLine"
Write-Host ''
