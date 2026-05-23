#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Network Printer Dashboard - Windows Agent Installer (standalone .exe)

.DESCRIPTION
    Installs the printer agent as a Windows Scheduled Task running as SYSTEM.
    Pulls a standalone printer_agent.exe directly from GitHub Releases - no
    Python install required on the target machine.

RMM one-liner (set the four env vars then run):
    $env:AGENT_URL="https://printers.yourco.com"; $env:AGENT_KEY="yourkey"
    $env:AGENT_SUBNET="192.168.1.0/24"; $env:AGENT_LOCATION="Station 12"
    irm -Headers @{"X-Agent-Key"=$env:AGENT_KEY} "$env:AGENT_URL/api/agent/download/install_windows.ps1" | iex

Or download and run locally:
    .\install_windows.ps1
#>
param(
    [string]$URL      = $env:AGENT_URL,
    [string]$KEY      = $env:AGENT_KEY,
    [string]$SUBNET   = $env:AGENT_SUBNET,
    [string]$LOCATION = $env:AGENT_LOCATION
)

$ErrorActionPreference = "Stop"

$TaskName    = "PrinterAgent"
$InstallDir  = "C:\PrinterAgent"
$AgentExe    = "$InstallDir\printer_agent.exe"
$LogFile     = "$InstallDir\agent.log"
$ExeUrl      = "https://github.com/mkeathley2/network-printer-dashboard/releases/latest/download/printer_agent.exe"

function Write-Status($msg) { Write-Host "[PrinterAgent] $msg" -ForegroundColor Cyan }
function Write-OK($msg)     { Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Fail($msg)   { Write-Host "[FAIL] $msg" -ForegroundColor Red; exit 1 }

Write-Status "Starting installation..."

# --- Validate required params ---
if (-not $URL)    { Write-Fail "URL is required. Set env:AGENT_URL before running." }
if (-not $KEY)    { Write-Fail "KEY is required. Set env:AGENT_KEY before running." }
if (-not $SUBNET) { Write-Fail "SUBNET is required. Set env:AGENT_SUBNET before running." }
if (-not $LOCATION) { $LOCATION = "" }

# --- Stop and remove existing scheduled task if present ---
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Status "Removing existing scheduled task..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Start-Sleep -Seconds 2
}

# --- Create install directory ---
if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir | Out-Null
}
Write-OK "Install directory: $InstallDir"

# --- Download the standalone .exe from GitHub Releases ---
Write-Status "Downloading printer_agent.exe from GitHub Releases..."
Write-Status "  $ExeUrl"
try {
    # No auth needed - release assets are public.  Use TLS 1.2 to be safe
    # on older Win10 builds where Server 2008 R2 defaults still bite.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $ExeUrl -OutFile $AgentExe -UseBasicParsing
    $sizeMB = [math]::Round((Get-Item $AgentExe).Length / 1MB, 1)
    Write-OK "Downloaded printer_agent.exe ($sizeMB MB)"
} catch {
    Write-Fail "Failed to download printer_agent.exe: $_"
}

# --- Write config ---
Write-Status "Writing agent_config.json..."
$config = @{
    dashboard_url         = $URL
    api_key               = $KEY
    subnets               = @($SUBNET)
    location              = $LOCATION
    snmp_community        = "public"
    snmp_timeout          = 3
    snmp_retries          = 1
    scan_interval_minutes = 60
} | ConvertTo-Json -Depth 5

# Write WITHOUT a BOM - Python's json.load (called via load_config in the
# agent) chokes on UTF-8 BOM unless the reader uses utf-8-sig.
[System.IO.File]::WriteAllText(
    "$InstallDir\agent_config.json",
    $config,
    (New-Object System.Text.UTF8Encoding $false)
)
Write-OK "Config written."

# --- Register as a Scheduled Task (runs as SYSTEM at startup, with restart-on-failure) ---
Write-Status "Registering scheduled task '$TaskName'..."

$action = New-ScheduledTaskAction `
    -Execute $AgentExe `
    -WorkingDirectory $InstallDir

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit 0 `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-OK "Scheduled task registered."

# --- Start the task now ---
Write-Status "Starting $TaskName..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 4

$taskInfo = Get-ScheduledTask -TaskName $TaskName
$taskState = $taskInfo.State
if ($taskState -eq "Running") {
    Write-OK "PrinterAgent is running!"
} else {
    Write-Status "Task state: $taskState (it may take a moment to start scanning)"
}

Write-Host ""
Write-Host "Installation complete." -ForegroundColor Green
Write-Host "  Install dir : $InstallDir"
Write-Host "  Executable  : $AgentExe"
Write-Host "  Log file    : $LogFile"
Write-Host "  Dashboard   : $URL"
Write-Host "  Subnet      : $SUBNET"
if ($LOCATION) { Write-Host "  Location    : $LOCATION" }
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Yellow
Write-Host "  Get-ScheduledTask -TaskName PrinterAgent           # check status"
Write-Host "  Get-Content C:\PrinterAgent\agent.log -Tail 30    # view logs"
Write-Host "  Start-ScheduledTask -TaskName PrinterAgent         # trigger check-in"
Write-Host "  Stop-ScheduledTask -TaskName PrinterAgent          # stop"
