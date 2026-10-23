"""Plotly figures. Gaps stay visible: missing data is never joined by a line."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import plotly.graph_objects as go

from src.core.intervals import Accounting
from src.models.schemas import GapReason, IntervalCategory
from src.ui.theme import COLORS
from src.utils.time_utils import get_zone

SEGMENT_STYLE = {
    IntervalCategory.RUNNING: ("Running", COLORS["running"]),
    IntervalCategory.STOPPED: ("Stopped", COLORS["stopped"]),
    IntervalCategory.UNKNOWN: ("Unknown (reported)", COLORS["unknown"]),
    IntervalCategory.GAP: ("Monitoring gap", COLORS["gap"]),
}


def to_local_naive(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(get_zone(tz)).replace(tzinfo=None)


def timeline_figure(items: Sequence[tuple[str, Accounting]], tz: str) -> go.Figure:
    """One horizontal bar per machine; colour AND legend name encode the category."""
    fig = go.Figure()
    shown: set[str] = set()
    for machine_id, acc in items:
        for seg in acc.segments:
            name, color = SEGMENT_STYLE[seg.category]
            reason = ""
            if seg.category is IntervalCategory.GAP and seg.gap_reason:
                reason = f" ({seg.gap_reason.value.replace('_', ' ').lower()})"
                if seg.gap_reason is GapReason.SOURCE_UNAVAILABLE:
                    reason = " (source unavailable)"
            fig.add_trace(
                go.Bar(
                    y=[machine_id],
                    x=[(seg.end - seg.start) / timedelta(milliseconds=1)],
                    base=[to_local_naive(seg.start, tz)],
                    orientation="h",
                    marker={"color": color, "line": {"width": 0}},
                    name=name,
                    legendgroup=name,
                    showlegend=name not in shown,
                    customdata=[[f"{to_local_naive(seg.start, tz):%Y-%m-%d %H:%M} → {to_local_naive(seg.end, tz):%H:%M}"]],
                    hovertemplate=f"{machine_id}<br>{name}{reason}<br>%{{customdata[0]}}<extra></extra>",
                )
            )
            shown.add(name)
    fig.update_layout(
        barmode="overlay",
        height=120 + 46 * max(len(items), 1),
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        xaxis={"type": "date", "title": f"Time ({tz})"},
        yaxis={"autorange": "reversed"},
        legend={"orientation": "h", "y": -0.25},
    )
    return fig


def runtime_bar_figure(rows: Sequence[dict]) -> go.Figure:
    fig = go.Figure()
    for key, label, color in (
        ("running_seconds", "Running", COLORS["running"]),
        ("stopped_seconds", "Stopped", COLORS["stopped"]),
        ("unknown_seconds", "Unknown (reported)", COLORS["unknown"]),
        ("monitoring_gap_seconds", "Monitoring gap", COLORS["gap"]),
    ):
        fig.add_trace(
            go.Bar(name=label, x=[r["machine_id"] for r in rows], y=[r[key] / 60 for r in rows], marker_color=color)
        )
    fig.update_layout(
        barmode="stack", yaxis_title="Minutes", height=320,
        margin={"l": 10, "r": 10, "t": 10, "b": 10}, legend={"orientation": "h", "y": -0.25},
    )
    return fig


def sensor_figure(
    points: Sequence[tuple[datetime, float]],
    title: str,
    unit: str | None,
    tz: str,
    gap_after: timedelta,
    lower: float | None = None,
    upper: float | None = None,
) -> go.Figure:
    """Line chart in time order with a break wherever data is missing for > ``gap_after``."""
    xs: list[datetime | None] = []
    ys: list[float | None] = []
    previous: datetime | None = None
    for t, v in sorted(points, key=lambda p: p[0]):
        if previous is not None and t - previous > gap_after:
            xs.append(None)
            ys.append(None)
        xs.append(to_local_naive(t, tz))
        ys.append(v)
        previous = t
    fig = go.Figure(go.Scatter(x=xs, y=ys, mode="lines+markers", connectgaps=False, name=title,
                               line={"color": COLORS["primary"]}, marker={"size": 4}))
    for value, label in ((upper, "upper threshold (configured)"), (lower, "lower threshold (configured)")):
        if value is not None:
            fig.add_hline(y=value, line_dash="dash", line_color=COLORS["critical"], annotation_text=label)
    fig.update_layout(
        height=340, margin={"l": 10, "r": 10, "t": 30, "b": 10},
        yaxis_title=f"Value ({unit})" if unit else "Value (unit unknown)",
        xaxis_title=f"Event time ({tz})", title=title,
    )
    return fig
