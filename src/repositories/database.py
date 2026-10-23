"""SQLite engine, sessions, schema versioning, backup/restore and reset.

Schema versioning
-----------------
The schema version is stored in SQLite's ``PRAGMA user_version``.

* Empty database            -> tables are created and the version is set.
* Same version              -> tables are checked; existing data is never touched.
* Older version             -> an automatic backup is written, then the registered
                               migrations ``MIGRATIONS[v]`` (v -> v+1) run in order.
* Newer version / unknown   -> startup is refused with a clear message (nothing is modified).

There are no migrations yet because version 1 is the first release.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import Connection, create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from src.models.orm import Base
from src.models.schemas import AppError, PersistenceError
from src.utils.time_utils import utc_now

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MIGRATIONS: dict[int, Callable[[Connection], None]] = {}


def _set_sqlite_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")  # enforced on EVERY connection
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


class Database:
    """Owns the SQLAlchemy engine and hands out transactional sessions."""

    def __init__(self, db_path: Path | str, echo: bool = False):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
            f"sqlite:///{self.path}",
            echo=echo,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        event.listen(self.engine, "connect", _set_sqlite_pragmas)
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        """Commit on success, roll back on any error.

        Database errors surface as :class:`PersistenceError`; nothing from a failed
        unit of work is saved and callers must not report success.
        """
        session = self._sessions()
        try:
            yield session
            session.commit()
        except AppError:
            session.rollback()
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            logger.exception("Database operation failed")
            raise PersistenceError(
                "The database operation failed and nothing was saved. "
                "See the application log for technical details."
            ) from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #

    def schema_version(self) -> int:
        try:
            with self.engine.connect() as conn:
                return int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)
        except SQLAlchemyError as exc:
            raise PersistenceError("The database could not be opened.") from exc

    def initialize(
        self,
        migrations: dict[int, Callable[[Connection], None]] | None = None,
        backup_dir: Path | None = None,
    ) -> None:
        """Create or validate the schema. Idempotent; never deletes user data."""
        migrations = MIGRATIONS if migrations is None else migrations
        try:
            with self.engine.connect() as conn:
                version = int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)
                tables = set(inspect(conn).get_table_names())
        except SQLAlchemyError as exc:
            raise PersistenceError(
                "The database file could not be opened. Check that the folder is writable "
                "and the file is a valid SQLite database."
            ) from exc

        expected = set(Base.metadata.tables)
        if version == 0 and not tables:
            self._create_fresh()
            return
        if version == 0:
            raise PersistenceError(
                "The database file contains tables but no schema version, so it was not "
                "created by this application. It has not been modified."
            )
        if version > SCHEMA_VERSION:
            raise PersistenceError(
                f"The database uses schema version {version}, newer than this application "
                f"supports ({SCHEMA_VERSION}). Update the application; the database was not modified."
            )
        if version < SCHEMA_VERSION:
            self._migrate(version, migrations, backup_dir)
            return
        missing = expected - tables
        if missing:
            raise PersistenceError(
                "The database is missing expected tables: " + ", ".join(sorted(missing)) + "."
            )

    def _create_fresh(self) -> None:
        try:
            Base.metadata.create_all(self.engine)
            with self.engine.begin() as conn:
                conn.exec_driver_sql(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        except SQLAlchemyError as exc:
            raise PersistenceError("The database schema could not be created.") from exc

    def _migrate(
        self,
        version: int,
        migrations: dict[int, Callable[[Connection], None]],
        backup_dir: Path | None,
    ) -> None:
        missing = [v for v in range(version, SCHEMA_VERSION) if v not in migrations]
        if missing:
            raise PersistenceError(
                f"No migration is available from schema version {missing[0]}. The database was not modified."
            )
        if backup_dir is not None:
            backup_database(self.path, backup_dir, label=f"pre-migration-v{version}")
        try:
            for v in range(version, SCHEMA_VERSION):
                with self.engine.begin() as conn:
                    migrations[v](conn)
                    conn.exec_driver_sql(f"PRAGMA user_version = {int(v + 1)}")
        except SQLAlchemyError as exc:
            raise PersistenceError("Schema migration failed; restore from the automatic backup.") from exc

    def reset_all(self, backup_dir: Path) -> Path | None:
        """DESTRUCTIVE: back up, then drop and recreate every table.

        Returns the backup path. Callers must obtain explicit user confirmation first.
        """
        backup_path = backup_database(self.path, backup_dir, label="pre-reset") if self.path.exists() else None
        try:
            Base.metadata.drop_all(self.engine)
            self._create_fresh()
        except SQLAlchemyError as exc:
            raise PersistenceError("The database could not be reset.") from exc
        logger.warning("Database reset performed; backup at %s", backup_path)
        return backup_path


# --------------------------------------------------------------------------- #
# Backup / restore (uses SQLite's online backup API, safe while the app runs)
# --------------------------------------------------------------------------- #


def backup_database(db_path: Path | str, backup_dir: Path | str, label: str = "backup", now: datetime | None = None) -> Path:
    db_path, backup_dir = Path(db_path), Path(backup_dir)
    if not db_path.exists():
        raise PersistenceError("There is no database file to back up.")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or utc_now()).strftime("%Y%m%dT%H%M%S%fZ")
    dest = backup_dir / f"{db_path.stem}-{label}-{stamp}.db"
    src = dst = None
    try:
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(dest))
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")  # make the backup a self-contained single file
    except sqlite3.Error as exc:
        if dest.exists():
            dest.unlink(missing_ok=True)
        raise PersistenceError("The backup could not be created.") from exc
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()
    return dest


def verify_backup(path: Path | str) -> int:
    """Check a backup file is a healthy database of a supported version; return its version."""
    path = Path(path)
    if not path.is_file():
        raise PersistenceError("The backup file does not exist.")
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise PersistenceError("The file is not a readable SQLite database.") from exc
    if not check or check[0] != "ok":
        raise PersistenceError("The backup failed SQLite's integrity check.")
    if version < 1 or version > SCHEMA_VERSION:
        raise PersistenceError(f"The backup has unsupported schema version {version}.")
    if version == SCHEMA_VERSION and not set(Base.metadata.tables) <= tables:
        raise PersistenceError("The backup is missing expected tables.")
    return version


def restore_database(backup_path: Path | str, db_path: Path | str, safety_dir: Path | str) -> Path:
    """Replace ``db_path`` with a verified backup. The application must be stopped.

    The current database (if any) is first copied to ``safety_dir`` so the restore
    itself can be undone. Returns the path of that safety copy.
    """
    backup_path, db_path, safety_dir = Path(backup_path), Path(db_path), Path(safety_dir)
    verify_backup(backup_path)
    safety = None
    if db_path.exists():
        try:  # refuse if another process (e.g. the running app) holds the database
            probe = sqlite3.connect(str(db_path), timeout=1)
            try:
                probe.execute("BEGIN EXCLUSIVE")
                probe.rollback()
            finally:
                probe.close()
        except sqlite3.OperationalError as exc:
            raise PersistenceError("The database is in use. Stop the application, then retry.") from exc
        safety = backup_database(db_path, safety_dir, label="pre-restore")
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    tmp = db_path.with_suffix(db_path.suffix + ".restoring")
    shutil.copyfile(backup_path, tmp)
    os.replace(tmp, db_path)
    return safety if safety is not None else db_path
