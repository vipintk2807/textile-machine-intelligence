# Test and verification report

**Read this first.** This project was generated in a sandbox with **no network access**. Only Python 3.12.3 (Linux),
`pandas` and `numpy` were installed; `streamlit`, `sqlalchemy`, `plotly`, `pytest` and `ruff` could **not** be installed
(`pip install` failed). Consequently the **database, service, UI and smoke-test layers were written but never executed**.
The first thing to do on your Windows machine is run `scripts\setup.bat` then `scripts\test.bat` and fix anything that fails.

## What was actually run (and results)

Command: `python3 -m unittest discover -s tests -t .` (the tests are `unittest.TestCase` classes, which `pytest` also collects;
**pytest itself was not run**).

| Test module | Scope | Result here |
|---|---|---|
| test_time_utils | timestamp parsing, naive policy, DST, windows, intervals | 15 passed |
| test_state_accounting | R/D/U/G accounting, required 45/15/0/0 scenario, boundaries, outage, ambiguity, 300 random invariant cases | 17 passed |
| test_validation | sensor/state/health record validation, form validation | 17 passed |
| test_rules_and_freshness | thresholds (strict), staleness, state duration, status derivation | 14 passed |
| test_csv_safety | formula-injection mitigation, blanks vs zero, no truncation | 7 passed |
| test_settings_rules | settings validation | 4 passed |
| test_simulated_source | determinism, chunk invariance, adapter contract, injected scenarios fed through the real validator, outage/recovery | 25 passed |
| test_environment | required packages importable, tz database | 2 passed, **1 failed (expected here)**: streamlit, sqlalchemy, plotly not installed |
| **Total executed** | | **101 passed, 1 failed (environment), 64 skipped** |

Skipped (SQLAlchemy/Streamlit missing — **not executed, no result**): test_database (13), test_machine_service (7),
test_state_and_ingestion (23), test_anomaly_maintenance_reports (12), test_simulation_and_e2e (6), test_app_smoke (3).
They cover: schema creation/versioning/migration/backup/restore/reset, foreign keys, rollback, machine CRUD, state derivation
through the DB, ingestion (quarantine, duplicates, late records, anomalies, source error/recovery, DB-failure rollback),
anomaly workflow, maintenance rules, reports and CSV exports, simulation lifecycle, end-to-end persistence across reopening,
and headless Streamlit rendering of every page.

## Other checks performed
* `ast.parse` of every `.py` file (syntax OK); a small custom AST script (not Ruff) for unused imports, undefined names
  and trailing whitespace — clean after fixes. Tokenizer scan for Python-3.12-only nested f-string quotes — none.
* `load_config` negative cases (bad log level, time zone, freshness, DB path) verified by hand.
* The `sqlite3` online-backup + WAL→DELETE conversion approach verified in isolation (not through the app).
* Performance (pure accounting only, this sandbox, 1 CPU): 10 000 observations 0.04 s; 100 000 → 0.72 s; 500 000 → 4.7 s.
  **No database, ingestion or UI performance was measured.**

## Not verified (must be checked on your machine)
* `pip install -r requirements.txt` (version ranges untested); Python 3.11 specifically (only 3.12.3 was available).
* All SQLAlchemy ORM/queries, session handling, SQLite PRAGMAs, `Database.initialize`, backup/restore code paths.
* Every Streamlit page: layout, widget behaviour, Plotly figures, `st.rerun`/flash flow, navigation callbacks,
  `st.dataframe` width handling across Streamlit versions, downloads. No browser test was done.
* `ruff check` / `ruff format`; `pytest` collection; Windows `.bat` scripts; GitHub publishing steps.
* Visual/accessibility review, real-data behaviour, any hardware.

## Where problems are most likely
1. Version-specific Streamlit/Plotly API differences (e.g. `width="stretch"`, `st.dataframe` options) — wrapped in fallbacks but untested.
2. SQLAlchemy mapping/query typos that static checks cannot reveal (the DB tests are designed to expose them quickly).
3. Ruff formatting preferences (`ruff format .` once).

If a database test fails, the traceback will point at the exact line; the pure core (accounting, validation, rules, simulator)
is independently tested and unaffected.
