"""Tests for import-uptime.py — uptime monitoring TSV → EPI manual YAML."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from epi.importers.uptime import compute_health, parse_downtime_min, parse_uptime_tsv, should_exclude, write_yaml

# ─── parse_downtime_min ───────────────────────────────────────────────────────


class TestParseDowntimeMin(unittest.TestCase):
    def test_zero(self):
        self.assertAlmostEqual(parse_downtime_min("0h 00m 00s"), 0.0, places=2)

    def test_minutes_only(self):
        self.assertAlmostEqual(parse_downtime_min("0h 32m 00s"), 32.0, places=2)

    def test_hours_and_minutes(self):
        self.assertAlmostEqual(parse_downtime_min("2h 34m 00s"), 154.0, places=2)

    def test_fractional_seconds(self):
        # 0h 01m 58s = 1 + 58/60 ≈ 1.967
        result = parse_downtime_min("0h 01m 58s")
        self.assertAlmostEqual(result, 1.0 + 58 / 60, places=2)

    def test_large_hours(self):
        # 62h 53m 45s
        expected = 62 * 60 + 53 + 45 / 60
        self.assertAlmostEqual(parse_downtime_min("62h 53m 45s"), expected, places=2)

    def test_dead_monitor(self):
        # 743h 44m 56s — "Dead Monitor" style dead check
        expected = 743 * 60 + 44 + 56 / 60
        self.assertAlmostEqual(parse_downtime_min("743h 44m 56s"), expected, places=1)

    def test_minutes_with_leading_zero(self):
        self.assertAlmostEqual(parse_downtime_min("0h 09m 00s"), 9.0, places=2)

    def test_seconds_only(self):
        self.assertAlmostEqual(parse_downtime_min("0h 00m 30s"), 0.5, places=2)


# ─── should_exclude ───────────────────────────────────────────────────────────


class TestShouldExclude(unittest.TestCase):
    def test_non_alerting_excluded(self):
        self.assertTrue(should_exclude("Example - Site Check (non-alerting)", "99.95%", []))

    def test_non_alerting_case_insensitive(self):
        self.assertTrue(should_exclude("Some Check (Non-Alerting)", "99.99%", []))

    def test_zero_uptime_excluded(self):
        # 0.00% uptime = dead monitor (e.g. Dead Monitor)
        self.assertTrue(should_exclude("Dead Monitor", "0.00%", []))

    def test_normal_check_included(self):
        self.assertFalse(should_exclude("Dashboard: app-one.example.com", "99.94%", []))

    def test_custom_pattern_excluded(self):
        self.assertTrue(should_exclude("Internal MC - Engineering", "99.93%", ["Internal MC"]))

    def test_custom_pattern_case_insensitive(self):
        self.assertTrue(should_exclude("Internal IDM", "99.97%", ["internal idm"]))

    def test_gt_uptime_not_excluded(self):
        self.assertFalse(should_exclude("Delivery API: app-one.example.com", ">99.99%", []))


# ─── parse_uptime_tsv ─────────────────────────────────────────────────────────


SAMPLE_TSV = """\
CHECK NAME\tUPTIME\tDOWNTIME\tOUTAGES\tRESPONSE TIME
svc-one:mission-control\t74.79%\t181h 30m 46s\t1\t426 ms
Dashboard: app-one.example.com\t99.94%\t0h 24m 00s\t14\t911 ms
Example - Site Check (non-alerting)\t99.98%\t0h 02m 00s\t1\t500 ms
Dead Monitor\t0.00%\t743h 44m 56s\t6\t0 ms
"""


class TestParseUptimeTsv(unittest.TestCase):
    def setUp(self):
        self.rows = parse_uptime_tsv(SAMPLE_TSV)

    def test_header_skipped(self):
        names = [r["name"] for r in self.rows]
        self.assertNotIn("CHECK NAME", names)

    def test_row_count(self):
        self.assertEqual(len(self.rows), 4)

    def test_fields_present(self):
        row = self.rows[0]
        self.assertIn("name", row)
        self.assertIn("uptime", row)
        self.assertIn("downtime_raw", row)
        self.assertIn("outages", row)

    def test_outages_int(self):
        self.assertEqual(self.rows[0]["outages"], 1)
        self.assertEqual(self.rows[1]["outages"], 14)

    def test_gt_uptime_preserved(self):
        # ">99.99%" should be stored as-is for exclusion check
        tsv = "Check\t>99.99%\t0h 01m 00s\t1\t400 ms\n"
        rows = parse_uptime_tsv(tsv)
        self.assertEqual(rows[0]["uptime"], ">99.99%")

    def test_empty_input(self):
        self.assertEqual(parse_uptime_tsv(""), [])

    def test_header_only(self):
        self.assertEqual(parse_uptime_tsv("CHECK NAME\tUPTIME\tDOWNTIME\tOUTAGES\tRESPONSE TIME\n"), [])


# ─── compute_health ───────────────────────────────────────────────────────────


class TestComputeHealth(unittest.TestCase):
    def test_basic_calculation(self):
        """Simple two-row case: verify totals and MTTR."""
        rows = [
            {"name": "Svc A", "uptime": "99.95%", "downtime_raw": "0h 10m 00s", "outages": 2},
            {"name": "Svc B", "uptime": "99.98%", "downtime_raw": "0h 05m 00s", "outages": 1},
        ]
        h = compute_health(rows, [])
        self.assertEqual(h["total_incidents"], 3)
        self.assertAlmostEqual(h["total_downtime_min"], 15.0, places=1)
        # MTTR = 15 min / 3 incidents / 60 = 0.083h → rounds to 0.08
        self.assertAlmostEqual(h["mttr_hours"], round(15 / 3 / 60, 2), places=2)

    def test_non_alerting_excluded(self):
        rows = [
            {"name": "Good Check", "uptime": "99.95%", "downtime_raw": "0h 10m 00s", "outages": 2},
            {"name": "Noise (non-alerting)", "uptime": "99.98%", "downtime_raw": "0h 05m 00s", "outages": 10},
        ]
        h = compute_health(rows, [])
        self.assertEqual(h["total_incidents"], 2)  # non-alerting not counted

    def test_dead_monitor_excluded(self):
        rows = [
            {"name": "Dead Monitor", "uptime": "0.00%", "downtime_raw": "743h 44m 56s", "outages": 6},
            {"name": "Real Check", "uptime": "99.98%", "downtime_raw": "0h 08m 00s", "outages": 1},
        ]
        h = compute_health(rows, [])
        self.assertEqual(h["total_incidents"], 1)
        self.assertAlmostEqual(h["total_downtime_min"], 8.0, places=1)

    def test_custom_exclude(self):
        rows = [
            {"name": "Internal MC - Engineering", "uptime": "99.93%", "downtime_raw": "5h 37m 35s", "outages": 264},
            {"name": "Real Check", "uptime": "99.98%", "downtime_raw": "0h 08m 00s", "outages": 1},
        ]
        h = compute_health(rows, ["Internal MC - Engineering"])
        self.assertEqual(h["total_incidents"], 1)

    def test_no_data_returns_none_mttr(self):
        h = compute_health([], [])
        self.assertIsNone(h["mttr_hours"])
        self.assertEqual(h["total_incidents"], 0)

    def test_april_2025_sample(self):
        """Spot-check the sample sample data matches expected totals."""
        rows = parse_uptime_tsv(SAMPLE_TSV)  # 4 rows, 2 excluded (non-alerting + Dead Monitor)
        h = compute_health(rows, [])
        # Only svc-one (1 incident, 181h30m) and app-one (14 incidents, 24m) included
        self.assertEqual(h["total_incidents"], 1 + 14)
        expected_min = parse_downtime_min("181h 30m 46s") + 24.0
        self.assertAlmostEqual(h["total_downtime_min"], expected_min, places=1)

    def test_excluded_count_reported(self):
        rows = [
            {"name": "Dead Monitor", "uptime": "0.00%", "downtime_raw": "0h 10m 00s", "outages": 1},
            {"name": "Good", "uptime": "99.99%", "downtime_raw": "0h 05m 00s", "outages": 1},
        ]
        h = compute_health(rows, [])
        self.assertEqual(h["excluded_count"], 1)
        self.assertEqual(h["included_count"], 1)


# ─── write_yaml ──────────────────────────────────────────────────────────────


class TestWriteYaml(unittest.TestCase):
    def _health(self, incidents=3, downtime_min=15.0) -> dict:
        return {
            "total_incidents": incidents,
            "total_downtime_min": downtime_min,
            "mttr_hours": round(downtime_min / incidents / 60, 2) if incidents else None,
            "included_count": 2,
            "excluded_count": 0,
            "top_checks": [("Svc A", 10.0, 2), ("Svc B", 5.0, 1)],
        }

    def test_notes_written_when_incidents_present(self):
        """Notes section must appear even when total_incidents > 0 (regression guard)."""
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            path = Path(f.name)
        self.addCleanup(path.unlink, missing_ok=True)
        write_yaml(path, "app_a", "2025-10", self._health(), "2026-04-14", {})
        text = path.read_text()
        self.assertIn("# --- Notes ---", text)
        self.assertIn("MTTR derived from uptime monitoring data", text)
        self.assertIn("Top checks by downtime", text)

    def test_notes_written_when_no_incidents(self):
        """Notes section must appear when total_incidents == 0."""
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            path = Path(f.name)
        self.addCleanup(path.unlink, missing_ok=True)
        write_yaml(path, "app_a", "2025-10", self._health(incidents=0, downtime_min=0.0), "2026-04-14", {})
        text = path.read_text()
        self.assertIn("# --- Notes ---", text)
        self.assertIn("mttr_hours: null", text)

    def test_mttr_comment_inline(self):
        """mttr_hours line includes avg calculation as inline comment."""
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            path = Path(f.name)
        self.addCleanup(path.unlink, missing_ok=True)
        write_yaml(path, "app_a", "2025-10", self._health(), "2026-04-14", {})
        text = path.read_text()
        self.assertIn("mttr_hours:", text)
        self.assertIn("min avg", text)

    def test_existing_non_health_fields_preserved(self):
        """Non-health fields from an existing YAML are written back unchanged."""
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            path = Path(f.name)
        self.addCleanup(path.unlink, missing_ok=True)
        preserve = {"deployment_frequency": 12, "rollbacks": 0}
        write_yaml(path, "app_a", "2025-10", self._health(), "2026-04-14", preserve)
        text = path.read_text()
        self.assertIn("deployment_frequency: 12", text)
        self.assertIn("rollbacks: 0", text)


if __name__ == "__main__":
    unittest.main()
