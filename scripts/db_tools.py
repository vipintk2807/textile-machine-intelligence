"""Database maintenance CLI.

    python scripts/db_tools.py check
    python scripts/db_tools.py backup
    python scripts/db_tools.py restore <backup-file>     (stop the app first)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config  # noqa: E402
from src.models.schemas import AppError  # noqa: E402
from src.repositories.database import (  # noqa: E402
    Database, backup_database, restore_database, verify_backup,
)


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"check", "backup", "restore"}:
        print(__doc__)
        return 2
    config = load_config()
    try:
        if argv[0] == "check":
            db = Database(config.db_path)
            db.initialize(backup_dir=config.backup_dir)
            print(f"OK: {config.db_path} (schema version {db.schema_version()})")
        elif argv[0] == "backup":
            print("Backup written to", backup_database(config.db_path, config.backup_dir))
        else:
            if len(argv) < 2:
                print("Provide the backup file path.")
                return 2
            version = verify_backup(argv[1])
            print(f"Backup verified (schema version {version}).")
            safety = restore_database(argv[1], config.db_path, config.backup_dir)
            print(f"Restored. Previous database saved as {safety}")
    except AppError as exc:
        print("ERROR:", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
