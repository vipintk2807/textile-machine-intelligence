"""Application configuration, loaded from defaults and optional environment variables.

Environment variables (all optional; set in CMD with ``set NAME=value``):

* ``TMI_DATA_DIR``        folder for the database, backups and logs (default: ./data)
* ``TMI_DB_PATH``         full path of the SQLite file (default: <data dir>/textile_machine_intelligence.db)
* ``TMI_LOG_LEVEL``       DEBUG, INFO, WARNING or ERROR (default: INFO)
* ``TMI_TIMEZONE``        default reporting time zone, IANA name (default: Asia/Kolkata)
* ``TMI_FRESHNESS_SECONDS`` default freshness threshold, 10-86400 (default: 300, illustrative)

No secrets are required or read. Invalid values raise :class:`ConfigError` with a
message naming the offending variable.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from src.models.schemas import ConfigError
from src.utils.time_utils import get_zone

APP_NAME = "Textile Machine Intelligence"
APP_VERSION = "0.1.0"
PROJECT_ROOT = Path(__file__).resolve().parent
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_FRESHNESS_SECONDS = 300  # illustrative software default, not an industry standard


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    db_path: Path
    backup_dir: Path
    log_dir: Path
    log_level: str
    default_timezone: str
    default_freshness_seconds: int


def load_config(env: Mapping[str, str] | None = None) -> AppConfig:
    env = os.environ if env is None else env

    data_dir = Path(env.get("TMI_DATA_DIR") or PROJECT_ROOT / "data").expanduser()
    if not data_dir.is_absolute():
        data_dir = (PROJECT_ROOT / data_dir).resolve()

    db_raw = env.get("TMI_DB_PATH")
    db_path = Path(db_raw).expanduser() if db_raw else data_dir / "textile_machine_intelligence.db"
    if db_path.suffix.lower() not in (".db", ".sqlite", ".sqlite3"):
        raise ConfigError("TMI_DB_PATH must end with .db, .sqlite or .sqlite3.")
    if db_path.exists() and db_path.is_dir():
        raise ConfigError("TMI_DB_PATH points to a folder, not a file.")

    level = (env.get("TMI_LOG_LEVEL") or "INFO").upper()
    if level not in VALID_LOG_LEVELS:
        raise ConfigError(f"TMI_LOG_LEVEL must be one of {', '.join(VALID_LOG_LEVELS)}.")

    tz = env.get("TMI_TIMEZONE") or DEFAULT_TIMEZONE
    try:
        get_zone(tz)
    except ValueError as exc:
        raise ConfigError(f"TMI_TIMEZONE is invalid: {exc}") from exc

    raw_fresh = env.get("TMI_FRESHNESS_SECONDS")
    try:
        fresh = int(raw_fresh) if raw_fresh else DEFAULT_FRESHNESS_SECONDS
    except ValueError as exc:
        raise ConfigError("TMI_FRESHNESS_SECONDS must be a whole number.") from exc
    if not 10 <= fresh <= 86400:
        raise ConfigError("TMI_FRESHNESS_SECONDS must be between 10 and 86400.")

    return AppConfig(
        data_dir=data_dir,
        db_path=db_path,
        backup_dir=data_dir / "backups",
        log_dir=data_dir / "logs",
        log_level=level,
        default_timezone=tz,
        default_freshness_seconds=fresh,
    )
