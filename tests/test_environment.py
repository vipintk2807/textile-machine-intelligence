"""Fails (does not skip) when a required package is missing, so a broken install is never hidden."""

import importlib
import sys
import unittest


class EnvironmentTests(unittest.TestCase):
    def test_python_version(self):
        self.assertGreaterEqual(sys.version_info[:2], (3, 11))

    def test_required_packages_importable(self):
        missing = []
        for name in ("streamlit", "sqlalchemy", "pandas", "plotly", "zoneinfo"):
            try:
                importlib.import_module(name)
            except ImportError:
                missing.append(name)
        self.assertEqual(missing, [], f"Missing packages: {missing}. Run: pip install -r requirements.txt")

    def test_timezone_database_available(self):
        from src.utils.time_utils import get_zone

        get_zone("Asia/Kolkata")  # needs the 'tzdata' package on Windows


if __name__ == "__main__":
    unittest.main()
