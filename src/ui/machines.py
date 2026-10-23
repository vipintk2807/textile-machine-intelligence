"""Machine registry, registration form and machine detail view."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import select

from src.models.orm import DataSource
from src.models.schemas import SourceType
from src.services.anomaly_service import AnomalyService
from src.services.machine_service import MachineService
from src.services.maintenance_service import MaintenanceService
from src.services.overview_service import machine_overviews
from src.services.report_service import ReportService
from src.services.sensor_service import SensorService
from src.ui import charts
from src.ui.components import (
    AppContext, empty_state, flash, guarded, header, period_selector, show_table, simulated_banner, window_caption,
)
from src.ui.theme import severity_text, status_badge, status_text
from src.utils.time_utils import format_duration


def render(ctx: AppContext) -> None:
    header("Machines", "Register machines, review their monitoring status and details.")
    tab_registry, tab_register, tab_detail = st.tabs(["Registry", "Register machine", "Machine details"])
    with tab_registry:
        _registry(ctx)
    with tab_register:
        _register(ctx)
    with tab_detail:
        _detail(ctx)


def _registry(ctx: AppContext) -> None:
    with ctx.db.session_scope() as session:
        service = MachineService(session)
        f1, f2, f3, f4 = st.columns(4)
        search = f1.text_input("Search ID or name", key="mach_search")
        mtype = f2.selectbox("Type", ["All", *service.distinct_values("machine_type")], key="mach_type")
        loc = f3.selectbox("Location", ["All", *service.distinct_values("location")], key="mach_loc")
        scope = f4.selectbox("Monitoring", ["Active", "Inactive", "All"], key="mach_scope")
        machines = service.list(
            search=search or None,
            machine_type=None if mtype == "All" else mtype,
            location=None if loc == "All" else loc,
            enabled={"Active": True, "Inactive": False, "All": None}[scope],
        )
        if not machines:
            empty_state("No machines match the current filters.")
            return
        overviews = {
            o.machine.machine_id: o
            for o in machine_overviews(session, ctx.settings, ctx.now, enabled_only=False,
                                       machine_ids=[m.machine_id for m in machines])
        }
        simulated_banner(any(o.source_type == SourceType.SIMULATED.value for o in overviews.values()))
        show_table(pd.DataFrame(
            [
                {
                    "Machine ID": m.machine_id,
                    "Name": m.name,
                    "Type": m.machine_type,
                    "Location": m.location or "—",
                    "Monitoring": "Active" if m.enabled else "Inactive",
                    "Status": status_text(overviews[m.machine_id].view.status.value) if m.enabled else "— (inactive)",
                    "Data": "Simulated" if overviews[m.machine_id].source_type == SourceType.SIMULATED.value else "Real source",
                }
                for m in machines
            ]
        ))


def _register(ctx: AppContext) -> None:
    with ctx.db.session_scope() as session:
        sources = list(session.scalars(select(DataSource).where(DataSource.enabled.is_(True))))
        # (id, label) pairs captured as plain values before the session closes
        options = {s.name: s.id for s in sources}
    if not options:
        empty_state("No monitoring source exists yet. Initialise the simulation under Settings first.")
        return
    with st.form("register_machine", clear_on_submit=False):
        machine_id = st.text_input("Machine ID *", help="Letters, digits, '_', '-' or '.'; unique and permanent.")
        name = st.text_input("Name *")
        machine_type = st.text_input("Type *", help="Free text; categories are not fixed (e.g. Knitting, Dyeing).")
        location = st.text_input("Location")
        description = st.text_area("Notes")
        source_name = st.selectbox("Monitoring source *", list(options))
        submitted = st.form_submit_button("Register machine")
    if submitted:
        def action() -> None:
            with ctx.db.session_scope() as session:
                MachineService(session).create(
                    machine_id=machine_id, name=name, machine_type=machine_type,
                    source_id=options[source_name], location=location, description=description,
                )
            flash(f"Machine '{machine_id.strip()}' registered. Data appears once the source reports for it.")

        guarded(action)


def _detail(ctx: AppContext) -> None:
    with ctx.db.session_scope() as session:
        machines = MachineService(session).list()
        if not machines:
            empty_state("No machines registered.")
            return
        ids = [m.machine_id for m in machines]
        machine_id = st.selectbox("Machine", ids, key="machine_detail_select")
        overview = machine_overviews(session, ctx.settings, ctx.now, enabled_only=False, machine_ids=[machine_id])[0]
        machine, view = overview.machine, overview.view
        simulated_banner(overview.source_type == SourceType.SIMULATED.value)

        st.markdown(f"### {machine.name}  &nbsp; {status_badge(view.status.value)}", unsafe_allow_html=True)
        if not machine.enabled:
            st.warning("Monitoring is inactive for this machine. History is retained.")
        st.caption(view.reason)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Last known state", view.last_known_state.value.title() if view.last_known_state else "—")
        c2.metric("In state since", ctx.local(view.state_since, "%m-%d %H:%M"))
        c3.metric("Last evidence", ctx.local(view.last_evidence_time, "%m-%d %H:%M"))
        c4.metric("Source health", view.source_health.value.title())
        st.write(f"**ID:** {machine.machine_id}  |  **Type:** {machine.machine_type}  |  **Location:** {machine.location or '—'}")
        if machine.description:
            st.write(machine.description)

        st.subheader("State timeline and runtime")
        window = period_selector(ctx, "mach", "Last 6 hours")
        if window is not None:
            window_caption(ctx, window)
            report = ReportService(session, ctx.settings)
            (_, acc, _), = report.accountings(window, [machine_id])
            st.plotly_chart(charts.timeline_figure([(machine_id, acc)], ctx.tz))
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Running", format_duration(acc.running))
            m2.metric("Stopped", format_duration(acc.stopped))
            m3.metric("Unknown (reported)", format_duration(acc.unknown))
            m4.metric("Monitoring gap", format_duration(acc.gap))
            share = acc.known_state_runtime_share
            m5.metric("Known-state runtime share", "n/a" if share is None else f"{share:.1f}%")
            if share is None:
                st.caption("No known running or stopped time in this period, so a share cannot be calculated.")

        st.subheader("Sensors")
        rows = SensorService(session).overview(ctx.now, ctx.settings.sensor_stale, machine_id)
        if rows:
            show_table(pd.DataFrame(
                [
                    {
                        "Sensor": r.definition.sensor_code,
                        "Type": r.definition.measurement_type,
                        "Latest value": "—" if r.latest is None else r.latest.value,
                        "Unit": r.definition.unit or "unknown",
                        "Reading time": ctx.local(r.latest.event_time if r.latest else None),
                        "Freshness": r.freshness.label.value.replace("_", " ").title(),
                    }
                    for r in rows
                ]
            ))
        else:
            empty_state("No sensors are defined for this machine.")

        st.subheader("Active anomalies and maintenance")
        a1, a2 = st.columns(2)
        with a1:
            anomalies = AnomalyService(session).list(machine_id=machine_id, statuses=["OPEN", "ACKNOWLEDGED"], limit=10)
            if anomalies:
                show_table(pd.DataFrame(
                    [{"Severity": severity_text(a.severity), "Rule": a.rule_code, "Detected": ctx.local(a.detected_at),
                      "Status": a.status.title()} for a in anomalies]
                ))
            else:
                empty_state("No active anomalies.")
        with a2:
            records = MaintenanceService(session).list(machine_id=machine_id, limit=10)
            if records:
                show_table(pd.DataFrame(
                    [{"Type": r.event_type, "Started": ctx.local(r.start_time), "User-entered note": r.description}
                     for r in records]
                ))
            else:
                empty_state("No maintenance records.")

    st.subheader("Edit machine")
    with st.form(f"edit_{machine_id}"):
        name = st.text_input("Name *", machine.name)
        machine_type = st.text_input("Type *", machine.machine_type)
        location = st.text_input("Location", machine.location or "")
        description = st.text_area("Notes", machine.description or "")
        saved = st.form_submit_button("Save changes")
    if saved:
        def save() -> None:
            with ctx.db.session_scope() as s:
                MachineService(s).update(machine_id, name=name, machine_type=machine_type,
                                         location=location, description=description)
            flash("Machine updated.")

        guarded(save)

    label = "Deactivate monitoring" if machine.enabled else "Reactivate monitoring"
    if st.button(label, key=f"toggle_{machine_id}"):
        def toggle() -> None:
            with ctx.db.session_scope() as s:
                MachineService(s).set_enabled(machine_id, not machine.enabled)
            flash("Monitoring status changed.")

        guarded(toggle)
        st.caption("Deactivating stops monitoring; no history is deleted.")
