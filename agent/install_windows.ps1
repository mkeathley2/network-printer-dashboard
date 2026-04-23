#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Network Printer Dashboard — Windows Agent Installer

.DESCRIPTION
    Installs the printer agent as a Windows Scheduled Task (runs at startup,
    restarts on failure). No external downloads required beyond Python.

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

$TaskName   = "PrinterAgent"
$InstallDir = "C:\PrinterAgent"
$AgentScript = "$InstallDir\printer_agent.py"
$LogFile     = "$InstallDir\agent.log"

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
        # Refresh PATH
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("PATH", "User")
        $python = "python"
        Write-OK "Python installed via winget."
    } catch {
        Write-Fail "Could not install Python automatically. Install Python 3.9+ manually then re-run."
    }
}

# --- Install dependencies ---
Write-Status "Installing Python dependencies (pysnmp, requests)..."
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
    dashboard_url         = $URL
    api_key               = $KEY
    subnets               = @($SUBNET)
    location              = $LOCATION
    snmp_community        = "public"
    snmp_timeout          = 3
    snmp_retries          = 1
    scan_interval_minutes = 60
    agent_version         = "1.0.0"
} | ConvertTo-Json -Depth 5

[System.IO.File]::WriteAllText("$InstallDir\agent_config.json", $config, [System.Text.Encoding]::UTF8)
Write-OK "Config written."

# --- Locate Python executable ---
$pythonExe = & $python -c "import sys; print(sys.executable)" 2>&1
Write-Status "Python executable: $pythonExe"

# --- Register as a Scheduled Task (runs at startup, restarts on failure) ---
Write-Status "Registering scheduled task '$TaskName'..."

$action = New-ScheduledTaskAction `
    -Execute $pythonExe `
    -Argument "`"$AgentScript`"" `
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
Write-Host "  Log file    : $LogFile"
Write-Host "  Dashboard   : $URL"
Write-Host "  Subnet      : $SUBNET"
if ($LOCATION) { Write-Host "  Location    : $LOCATION" }
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Yellow
Write-Host "  Get-ScheduledTask -TaskName PrinterAgent           # check status"
Write-Host "  Get-Content C:\PrinterAgent\agent.log -Tail 30    # view logs"
Write-Host "  Start-ScheduledTask -TaskName PrinterAgent         # start"
Write-Host "  Stop-ScheduledTask -TaskName PrinterAgent          # stop"
