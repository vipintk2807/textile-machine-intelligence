"""Visual tokens and small HTML helpers. Status is never conveyed by colour alone:
every status badge carries an icon and a text label."""

from __future__ import annotations

from html import escape

import streamlit as st

COLORS = {
    "bg": "#F6F7F9",
    "surface": "#FFFFFF",
    "text": "#1F2933",
    "muted": "#52606D",
    "border": "#D9DEE5",
    "primary": "#0F766E",
    "running": "#2E7D32",
    "stopped": "#52667A",
    "unknown": "#9A6700",
    "disconnected": "#8A3B12",
    "gap": "#C5CCD6",
    "info": "#1D5FA8",
    "warning": "#9A6700",
    "critical": "#B42318",
}

STATUS_STYLE = {
    "RUNNING": ("▶", "Running", COLORS["running"]),
    "STOPPED": ("■", "Stopped", COLORS["stopped"]),
    "UNKNOWN": ("?", "Unknown", COLORS["unknown"]),
    "DISCONNECTED": ("⛔", "Disconnected", COLORS["disconnected"]),
}
SEVERITY_STYLE = {
    "INFO": ("ℹ", COLORS["info"]),
    "WARNING": ("⚠", COLORS["warning"]),
    "CRITICAL": ("‼", COLORS["critical"]),
}

_CSS = f"""
<style>
.block-container {{ padding-top: 1.6rem; max-width: 1400px; }}
.tmi-badge {{ display:inline-block; padding:2px 10px; border-radius:12px; font-weight:600;
  font-size:0.85rem; border:1px solid currentColor; background:#fff; white-space:nowrap; }}
.tmi-sim {{ background:#FFF4D6; border:1px solid #E5B93C; color:#5C4300; padding:6px 12px;
  border-radius:6px; font-size:0.9rem; margin-bottom:0.8rem; }}
.tmi-note {{ color:{COLORS['muted']}; font-size:0.88rem; }}
.tmi-card {{ background:{COLORS['surface']}; border:1px solid {COLORS['border']};
  border-radius:8px; padding:10px 14px; }}
</style>
"""


def inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def status_badge(status: str) -> str:
    icon, label, color = STATUS_STYLE.get(status, ("?", status.title(), COLORS["muted"]))
    return f'<span class="tmi-badge" style="color:{color}">{icon} {escape(label)}</span>'


def severity_badge(severity: str) -> str:
    icon, color = SEVERITY_STYLE.get(severity, ("•", COLORS["muted"]))
    return f'<span class="tmi-badge" style="color:{color}">{icon} {escape(severity.title())}</span>'


def status_text(status: str) -> str:
    """Plain-text status for tables (icon + label)."""
    icon, label, _ = STATUS_STYLE.get(status, ("?", status.title(), ""))
    return f"{icon} {label}"


def severity_text(severity: str) -> str:
    icon, _ = SEVERITY_STYLE.get(severity, ("•", ""))
    return f"{icon} {severity.title()}"
