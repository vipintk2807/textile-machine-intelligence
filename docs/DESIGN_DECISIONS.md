# Design decisions

## 1. Reconciling the specification documents
* **Accounting formula.** `app_workflow.md`/`project_explanation.md` describe `T = R + D + U` with
  `utilization = R / (T − U)`; `master_prompt.md`, `technical_specification.md` and `testing_requirements.md`
  describe `T = R + D + U + G` with `known-state runtime share = R / (R + D)`. The latter was implemented. The two
  are numerically consistent when the workflow's "unclassified" time `U_wf` is read as `U + G`:
  `R / (T − U_wf) = R / (R + D)`. The UI therefore shows Unknown (reported) and Monitoring gap separately, which is a
  refinement, not a contradiction. No blocking contradiction was found.
* **Pydantic.** The stack lists "Pydantic where appropriate". All validation is implemented in pure functions that return
  explicit per-field issue codes (needed for quarantine diagnostics), so Pydantic was **not** used and is not a dependency.
* **Extra module.** `src/core/` (pure logic) and `src/models/orm.py`, `src/services/simulation_service.py`,
  `overview_service.py` were added beyond the suggested tree so the business logic is testable without Streamlit or a database.

## 2. State vocabulary
Sources may report `RUNNING`, `STOPPED`, `UNKNOWN`. `DISCONNECTED` is a *monitoring condition* derived from source health
and is **rejected** if a source tries to report it as a machine state. Effective status precedence:
source UNAVAILABLE → `DISCONNECTED`; no observation → `UNKNOWN`; latest older than freshness threshold → `UNKNOWN (stale)`;
latest reported/ambiguous UNKNOWN → `UNKNOWN`; else `RUNNING`/`STOPPED`. The last known state is always shown separately.

## 3. Evidence model for accounting
Stored rows are *observations*; `is_transition`/`previous_state` are derived by event-time order (and re-derived for the
next event when a late record arrives). Each observation supports its state from its event time until the next observation,
for at most the **evidence window** (= the freshness threshold) and never into a known source outage. Silence beyond that is
a **gap (G)** — not downtime. A source that only reports transitions must therefore send periodic heartbeats or the
freshness threshold must be raised. Changing the freshness setting changes reports for past periods (the value used is shown).

## 4. Ordering, duplicates, ambiguity
* Ordering uses event time; ingestion order never decides the current state. Out-of-order records are stored with
  quality `DELAYED` and raise a (deduplicated) data-quality anomaly.
* Duplicates: only `(source, source_event_id)` identifies a duplicate (database unique constraint + pre-check). Identical
  values without an id are kept (they may be genuine repeats).
* Same event time, different states: resolved by `source_sequence` when all records carry one; otherwise the instant is
  **ambiguous** and counted as UNKNOWN.

## 5. Source outages
An adapter failure is a structured `SourceError`, never an empty list. It is recorded as source health `UNAVAILABLE`
(start = detection time), creates a connectivity anomaly per enabled machine of that source, and the outage interval is
accounted as monitoring gap. No states are fabricated and no replay is invented; "valid empty" (source healthy, nothing new)
is a different outcome. A batch with rejected records sets health `DEGRADED`.

## 6. Anomaly policy
Strict comparisons (`>` upper, `<` lower, `>` stale/duration limits). One active episode per (machine, sensor, rule); repeats
update `occurrence_count`; a new episode starts only after resolution. Stale-sensor alerts are suppressed while the source is
unavailable (root cause known) and are classed `DATA_QUALITY`, not machine faults. Default severities are never CRITICAL.

## 7. Time
UTC storage via a `TypeDecorator` that rejects naive datetimes; reporting in a configurable IANA zone (default `Asia/Kolkata`);
DST gap → error, DST overlap → first occurrence. Windows are half-open.

## 8. Simulator
Deterministic function of `(seed, machine, time)`; chunk-invariant; ids are stable so re-reads de-duplicate. Sensor "units"
(`sim-units`, unknown) are illustrative. Reset removes only records whose provenance is SIMULATED.

## 9. Security / safety
Local-only (127.0.0.1), no authentication, no secrets, telemetry disabled in `.streamlit/config.toml`. Adapters have no write
or control path. User text rendered with unsafe HTML is escaped; CSV cells are formula-neutralised.
