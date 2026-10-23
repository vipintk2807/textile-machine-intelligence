import unittest
from datetime import UTC, datetime

from src.models.schemas import ExportError
from src.utils.csv_utils import format_cell, rows_to_csv, sanitize_text_cell


class CsvSafetyTests(unittest.TestCase):
    def test_formula_prefixes_neutralised(self):
        for text in ("=SUM(A1)", "+1+1", "-2+3", "@cmd", "\t=1", "\r=1"):
            with self.subTest(text=text):
                self.assertTrue(sanitize_text_cell(text).startswith("'"))
        self.assertEqual(sanitize_text_cell("normal"), "normal")
        self.assertEqual(sanitize_text_cell(""), "")

    def test_numbers_unchanged_including_negative_and_zero(self):
        self.assertEqual(format_cell(-3.5), -3.5)
        self.assertEqual(format_cell(0), 0)
        self.assertEqual(format_cell(0.0), 0.0)

    def test_missing_is_blank_not_zero(self):
        self.assertEqual(format_cell(None), "")
        self.assertEqual(format_cell(float("nan")), "")

    def test_datetime_and_bool(self):
        self.assertEqual(format_cell(datetime(2026, 3, 1, 9, tzinfo=UTC)), "2026-03-01T09:00:00Z")
        self.assertEqual(format_cell(True), "true")

    def test_rows_to_csv(self):
        text = rows_to_csv(["a", "b"], [{"a": "=evil()", "b": -1.5}, {"a": None, "b": 0}])
        lines = text.splitlines()
        self.assertEqual(lines[0], "a,b")
        self.assertEqual(lines[1], "'=evil(),-1.5")
        self.assertEqual(lines[2], ",0")

    def test_empty_export_has_header(self):
        self.assertEqual(rows_to_csv(["a"], []).strip(), "a")

    def test_never_silently_truncates(self):
        with self.assertRaises(ExportError):
            rows_to_csv(["a"], ({"a": i} for i in range(5)), max_rows=3)


if __name__ == "__main__":
    unittest.main()
