"""Dashboard: fleet status, period summary, recent transitions and anomalies."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.models.schemas import SourceType
from src.services.anomaly_service import AnomalyService
from src.services.overview_service import machine_overviews, status_counts
from src.services.report_service import ReportService
from src.services.state_service import StateService
from src.ui import charts
from src.ui.components import (
    AppContext, empty_state, goto, header, period_selector, show_table, simulated_banner, window_caption,
)
from src.ui.theme import severity_text, status_text
from src.utils.time_utils import format_duration
from datetime import timedelta


def render(ctx: AppContext) -> None:
    header("Dashboard", "Current machine status, derived from the latest valid observations.")
    with ctx.db.session_scope() as session:
        overviews = machine_overviews(session, ctx.settings, ctx.now)
        if not overviews:
            empty_state(
                "No machines are being monitored yet. Open **Settings → Simulation** to create "
                "labelled synthetic demo machines, or register a machine under **Machines**."
            )
            return
        simulated_banner(any(o.source_type == SourceType.SIMULATED.value for o in overviews))
        counts = status_counts(overviews)
        anomalies = AnomalyService(session)

        cols = st.columns(6)
        cols[0].metric("Active machines", counts["active"])
        cols[1].metric("▶ Running", counts["RUNNING"])
        cols[2].metric("■ Stopped", counts["STOPPED"])
        cols[3].metric("? Unknown", counts["UNKNOWN"], help="Includes machines whose latest observation is stale.")
        cols[4].metric("⛔ Disconnected", counts["DISCONNECTED"])
        cols[5].metric("⚠ Active anomalies", anomalies.active_count())
        if counts["stale"] or counts["source_unavailable"]:
            st.warning(
                f"{counts['stale']} machine(s) have stale observations and {counts['DISCONNECTED']} are "
                "disconnected. Stale or disconnected machines are NOT counted as stopped."
            )

        st.subheader("Machine status")
        table = pd.DataFrame(
            [
                {
                    "Machine": o.machine.machine_id,
                    "Name": o.machine.name,
                    "Status": status_text(o.view.status.value),
                    "Last known state": o.view.last_known_state.value.title() if o.view.last_known_state else "—",
                    "In state since": ctx.local(o.view.state_since),
                    "Last evidence": ctx.local(o.view.last_evidence_time),
                    "Stale": "Yes" if o.view.is_stale else "No",
                    "Active anomalies": o.active_anomalies,
                }
                for o in overviews
            ]
        )
        show_table(table)
        st.caption("Status reflects event time, not ingestion time. 'Unknown' with 'Stale = Yes' means no recent observation — not that the machine stopped.")
        pick = st.selectbox("Open machine details", [o.machine.machine_id for o in overviews], key="dash_pick")
        st.button("Open details", on_click=goto, args=("Machines", pick), key="dash_open")

        st.subheader("Runtime summary")
        window = period_selector(ctx, "dash", "Last 6 hours")
        if window is not None:
            window_caption(ctx, window)
            report = ReportService(session, ctx.settings)
            rows = report.runtime_rows(window, [o.machine.machine_id for o in overviews])
            if rows:
                items = [(m.machine_id, acc) for m, acc, _ in report.accountings(window, [o.machine.machine_id for o in overviews])]
                st.plotly_chart(charts.timeline_figure(items, ctx.tz))
                show_table(pd.DataFrame(
                    [
                        {
                            "Machine": r["machine_id"],
                            "Running": format_duration(timedelta(seconds=r["running_seconds"])),
                            "Stopped": format_duration(timedelta(seconds=r["stopped_seconds"])),
                            "Unknown (reported)": format_duration(timedelta(seconds=r["unknown_seconds"])),
                            "Monitoring gap": format_duration(timedelta(seconds=r["monitoring_gap_seconds"])),
                            "Known-state runtime share": "n/a"
                            if r["known_state_runtime_share_percent"] is None
                            else f"{r['known_state_runtime_share_percent']:.1f}%",
                        }
                        for r in rows
                    ]
                ))
                st.caption(
                    "Total = Running + Stopped + Unknown + Gap. Share = Running ÷ (Running + Stopped); "
                    "unknown and gap time are excluded. This is not OEE or factory availability."
                )

        left, right = st.columns(2)
        with left:
            st.subheader("Recent state changes")
            events = StateService(session).recent_transitions(10)
            if events:
                show_table(pd.DataFrame(
                    [{"Machine": e.machine_id, "Event time": ctx.local(e.event_time),
                      "From": e.previous_state or "—", "To": e.state} for e in events]
                ))
            else:
                empty_state("No state changes recorded yet.")
        with right:
            st.subheader("Recent anomalies")
            recent = anomalies.list(statuses=["OPEN", "ACKNOWLEDGED"], limit=8)
            if recent:
                show_table(pd.DataFrame(
                    [{"Machine": a.machine_id, "Severity": severity_text(a.severity), "Rule": a.rule_code,
                      "Detected": ctx.local(a.detected_at), "Status": a.status.title()} for a in recent]
                ))
            else:
                empty_state("No active anomalies.")
