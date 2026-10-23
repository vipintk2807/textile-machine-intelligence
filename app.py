"""Textile Machine Intelligence - Streamlit entry point.

Run from the project folder:  streamlit run app.py
"""

from __future__ import annotations

import logging
import logging.handlers

import streamlit as st

from config import APP_NAME, APP_VERSION, AppConfig, load_config
from src.models.schemas import AppError
from src.repositories.database import Database
from src.services.settings_service import SettingsService, defaults_from_config
from src.ui import (
    anomalies, dashboard, history, machines, maintenance, sensors, settings,
)
from src.ui.components import PAGE_ICONS, PAGES, AppContext, show_error, show_flash
from src.ui.theme import inject_css
from src.utils.time_utils import utc_now

st.set_page_config(page_title=APP_NAME, page_icon="🧵", layout="wide", initial_sidebar_state="expanded")

PAGE_MODULES = {
    "Dashboard": dashboard, "Machines": machines, "History": history, "Sensors": sensors,
    "Anomalies": anomalies, "Maintenance": maintenance, "Settings": settings,
}


def _setup_logging(config: AppConfig) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_tmi", False) for h in root.handlers):
        return
    config.log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        config.log_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler._tmi = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(config.log_level)


@st.cache_resource(show_spinner="Opening database...")
def _open_database(db_path: str, backup_dir: str) -> Database:
    from pathlib import Path

    db = Database(db_path)
    db.initialize(backup_dir=Path(backup_dir))
    return db


def _bootstrap() -> tuple[AppConfig, Database]:
    try:
        config = load_config()
        _setup_logging(config)
        return config, _open_database(str(config.db_path), str(config.backup_dir))
    except AppError as exc:
        st.error(f"The application could not start: {exc}")
        st.stop()
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("Startup failure")
        st.error("The application could not start. Check the log file in the data folder for details.")
        st.stop()
    raise RuntimeError("unreachable")  # pragma: no cover


def main() -> None:
    inject_css()
    config, db = _bootstrap()
    with db.session_scope() as session:
        settings_snapshot = SettingsService(session, defaults_from_config(config)).load()
    ctx = AppContext(db=db, config=config, settings=settings_snapshot, now=utc_now())

    st.session_state.setdefault("nav_page", PAGES[0])
    with st.sidebar:
        st.markdown(f"### 🧵 {APP_NAME}")
        st.caption(f"Version {APP_VERSION} · read-only monitoring")
        st.radio("Navigate", PAGES, key="nav_page", format_func=lambda p: f"{PAGE_ICONS[p]}  {p}")
        st.divider()
        st.caption(f"Time zone: {ctx.tz}")
        st.caption(f"Now: {ctx.local(ctx.now)}")
        st.button("↻ Refresh view")

    show_flash()
    page = st.session_state["nav_page"]
    try:
        PAGE_MODULES[page].render(ctx)
    except Exception as exc:  # noqa: BLE001 - shown to the user, unexpected ones logged
        show_error(exc)


main()
