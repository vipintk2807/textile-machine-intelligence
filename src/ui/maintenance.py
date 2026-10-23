"""User-entered maintenance records. These are entries typed by people, not system findings."""

from __future__ import annotations

from datetime import datetime, time

import pandas as pd
import streamlit as st

from src.models.schemas import MAINTENANCE_TYPES, ExportError
from src.services.anomaly_service import AnomalyService
from src.services.machine_service import MachineService
from src.services.maintenance_service import MaintenanceService
from src.services.report_service import ReportService
from src.ui.components import (
    AppContext, empty_state, flash, guarded, header, period_selector, show_error, show_table, window_caption,
)
from src.utils.time_utils import get_zone, local_naive_to_utc


def _to_utc(ctx: AppContext, d, t: time):
    return local_naive_to_utc(datetime.combine(d, t), ctx.tz)


def render(ctx: AppContext) -> None:
    header("Maintenance", "Maintenance records entered by users. They are not verified by the system.")
    with ctx.db.session_scope() as session:
        machines = [(m.machine_id, m.name) for m in MachineService(session).list()]
    if not machines:
        empty_state("Register a machine before recording maintenance.")
        return
    labels = {f"{mid} — {name}": mid for mid, name in machines}
    tab_list, tab_new = st.tabs(["Records", "Add record"])

    with tab_list:
        with ctx.db.session_scope() as session:
            f1, f2 = st.columns(2)
            machine = f1.selectbox("Machine", ["All", *labels], key="mt_machine")
            window = period_selector(ctx, "mt", "Last 7 days")
            if window is None:
                return
            window_caption(ctx, window)
            machine_id = None if machine == "All" else labels[machine]
            service = MaintenanceService(session)
            records = [r for r in service.list(machine_id=machine_id, window=window, limit=300)]
            if not records:
                empty_state("No maintenance records in this period.")
            else:
                show_table(pd.DataFrame(
                    [{"ID": r.id, "Machine": r.machine_id, "Type": r.event_type, "Started": ctx.local(r.start_time),
                      "Completed": ctx.local(r.end_time), "Description": r.description,
                      "User-entered finding": r.finding or "—", "Responsible": r.responsible or "—"} for r in records]
                ))
                pick = st.selectbox("Edit record", [r.id for r in records], key="mt_pick")
                _edit_form(ctx, next(r for r in records if r.id == pick))
            try:
                data = ReportService(session, ctx.settings).export_csv(
                    "maintenance", window, None if machine_id is None else [machine_id])
                st.download_button("Download maintenance (CSV)", data.encode("utf-8-sig"),
                                   file_name="maintenance.csv", mime="text/csv", key="dl_maint")
            except ExportError as exc:
                show_error(exc)

    with tab_new:
        _new_form(ctx, labels)


def _anomaly_options(ctx: AppContext, machine_id: str) -> dict[str, int | None]:
    with ctx.db.session_scope() as session:
        items = AnomalyService(session).list(machine_id=machine_id, limit=50)
        options: dict[str, int | None] = {"(none)": None}
        for a in items:
            options[f"#{a.id} {a.rule_code} ({a.status.title()})"] = a.id
        return options


def _new_form(ctx: AppContext, labels: dict[str, str]) -> None:
    machine_label = st.selectbox("Machine *", list(labels), key="mt_new_machine")
    machine_id = labels[machine_label]
    options = _anomaly_options(ctx, machine_id)
    local_now = ctx.now.astimezone(get_zone(ctx.tz))
    with st.form("maintenance_new"):
        event_type = st.selectbox("Event type *", MAINTENANCE_TYPES)
        c1, c2, c3, c4 = st.columns(4)
        d1 = c1.date_input("Start date *", local_now.date())
        t1 = c2.time_input("Start time *", time(local_now.hour, local_now.minute))
        has_end = st.checkbox("Completed (enter completion time)", True)
        d2 = c3.date_input("Completion date", local_now.date())
        t2 = c4.time_input("Completion time", time(local_now.hour, local_now.minute))
        description = st.text_area("Description *")
        finding = st.text_area("Finding or cause (user-entered)")
        responsible = st.text_input("Responsible person or team")
        notes = st.text_area("Notes")
        link = st.selectbox("Related anomaly (optional)", list(options))
        submitted = st.form_submit_button("Save maintenance record")
    st.caption(f"Times are entered in {ctx.tz}. Maintenance records are not linked to anomalies automatically.")
    if submitted:
        def action() -> None:
            start = _to_utc(ctx, d1, t1)
            end = _to_utc(ctx, d2, t2) if has_end else None
            with ctx.db.session_scope() as session:
                MaintenanceService(session).create(
                    machine_id=machine_id, event_type=event_type, start_time=start, end_time=end,
                    description=description, finding=finding, responsible=responsible, notes=notes,
                    related_anomaly_id=options[link],
                )
            flash("Maintenance record saved.")

        guarded(action)


def _edit_form(ctx: AppContext, record) -> None:
    zone = get_zone(ctx.tz)
    start = record.start_time.astimezone(zone)
    end = record.end_time.astimezone(zone) if record.end_time else None
    options = _anomaly_options(ctx, record.machine_id)
    current = next((k for k, v in options.items() if v == record.related_anomaly_id), "(none)")
    with st.form(f"maint_edit_{record.id}"):
        event_type = st.selectbox("Event type", MAINTENANCE_TYPES,
                                  index=MAINTENANCE_TYPES.index(record.event_type) if record.event_type in MAINTENANCE_TYPES else 0)
        c1, c2, c3, c4 = st.columns(4)
        d1 = c1.date_input("Start date", start.date())
        t1 = c2.time_input("Start time", start.time().replace(second=0, microsecond=0))
        has_end = st.checkbox("Completed", end is not None)
        d2 = c3.date_input("Completion date", (end or start).date())
        t2 = c4.time_input("Completion time", (end or start).time().replace(second=0, microsecond=0))
        description = st.text_area("Description", record.description)
        finding = st.text_area("Finding or cause (user-entered)", record.finding or "")
        responsible = st.text_input("Responsible", record.responsible or "")
        notes = st.text_area("Notes", record.notes or "")
        link = st.selectbox("Related anomaly", list(options), index=list(options).index(current))
        saved = st.form_submit_button("Save changes")
    if saved:
        def action() -> None:
            with ctx.db.session_scope() as session:
                MaintenanceService(session).update(
                    record.id, event_type=event_type, start_time=_to_utc(ctx, d1, t1),
                    end_time=_to_utc(ctx, d2, t2) if has_end else None, description=description,
                    finding=finding, responsible=responsible, notes=notes, related_anomaly_id=options[link],
                )
            flash("Maintenance record updated.")

        guarded(action)
