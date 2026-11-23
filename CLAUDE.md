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
1. `VERSION`  (e.g. `v0.0.33`)
2. `agent/printer_agent.py` — `AGENT_VERSION = "v0.0.33"`  (search for it)
3. `agent/printer_agent.py` — docstring at top: `Version: v0.0.33`
4. `release-notes/v0.0.33.md` — the release notes (becomes the GitHub release body)

Then: 5. Commit ALL changed files  6. `git push` to **master** (the default branch —
there is no `main`).

**The release publishes itself.** `.github/workflows/auto-release.yml` fires when
`VERSION` changes on master: it builds `printer_agent.exe`, creates the release
`vX.Y.Z` with the notes file as its body, and attaches the exe before publishing.
Do NOT also run `gh release create` (cloud Claude sessions can push but are not
allowed to create releases — that is why this workflow exists). After pushing,
confirm with `gh run list` / `gh release view vX.Y.Z` that it published, and only
then tell the owner to run `./update.sh`: once the VM is on the new VERSION it
tells every agent to update, and they download the exe from that release.

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
| `.github/workflows/auto-release.yml` | Publishes the release + `printer_agent.exe` when `VERSION` changes on master |
| `.github/workflows/build-agent-exe.yml` | Fallback: builds the exe for a manually created release / manual rebuild |
| `release-notes/vX.Y.Z.md` | Release notes, one file per version (used as the GitHub release body) |
| `app/web/__init__.py` | App factory + `_run_migrations()` (all schema changes) + seeds |
| `app/run.py` | Entry point + APScheduler jobs (poll, predictive, reports, stale agents) |
| `app/web/routes/agent_api.py` | Agent check-in endpoint — auto-queues updates on version mismatch |
| `app/utils/version.py` | Reads VERSION; fetches GitHub releases for the Updates tab |
| `app/snmp/client.py` | SNMP get/walk — ALL values pass through `_coerce_value()` |
| `app/snmp/vendor/*.py` | `generic.py` (RFC 3805 + vendor detect) + per-vendor enrich |
| `app/alerts/evaluator.py` | Threshold/replacement detection, one-shot email state, tickets |
| `app/alerts/offline_review.py` | Long-offline printer ticket + stale-agent ticket (hourly job), probable-replacement hint |
| `app/scanner/poller.py` | Server poll (LOCAL printers only) + serial-change replace/move logic |
| `app/scanner/auto_scan.py` | Daily scan of server-polled subnets → `discovered_devices` approval queue |
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

## POLLING + DB TRANSACTION RULES (v0.0.32 lesson)
- Prod MariaDB enforces snapshot isolation: an UPDATE to a row that another
  transaction changed since this transaction first read fails with error 1020
  ("Record has changed since last read"). Agent check-ins and the server poll
  both write `printers`.
- NEVER hold a DB transaction open across network I/O (SNMP, SMTP). Read →
  `rollback()` → do the slow work → re-read fresh → write → commit, one printer
  per transaction. `poll_all_printers` and `offline_review` follow this; copy them.
- The server poll covers LOCAL printers only (`agent_id IS NULL`). Agent printers
  are written solely by `/api/agent/checkin`.
- Dedup flags set after sending an email use a bare `UPDATE` in a fresh
  transaction (`offline_review._exec_update`), never read-modify-write.

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
- **More than one LOCAL printer row can share an IP** (v0.0.33): replacing a
  printer archives the old row and starts a new one at the same address (MariaDB
  treats NULL `agent_id` as distinct in `uq_printer_ip_agent`). Never look a local
  printer up with `filter_by(ip_address=…).first()` — use
  `app/utils/printer_lookup.find_local_printer_by_ip()` (prefers the active row,
  ignores agent rows).
- **Serial-change logic needs two consecutive polls** agreeing on the new serial
  (`Printer.pending_serial`) before it archives/moves anything, and
  `_update_printer_status` never overwrites a differing serial. Don't "simplify"
  either — one flaky SNMP read would split a printer's history. Agent printers
  are excluded on purpose (sites reuse the same private IPs).
- **Agent settings ride on every check-in reply** (`resp["settings"]`: interval,
  keep_awake). The one-shot `pending_command="config"` slot is still used for
  subnet pushes and as a fallback for pre-v0.0.33 agents; don't rely on it for
  anything that must not be lost (an `update` command overwrites the slot).
- **agent.log is trimmed in place**, not rotated: on the Pi systemd appends
  stdout to the same file, and an in-place truncate keeps that writer valid.
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
