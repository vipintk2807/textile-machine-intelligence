"""Historical runtime, state-change review and CSV export."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st

from src.models.schemas import ExportError
from src.services.machine_service import MachineService
from src.services.report_service import ReportService
from src.ui import charts
from src.ui.components import AppContext, empty_state, header, period_selector, show_error, show_table, window_caption
from src.utils.time_utils import format_duration


def render(ctx: AppContext) -> None:
    header("History", "Runtime, downtime and state changes over a selected period.")
    with ctx.db.session_scope() as session:
        machines = MachineService(session).list()
        if not machines:
            empty_state("No machines registered, so there is no history to show.")
            return
        ids = [m.machine_id for m in machines]
        selected = st.multiselect("Machines", ids, default=ids[:5], key="hist_machines")
        window = period_selector(ctx, "hist", "Last 6 hours")
        only_transitions = st.checkbox("Show state changes only (hide repeated identical observations)", True, key="hist_trans")
        if window is None or not selected:
            if not selected:
                empty_state("Select at least one machine.")
            return
        window_caption(ctx, window)
        report = ReportService(session, ctx.settings)

        st.subheader("Runtime summary")
        acc_rows = report.accountings(window, selected)
        st.plotly_chart(charts.timeline_figure([(m.machine_id, a) for m, a, _ in acc_rows], ctx.tz))
        summary = report.runtime_rows(window, selected)
        show_table(pd.DataFrame(
            [
                {
                    "Machine": r["machine_id"],
                    "Running": format_duration(timedelta(seconds=r["running_seconds"])),
                    "Stopped": format_duration(timedelta(seconds=r["stopped_seconds"])),
                    "Unknown (reported)": format_duration(timedelta(seconds=r["unknown_seconds"])),
                    "Monitoring gap": format_duration(timedelta(seconds=r["monitoring_gap_seconds"])),
                    "Total": format_duration(timedelta(seconds=r["total_seconds"])),
                    "Known-state runtime share": "n/a" if r["known_state_runtime_share_percent"] is None
                    else f"{r['known_state_runtime_share_percent']:.1f}%",
                    "Coverage": f"{r['monitoring_coverage_percent']:.1f}%",
                }
                for r in summary
            ]
        ))
        st.caption(
            f"A state is considered supported for {ctx.settings.freshness_seconds} s after each observation "
            "(freshness setting); longer silence is a monitoring gap, never downtime. "
            "Share = Running ÷ (Running + Stopped). Not OEE."
        )

        st.subheader("State events")
        page = report.state_events(window, selected, None, only_transitions, limit=500)
        if page.rows:
            show_table(pd.DataFrame(
                [{"Machine": r["machine_id"], "Event time": ctx.local(r["event_time_utc"]),
                  "From": r["previous_state"] or "—", "State": r["state"], "Quality": r["quality_status"],
                  "Data": "Simulated" if r["source_type"] == "SIMULATED" else "Real source"} for r in page.rows]
            ))
            if page.truncated:
                st.info(f"Showing the newest {len(page.rows)} of {page.total:,} events. CSV exports contain all of them.")
        else:
            empty_state("No state events in this period.")

        st.subheader("Export CSV")
        st.caption("Exports use the filters above, UTC timestamps, and include the reporting period and data provenance.")
        for kind, label in (("runtime_summary", "Runtime summary"), ("state_history", "State events")):
            try:
                data = report.export_csv(kind, window, selected, transitions_only=only_transitions)
            except ExportError as exc:
                show_error(exc)
                continue
            st.download_button(f"Download {label} (CSV)", data.encode("utf-8-sig"),
                               file_name=f"{kind}.csv", mime="text/csv", key=f"dl_{kind}")
