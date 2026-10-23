# Textile Machine Intelligence

A local, read-only **machine-monitoring and machine-health application** for textile machinery:
machine states, runtime / downtime / unknown / monitoring-gap accounting, sensor readings, data freshness,
rule-based anomalies and user-entered maintenance records, with CSV export.

> **Status: version 0.1.0 — monitoring foundation, simulator-driven.**
> There is **no real machine, PLC, gateway or sensor integration** in this release. All demo data is
> **synthetic** and labelled `SIMULATED` everywhere. Predictive maintenance is **not implemented**.
> Nothing here has been validated on real textile equipment. See [Known limitations](#known-limitations).

## What it does

| Area | Behaviour |
|---|---|
| Machine registry | Register, edit, deactivate (history is kept) machines; free-text type/location. |
| Status | `Running`, `Stopped`, `Unknown`, `Disconnected`. Derived from the latest **valid** observation by **event time**. Stale data → `Unknown` (never "Stopped"); source outage → `Disconnected`. |
| Accounting | `T = R + D + U + G` (running, stopped, reported-unknown, monitoring gap). Gaps and outages are never counted as downtime. *Known-state runtime share* = `R ÷ (R + D)`; shown as `n/a` when there is no known time. Not OEE. |
| Sensors | Readings with explicit units (or "unknown"), freshness, charts that break at data gaps, configurable thresholds (strict comparison: equal does **not** trigger). |
| Data quality | Every record is validated (NaN/inf, bad/naive/future timestamps, unknown machine/sensor, unit mismatch, invalid state…). Invalid records are **quarantined** with a diagnosis, never used in calculations. Duplicates are ignored by `(source, source_event_id)`. |
| Anomalies | Rule-triggered (threshold, stale sensor, prolonged state, rejected/out-of-order record, source unavailable). Open → Acknowledged → Resolved (resolution note required). Acknowledging never resolves. |
| Maintenance | User-entered records, clearly labelled as user-entered; optional link to an anomaly of the same machine. |
| Export | CSV for state history, runtime summary, sensor readings, anomalies, maintenance. UTC timestamps, reporting period and provenance columns; spreadsheet-formula injection neutralised; never silently truncated. |
| Storage | Local SQLite (WAL), schema versioning, online backup, verified restore, guarded reset. |

## Requirements

* Windows 10/11 (also works on Linux/macOS), **Python 3.11 or newer** (python.org installer; tick *Add python.exe to PATH*).
* No GPU, no paid service, no cloud account, no API key. All dependencies are free/open source
  (Streamlit, SQLAlchemy, pandas, Plotly, tzdata, pytest, Ruff). Internet is needed only to `pip install`.

## Windows setup and run (Command Prompt)

```bat
cd path\to\textile-machine-intelligence
scripts\setup.bat
scripts\run.bat
```

`setup.bat` creates `.venv` and installs `requirements.txt`; `run.bat` starts the app at
<http://127.0.0.1:8501> (bound to this computer only). Equivalent manual commands:

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.address 127.0.0.1
```

### First use

1. Open **Settings → Simulation** and click **Initialise synthetic demo data** (4 machines, 6 h of history).
   The history deliberately contains: a threshold spike, an UNKNOWN period, a machine that went silent while the
   source stayed healthy (stale data), a source interruption, late/out-of-order records and 10 invalid records.
2. Browse **Dashboard**, **Machines**, **History**, **Sensors**, **Anomalies**.
3. Use **Poll simulated source now** to extend the stream, or **Simulate source interruption** to see
   `Disconnected` (machines are *not* shown as stopped) and recovery with the gap preserved.
4. Register your own machine under **Machines → Register machine**. (A machine only receives simulated data while
   it is assigned to the simulated source.)

## Tests and checks

```bat
scripts\test.bat
```

runs `python -m pytest` and `python -m ruff check .`. Run `python -m ruff format .` once if you want
Ruff-style formatting. Results from the environment where this project was generated are in
[`docs/TEST_REPORT.md`](docs/TEST_REPORT.md) — **read it**: the database, UI and smoke tests were written but could
not be executed there.

## Configuration

Optional environment variables (CMD: `set NAME=value` before `scripts\run.bat`):

| Variable | Default | Meaning |
|---|---|---|
| `TMI_DATA_DIR` | `.\data` | Folder for database, `backups\`, `logs\` |
| `TMI_DB_PATH` | `<data>\textile_machine_intelligence.db` | SQLite file |
| `TMI_LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `TMI_TIMEZONE` | `Asia/Kolkata` | Default reporting time zone (IANA name) |
| `TMI_FRESHNESS_SECONDS` | `300` | Default freshness threshold (illustrative) |

Runtime settings (freshness, stale limits, duration limits, severities, thresholds) are edited in **Settings**.
**All numeric defaults and thresholds are illustrative software settings, not textile-industry standards.**

## Time policy

Timestamps are stored in UTC and shown in the reporting time zone. Event time (when it happened at the source) and
ingestion time (when this app stored it) are kept separately; freshness, ordering and accounting use **event time**.
Reporting windows are half-open `[start, end)`. Naive timestamps from a source are **rejected** (never guessed).
Local date/time inputs that fall in a daylight-saving gap are rejected; ambiguous ones use the first occurrence.

## Backup, restore, reset

* **Backup:** Settings → Database → *Create backup now*, or `python scripts\db_tools.py backup` (files in `data\backups`).
* **Restore (app closed):** `scripts\restore_backup.bat data\backups\<file>.db` — the backup is verified first and the
  current database is saved as a safety copy.
* **Reset simulated data:** removes only synthetic states/readings/health/quarantine/anomalies; machines, sensor
  definitions and your maintenance records are kept (phrase confirmation required).
* **Delete all data:** Settings → Database (phrase confirmation; automatic backup first).
* Check a database: `python scripts\db_tools.py check`.

## Project layout

```
app.py                     Streamlit entry point        config.py   validated configuration
src/core/                  pure logic: intervals, freshness, rules, validation, settings (stdlib only)
src/data_sources/          adapter interface + deterministic simulator
src/models/                enums/contracts (schemas.py) and SQLAlchemy models (orm.py)
src/repositories/          database engine, schema versioning, backup/restore
src/services/              machine, state, sensor, anomaly, maintenance, ingestion, simulation, report services
src/ui/                    Streamlit pages, theme, charts
src/utils/                 time and CSV utilities
tests/  scripts/  docs/    tests, Windows/CLI helpers, design + test documentation
```

See [`docs/DESIGN_DECISIONS.md`](docs/DESIGN_DECISIONS.md) for the accounting model, ordering/duplicate/outage policies
and the reconciliation of the specification documents, and
[`docs/INTEGRATION_PREREQUISITES.md`](docs/INTEGRATION_PREREQUISITES.md) for what is needed before any real machine is connected.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `python` not recognised | Reinstall Python and tick *Add python.exe to PATH*, or use `py -3.11`. |
| `Unknown time zone` | `pip install tzdata` (Windows has no system time-zone database). |
| Port 8501 busy | `python -m streamlit run app.py --server.port 8502` |
| "The application could not start" | Read the message (bad `TMI_*` variable, newer/unknown database, unwritable folder). Details: `data\logs\app.log`. |
| "database is in use" on restore | Close the app (and any `streamlit` window), then retry. |
| Pip install errors | Upgrade pip (`python -m pip install --upgrade pip`); check Python ≥ 3.11. |
| Blank dashboard | No machines yet — initialise the simulation (Settings → Simulation). |

## Known limitations

* **Simulator only.** No OPC UA, Modbus, MQTT, PLC or gateway adapter exists; see the integration prerequisites document.
* **No predictive maintenance, no ML, no authentication/multi-user access** (single local user, bound to 127.0.0.1).
* Rule thresholds, freshness and duration limits are demonstration settings, not validated limits.
* Sensor definitions are created by the simulator; the UI edits thresholds/stale limits but does not create arbitrary sensors.
* Nothing in this release has been validated with real machine data or hardware.
* Package versions are ranges that were **not** install-tested where the project was generated (see test report).

## Publishing to GitHub (account `vipintk2807`, repository `textile-machine-intelligence`)

Run in CMD **inside the extracted project folder**. Never paste tokens, passwords or private keys anywhere.

```bat
:: 1. Confirm you are in the right folder and nothing private is present
cd path\to\textile-machine-intelligence
dir
git status
:: Expect: no data\*.db, no .env, no planning documents (they are git-ignored; double-check "git status")

:: 2. First time only
git init -b main

:: 3. Repository-LOCAL identity (do not rely on the global one)
git config user.name "Vipin T K"
git config user.email "YOUR-GITHUB-NOREPLY-OR-PUBLIC-EMAIL"
git config user.name
git config user.email

:: 4. Check the SSH alias exists in %USERPROFILE%\.ssh\config (Host github-vipintk2807) and authenticates as the right account
type %USERPROFILE%\.ssh\config
ssh -T git@github-vipintk2807
:: GitHub should answer: "Hi vipintk2807! You've successfully authenticated..."  If it names another account, STOP and fix the alias.

:: 5. Create an EMPTY repository named textile-machine-intelligence under vipintk2807 on github.com (web UI), then:
git remote add origin git@github-vipintk2807:vipintk2807/textile-machine-intelligence.git
git remote -v

:: 6. Review what will be committed, then publish
git add .
git status
git commit -m "Initial release: simulator-driven machine monitoring foundation"
git branch --show-current
git push -u origin main
```

If `git remote add` says `origin` exists: `git remote -v` and, if it points elsewhere, `git remote set-url origin <correct-url>`.
Do not modify other accounts' SSH entries. The MIT licence in `LICENSE` is a default choice — change it before publishing if you prefer another.
