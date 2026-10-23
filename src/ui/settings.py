"""Settings: thresholds, freshness, time zone, simulation lifecycle and database tools."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.core.settings_rules import (
    FRESHNESS_RANGE, SIM_LOOKBACK_RANGE, SIM_MACHINES_RANGE, SIM_SEED_RANGE,
)
from src.models.schemas import Severity
from src.repositories.database import backup_database
from src.services.sensor_service import SensorService
from src.services.settings_service import SettingsService, defaults_from_config
from src.services.simulation_service import RESET_PHRASE, SimulationService
from src.ui.components import AppContext, empty_state, flash, guarded, header, show_table
from src.utils.time_utils import COMMON_TIMEZONES, utc_now

RESET_ALL_PHRASE = "DELETE ALL DATA"


def render(ctx: AppContext) -> None:
    header("Settings", "Configuration, simulation controls and database tools.")
    tabs = st.tabs(["Monitoring", "Sensor thresholds", "Simulation", "Database"])
    with tabs[0]:
        _monitoring(ctx)
    with tabs[1]:
        _thresholds(ctx)
    with tabs[2]:
        _simulation(ctx)
    with tabs[3]:
        _database(ctx)


def _monitoring(ctx: AppContext) -> None:
    s = ctx.settings
    st.caption("All defaults are illustrative software settings, not textile-industry standards.")
    with st.form("settings_monitoring"):
        fresh = st.number_input(
            "Freshness threshold (seconds) — a machine state older than this is 'stale'; also how long one observation supports a state in reports",
            FRESHNESS_RANGE[0], FRESHNESS_RANGE[1], s.freshness_seconds, step=10)
        stale = st.number_input("Default stale-sensor limit (seconds)", 10, 604_800, s.sensor_stale_seconds, step=30)
        zones = list(dict.fromkeys([s.timezone, *COMMON_TIMEZONES]))
        tz = st.selectbox("Reporting time zone", zones, index=0)
        use_run = st.checkbox("Enable maximum continuous RUNNING duration rule", s.max_running_seconds is not None)
        run_lim = st.number_input("Max running (seconds)", 60, 604_800, s.max_running_seconds or 28_800, step=60)
        use_stop = st.checkbox("Enable maximum continuous STOPPED duration rule", s.max_stopped_seconds is not None)
        stop_lim = st.number_input("Max stopped (seconds)", 60, 604_800, s.max_stopped_seconds or 3_600, step=60)
        st.markdown("**Severity per rule** (nothing is CRITICAL unless you choose it)")
        sev = {}
        cols = st.columns(3)
        for i, (rule, current) in enumerate(sorted(s.severities.items())):
            sev[rule] = cols[i % 3].selectbox(rule, [x.value for x in Severity], index=[x.value for x in Severity].index(Severity(current).value), key=f"sev_{rule}")
        saved = st.form_submit_button("Save settings")
    if saved:
        def action() -> None:
            with ctx.db.session_scope() as session:
                SettingsService(session, defaults_from_config(ctx.config)).update({
                    "freshness_seconds": int(fresh), "sensor_stale_seconds": int(stale), "timezone": tz,
                    "max_running_seconds": int(run_lim) if use_run else None,
                    "max_stopped_seconds": int(stop_lim) if use_stop else None, "severities": sev,
                })
            flash("Settings saved.")

        guarded(action)


def _thresholds(ctx: AppContext) -> None:
    with ctx.db.session_scope() as session:
        definitions = SensorService(session).definitions()
        if not definitions:
            empty_state("No sensors defined yet. Initialise the simulation to create demo sensors.")
            return
        show_table(pd.DataFrame(
            [{"Machine": d.machine_id, "Sensor": d.sensor_code, "Unit": d.unit or "unknown",
              "Lower": d.lower_threshold, "Upper": d.upper_threshold, "Severity": d.threshold_severity.title(),
              "Stale after (s)": d.stale_after_seconds} for d in definitions]
        ))
        labels = {f"{d.machine_id} / {d.sensor_code}": d for d in definitions}
        choice = st.selectbox("Edit sensor", list(labels), key="thr_pick")
        d = labels[choice]
        sensor_id, lower0, upper0, sev0, stale0 = d.id, d.lower_threshold, d.upper_threshold, d.threshold_severity, d.stale_after_seconds
    st.caption("Leave a threshold empty to disable it. A reading exactly equal to a threshold does NOT trigger. Thresholds are your configuration, not validated limits.")
    with st.form(f"thr_{sensor_id}"):
        lower = st.text_input("Lower threshold", "" if lower0 is None else str(lower0))
        upper = st.text_input("Upper threshold", "" if upper0 is None else str(upper0))
        sev = st.selectbox("Severity", [x.value for x in Severity], index=[x.value for x in Severity].index(sev0))
        stale = st.text_input("Stale after (seconds, empty = default)", "" if stale0 is None else str(stale0))
        saved = st.form_submit_button("Save thresholds")
    if saved:
        def action() -> None:
            from src.models.schemas import ValidationFailed, ValidationIssue

            def num(text: str, name: str, cast):
                text = text.strip()
                if not text:
                    return None
                try:
                    return cast(text)
                except ValueError as exc:
                    raise ValidationFailed([ValidationIssue("NOT_A_NUMBER", name, "Enter a number or leave empty.")]) from exc

            with ctx.db.session_scope() as session:
                SensorService(session).update_thresholds(
                    sensor_id, lower_threshold=num(lower, "lower", float), upper_threshold=num(upper, "upper", float),
                    severity=Severity(sev), stale_after_seconds=num(stale, "stale_after", int))
            flash("Thresholds saved.")

        guarded(action)


def _simulation(ctx: AppContext) -> None:
    st.markdown("**All simulated data is synthetic.** It exists to demonstrate and test the application.")
    with ctx.db.session_scope() as session:
        service = SimulationService(session, ctx.settings)
        info = service.status()
    if not info["initialized"]:
        with st.form("sim_init"):
            seed = st.number_input("Random seed", SIM_SEED_RANGE[0], SIM_SEED_RANGE[1], ctx.settings.sim_seed)
            count = st.number_input("Demo machines", SIM_MACHINES_RANGE[0], SIM_MACHINES_RANGE[1], ctx.settings.sim_machine_count)
            hours = st.number_input("History to generate (hours)", SIM_LOOKBACK_RANGE[0], SIM_LOOKBACK_RANGE[1], ctx.settings.sim_lookback_hours)
            go = st.form_submit_button("Initialise synthetic demo data")
        st.caption("Creates labelled demo machines and back-fills history including injected demo situations (a spike, an unknown period, a silent machine, a source interruption, late records and invalid records).")
        if go:
            def action() -> None:
                with st.spinner("Generating synthetic history..."):
                    with ctx.db.session_scope() as session:
                        report = SimulationService(session, ctx.settings).initialize_demo(
                            seed=int(seed), machine_count=int(count), lookback_hours=int(hours))
                        SettingsService(session, defaults_from_config(ctx.config)).update(
                            {"sim_seed": int(seed), "sim_machine_count": int(count), "sim_lookback_hours": int(hours)})
                flash(report.message)

            guarded(action)
        return

    st.success(f"Simulation initialised (seed {info['seed']}, anchor {info['anchor']}).")
    st.caption("Machine SIM-M03 is scripted to be silent until the first poll, to demonstrate stale data while the source is healthy.")
    c1, c2 = st.columns(2)
    if c1.button("Poll simulated source now"):
        def poll() -> None:
            with ctx.db.session_scope() as session:
                report = SimulationService(session, ctx.settings).poll(utc_now())
            flash(report.message)

        guarded(poll)
    minutes = c2.number_input("Interruption length (minutes)", 1, 60, 2)
    if c2.button("Simulate source interruption starting now"):
        def interrupt() -> None:
            with ctx.db.session_scope() as session:
                svc = SimulationService(session, ctx.settings)
                svc.start_interruption(int(minutes), utc_now())
                svc.poll(utc_now())
            flash("Interruption started. Machines show DISCONNECTED (not stopped) until polling succeeds after it ends.")

        guarded(interrupt)

    st.markdown("---")
    st.markdown(f"**Reset simulated data** removes only synthetic states, readings, health events, quarantined records and anomalies. Machines, sensor definitions and your maintenance records are kept. Type `{RESET_PHRASE}` to confirm.")
    phrase = st.text_input("Confirmation", key="sim_reset_phrase")
    if st.button("Reset simulated data"):
        def reset() -> None:
            with ctx.db.session_scope() as session:
                counts = SimulationService(session, ctx.settings).reset_simulated_data(phrase)
            flash("Simulated data removed: " + ", ".join(f"{k}={v}" for k, v in counts.items()))

        guarded(reset)


def _database(ctx: AppContext) -> None:
    st.write(f"Database file: `{ctx.config.db_path}`")
    st.write(f"Backups folder: `{ctx.config.backup_dir}`")
    if st.button("Create backup now"):
        def action() -> None:
            path = backup_database(ctx.config.db_path, ctx.config.backup_dir)
            flash(f"Backup created: {path.name}")

        guarded(action)
    st.caption("To restore a backup, close the app and run scripts\\restore_backup.bat (see README).")
    st.markdown("---")
    st.markdown(f"**Delete ALL data** (machines, maintenance, everything). A backup is made first. Type `{RESET_ALL_PHRASE}` to confirm.")
    phrase = st.text_input("Confirmation", key="db_reset_phrase")
    if st.button("Delete all data"):
        def reset() -> None:
            from src.models.schemas import ValidationFailed, ValidationIssue

            if phrase != RESET_ALL_PHRASE:
                raise ValidationFailed([ValidationIssue("CONFIRMATION_REQUIRED", "confirmation", f"Type {RESET_ALL_PHRASE!r} exactly.")])
            backup = ctx.db.reset_all(ctx.config.backup_dir)
            flash(f"All data deleted. Backup saved as {backup.name if backup else 'n/a'}.")

        guarded(reset)
