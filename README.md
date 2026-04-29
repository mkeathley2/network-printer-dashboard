# Network Printer Dashboard

A self-hosted web dashboard for monitoring network printers via SNMP. Tracks supply levels, sends alerts, generates reports, and supports remote agents for monitoring printers on networks the dashboard can't reach directly.

[![GitHub release](https://img.shields.io/github/v/release/mkeathley2/network-printer-dashboard?label=version&color=blue)](https://github.com/mkeathley2/network-printer-dashboard/releases)
![Docker](https://img.shields.io/badge/docker-compose-2496ED)
![Python](https://img.shields.io/badge/python-3.12-3776AB)

---

## What It Does

Polls your printers over SNMP and gives you a single-pane view of every device on your network — toner levels, online/offline status, model info, page counts, and alerts — without requiring any software installed on the printers themselves. Built-in reports turn that polling history into actionable data: who's printing the most, which printers cost the most to run per page, when each cartridge is predicted to run out.

---

## Features

### Live Dashboard
- Real-time grid of all active printers with status dots (online / offline / unknown)
- Color-matched supply bars (Black, Cyan, Magenta, Yellow) with warning/critical highlighting
- Filter by location, status, or search by name
- Auto-refreshes every 5 minutes
- One-click "Poll All Now" for admins

### Printer Detail Page
- Full asset info — IP, hostname, model, serial, vendor, page count, plus optional fields (assigned person, SQL number, computer, phone ext., printer web login credentials)
- Live supply level bars
- **Toner Replacement History** with admin-editable cost per replacement
- Per-printer alert threshold overrides
- Recent alerts log
- One-click actions: Poll Now, Resend Alerts, Create Helpdesk Ticket, Set Thresholds, Remove Printer

### Per-Printer History Page
- **Date-range filter pills** — 7d / 30d / 90d / All time (default: 90 days)
- **Active Supplies summary table** — color, status badge, current %, consumption rate (% per day), days remaining at current rate, predicted empty date
- **Pages-per-day** average card alongside the depletion table
- **Supply Levels Over Time chart** with vertical dashed markers showing each toner replacement event (color-coded to the cartridge), labeled dates on the X-axis
- **Page Count Over Time chart** with the same date-aware time axis
- Depletion estimates are **replacement-aware** — the regression only fits to data after the most recent replacement, so the numbers stay accurate right after you swap a cartridge

### Vendor Support
- Auto-detection from SNMP `sysObjectID`
- Enhanced data for **HP**, **Brother**, **Canon**, **Kyocera/ECOSYS**, **Ricoh**
- Generic RFC 3805 Printer-MIB support for any other SNMP-capable printer
- Smart toner-color parsing (e.g. Kyocera TK-5242C → Cyan)

### Reports (all six available to every signed-in user, with CSV export)
1. **Print Volume** — pages printed per printer in a date range, grouped by printer/person/location
2. **Page Count Over Time** — multi-line chart of cumulative pages over time
3. **Toner Cost** — every replacement event with the cost you entered; totals by printer and color
4. **Cost Per Page** — total toner spend ÷ pages printed (printer efficiency metric)
5. **Consumption Rate** — % per day depletion via linear regression, with projected days remaining
6. **Reliability** — offline event count per printer

### Alerts & Predictive Maintenance
- Email alerts on toner low (warning), toner critical, toner replaced, drum events, and printer offline
- Per-event-type toggles
- Site-wide and per-printer warning/critical thresholds
- **Predictive Toner Alerts** — hourly background job uses linear regression on supply history to predict depletion; auto-creates a helpdesk ticket when a cartridge will run out within N days (configurable)
- One helpdesk ticket per supply lifecycle (deduped — no spam)
- "Resend Alerts" button on each printer to re-fire active alerts after fixing email config

### Network Discovery
- Scan any CIDR range to find SNMP-responsive printers
- Add discovered printers individually or in bulk
- Configurable SNMP community per scan

### Remote Agents
For monitoring printers at sites the dashboard server can't reach directly (separate networks, branch offices, etc.):
- Lightweight standalone Python agent (Windows or Raspberry Pi)
- Reports back to the central dashboard over HTTPS — no VPN or port-forwarding required
- One-line install command (PowerShell on Windows, bash on Pi)
- **Auto-detects local subnet** if not specified — uses OS routing tables
- **Auto-updates** when the dashboard version changes
- Subnet, scan interval, and location editable from the dashboard
- Stale-detection alerts when an agent stops checking in
- Tailscale Funnel works great as the public HTTPS endpoint (free, no domain required)

### User Accounts & Security
- Login-protected; every page requires authentication
- Two roles: **Admin** (full access) and **Viewer** (read-only — sees printers, reports, alerts)
- **Temporary passwords + forced change**: when an admin creates a user with an email, the system generates a random temp password, emails it via the welcome email, and forces the user to set their own password on first login
- Admins can hit "Send Reset Email" to issue a new temp password to any user with an email on file
- Manual password set still available for users without email
- Audit log of every administrative action (30-day retention)

### Configuration (Admin)
- **Email / SMTP** — STARTTLS, SSL/TLS, or none; with built-in test email
- **Locations** — tag printers by location for filtering and grouping
- **Spreadsheet Import** — bulk-import asset fields from .xlsx (matches by IP)
- **Thresholds** — site-wide warning/critical %, poll interval, timezone; one-click button to bulk-reset per-printer threshold overrides back to site defaults
- **Alert Settings** — per-event-type email toggles + Predictive Toner Alerts config
- **Activity Log** — every admin action with CSV export
- **Backup & Reset** — SQL dump download or factory reset
- **Updates** — version comparison vs latest GitHub release with release notes

### In-App Help
- Comprehensive user manual at **`/help`** (your username menu → Help)
- Sidebar table of contents, scrollable single-page format
- Admin-only sections automatically hidden from Viewer accounts

---

## Requirements

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) (Engine 20.10+ for the v2 plugin)
- Network access to your printers via SNMP (UDP port 161)
- Printers must have SNMP v2c enabled (community string `public` by default)

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/mkeathley2/network-printer-dashboard.git
cd network-printer-dashboard

# 2. Configure
cp .env.example .env
nano .env    # set DB_ROOT_PASSWORD, DB_PASSWORD, SECRET_KEY

# 3. Start
docker compose up -d

# 4. Open
# Browse to http://localhost:7070  (or your server IP)
# Log in with admin / admin and change the password immediately
```

SMTP settings are optional at install time — configure them in **Config → Email / SMTP** whenever you're ready. Without SMTP, alerts won't be emailed but everything else works fine.

---

## Platform Notes

### Linux (Debian/Ubuntu)
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
sudo apt-get install -y docker-compose-plugin
```
Then follow Quick Start. Containers use `restart: unless-stopped` so they come back automatically after host reboot.

### Windows
1. Install [Docker Desktop](https://docs.docker.com/desktop/install/windows-install/) (WSL 2 backend)
2. Make sure Docker Desktop is running, then follow Quick Start in PowerShell
3. Enable "Start Docker Desktop when you log in" if you want it auto-running

### macOS
1. Install [Docker Desktop for Mac](https://docs.docker.com/desktop/install/mac-install/) (works on Intel and Apple Silicon)
2. Follow Quick Start in Terminal

---

## Updating

Updates are applied manually via SSH for security reasons (see [Security Model](#security-model) below). The Updates tab in the dashboard checks the latest GitHub release and shows the commands to run.

The repo includes an `update.sh` script that handles it in one command:

```bash
cd /path/to/network-printer-dashboard
./update.sh
```

Or step by step:

```bash
git pull
docker compose up --build -d
```

Database migrations run automatically on startup — no manual SQL required.

### Rolling back to a previous release

Each release is git-tagged. To roll back:

```bash
git fetch --tags
git checkout v0.0.11           # replace with desired tag
docker compose up --build -d
```

Return to latest with `git checkout master`.

---

## Configuration

### `config.yaml`
Polling intervals, SNMP timeouts, and alert defaults. The shipped defaults work for most environments:

```yaml
polling:
  interval_minutes: 60   # Site-wide poll interval (overridable in UI)
  poll_workers: 20       # Concurrent polling threads

snmp:
  timeout: 3
  retries: 2
  community_v2c: public
```

### Environment Variables (`.env`)

| Variable | Required | Description |
|---|---|---|
| `DB_ROOT_PASSWORD` | yes | MariaDB root password |
| `DB_PASSWORD` | yes | App database user password |
| `SECRET_KEY` | yes | Flask session secret (long random string) |
| `SMTP_HOST` | no | SMTP server (also set in UI) |
| `SMTP_PORT` | no | Default 587 |
| `SMTP_USER` | no | SMTP username |
| `SMTP_PASSWORD` | no | SMTP password |
| `SMTP_FROM` | no | From address for outbound emails |

SMTP settings can also be edited live in the UI (no restart required). UI values take precedence over `.env`.

### Ports

| Port | Service |
|---|---|
| `7070` | Web dashboard (HTTP) |

To change the host port, edit `docker-compose.yml`:

```yaml
ports:
  - "8080:7070"
```

For internet exposure, put it behind a reverse proxy (Caddy, Nginx, Cloudflare Tunnel, **Tailscale Funnel** — Tailscale is free and works great).

---

## Security Model

The web container intentionally has **no permission to modify the host system** — no Docker socket mount, no full repo write access. Updates are applied manually via SSH so that even in the event of a successful attack on the dashboard, the attacker cannot pivot to the VM. This is a small UX cost (one minute of typing per update) for a meaningful security improvement.

If you expose the dashboard to the internet (Tailscale Funnel, Cloudflare Tunnel, etc.), consider:
- Set a strong `SECRET_KEY` in `.env` (32+ random chars)
- Change the default `admin` / `admin` password immediately on first login
- Configure SMTP and use the welcome-email flow for new users (so passwords never travel through chat or shared docs)
- Keep the install up to date

---

## Tech Stack

- **Backend:** Python 3.12, Flask, SQLAlchemy 2.x, APScheduler, pysnmp 7.x (asyncio), Flask-Login
- **Database:** MariaDB 11
- **Frontend:** Bootstrap 5.3, Bootstrap Icons, HTMX, Chart.js 4.4
- **Container:** Docker + Docker Compose v2

---

## Default Credentials

| Username | Password | Role |
|---|---|---|
| `admin` | `admin` | Admin |

**Change this immediately after first login** (top-right user menu → Change Password).

---

## Documentation

- **In-app user manual** — log in and click your username → **Help**
- **Release notes** — see [GitHub Releases](https://github.com/mkeathley2/network-printer-dashboard/releases) for the changelog of every version
- **Issues / questions** — open an issue on the [GitHub repo](https://github.com/mkeathley2/network-printer-dashboard/issues)

---

## License

See repository for license terms.
