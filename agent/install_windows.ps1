#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Network Printer Dashboard — Windows Agent Installer

.DESCRIPTION
    Installs the printer agent as a Windows service.
    Designed to be run as an RMM PowerShell script with variables set per-deployment.

.PARAMETER URL
    Public dashboard URL, e.g. https://printers.yourcompany.com
.PARAMETER KEY
    Agent API key generated in Config -> Remote Agents
.PARAMETER SUBNET
    CIDR subnet to scan, e.g. 192.168.10.0/24
.PARAMETER LOCATION
    Human-readable site name, e.g. "Station 12 - Main St"

RMM one-liner (set the four variables in your RMM, then run):
    $URL="https://printers.yourco.com"; $KEY="yourkey"; $SUBNET="192.168.1.0/24"; $LOCATION="Station 12"
    irm "$URL/api/agent/download/install_windows.ps1" | iex

Or download and run locally:
    .\install_windows.ps1 -URL "..." -KEY "..." -SUBNET "..." -LOCATION "..."
#>
param(
    [string]$URL      = $env:AGENT_URL,
    [string]$KEY      = $env:AGENT_KEY,
    [string]$SUBNET   = $env:AGENT_SUBNET,
    [string]$LOCATION = $env:AGENT_LOCATION
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ServiceName  = "PrinterAgent"
$InstallDir   = "C:\PrinterAgent"
$AgentScript  = "$InstallDir\printer_agent.py"
$NssmExe      = "$InstallDir\nssm.exe"
$NssmUrl      = "https://nssm.cc/release/nssm-2.24.zip"
$NssmZip      = "$env:TEMP\nssm.zip"

function Write-Status($msg) { Write-Host "[PrinterAgent] $msg" -ForegroundColor Cyan }
function Write-OK($msg)     { Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Fail($msg)   { Write-Host "[FAIL] $msg" -ForegroundColor Red; exit 1 }

Write-Status "Starting installation..."

# --- Validate required params ---
if (-not $URL)    { Write-Fail "URL is required. Pass -URL or set AGENT_URL env var." }
if (-not $KEY)    { Write-Fail "KEY is required. Pass -KEY or set AGENT_KEY env var." }
if (-not $SUBNET) { Write-Fail "SUBNET is required. Pass -SUBNET or set AGENT_SUBNET env var." }
if (-not $LOCATION) { $LOCATION = "" }

# --- Stop existing service if running ---
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Status "Stopping existing $ServiceName service..."
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# --- Create install directory ---
if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir | Out-Null
}
Write-OK "Install directory: $InstallDir"

# --- Check / install Python ---
$python = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python 3\.[89]|Python 3\.1[0-9]") {
            $python = $candidate
            Write-OK "Found Python: $ver"
            break
        }
    } catch { }
}

if (-not $python) {
    Write-Status "Python 3.9+ not found. Installing via winget..."
    try {
        winget install --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
        $python = "python"
        Write-OK "Python installed via winget."
    } catch {
        Write-Fail "Could not install Python automatically. Install Python 3.9+ manually then re-run this script."
    }
}

# --- Install dependencies ---
Write-Status "Installing Python dependencies..."
& $python -m pip install --quiet --upgrade pysnmp requests
Write-OK "Dependencies installed."

# --- Download agent script ---
Write-Status "Downloading agent script from $URL ..."
$headers = @{ "X-Agent-Key" = $KEY }
try {
    Invoke-WebRequest -Uri "$URL/api/agent/download/agent.py" `
                      -Headers $headers `
                      -OutFile $AgentScript `
                      -UseBasicParsing
    Write-OK "Agent script downloaded to $AgentScript"
} catch {
    Write-Fail "Failed to download agent script: $_"
}

# --- Write config ---
Write-Status "Writing agent_config.json..."
$config = @{
    dashboard_url       = $URL
    api_key             = $KEY
    subnets             = @($SUBNET)
    location            = $LOCATION
    snmp_community      = "public"
    snmp_timeout        = 3
    snmp_retries        = 1
    scan_interval_minutes = 60
    agent_version       = "1.0.0"
} | ConvertTo-Json -Depth 5

[System.IO.File]::WriteAllText("$InstallDir\agent_config.json", $config, [System.Text.Encoding]::UTF8)
Write-OK "Config written."

# --- Download and install NSSM ---
if (-not (Test-Path $NssmExe)) {
    Write-Status "Downloading NSSM service manager..."
    try {
        Invoke-WebRequest -Uri $NssmUrl -OutFile $NssmZip -UseBasicParsing
        Expand-Archive -Path $NssmZip -DestinationPath "$env:TEMP\nssm_extract" -Force
        $nssmBin = Get-ChildItem "$env:TEMP\nssm_extract" -Recurse -Filter "nssm.exe" |
                   Where-Object { $_.FullName -match "win64" } | Select-Object -First 1
        if (-not $nssmBin) {
            $nssmBin = Get-ChildItem "$env:TEMP\nssm_extract" -Recurse -Filter "nssm.exe" |
                       Select-Object -First 1
        }
        Copy-Item $nssmBin.FullName $NssmExe
        Write-OK "NSSM installed."
    } catch {
        Write-Fail "Failed to download/install NSSM: $_"
    }
}

# --- Locate Python executable for the service ---
$pythonExe = & $python -c "import sys; print(sys.executable)" 2>&1
Write-Status "Python executable: $pythonExe"

# --- Remove old service if present ---
if ($existing) {
    Write-Status "Removing old service registration..."
    & $NssmExe remove $ServiceName confirm 2>$null
    Start-Sleep -Seconds 1
}

# --- Install the Windows service ---
Write-Status "Installing $ServiceName as a Windows service..."
& $NssmExe install $ServiceName $pythonExe
& $NssmExe set $ServiceName AppParameters "`"$AgentScript`""
& $NssmExe set $ServiceName AppDirectory $InstallDir
& $NssmExe set $ServiceName AppStdout "$InstallDir\agent.log"
& $NssmExe set $ServiceName AppStderr "$InstallDir\agent.log"
& $NssmExe set $ServiceName AppRotateFiles 1
& $NssmExe set $ServiceName AppRotateBytes 5242880
& $NssmExe set $ServiceName Start SERVICE_AUTO_START
& $NssmExe set $ServiceName ObjectName LocalSystem
Write-OK "Service registered."

# --- Start service ---
Write-Status "Starting $ServiceName service..."
Start-Service -Name $ServiceName
Start-Sleep -Seconds 3

$svc = Get-Service -Name $ServiceName
if ($svc.Status -eq "Running") {
    Write-OK "PrinterAgent service is running!"
    Write-Host ""
    Write-Host "Installation complete." -ForegroundColor Green
    Write-Host "  Install dir : $InstallDir"
    Write-Host "  Log file    : $InstallDir\agent.log"
    Write-Host "  Dashboard   : $URL"
    Write-Host "  Subnet      : $SUBNET"
    if ($LOCATION) { Write-Host "  Location    : $LOCATION" }
} else {
    Write-Fail "Service installed but failed to start. Check $InstallDir\agent.log for details."
}
