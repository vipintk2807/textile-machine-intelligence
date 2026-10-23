"""Sensor monitoring: latest values, freshness, charts and quarantined records."""

from __future__ import annotations


import pandas as pd
import streamlit as st

from src.models.schemas import ExportError, SourceType
from src.services.machine_service import MachineService
from src.services.report_service import ReportService
from src.services.sensor_service import SensorService
from src.ui import charts
from src.ui.components import AppContext, empty_state, header, period_selector, show_error, show_table, simulated_banner, window_caption


def render(ctx: AppContext) -> None:
    header("Sensors", "Latest readings, data freshness and sensor history. Units are shown only when known.")
    with ctx.db.session_scope() as session:
        machines = MachineService(session).list()
        sensors = SensorService(session)
        if not machines:
            empty_state("No machines registered, so no sensors exist.")
            return
        ids = [m.machine_id for m in machines]
        machine_id = st.selectbox("Machine", ["All", *ids], key="sens_machine")
        rows = sensors.overview(ctx.now, ctx.settings.sensor_stale, None if machine_id == "All" else machine_id)
        if not rows:
            empty_state("No sensors are defined for the selection.")
            return
        simulated_banner(any(r.latest is not None and r.latest.source_type == SourceType.SIMULATED.value for r in rows))
        show_table(pd.DataFrame(
            [
                {
                    "Machine": r.definition.machine_id,
                    "Sensor": r.definition.sensor_code,
                    "Measurement": r.definition.measurement_type,
                    "Latest value": "—" if r.latest is None else r.latest.value,
                    "Unit": r.definition.unit or "unknown",
                    "Reading time": ctx.local(r.latest.event_time if r.latest else None),
                    "Freshness": {"CURRENT": "Current", "STALE": "⚠ Stale", "NO_DATA": "No data"}[r.freshness.label.value],
                    "Quality": r.latest.quality_status.title() if r.latest else "—",
                    "Active anomalies": r.active_anomalies,
                }
                for r in rows
            ]
        ))
        st.caption("Stale = no reading within the sensor's configured limit. A missing reading is shown as such — it is never treated as zero.")

        labels = {f"{r.definition.machine_id} / {r.definition.sensor_code}": r for r in rows}
        choice = st.selectbox("Sensor detail", list(labels), key="sens_choice")
        row = labels[choice]
        window = period_selector(ctx, "sens", "Last 6 hours")
        if window is None:
            return
        window_caption(ctx, window)
        readings = sensors.readings(sensor_id=row.definition.id, window=window, limit=5000, newest_first=False)
        if readings:
            st.plotly_chart(charts.sensor_figure(
                [(r.event_time, r.value) for r in readings],
                f"{choice} — {row.definition.measurement_type}", row.definition.unit, ctx.tz,
                gap_after=row.stale_after, lower=row.definition.lower_threshold, upper=row.definition.upper_threshold,
            ))
            st.caption("Lines break where data is missing for longer than the stale limit. Dashed lines are configured (illustrative) thresholds.")
            show_table(pd.DataFrame(
                [{"Event time": ctx.local(r.event_time), "Value": r.value, "Unit": r.unit or "unknown",
                  "Quality": r.quality_status.title()} for r in reversed(readings[-200:])]
            ))
        else:
            empty_state("No readings for this sensor in the selected period.")

        report = ReportService(session, ctx.settings)
        try:
            data = report.export_csv("sensor_readings", window, None if machine_id == "All" else [machine_id])
            st.download_button("Download sensor readings (CSV)", data.encode("utf-8-sig"),
                               file_name="sensor_readings.csv", mime="text/csv", key="dl_sensors")
        except ExportError as exc:
            show_error(exc)

        st.subheader("Rejected (quarantined) records")
        st.caption("Records that failed validation are kept here with their diagnosis and are never used in calculations.")
        page = report.rejected_records(window, None if machine_id == "All" else machine_id, limit=100)
        if page.rows:
            show_table(pd.DataFrame(
                [{"Received": ctx.local(r["received_at_utc"]), "Type": r["record_type"],
                  "Machine ref": r["machine_ref"] or "—", "Diagnosis": r["diagnosis"]} for r in page.rows]
            ))
            if page.truncated:
                st.info(f"Showing {len(page.rows)} of {page.total:,} rejected records.")
        else:
            empty_state("No rejected records in this period.")
