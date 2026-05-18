<#
.SYNOPSIS
    Vision Labs Windows installer -- sets up WSL2 + Hyper-V firewall rules,
    then hands off to the Linux installer running inside WSL.

.DESCRIPTION
    Two-phase install because Windows requires a reboot after WSL2 is enabled:

    PHASE 1 (this script, first run as Admin):
      - Verify Windows 11 (10 may work, untested)
      - Verify NVIDIA GPU present + driver has WSL-CUDA support
      - Run `wsl --install` (installs WSL2 + Ubuntu, requires reboot)
      - Add Hyper-V firewall rules for ONVIF discovery (UDP 1900 + 3702)
      - Write a .wslconfig with mirrored networking mode
      - Tell the user to reboot

    PHASE 2 (user runs this in Ubuntu after reboot):
      - User opens Ubuntu (start menu -> "Ubuntu")
      - User runs:  curl -fsSL <repo-url>/install-linux.sh | bash
      - Linux installer takes over from there

    We don't fully automate phase 2 because forcing a reboot mid-script and
    auto-resuming on the other side is fragile across Windows versions.

.PARAMETER SkipFirewallRules
    Skip adding the Hyper-V firewall rules for ONVIF discovery. Only matters
    if you don't plan to auto-discover ONVIF cameras (you can still add them
    manually by RTSP URL).

.EXAMPLE
    # In an elevated PowerShell. If you get "running scripts is disabled
    # on this system", use the explicit bypass form:
    powershell.exe -ExecutionPolicy Bypass -File .\install-windows.ps1

    # Or to allow any local script in this session (reverts on close):
    Set-ExecutionPolicy -Scope Process Bypass -Force
    .\install-windows.ps1

.NOTES
    Requires:  Admin privileges, Windows 10 22H2 or Windows 11, NVIDIA GPU
               with a recent driver (R535+ for WSL-CUDA), internet.
               If running scripts is blocked by ExecutionPolicy (default
               on stock Windows), use the bypass forms shown above.
#>

param(
    [switch]$SkipFirewallRules
)

$ErrorActionPreference = "Stop"

function Write-Step    { param([string]$msg) Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok      { param([string]$msg) Write-Host " [OK] $msg" -ForegroundColor Green }
function Write-Warn    { param([string]$msg) Write-Host " [!] $msg" -ForegroundColor Yellow }
function Write-Err     { param([string]$msg) Write-Host " [X] $msg" -ForegroundColor Red }
function Write-Heading { param([string]$msg) Write-Host "`n$msg" -ForegroundColor White -BackgroundColor DarkBlue }

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
Write-Heading "Vision Labs Windows installer"

# Must be admin
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(`
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Err "This script must be run as Administrator."
    Write-Host "  Right-click PowerShell -> Run as administrator, then re-run this script." -ForegroundColor Yellow
    exit 1
}
Write-Ok "Running as Administrator"

# Windows version
$osVer = [System.Environment]::OSVersion.Version
if ($osVer.Major -lt 10) {
    Write-Err "Requires Windows 10 22H2 or Windows 11. You have version $($osVer.Major).$($osVer.Minor)."
    exit 1
}
$winVer = (Get-CimInstance Win32_OperatingSystem).Caption
Write-Ok "Detected: $winVer"

# NVIDIA GPU + driver check
$gpus = @(Get-CimInstance Win32_VideoController | Where-Object { $_.Name -like "*NVIDIA*" })
if ($gpus.Count -eq 0) {
    Write-Err "No NVIDIA GPU detected. Vision Labs requires an NVIDIA GPU."
    Write-Host "  Apple Silicon / Intel iGPU / AMD GPU are not supported in v1." -ForegroundColor Yellow
    exit 1
}
$gpuName = $gpus[0].Name
$driverVersion = $gpus[0].DriverVersion
Write-Ok "GPU detected: $gpuName (driver $driverVersion)"

# Heuristic: WSL CUDA needs driver R535+ which corresponds to file version 31.0.15.3573 or so.
# A more reliable check is whether `nvidia-smi.exe` exists and reports something.
if (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue) {
    Write-Ok "nvidia-smi found -- driver supports CUDA workloads"
} else {
    Write-Warn "nvidia-smi.exe not in PATH. Driver may be too old for WSL-CUDA."
    Write-Host "  Recommend: download the latest driver from https://www.nvidia.com/Download/index.aspx" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# WSL2 install / verify
# ---------------------------------------------------------------------------
Write-Heading "Step 1/3 -- WSL2"

$wslOk = $false
try {
    $wslVersion = (wsl --version 2>$null | Out-String).Trim()
    if ($wslVersion -match "WSL version") {
        Write-Ok "WSL2 already installed"
        Write-Host "$wslVersion" -ForegroundColor DarkGray
        $wslOk = $true
    }
} catch {
    # wsl.exe not found or errored
}

if (-not $wslOk) {
    Write-Step "Installing WSL2 + Ubuntu (this will take 2-5 minutes + REQUIRE A REBOOT)..."
    try {
        & wsl --install --no-launch
        Write-Ok "WSL2 + Ubuntu installation queued"
        $needsReboot = $true
    } catch {
        Write-Err "wsl --install failed: $_"
        Write-Host "  Try manually: wsl --install --no-launch" -ForegroundColor Yellow
        exit 1
    }
}

# ---------------------------------------------------------------------------
# .wslconfig -- mirrored networking mode (enables LAN visibility + multicast)
# ---------------------------------------------------------------------------
Write-Heading "Step 2/3 -- WSL networking config"

$wslConfigPath = "$env:USERPROFILE\.wslconfig"
$mirroredAlreadySet = $false
if (Test-Path $wslConfigPath) {
    $content = Get-Content $wslConfigPath -Raw
    if ($content -match "networkingMode\s*=\s*mirrored") {
        Write-Ok ".wslconfig already has mirrored networking enabled"
        $mirroredAlreadySet = $true
    } else {
        Write-Warn ".wslconfig exists but doesn't set mirrored mode. Backing up + replacing."
        Copy-Item $wslConfigPath "$wslConfigPath.bak.$(Get-Date -Format 'yyyyMMddHHmmss')"
    }
}

if (-not $mirroredAlreadySet) {
    @"
[wsl2]
networkingMode=mirrored
"@ | Set-Content -Path $wslConfigPath -Encoding ASCII
    Write-Ok "Wrote $wslConfigPath with mirrored networking mode"
    Write-Host "  This gives WSL the Windows host's network interface (better for LAN cameras)." -ForegroundColor DarkGray
    $needsReboot = $true
}

# ---------------------------------------------------------------------------
# Hyper-V firewall rules for ONVIF auto-discovery
# ---------------------------------------------------------------------------
Write-Heading "Step 3/3 -- Hyper-V firewall rules for ONVIF discovery"

if ($SkipFirewallRules) {
    Write-Warn "Skipping firewall rules (you passed -SkipFirewallRules)."
    Write-Host "  Manual RTSP URL entry will still work; auto-discovery via multicast won't." -ForegroundColor Yellow
} else {
    # Find the WSL VM creator ID from existing Hyper-V firewall settings.
    # If WSL was just installed, this query may return nothing until reboot.
    $wslVMId = $null
    try {
        $vmSetting = Get-NetFirewallHyperVVMSetting -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($vmSetting) { $wslVMId = $vmSetting.Name }
    } catch {
        # Older Windows without Hyper-V firewall cmdlets
    }

    if (-not $wslVMId) {
        Write-Warn "No Hyper-V VM detected yet (probably because WSL2 was just installed)."
        Write-Host "  After you reboot, re-run this script -- it'll add the firewall rules then." -ForegroundColor Yellow
    } else {
        Write-Ok "Found Hyper-V VM: $wslVMId"

        function Add-HyperVRule {
            param([string]$Name, [int]$Port, [string]$Description)
            $existing = Get-NetFirewallHyperVRule -Name $Name -ErrorAction SilentlyContinue
            if ($existing) {
                Write-Host "  '$Description' already exists -- skipping." -ForegroundColor DarkGray
                return
            }
            New-NetFirewallHyperVRule -Name $Name -DisplayName $Description `
                -VMCreatorId $wslVMId -Direction Inbound -Action Allow `
                -Protocol UDP -LocalPorts $Port | Out-Null
            Write-Ok "Added: $Description (UDP $Port)"
        }

        Add-HyperVRule -Name "VL-SSDP-Inbound" -Port 1900 -Description "Vision Labs SSDP (UDP 1900)"
        Add-HyperVRule -Name "VL-WSDiscovery-Inbound" -Port 3702 -Description "Vision Labs WS-Discovery (UDP 3702)"
    }
}

# ---------------------------------------------------------------------------
# Final instructions
# ---------------------------------------------------------------------------
Write-Heading "Phase 1 complete."

if ($needsReboot) {
    Write-Host ""
    Write-Warn "REBOOT REQUIRED before continuing."
    Write-Host ""
    Write-Host "After the reboot:" -ForegroundColor White
    Write-Host "  1. WSL will finish setting up Ubuntu and prompt you to create a Linux username." -ForegroundColor Gray
    Write-Host "     (Pick any username + password -- they're local to WSL only.)" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  2. Open the Ubuntu terminal (Start menu -> 'Ubuntu') and run:" -ForegroundColor Gray
    Write-Host "        sudo apt update && sudo apt install -y git" -ForegroundColor Cyan
    Write-Host "        git clone https://github.com/gammahazard/vision-labs-v2.git ~/vision-labs" -ForegroundColor Cyan
    Write-Host "        cd ~/vision-labs" -ForegroundColor Cyan
    Write-Host "        bash scripts/install-linux.sh" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  3. After the Linux installer finishes, open http://localhost:8080 in your" -ForegroundColor Gray
    Write-Host "     Windows browser -- the wizard will walk you through camera setup." -ForegroundColor Gray
    Write-Host ""
    Write-Host "Reboot now? (Y/n): " -NoNewline -ForegroundColor Yellow
    $answer = Read-Host
    if ($answer -ne "n" -and $answer -ne "N") {
        Restart-Computer -Confirm:$false
    } else {
        Write-Host "  OK -- reboot manually when you're ready." -ForegroundColor Yellow
    }
} else {
    Write-Host ""
    Write-Host "WSL2 was already set up. Open Ubuntu and run:" -ForegroundColor White
    Write-Host "    cd <your vision-labs checkout>" -ForegroundColor Cyan
    Write-Host "    bash scripts/install-linux.sh" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Then open http://localhost:8080 in your browser." -ForegroundColor Gray
}
