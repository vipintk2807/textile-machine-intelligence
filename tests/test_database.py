import sqlite3
import unittest
from unittest import mock

from tests.helpers import HAS_SQLALCHEMY, NOW, DbTestCase, requires_sqlalchemy

if HAS_SQLALCHEMY:
    from sqlalchemy.exc import IntegrityError

    from src.models.orm import Machine
    from src.models.schemas import PersistenceError
    from src.repositories import database as dbmod
    from src.repositories.database import Database, backup_database, restore_database, verify_backup


@requires_sqlalchemy
class DatabaseTests(DbTestCase):
    def test_fresh_database_has_schema_version_and_tables(self):
        self.assertEqual(self.db.schema_version(), dbmod.SCHEMA_VERSION)
        from sqlalchemy import inspect

        self.assertIn("machines", inspect(self.db.engine).get_table_names())

    def test_reinitialise_preserves_data(self):
        with self.db.session_scope() as s:
            self.make_machine(s, self.make_source(s))
        self.db.initialize()
        with self.db.session_scope() as s:
            self.assertEqual(s.get(Machine, "M1").name, "Machine M1")

    def test_newer_schema_is_refused_without_modification(self):
        with self.db.engine.begin() as c:
            c.exec_driver_sql("PRAGMA user_version = 99")
        with self.assertRaises(PersistenceError):
            self.db.initialize()
        self.assertEqual(self.db.schema_version(), 99)

    def test_foreign_unversioned_database_is_refused(self):
        other = self.dir / "other.db"
        con = sqlite3.connect(other)
        con.execute("create table x(a)")
        con.commit()
        con.close()
        with self.assertRaises(PersistenceError):
            Database(other).initialize()

    def test_migration_runs_with_backup(self):
        ran = []
        with mock.patch.object(dbmod, "SCHEMA_VERSION", 2):
            self.db.initialize(migrations={1: lambda conn: ran.append("v1->v2")}, backup_dir=self.dir / "bk")
        self.assertEqual(ran, ["v1->v2"])
        self.assertEqual(self.db.schema_version(), 2)
        self.assertTrue(list((self.dir / "bk").glob("*pre-migration*")))

    def test_missing_migration_refused(self):
        with mock.patch.object(dbmod, "SCHEMA_VERSION", 2):
            with self.assertRaises(PersistenceError):
                self.db.initialize(migrations={})

    def test_foreign_keys_enforced(self):
        with self.assertRaises(IntegrityError):
            with self.db.engine.begin() as c:
                c.exec_driver_sql(
                    "INSERT INTO machines (machine_id,name,machine_type,source_id,enabled,created_at,updated_at) "
                    "VALUES ('X','n','t',999,1,'2026-01-01','2026-01-01')"
                )

    def test_failed_unit_of_work_is_rolled_back(self):
        with self.assertRaises(RuntimeError):
            with self.db.session_scope() as s:
                self.make_machine(s, self.make_source(s))
                raise RuntimeError("boom")
        with self.db.session_scope() as s:
            self.assertIsNone(s.get(Machine, "M1"))

    def test_naive_datetime_rejected_by_column_type(self):
        from datetime import datetime

        from src.models.orm import DataSource

        with self.assertRaises(Exception):
            with self.db.session_scope() as s:
                s.add(DataSource(name="n", source_type="SIMULATED", enabled=True, created_at=datetime(2026, 1, 1)))

    def test_datetimes_round_trip_as_aware_utc(self):
        with self.db.session_scope() as s:
            src = self.make_source(s)
            m = self.make_machine(s, src)
            stamp = m.created_at
        with self.db.session_scope() as s:
            self.assertEqual(s.get(Machine, "M1").created_at, stamp)
            self.assertIsNotNone(s.get(Machine, "M1").created_at.tzinfo)

    def test_backup_and_restore_round_trip(self):
        with self.db.session_scope() as s:
            self.make_machine(s, self.make_source(s))
        backup = backup_database(self.db.path, self.dir / "bk", now=NOW)
        self.assertEqual(verify_backup(backup), dbmod.SCHEMA_VERSION)
        with self.db.session_scope() as s:
            s.delete(s.get(Machine, "M1"))
        self.db.dispose()
        restore_database(backup, self.db.path, self.dir / "safety")
        db2 = Database(self.db.path)
        try:
            with db2.session_scope() as s:
                self.assertIsNotNone(s.get(Machine, "M1"))
        finally:
            db2.dispose()

    def test_corrupt_backup_rejected(self):
        bad = self.dir / "bad.db"
        bad.write_bytes(b"not a database at all" * 100)
        with self.assertRaises(PersistenceError):
            verify_backup(bad)

    def test_reset_all_backs_up_then_empties(self):
        with self.db.session_scope() as s:
            self.make_machine(s, self.make_source(s))
        backup = self.db.reset_all(self.dir / "bk")
        self.assertTrue(backup and backup.exists())
        with self.db.session_scope() as s:
            self.assertIsNone(s.get(Machine, "M1"))
        self.assertEqual(self.db.schema_version(), dbmod.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
