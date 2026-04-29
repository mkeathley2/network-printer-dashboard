# Claude Instructions — Network Printer Dashboard

## VERSION BUMP CHECKLIST
Whenever the version number is changed, ALL of the following must be updated
in the same commit — no exceptions:

1. `VERSION`  (e.g. `v0.0.9`)
2. `agent/printer_agent.py`  — `AGENT_VERSION = "v0.0.9"`  (line ~81)
3. `agent/printer_agent.py`  — docstring at top:  `Version: v0.0.9`

After making those changes:
4. Commit ALL changed files to git
5. Push to GitHub (`git push`)
6. Create a GitHub Release tagged with the new version (`gh release create v0.0.9 ...`)

Skipping any of these steps causes agent version mismatches (the dashboard
auto-queues an update for any agent whose version differs from VERSION) and
makes the GitHub release / dashboard Updates tab go out of sync.

---

## Project Overview
- **Stack**: Flask + SQLAlchemy 2.x + MariaDB 11, Docker + docker-compose on Linux VM
- **Root dir**: `C:\Users\mkeat\OneDrive\Desktop\Network Printer Dashboard`
- **Port**: 7070
- **Public URL**: https://mattsvm.taild0c836.ts.net  (Tailscale Funnel)
- **GitHub repo**: mkeathley2/network-printer-dashboard

## Key Files
| File | Purpose |
|------|---------|
| `VERSION` | Single source of truth for dashboard version |
| `agent/printer_agent.py` | Standalone remote agent script — `AGENT_VERSION` must match `VERSION` |
| `app/utils/version.py` | Reads VERSION file; fetches GitHub releases for update checker |
| `app/web/routes/agent_api.py` | Checkin endpoint — auto-queues update when agent version ≠ dashboard version |
| `app/run.py` | App entry point + APScheduler jobs |
| `app/web/__init__.py` | App factory + `_run_migrations()` for schema changes |

## Agent Install
- Windows: `Register-ScheduledTask` (task name: `PrinterAgent`, dir: `C:\PrinterAgent\`)
- Linux/Pi: systemd service (`printer-agent`)
- Manual checkin trigger (Windows): `Start-ScheduledTask -TaskName "PrinterAgent"`
- Manual checkin trigger (Linux): `sudo systemctl restart printer-agent`

## Migrations
All schema changes go in `_run_migrations()` in `app/web/__init__.py` using
idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements.
