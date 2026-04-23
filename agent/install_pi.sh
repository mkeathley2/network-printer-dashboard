#!/usr/bin/env bash
# =============================================================================
#  Network Printer Dashboard — Raspberry Pi / Linux Agent Installer
# =============================================================================
#
#  One-liner (run on the Pi at the station):
#
#    AGENT_URL="https://printers.yourco.com" \
#    AGENT_KEY="yourkey" \
#    AGENT_SUBNET="192.168.10.0/24" \
#    AGENT_LOCATION="Station 12" \
#    bash <(curl -sSL "https://printers.yourco.com/api/agent/download/install_pi.sh")
#
#  Or download first and run:
#    bash install_pi.sh
#  (The script will prompt for values not set via env vars.)
#
# =============================================================================
set -e

SERVICE_NAME="printer-agent"
INSTALL_DIR="/opt/printer-agent"
SCRIPT_PATH="$INSTALL_DIR/printer_agent.py"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

# Colours
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
info() { echo -e "${CYAN}[>>]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }

echo ""
echo "=================================================="
echo "  Network Printer Dashboard — Agent Installer"
echo "=================================================="
echo ""

# ---------------------------------------------------------------------------
# Collect config values — env vars take priority, then prompt
# ---------------------------------------------------------------------------
_ask() {
    local var_name="$1" prompt="$2" default="$3" current
    current="${!var_name:-}"
    if [ -n "$current" ]; then
        echo "$prompt: $current"
        return
    fi
    read -rp "$prompt [$default]: " input
    eval "$var_name=\"${input:-$default}\""
}

_ask AGENT_URL      "Dashboard URL"                  "https://printers.yourcompany.com"
_ask AGENT_KEY      "API Key"                        ""
_ask AGENT_SUBNET   "Subnet to scan (CIDR)"          "192.168.1.0/24"
_ask AGENT_LOCATION "Location name (optional)"       ""
_ask SNMP_COMMUNITY "SNMP community string"          "public"
_ask SCAN_INTERVAL  "Scan interval (minutes)"        "60"

[ -z "$AGENT_KEY" ] && fail "API Key is required."

# ---------------------------------------------------------------------------
# System dependencies
# ---------------------------------------------------------------------------
info "Installing system dependencies..."
if command -v apt-get &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3 python3-pip python3-venv curl
elif command -v yum &>/dev/null; then
    sudo yum install -y python3 python3-pip curl
elif command -v dnf &>/dev/null; then
    sudo dnf install -y python3 python3-pip curl
else
    info "Unknown package manager — assuming python3 and pip are already installed."
fi
ok "System dependencies ready."

# ---------------------------------------------------------------------------
# Python packages
# ---------------------------------------------------------------------------
info "Installing Python packages (pysnmp, requests)..."
pip3 install --quiet --upgrade pysnmp requests
ok "Python packages installed."

# ---------------------------------------------------------------------------
# Create install directory
# ---------------------------------------------------------------------------
info "Creating install directory $INSTALL_DIR ..."
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$USER:$USER" "$INSTALL_DIR" 2>/dev/null || true
ok "Directory created."

# ---------------------------------------------------------------------------
# Download agent script
# ---------------------------------------------------------------------------
info "Downloading agent script from $AGENT_URL ..."
curl -sSL \
     -H "X-Agent-Key: $AGENT_KEY" \
     "$AGENT_URL/api/agent/download/agent.py" \
     -o "$SCRIPT_PATH" || fail "Failed to download agent script. Check the URL and API key."
chmod +x "$SCRIPT_PATH"
ok "Agent script downloaded."

# ---------------------------------------------------------------------------
# Write config file
# ---------------------------------------------------------------------------
info "Writing config file..."
cat > "$INSTALL_DIR/agent_config.json" <<EOF
{
  "dashboard_url": "${AGENT_URL%/}",
  "api_key": "$AGENT_KEY",
  "subnets": ["$AGENT_SUBNET"],
  "location": "$AGENT_LOCATION",
  "snmp_community": "$SNMP_COMMUNITY",
  "snmp_timeout": 3,
  "snmp_retries": 1,
  "scan_interval_minutes": $SCAN_INTERVAL,
  "agent_version": "1.0.0"
}
EOF
ok "Config written to $INSTALL_DIR/agent_config.json"

# ---------------------------------------------------------------------------
# Get python3 path
# ---------------------------------------------------------------------------
PYTHON3=$(command -v python3) || fail "python3 not found in PATH."
info "Using Python: $PYTHON3"

# ---------------------------------------------------------------------------
# systemd service
# ---------------------------------------------------------------------------
info "Installing systemd service..."
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Network Printer Dashboard Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON3 $SCRIPT_PATH
Restart=on-failure
RestartSec=60
StandardOutput=append:$INSTALL_DIR/agent.log
StandardError=append:$INSTALL_DIR/agent.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
sleep 3

if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "Service '$SERVICE_NAME' is running!"
else
    fail "Service failed to start. Check logs: sudo journalctl -u $SERVICE_NAME -n 50"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}Installation complete!${NC}"
echo "  Install dir : $INSTALL_DIR"
echo "  Log file    : $INSTALL_DIR/agent.log"
echo "  Dashboard   : $AGENT_URL"
echo "  Subnet      : $AGENT_SUBNET"
[ -n "$AGENT_LOCATION" ] && echo "  Location    : $AGENT_LOCATION"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status $SERVICE_NAME    # check status"
echo "  sudo journalctl -u $SERVICE_NAME -f    # live logs"
echo "  sudo systemctl restart $SERVICE_NAME   # restart"
echo "  sudo systemctl stop $SERVICE_NAME      # stop"
echo ""
