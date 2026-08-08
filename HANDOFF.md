# HANDOFF — Network Printer Dashboard

Complete maintainer brain-dump, written 2026-08-08 when development moved to a
new Claude Code account. **CLAUDE.md** is the per-session operating manual;
this file is the deep context. This repo is **public**, so infrastructure
*access* specifics (SSH targets, account identities) live in
**HANDOFF-PRIVATE.md** — gitignored, carried between machines by the owner,
never committed. If you don't have it, ask the owner.

---

## 1. What this is and who uses it

A self-hosted web dashboard that monitors network printers over SNMP:
live supply levels, offline detection, email alerts, auto-filed helpdesk
tickets, toner-cost tracking, six reports with CSV export + scheduled email
delivery, predictive "toner will run out in N days" alerts, and **remote
agents** that extend monitoring to sites the server can't reach directly.

Users: the owner (superadmin) plus tech users with `admin` or `viewer`
dashboard accounts. Techs receive the alert emails and helpdesk tickets and
enter toner costs when prompted. Sites are deployed/administered via
ConnectWise Backstage (RMM) — that's a deployment vehicle, not an integration.

## 2. Infrastructure map (access specifics → HANDOFF-PRIVATE.md)

| Piece | What / where |
|---|---|
| Production host | On-prem Linux VM (owner-controlled, local hardware). Docker Compose: `app` (Flask, port 7070) + `db` (MariaDB 11, named volume `db_data`) |
| Public URL | `https://mattsvm.taild0c836.ts.net` — Tailscale **Funnel** on the owner's personal tailnet. **Baked into every deployed agent's config** — changing it means touching every site |
| Dashboard version | See `VERSION` / the footer / Config → Updates |
| Remote sites | Windows machines run `C:\PrinterAgent\printer_agent.exe` via Scheduled Task `PrinterAgent` (SYSTEM, at-startup, 15-min restart interval); Raspberry Pi/Linux run `printer_agent.py` via systemd `printer-agent` |
| GitHub | `mkeathley2/network-printer-dashboard`, **public** (required: agents download the exe from Releases anonymously). Actions workflow builds `printer_agent.exe` on every release |
| SMTP | An **internal SMTP relay on the owner's network** — deliberately not documented here (it may change). Configured live in Config → Email/SMTP |
| DNS/TLS | All handled by Tailscale Funnel; no separate domain, certs, or reverse proxy |

**There is no staging environment.** Local `docker compose up` is the test
bed; prod is the only deployment.

## 3. Version history — what and WHY

| Version | What | Why / lesson |
|---|---|---|
| v0.0.1–0.0.3 | Core dashboard: SNMP polling, discovery scan, supply bars, removed/restore, auth, locations, helpdesk ticket email | Foundation |
| v0.0.4–0.0.6 | Kyocera model/serial/color fixes, Poll All, UX | Early SNMP reality: every vendor lies differently |
| v0.0.7 | Reports (6 of them + CSV), toner-cost tracking, predictive toner alerts, first remote agent | Data was captured but not actionable |
| v0.0.8 | Agent auto-update on version mismatch, subnet auto-detect, dashboard-pushed config | Hand-updating agents didn't scale |
| v0.0.9 | Temp passwords + forced change on first login | Admins shouldn't know user passwords |
| v0.0.10 → **v0.0.12** | Built an in-app updater… then **removed it** | Updater needed docker.sock in the container = root-equivalent escape risk. Decision: manual `./update.sh` over SSH forever. Don't re-add |
| v0.0.11 | Favicon, in-app Help manual, UX polish | Onboarding |
| v0.0.14 | Bulk reset per-printer threshold overrides | Admin QoL |
| v0.0.15–0.0.16 | History page overhaul (date axes, range pills, depletion estimates, replacement markers); consumption-rate regression fixed to cut window at last replacement; OverflowError hotfix | Regression across a toner swap produced nonsense rates |
| v0.0.17, 0.0.23 | Documentation refreshes | Docs ship with features, same release |
| v0.0.18 | **Standalone Windows .exe agent** (PyInstaller onefile, built by Actions, attached to Releases); never-deployed agent delete fix | Win10 targets often have no Python; Backstage installs as SYSTEM |
| v0.0.19 | Auto-ticket on critical, scheduled report emails, dashboard links in emails | Alert → action gap |
| v0.0.20 | Cost-entry helpdesk ticket on replacement (+ test button) | Missing costs made cost reports useless |
| v0.0.21 | **pysnmp hex-string fix** (`_clean_octet_string`); multi-select discovery add + location | HP NUL-terminates strings → `prettyPrint()` hex garbage |
| v0.0.22 | Cartridge column on Toner Cost, styled consumption email, **Super Admin role** (Reset/Restore gated; existing admins auto-promoted once) | Techs need admin without destructive power |
| v0.0.24 | Activity-log retention 30d → 365d + retention docs | Owner feared toner history pruned at 30d — it never was; docs conflated audit log with monitoring data |
| v0.0.25 | Per-printer `supplies_under_contract` flag | Contract printers polluted cost reports and fired pointless cost tickets |
| v0.0.26 | Location rename/mass-move, blank-subnet = auto-detect deploys, per-agent printer counts | Typos were permanent; placeholder subnet defeated auto-detect |
| v0.0.27 | Duplicate detection (by serial), Brother maintenance-blob toner %, **supply TYPE col 4→5 fix** | Brother sentinel `-2/-3`; wrong RFC 3805 column made drums = toner everywhere |
| v0.0.28 | Konica Minolta vendor (enterprise 18334), reverse-DNS hostname fallback | Vendor enum must widen in model + migration + agent_api together |
| v0.0.29 | Duplicates view moved into Config | Feature was orphaned on the linkless `/printers` page |
| v0.0.30 | **Agent temp-leak fix**: `_MEI` startup sweep, never-hard-exit service mode, 15-min restart interval | One site leaked **307 GB / 16,717 folders** in `C:\Windows\Temp` — crash-relaunch ~1/min × onefile extraction. Root crash cause never diagnosed (deliberate; hardened instead) |
| v0.0.31 | Maintainer handoff docs (this file, CLAUDE.md rewrite) | Development moved to a new Claude Code account |

## 4. Third-party integrations

- **GitHub** — repo hosting, Releases, and Actions (`build-agent-exe.yml`:
  on release-created, windows-latest runner, PyInstaller
  `--onefile --collect-all pysnmp`, attaches `printer_agent.exe`). Auth on dev
  machines via `gh` CLI as **mkeathley2** (repo stays on the personal account —
  decided at handoff). No PATs, no Actions secrets; the implicit
  `GITHUB_TOKEN` suffices.
- **Tailscale** — owner's personal tailnet; Funnel exposes port 7070 as the
  public HTTPS URL. Free tier. If the tailnet/URL ever changes: update
  `public_url` in Config → Remote Agents AND each site's `agent_config.json`.
- **SMTP** — internal relay (see §2); settings + credentials live in the
  `site_settings` DB table via Config → Email/SMTP. `.env` `SMTP_*` variables
  exist as an alternative bootstrap path but the DB settings are what's used.
- **Helpdesk** — not an API: tickets are formatted emails sent to the
  `helpdesk_email` address configured in Config.
- That's all. No analytics, no external APIs, no payment/auth providers.

## 5. SECRETS INVENTORY — locations only, NEVER values

| Secret | Lives in | Notes |
|---|---|---|
| DB passwords (`DB_ROOT_PASSWORD`, `DB_PASSWORD`), Flask `SECRET_KEY` | `.env` at repo root on the **prod VM** (and any dev machine) | Gitignored. Compose injects them. Local dev: generate fresh random values, never copy prod's |
| SMTP credentials | `site_settings` table in prod MariaDB | Entered via Config → Email/SMTP (superadmin/admin) |
| Per-agent API keys | `remote_agents.api_key` in prod DB; mirrored in each site's `C:\PrinterAgent\agent_config.json` (or Pi equivalent) | Regenerate per agent from Config → Agents |
| Dashboard user passwords | `users` table (werkzeug hashes only) | Reset via Config → Users |
| GitHub auth | `gh` CLI credential store on each dev machine | `gh auth login` as mkeathley2 |
| Tailscale account | Owner's Tailscale login (external) | See HANDOFF-PRIVATE.md |
| VM login | HANDOFF-PRIVATE.md | Not recorded publicly |

Nothing secret is committed anywhere in this repo or its history. In-app DB
backups **contain the site_settings table** (i.e., SMTP credentials) — treat
backup files as secrets.

## 6. Operations runbook

**Deploy** — on the VM, in the repo dir: `./update.sh` (ff-only pull +
`docker compose up --build -d`; migrations run on app start). Rollback:
`git reset --hard <previous-tag>` + `docker compose up --build -d`
(migrations are additive; old code runs fine against a newer schema).

**Logs** — `docker compose logs -f app` (VM). Per-site agent:
`C:\PrinterAgent\agent.log` / `journalctl -u printer-agent`.

**Backups** — ⚠️ **No scheduled backups exist.** The only mechanism is the
manual Config → Backup & Reset → Backup download (SQL dump; superadmin sees
Restore/Reset, admins see Backup only). Adding a cron'd `mariadb-dump` on the
VM is the **top backlog item**. Restore: Config → Restore (superadmin).

**Recovery scenarios**
- *VM dies*: rebuild Linux + Docker, clone repo, recreate `.env` (new values
  are fine — real config lives in the DB), `docker compose up -d`, restore the
  newest backup via Config → Restore, re-enable Tailscale Funnel. Agents
  reconnect on their own (URL unchanged).
- *Agent stranded/stale*: dashboard flags stale agents. Re-run the install
  one-liner from Config → Agents via Backstage (idempotent), or delete the row
  (never-deployed and stale agents hard-delete immediately).
- *Agent filled `C:\Windows\Temp` with `_MEI` folders* (pre-v0.0.30 damage):
  ```powershell
  Stop-ScheduledTask -TaskName PrinterAgent
  Get-ChildItem 'C:\Windows\Temp' -Directory -Filter '_MEI*' |
    Where-Object { Test-Path (Join-Path $_.FullName 'pysnmp') } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  Start-ScheduledTask -TaskName PrinterAgent
  ```
- *Updates tab shows stale release info*: 1-hour cache; Config → Updates has a
  refresh that busts it.
- *Locked out*: any superadmin can reset others; if ALL access is lost, a
  direct DB update on the `users` table from the VM is the escape hatch.

## 7. From-scratch setup (new dev machine)

1. Install: git, GitHub CLI (`gh`), Docker Desktop (or engine), Python 3.12+
   (only needed for `py_compile` agent syntax checks).
2. `gh auth login` → GitHub.com → HTTPS → browser → sign in as **mkeathley2**
   (the repo stays on the personal account; no collaborator setup needed).
3. `gh repo clone mkeathley2/network-printer-dashboard`
4. Create `.env` in the repo root with fresh random values:
   `DB_ROOT_PASSWORD`, `DB_PASSWORD`, `SECRET_KEY` (optional `SMTP_*` — leave
   unset locally; you don't want a dev box emailing people).
5. `docker compose up --build` → http://localhost:7070 → login `admin`/`admin`
   (fresh-DB seed; local only — prod's accounts are its own).
6. Place the owner's **HANDOFF-PRIVATE.md** in the repo root (stays untracked).
7. Optional: add a printer on your LAN by IP to watch a real poll succeed.

## 8. Working conventions (owner preferences)

- **Version lockstep, always** — the CLAUDE.md checklist is law; even
  docs-only releases bump `VERSION` + `AGENT_VERSION` + docstring together.
- **Plan before build** — present a plan; ask clarifying questions as concise
  multiple-choice; the owner decides trade-offs (they regularly pick the
  pragmatic option over the architecturally pure one — e.g. keeping the
  onefile exe with a sweep rather than switching to onedir).
- Releases via `gh release create` with real, explanatory notes — the release
  page doubles as the changelog. Commit messages: `vX.Y.Z: summary` + a body
  explaining *why*.
- Docs ship in the same release as the feature (in-app Help §s + README).
- Schema changes only via idempotent `_run_migrations()` DDL.
- Verify SNMP work against live printers (owner grants read-only probe
  permission per printer on request). Report findings with real numbers.
- No automated tests exist — be explicit about what was and wasn't verified.
- Destructive ops (Factory Reset / Restore) stay superadmin-only.
- The owner values root-cause investigation but will explicitly choose
  "harden defensively, don't diagnose" when time matters — ask which they want.

## 9. Backlog — discussed but not built

1. **Scheduled DB backups** (top priority): cron'd `mariadb-dump` on the VM +
   retention + off-VM copy. Today a VM disk failure loses everything since the
   last manual backup.
2. **Dashboard-hosted agent distribution → then make the repo private**
   (designed at handoff, deferred): dashboard serves the exe to agents
   authenticated by their API keys (PAT-pull from GitHub or Actions-push);
   agent + `install_windows.ps1` download from the dashboard instead of
   GitHub; Updates tab gets a token. Sequencing is critical: ship it while
   public → wait until Config → Agents shows the whole fleet on the new
   version → only then flip private. Flipping early strands agents.
3. **ridge-svr crash-loop root cause**: v0.0.30 hardened around a ~1/min agent
   exit loop (began 2026-06-13) without diagnosing it. That site's admin was
   asked to save `agent.log` aside before cleanup — if it resurfaces, read it.
4. **One-time data cleanup** of pre-v0.0.21 hex-encoded strings
   (`supply_color`/description on old AlertEvents) — cosmetic, declined so far.
5. **PyInstaller `--onedir`** (declined — don't re-propose unless temp issues
   recur) and **trimming `--collect-all pysnmp`** (kept for safety; pysnmp
   loads base MIBs at engine init).
6. **SNMPv3** — client is v2c-only by config; pysnmp supports v3 if a site
   ever requires auth/priv.

## 10. State at handoff (2026-08-08)

Current release **v0.0.31** (docs only — functionally identical to v0.0.30).
All features stable; agent fleet auto-updates on check-in; no known bugs.
Monitoring data retention is permanent by design; audit log prunes at 1 year.
The owner continues as operator — this handoff changes the *development*
account, not ownership, infrastructure, or the GitHub account.
