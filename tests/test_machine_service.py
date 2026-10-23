import unittest

from tests.helpers import HAS_SQLALCHEMY, DbTestCase, requires_sqlalchemy

if HAS_SQLALCHEMY:
    from src.models.schemas import DuplicateError, NotFoundError, ValidationFailed
    from src.services.machine_service import MachineService


@requires_sqlalchemy
class MachineServiceTests(DbTestCase):
    def test_create_and_get(self):
        with self.db.session_scope() as s:
            src = self.make_source(s)
            MachineService(s).create(machine_id=" M-1 ", name=" Knit 1 ", machine_type="Knitting",
                                     source_id=src.id, location=" ")
        with self.db.session_scope() as s:
            m = MachineService(s).get("M-1")
            self.assertEqual((m.name, m.location, m.enabled), ("Knit 1", None, True))

    def test_duplicate_id_rejected_case_insensitively(self):
        with self.db.session_scope() as s:
            src = self.make_source(s)
            self.make_machine(s, src, "M-1")
            with self.assertRaises(DuplicateError):
                self.make_machine(s, src, "m-1")

    def test_validation_errors_per_field(self):
        with self.db.session_scope() as s:
            src = self.make_source(s)
            with self.assertRaises(ValidationFailed) as ctx:
                MachineService(s).create(machine_id="", name="", machine_type="", source_id=src.id)
            self.assertEqual(set(ctx.exception.field_errors), {"machine_id", "name", "machine_type"})

    def test_invalid_source_rejected(self):
        with self.db.session_scope() as s:
            with self.assertRaises(ValidationFailed):
                MachineService(s).create(machine_id="M1", name="n", machine_type="t", source_id=12345)

    def test_update_and_deactivate_keep_record(self):
        with self.db.session_scope() as s:
            self.make_machine(s, self.make_source(s))
            svc = MachineService(s)
            svc.update("M1", name="Renamed", machine_type="Dyeing", location="Hall A")
            svc.set_enabled("M1", False)
        with self.db.session_scope() as s:
            m = MachineService(s).get("M1")
            self.assertEqual((m.name, m.location, m.enabled), ("Renamed", "Hall A", False))
            self.assertEqual(MachineService(s).list(enabled=True), [])
            self.assertEqual(len(MachineService(s).list(enabled=False)), 1)

    def test_missing_machine(self):
        with self.db.session_scope() as s:
            with self.assertRaises(NotFoundError):
                MachineService(s).get("nope")

    def test_search_and_filters(self):
        with self.db.session_scope() as s:
            src = self.make_source(s)
            self.make_machine(s, src, "A1", name="Alpha", machine_type="Knitting")
            self.make_machine(s, src, "B1", name="Beta", machine_type="Dyeing")
            svc = MachineService(s)
            self.assertEqual([m.machine_id for m in svc.list(search="alp")], ["A1"])
            self.assertEqual([m.machine_id for m in svc.list(machine_type="Dyeing")], ["B1"])
            self.assertEqual(svc.distinct_values("machine_type"), ["Dyeing", "Knitting"])


if __name__ == "__main__":
    unittest.main()
