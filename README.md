# Network Printer Dashboard

A self-hosted web dashboard for monitoring network printers via SNMP. Runs in Docker and works on any network with SNMP-enabled printers.

![Dashboard](https://img.shields.io/badge/version-v0.0.4-blue) ![Docker](https://img.shields.io/badge/docker-compose-2496ED)

---

## What It Does

Network Printer Dashboard polls your printers over SNMP and gives you a single-pane view of every device on your network — toner levels, online/offline status, model info, page counts, and alerts — without requiring any software installed on the printers themselves.

---

## Features

### Dashboard
- Live grid of all printers with online/offline status indicators
- Toner/supply level bars color-matched to cartridge color (Black, Cyan, Magenta, Yellow)
- Cartridge name and color label on each card
- Summary stats: total printers, online, offline, alerts today
- Filter by printer group
- Auto-refreshes every 5 minutes without page flash
- Poll All button (admin) to immediately refresh all printers

### Printer Detection & Vendor Support
- Auto-detects vendor from SNMP sysObjectID (HP, Kyocera, Brother, Canon)
- Fallback detection via sysDescr string and enterprise OID probe for printers that don't respond to standard OIDs
- Supported vendors with enhanced data: **HP**, **Kyocera/ECOSYS**, **Brother**, **Canon**
- Generic RFC 3805 Printer-MIB support for any other SNMP-capable printer
- Model name retrieved from `hrDeviceDescr` (reliable across all vendors)
- Kyocera toner model suffix parsing (TK-5242C → Cyan, TK-5242K → Black)
- Single black-cartridge printers automatically labeled as Black

### Printer Detail Page
- Full printer info: IP, hostname, model, serial, vendor, page count
- Supply levels table with color-matched progress bars
- Per-printer alert threshold overrides (admin only)
- Poll Now, Edit, Remove controls (admin only)
- Create Helpdesk Ticket button (sends email with printer info and supply levels)
- Supply history chart

### Network Discovery
- Scan any CIDR range to find SNMP printers
- Add individual printers or all discovered printers at once
- Configurable SNMP community string per scan

### Alerts
- Configurable warning and critical thresholds (site-wide default + per-printer override)
- Email alerts for low toner and offline printers
- Alert history log

### Configuration (Admin)
- **Email / SMTP** — configure outbound email for alerts and helpdesk tickets, with test button
- **Thresholds** — set site-wide warning % and critical % for supply levels
- **Users & Roles** — add/remove users, set passwords, assign Admin or Viewer roles
- **Printer Groups** — organize printers into groups for filtering

### Users & Security
- Login-protected; all pages require authentication
- Two roles: **Admin** (full access) and **Viewer** (read-only)
- Default credentials on first run: `admin` / `admin` (change immediately)

---

## Requirements

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/)
- Network access to your printers via SNMP (UDP port 161)
- Printers must have SNMP v2c enabled (community string `public` by default)

---

## Deployment

### Quick Start (All Platforms)

**1. Clone the repository**
```bash
git clone https://github.com/mkeathley2/network-printer-dashboard.git
cd network-printer-dashboard
```

**2. Create your `.env` file**
```bash
cp .env.example .env
```
Edit `.env` and set secure values:
```env
DB_ROOT_PASSWORD=your_secure_root_password
DB_PASSWORD=your_secure_app_password
SECRET_KEY=a_long_random_string_at_least_32_chars
```
SMTP settings are optional — you can configure them later in the web UI.

**3. Start the stack**
```bash
docker compose up -d
```

**4. Open the dashboard**

Navigate to `http://localhost:7070` (or replace `localhost` with the server's IP if accessing from another machine).

Log in with `admin` / `admin` and change the password immediately via the user menu.

---

### Windows

1. Install [Docker Desktop for Windows](https://docs.docker.com/desktop/install/windows-install/)
2. Ensure WSL 2 is enabled (Docker Desktop will prompt you if needed)
3. Open **PowerShell** or **Windows Terminal** and follow the Quick Start steps above
4. Docker Desktop must be running before you start the stack

> **Tip:** To have the dashboard start automatically with Windows, enable "Start Docker Desktop when you log in" in Docker Desktop settings.

---

### Linux (Debian/Ubuntu)

```bash
# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Install Docker Compose plugin
sudo apt-get install -y docker-compose-plugin

# Clone and start
git clone https://github.com/mkeathley2/network-printer-dashboard.git
cd network-printer-dashboard
cp .env.example .env
nano .env          # set your passwords and secret key
docker compose up -d
```

To have the stack start automatically on boot:
```bash
# Docker itself starts on boot by default via systemd.
# The containers are set to restart: unless-stopped,
# so they will start automatically when Docker starts.
```

---

### macOS

1. Install [Docker Desktop for Mac](https://docs.docker.com/desktop/install/mac-install/) (supports both Intel and Apple Silicon)
2. Open **Terminal** and follow the Quick Start steps above
3. Docker Desktop must be running before you start the stack

> **Apple Silicon (M1/M2/M3):** The stack builds and runs natively — no extra configuration needed.

---

## Updating

Pull the latest code and rebuild:

```bash
git pull
docker compose build app
docker compose up -d app
```

Database migrations are applied automatically on startup — no manual SQL required.

---

## Configuration

### `config.yaml`

Controls polling intervals, SNMP timeouts, and alert thresholds. The defaults work for most environments:

```yaml
polling:
  interval_minutes: 60   # How often to poll all printers
  poll_workers: 20       # Concurrent polling threads

snmp:
  timeout: 3             # Seconds per SNMP request
  retries: 2             # Retries on timeout
  community_v2c: public  # Default community string
```

### Environment Variables (`.env`)

| Variable | Required | Description |
|---|---|---|
| `DB_ROOT_PASSWORD` | Yes | MariaDB root password |
| `DB_PASSWORD` | Yes | App database user password |
| `SECRET_KEY` | Yes | Flask session secret key (make it long and random) |
| `SMTP_HOST` | No | SMTP server for alert emails |
| `SMTP_PORT` | No | SMTP port (default: 587) |
| `SMTP_USER` | No | SMTP username |
| `SMTP_PASSWORD` | No | SMTP password |
| `SMTP_FROM` | No | From address for outbound emails |

SMTP can also be configured at any time in the web UI under **Config → Email / SMTP** without restarting.

---

## Ports

| Port | Service |
|---|---|
| `7070` | Web dashboard (HTTP) |

To change the port, edit the `ports` section in `docker-compose.yml`:
```yaml
ports:
  - "8080:7070"   # Access on port 8080 instead
```

---

## Tech Stack

- **Backend:** Python 3.12, Flask, SQLAlchemy, APScheduler
- **Database:** MariaDB 11
- **SNMP:** pysnmp 7.x (asyncio)
- **Frontend:** Bootstrap 5, HTMX, Chart.js
- **Container:** Docker Compose (single `docker compose up -d`)

---

## Rollback

Each stable release is tagged in git. To roll back to a previous version:

```bash
git checkout v0.0.4
docker compose build app
docker compose up -d app
```

To return to the latest:
```bash
git checkout master
docker compose build app
docker compose up -d app
```

---

## Default Credentials

| Username | Password | Role |
|---|---|---|
| `admin` | `admin` | Admin |

**Change the default password immediately after first login.**
