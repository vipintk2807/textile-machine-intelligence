"""Headless Streamlit smoke tests using streamlit.testing.v1.AppTest (needs Streamlit installed)."""

import os
import tempfile
import unittest
from pathlib import Path

from tests.helpers import HAS_SQLALCHEMY

try:
    from streamlit.testing.v1 import AppTest

    HAS_STREAMLIT = HAS_SQLALCHEMY
except ImportError:  # pragma: no cover
    HAS_STREAMLIT = False

ROOT = Path(__file__).resolve().parents[1]
PAGES = ["Dashboard", "Machines", "History", "Sensors", "Anomalies", "Maintenance", "Settings"]


@unittest.skipUnless(HAS_STREAMLIT, "Streamlit/SQLAlchemy not installed in this environment")
class AppSmokeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._old = os.environ.get("TMI_DATA_DIR")
        os.environ["TMI_DATA_DIR"] = self._tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("TMI_DATA_DIR", None)
        else:
            os.environ["TMI_DATA_DIR"] = self._old
        self._tmp.cleanup()

    def run_page(self, page):
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
        at.session_state["nav_page"] = page
        at.run()
        return at

    def test_every_page_renders_on_an_empty_database(self):
        for page in PAGES:
            with self.subTest(page=page):
                at = self.run_page(page)
                self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])

    def test_every_page_renders_with_demo_data(self):
        from config import load_config
        from src.core.settings_rules import RuntimeSettings
        from src.repositories.database import Database
        from src.services.simulation_service import SimulationService
        from src.utils.time_utils import utc_now

        config = load_config()
        db = Database(config.db_path)
        db.initialize()
        with db.session_scope() as s:
            SimulationService(s, RuntimeSettings(timezone="Asia/Kolkata")).initialize_demo(
                seed=42, machine_count=4, lookback_hours=6, now=utc_now())
        db.dispose()
        for page in PAGES:
            with self.subTest(page=page):
                at = self.run_page(page)
                self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
                self.assertEqual(len(at.error), 0, [e.value for e in at.error])

    def test_invalid_configuration_is_reported_not_crashed(self):
        os.environ["TMI_LOG_LEVEL"] = "LOUD"
        try:
            at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
            at.run()
            self.assertTrue(any("could not start" in e.value for e in at.error))
        finally:
            os.environ.pop("TMI_LOG_LEVEL", None)


if __name__ == "__main__":
    unittest.main()
