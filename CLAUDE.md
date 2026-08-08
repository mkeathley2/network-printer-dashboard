# Claude Instructions — Network Printer Dashboard

Operating manual for every session. The full maintainer brain-dump (history,
infrastructure, runbook, backlog) is in **HANDOFF.md** — read it when context
is missing. Infra access specifics live in **HANDOFF-PRIVATE.md** (gitignored —
NEVER commit it; if absent, ask the owner for their copy).

## PROJECT SNAPSHOT
- **What**: Self-hosted dashboard monitoring network printers via SNMP — supply
  levels, alerts/emails, helpdesk tickets, reports, predictive toner, remote
  site agents. Used by the owner's IT operation across multiple sites.
- **Stack**: Flask + SQLAlchemy 2.x + MariaDB 11, Docker Compose, Bootstrap 5 +
  HTMX + Chart.js (CDN, no npm), APScheduler in-process, pysnmp.
- **Prod**: Docker Compose on an on-prem Linux VM (details in HANDOFF-PRIVATE.md).
  Port **7070**. Public URL **https://mattsvm.taild0c836.ts.net** (Tailscale Funnel).
- **Repo**: github.com/mkeathley2/network-printer-dashboard — **PUBLIC on purpose**:
  deployed agents download `printer_agent.exe` from Releases anonymously.
  Do NOT make it private (see HANDOFF.md backlog for the migration that must come first).

## VERSION BUMP CHECKLIST
Whenever the version number changes, ALL of the following in the SAME commit:
1. `VERSION`  (e.g. `v0.0.31`)
2. `agent/printer_agent.py` — `AGENT_VERSION = "v0.0.31"`  (line ~99)
3. `agent/printer_agent.py` — docstring at top: `Version: v0.0.31`

Then: 4. Commit ALL changed files  5. `git push`  6. `gh release create v0.0.31 ...`

Skipping any step causes agent version mismatches (dashboard auto-queues an
update for any agent whose version ≠ VERSION) and desyncs the Updates tab.
Every release triggers GitHub Actions to rebuild `printer_agent.exe` (~2 min)
and the whole agent fleet auto-updates on next check-in — even for docs-only
bumps; that lockstep is intentional.

## LOCAL DEV + TESTING
- Local run: create `.env` (keys: `DB_ROOT_PASSWORD`, `DB_PASSWORD`, `SECRET_KEY`;
  optional `SMTP_*`) → `docker compose up --build` → http://localhost:7070.
  Fresh DB seeds login **admin / admin** (superadmin; local only).
- **No automated test suite.** Verify by running locally + manual checks, and
  `python -m py_compile agent/printer_agent.py` after any agent change.
  SNMP changes should be verified against a live printer when possible
  (owner grants per-printer read-only probe permission on request).
- **Deploy to prod is manual**: SSH to the VM, repo dir, `./update.sh`
  (git pull --ff-only + `docker compose up --build -d`). In-app updates were
  deliberately removed in v0.0.12 (no docker.sock mount) — never re-add.
- Never develop against prod; local compose is fully isolated.

## KEY FILES
| File | Purpose |
|------|---------|
| `VERSION` | Single source of truth for dashboard version |
| `agent/printer_agent.py` | Standalone remote agent — `AGENT_VERSION` must match `VERSION` |
| `agent/install_windows.ps1` / `install_pi.sh` | Site installers (served by the dashboard) |
| `.github/workflows/build-agent-exe.yml` | Builds `printer_agent.exe` on every release |
| `app/web/__init__.py` | App factory + `_run_migrations()` (all schema changes) + seeds |
| `app/run.py` | Entry point + APScheduler jobs (poll, predictive, reports, stale agents) |
| `app/web/routes/agent_api.py` | Agent check-in endpoint — auto-queues updates on version mismatch |
| `app/utils/version.py` | Reads VERSION; fetches GitHub releases for the Updates tab |
| `app/snmp/client.py` | SNMP get/walk — ALL values pass through `_coerce_value()` |
| `app/snmp/vendor/*.py` | `generic.py` (RFC 3805 + vendor detect) + per-vendor enrich |
| `app/alerts/evaluator.py` | Threshold/replacement detection, one-shot email state, tickets |
| `update.sh` | The prod deploy script (run ON the VM) |
| `config.yaml` | Non-secret defaults (SNMP timing, thresholds) — committed |

## ARCHITECTURE NOTES
- Flask app factory registers blueprints (dashboard, printers, discovery, alerts,
  reports, config, agents API, help, history). Templates: Jinja + Bootstrap.
- Poll flow: APScheduler → poller threads → `vendor/generic.probe()` (+ vendor
  enrich) → `normalizer` → `TelemetrySnapshot`/`SupplySnapshot` rows →
  `evaluator` (thresholds, replacement jump ≥20pp, offline detection) →
  `notifier` (SMTP email / helpdesk ticket).
- **Single gunicorn worker is intentional** — APScheduler runs in-process;
  more workers = duplicate schedulers = duplicate polls/emails.
- Remote agents: standalone script/exe scans site subnets, POSTs results to
  `agent_api` with a per-agent API key; check-in response can carry a command
  (update / config / rescan / uninstall). Windows = Scheduled Task `PrinterAgent`
  running the .exe from `C:\PrinterAgent\` as SYSTEM; Pi = systemd `printer-agent`.
- Roles: `viewer` / `admin` / `superadmin`. Factory Reset + Restore are
  superadmin-only; last superadmin can't be demoted.
- Data retention: monitoring data (snapshots, alert events, costs) is permanent;
  only the admin Activity Log auto-prunes (365 days, `app/utils/audit.py`).

## MIGRATIONS
All schema changes go in `_run_migrations()` in `app/web/__init__.py` as
idempotent DDL (`ADD COLUMN IF NOT EXISTS`; enum changes via `MODIFY COLUMN`
listing EVERY value). They run automatically on container start.

## GOTCHAS (learned the hard way)
- **Adding a vendor** touches FIVE places: `snmp/oids.py` prefix map,
  `vendor/generic.py` detection (+enterprise map + sysDescr keywords),
  `models/printer.py` vendor Enum, a `MODIFY COLUMN` migration, and
  `agent_api.py` `valid_vendors` — plus the agent's own copies. Miss one and
  the vendor silently coerces to `generic` (Konica lesson, v0.0.28).
- **pysnmp OctetString**: `prettyPrint()` returns `0x…` hex if ANY byte is
  non-printable (HP NUL-terminates strings). `_clean_octet_string()` in
  `snmp/client.py` fixes this — never bypass `_coerce_value()` (v0.0.21).
- **prtMarkerSuppliesTable**: column 4 is *Class*, column **5** is *Type*
  (RFC 3805). We shipped the wrong column once (v0.0.27). App keys on the
  strings `tonerCartridge` and `opc` — don't rename them.
- **Brother mono printers** report toner as sentinel `-2/-3` in the MIB; real %
  comes from the proprietary maintenance blob (`vendor/brother.py` decodes
  id `0x81`/`0x6f` records). Drum uses standard MIB. Misassigned Brother OID
  constants once wrote firmware version into serial (v0.0.27).
- **Agent is a PyInstaller ONE-FILE exe**: every launch extracts ~19 MB to
  `%TEMP%\_MEIxxxxxx` (SYSTEM → `C:\Windows\Temp`), cleaned only on clean exit.
  A crash-relaunch loop leaked **307 GB** on one server (v0.0.30). Protections:
  startup `_cleanup_orphan_mei()` sweep + service mode NEVER hard-exits
  (catches everything, sleeps 15 min, retries in-process) + Task Scheduler
  RestartInterval 15 min. Never reintroduce exits in service mode; never
  narrow the sweep; `SystemExit` must still propagate (self-update relies on it).
- **Repo must stay public** — agent self-update (`printer_agent.py` ~line 90)
  and `install_windows.ps1` download the exe anonymously. Going private
  strands every Windows agent (rework path is in HANDOFF.md backlog).
- **Release → exe gap**: the .exe asset appears ~2 min after
  `gh release create`; installs during the gap 404. Check Actions if in doubt.
- The `/printers` list page is **orphaned** (nav link removed) — printer
  housekeeping lives in **Config** (Removed Printers, Duplicates tabs).
  Check Config before assuming a top-level nav tab exists.
- Windows dev box uses PowerShell 5.1 (no `&&`); pass multi-line commit
  messages via `git commit -F -` heredoc in the Bash tool — a PowerShell
  `@'…'@` here-string once leaked literal `@` lines into a commit message.
- OneDrive hosts the repo path — files open in Excel/Office can hard-lock
  (EBUSY) reads; copy or close the app first.

## AGENT QUICK-REF
- Manual check-in (Windows): `Start-ScheduledTask -TaskName "PrinterAgent"`
- Manual check-in (Pi): `sudo systemctl restart printer-agent`
- Site logs: `C:\PrinterAgent\agent.log` / `journalctl -u printer-agent`
- Leaked-temp cleanup + stranded-agent recovery: HANDOFF.md runbook.
