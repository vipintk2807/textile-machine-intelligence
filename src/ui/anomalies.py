"""Anomaly review and workflow (acknowledge, note, resolve)."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from src.models.schemas import AnomalyCategory, ExportError, Severity, SourceType
from src.services.anomaly_service import AnomalyService
from src.services.machine_service import MachineService
from src.services.report_service import ReportService
from src.ui.components import (
    AppContext, empty_state, flash, guarded, header, period_selector, show_error, show_table, simulated_banner, window_caption,
)
from src.ui.theme import severity_badge, severity_text

STATUS_FILTERS = {"Active (open + acknowledged)": ["OPEN", "ACKNOWLEDGED"], "Open": ["OPEN"],
                  "Acknowledged": ["ACKNOWLEDGED"], "Resolved": ["RESOLVED"], "All": None}


def render(ctx: AppContext) -> None:
    header("Anomalies", "Rule-triggered conditions. An anomaly is a configured rule firing — not a diagnosed fault.")
    with ctx.db.session_scope() as session:
        service = AnomalyService(session)
        machines = [m.machine_id for m in MachineService(session).list()]
        f1, f2, f3, f4 = st.columns(4)
        status = f1.selectbox("Status", list(STATUS_FILTERS), key="an_status")
        machine = f2.selectbox("Machine", ["All", *machines], key="an_machine")
        severity = f3.selectbox("Severity", ["All", *[s.value for s in Severity]], key="an_sev")
        category = f4.selectbox("Category", ["All", *[c.value for c in AnomalyCategory]], key="an_cat")
        items = service.list(
            machine_id=None if machine == "All" else machine,
            statuses=STATUS_FILTERS[status],
            severity=None if severity == "All" else severity,
            category=None if category == "All" else category,
            limit=300,
        )
        if not items:
            empty_state("No anomalies match the filters.")
            _export(ctx, session)
            return
        simulated_banner(any(a.source_type == SourceType.SIMULATED.value for a in items))
        show_table(pd.DataFrame(
            [
                {
                    "ID": a.id, "Machine": a.machine_id, "Severity": severity_text(a.severity),
                    "Category": a.category.replace("_", " ").title(), "Rule": a.rule_code,
                    "Status": a.status.title(), "Detected": ctx.local(a.detected_at),
                    "Last observed": ctx.local(a.last_observed_at), "Count": a.occurrence_count,
                }
                for a in items
            ]
        ))
        chosen = st.selectbox("Review anomaly", [a.id for a in items], key="an_pick",
                              format_func=lambda i: next(f"#{a.id} — {a.machine_id} — {a.rule_code}" for a in items if a.id == i))
        a = next(x for x in items if x.id == chosen)
        st.markdown(f"#### Anomaly #{a.id} &nbsp; {severity_badge(a.severity)}", unsafe_allow_html=True)
        st.write(a.description)
        d1, d2, d3 = st.columns(3)
        d1.metric("Status", a.status.title())
        d2.metric("Observed value", "—" if a.observed_value is None else f"{a.observed_value:g}")
        d3.metric("Configured threshold", "—" if a.threshold_value is None else f"{a.threshold_value:g}")
        st.caption(
            f"Detected {ctx.local(a.detected_at)} · last observed {ctx.local(a.last_observed_at)} · "
            f"occurred {a.occurrence_count}× · provenance: {'SIMULATED' if a.source_type == SourceType.SIMULATED.value else 'real source'}"
        )
        with st.expander("Evidence (raw)"):
            try:
                st.json(json.loads(a.evidence_json))
            except ValueError:
                st.write(a.evidence_json)
        if a.investigation_notes:
            st.text(a.investigation_notes)
        if a.resolution_note:
            st.success(f"Resolved {ctx.local(a.resolved_at)}: {a.resolution_note}")

        if a.status != "RESOLVED":
            note = st.text_area("Note / resolution note", key=f"an_note_{a.id}")
            b1, b2, b3 = st.columns(3)
            if a.status == "OPEN" and b1.button("Acknowledge (reviewed, not fixed)", key=f"ack_{a.id}"):
                def ack() -> None:
                    with ctx.db.session_scope() as s:
                        AnomalyService(s).acknowledge(a.id, note)
                    flash("Acknowledged. The anomaly stays active until it is resolved.")
                guarded(ack)
            if b2.button("Add note", key=f"addnote_{a.id}"):
                def add() -> None:
                    with ctx.db.session_scope() as s:
                        AnomalyService(s).add_note(a.id, note)
                    flash("Note added.")
                guarded(add)
            if b3.button("Resolve (note required)", key=f"res_{a.id}"):
                def resolve() -> None:
                    with ctx.db.session_scope() as s:
                        AnomalyService(s).resolve(a.id, note)
                    flash("Resolved.")
                guarded(resolve)
        _export(ctx, session)


def _export(ctx: AppContext, session) -> None:
    st.subheader("Export")
    window = period_selector(ctx, "an_exp", "Last 7 days")
    if window is None:
        return
    window_caption(ctx, window)
    try:
        data = ReportService(session, ctx.settings).export_csv("anomalies", window)
        st.download_button("Download anomalies (CSV)", data.encode("utf-8-sig"),
                           file_name="anomalies.csv", mime="text/csv", key="dl_anomalies")
    except ExportError as exc:
        show_error(exc)
