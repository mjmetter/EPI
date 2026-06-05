"""Tests for YoY section: build_yoy_series, render_yoy_section, compute_seasonal_baseline,
compute_residuals, and integration with render_overview_page."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from epi.reports.productivity import (
    build_yoy_series,
    compute_residuals,
    compute_seasonal_baseline,
    render_overview_page,
    render_yoy_section,
)

# ─── FIXTURE HELPERS ──────────────────────────────────────────────────────────


def _make_metrics_json(
    repo: str,
    month: str,
    commits: int = 20,
    contributors: int | None = None,
    mrs: int = 5,
) -> dict:
    """Build a minimal per-repo metrics JSON dict."""
    # If contributors not specified, use commits so commits_per_engineer == 1.0
    contribs = contributors if contributors is not None else commits
    per_contributor = {f"user{i}": 1 for i in range(contribs)} if contribs > 0 else {}
    return {
        "meta": {"repo": repo, "month": month, "category": "product"},
        "commits": {"total": commits, "per_contributor": per_contributor},
        "mrs": {"total": mrs},
        "lines_changed": {"added": 200, "removed": 100},
        "commit_classification": {"feature": 4},
    }


def _make_health_json(repo: str, month: str, ai_rate: float) -> dict:
    """Build a minimal health JSON dict with ai_assisted_commit_rate."""
    return {
        "meta": {"repo": repo, "month": month},
        "ai_adoption": {"ai_assisted_commit_rate": ai_rate, "ai_assisted_commits": int(ai_rate * 10)},
    }


def _write_product_months(
    base_dir: Path,
    product: str,
    month_commits: dict[str, int],
    *,
    repo: str = "repo-a",
    use_archive: bool = False,
) -> None:
    """Write per-repo metric JSONs for each month in month_commits.

    If use_archive=True months are written into products/<p>/archive/<year>/<month>_<repo>.json.
    """
    for month, commits in month_commits.items():
        year = month.split("-")[0]
        if use_archive:
            dest_dir = base_dir / "products" / product / "archive" / year
        else:
            dest_dir = base_dir / "products" / product
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / f"{month}_{repo}.json").write_text(
            json.dumps(_make_metrics_json(repo, month, commits=commits, contributors=commits))
        )


def _write_health_months(
    base_dir: Path,
    product: str,
    month_rates: dict[str, float],
    *,
    repo: str = "repo-a",
    use_archive: bool = False,
) -> None:
    """Write health JSONs for each month in month_rates."""
    for month, rate in month_rates.items():
        year = month.split("-")[0]
        if use_archive:
            dest_dir = base_dir / "products" / product / "archive" / year
        else:
            dest_dir = base_dir / "products" / product
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / f"health-{month}_{repo}.json").write_text(json.dumps(_make_health_json(repo, month, rate)))


# ─── TestBuildYoYSeries ───────────────────────────────────────────────────────


class TestBuildYoYSeries(unittest.TestCase):
    """Tests for build_yoy_series() pure function."""

    def _two_year_dir(self, base_dir: Path) -> Path:
        """Create a product with data in Jan-Mar for both 2024 and 2025.

        commits_per_engineer is forced to 1.0 (commits == contributors).
        2024: Jan=10, Feb=20, Mar=30
        2025: Jan=40, Feb=50, Mar=60
        """
        _write_product_months(base_dir, "prod1", {"2024-01": 10, "2024-02": 20, "2024-03": 30})
        _write_product_months(base_dir, "prod1", {"2025-01": 40, "2025-02": 50, "2025-03": 60})
        return base_dir

    def test_build_yoy_series_two_years(self):
        """Two years of data → by_year has 2 keys; deltas has overlapping months."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            self._two_year_dir(base_dir)
            all_months = ["2024-01", "2024-02", "2024-03", "2025-01", "2025-02", "2025-03"]
            by_year, deltas = build_yoy_series(["prod1"], base_dir, all_months, "commits_per_engineer")

        self.assertEqual(set(by_year.keys()), {2024, 2025})
        # 2024 has values at calendar months 1, 2, 3 (indices 0, 1, 2)
        self.assertAlmostEqual(by_year[2024][0], 1.0)  # Jan 2024: 10 commits / 10 contributors
        self.assertAlmostEqual(by_year[2024][1], 1.0)  # Feb 2024
        self.assertAlmostEqual(by_year[2024][2], 1.0)  # Mar 2024
        self.assertIsNone(by_year[2024][3])  # Apr 2024 — no data
        # 2025 same pattern
        self.assertAlmostEqual(by_year[2025][0], 1.0)

        # Deltas should cover Jan, Feb, Mar (present in both years)
        self.assertEqual(len(deltas), 3)
        delta_months = [d[0] for d in deltas]
        self.assertIn("Jan", delta_months)
        self.assertIn("Feb", delta_months)
        self.assertIn("Mar", delta_months)

    def test_build_yoy_series_one_year(self):
        """Only one year of data → by_year has 1 key; deltas is empty."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            _write_product_months(base_dir, "prod1", {"2025-01": 20, "2025-02": 30})
            all_months = ["2025-01", "2025-02"]
            by_year, deltas = build_yoy_series(["prod1"], base_dir, all_months, "commits_per_engineer")

        self.assertEqual(len(by_year), 1)
        self.assertIn(2025, by_year)
        self.assertEqual(deltas, [])

    def test_build_yoy_series_delta_values(self):
        """Verify delta absolute and percentage values are computed correctly.

        _write_product_months sets contributors == commits, so commits_per_engineer is always 1.0.
        To get a measurable delta we write the JSONs manually with different contributor counts.
        Feb 2024: 10 commits / 10 contributors = 1.0
        Feb 2025: 10 commits / 5 contributors  = 2.0  → delta = +100%
        """
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            prod_dir = base_dir / "products" / "prod1"
            prod_dir.mkdir(parents=True)
            # 2024-01 and 2024-02: 10 commits, 10 contributors each → cpe=1.0
            for month in ("2024-01", "2024-02"):
                (prod_dir / f"{month}_repo-a.json").write_text(
                    json.dumps(_make_metrics_json("repo-a", month, commits=10, contributors=10))
                )
            # 2025-01: 10 commits, 10 contributors → cpe=1.0
            (prod_dir / "2025-01_repo-a.json").write_text(
                json.dumps(_make_metrics_json("repo-a", "2025-01", commits=10, contributors=10))
            )
            # 2025-02: 10 commits, 5 contributors → cpe=2.0
            (prod_dir / "2025-02_repo-a.json").write_text(
                json.dumps(_make_metrics_json("repo-a", "2025-02", commits=10, contributors=5))
            )
            all_months = ["2024-01", "2024-02", "2025-01", "2025-02"]
            _by_year, deltas = build_yoy_series(["prod1"], base_dir, all_months, "commits_per_engineer")

        feb_delta = next(d for d in deltas if d[0] == "Feb")
        # prior=1.0, latest=2.0, pct=+100.0%
        self.assertAlmostEqual(feb_delta[1], 1.0)  # prior (2024-Feb)
        self.assertAlmostEqual(feb_delta[2], 2.0)  # latest (2025-Feb)
        self.assertAlmostEqual(feb_delta[3], 100.0)  # pct

    def test_build_yoy_series_with_archive_months(self):
        """Archive months are loaded correctly via _product_dir_for_month."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            # 2024 data in archive, 2025 data in root
            _write_product_months(base_dir, "prod1", {"2024-01": 10, "2024-02": 20}, use_archive=True)
            _write_product_months(base_dir, "prod1", {"2025-01": 10, "2025-02": 20})
            all_months = ["2024-01", "2024-02", "2025-01", "2025-02"]
            by_year, deltas = build_yoy_series(["prod1"], base_dir, all_months, "commits_per_engineer")

        self.assertEqual(set(by_year.keys()), {2024, 2025})
        self.assertAlmostEqual(by_year[2024][0], 1.0)  # Jan 2024 from archive
        self.assertAlmostEqual(by_year[2025][0], 1.0)  # Jan 2025 from root
        self.assertEqual(len(deltas), 2)  # Jan + Feb present in both years


# ─── TestComputeSeasonalBaseline ──────────────────────────────────────────────


class TestComputeSeasonalBaseline(unittest.TestCase):
    """Tests for compute_seasonal_baseline() pure function."""

    def test_compute_seasonal_baseline_two_years(self):
        """Baseline for each month is the average across years."""
        by_year: dict[int, list[float | None]] = {
            2024: [10.0, 20.0, None, None, None, None, None, None, None, None, None, None],
            2025: [20.0, 40.0, None, None, None, None, None, None, None, None, None, None],
        }
        baseline = compute_seasonal_baseline(by_year)
        self.assertEqual(len(baseline), 12)
        self.assertAlmostEqual(baseline[0], 15.0)  # Jan: (10 + 20) / 2
        self.assertAlmostEqual(baseline[1], 30.0)  # Feb: (20 + 40) / 2
        self.assertIsNone(baseline[2])  # Mar: no data

    def test_compute_seasonal_baseline_three_years(self):
        """Baseline averages across 3 years, ignoring Nones."""
        by_year: dict[int, list[float | None]] = {
            2023: [6.0, None, None, None, None, None, None, None, None, None, None, None],
            2024: [9.0, None, None, None, None, None, None, None, None, None, None, None],
            2025: [12.0, None, None, None, None, None, None, None, None, None, None, None],
        }
        baseline = compute_seasonal_baseline(by_year)
        self.assertAlmostEqual(baseline[0], 9.0)  # Jan: (6 + 9 + 12) / 3

    def test_compute_seasonal_baseline_single_year(self):
        """Single year: baseline equals that year's values."""
        by_year: dict[int, list[float | None]] = {
            2025: [5.0, 10.0] + [None] * 10,
        }
        baseline = compute_seasonal_baseline(by_year)
        self.assertAlmostEqual(baseline[0], 5.0)
        self.assertAlmostEqual(baseline[1], 10.0)


# ─── TestComputeResiduals ─────────────────────────────────────────────────────


class TestComputeResiduals(unittest.TestCase):
    """Tests for compute_residuals() pure function."""

    def test_compute_residuals_known_values(self):
        """Residual = actual - (baseline + yoy_slope * year_offset).

        With slope=0 (flat trend), residual == actual - baseline.
        """
        by_year: dict[int, list[float | None]] = {
            2024: [10.0, 20.0] + [None] * 10,
            2025: [15.0, 25.0] + [None] * 10,
        }
        baseline: list[float | None] = [12.0, 22.0] + [None] * 10
        residuals = compute_residuals(by_year, baseline, yoy_slope=0.0)
        # 2024 year_offset = -0.5 (midpoint of [2024, 2025] = 2024.5; 2024 - 2024.5 = -0.5)
        # residual(2024, Jan) = 10 - (12 + 0 * -0.5) = -2
        self.assertAlmostEqual(residuals[2024][0], -2.0)
        # residual(2024, Feb) = 20 - (22 + 0 * -0.5) = -2
        self.assertAlmostEqual(residuals[2024][1], -2.0)
        # residual(2025, Jan) = 15 - (12 + 0 * 0.5) = 3
        self.assertAlmostEqual(residuals[2025][0], 3.0)

    def test_compute_residuals_with_slope(self):
        """With non-zero slope, year offset is factored in."""
        by_year: dict[int, list[float | None]] = {
            2024: [10.0] + [None] * 11,
            2025: [14.0] + [None] * 11,
        }
        baseline: list[float | None] = [12.0] + [None] * 11
        # slope=2: each year adds 2 to the expected; year_offsets: 2024→-0.5, 2025→+0.5
        # residual(2024, Jan) = 10 - (12 + 2*-0.5) = 10 - 11 = -1
        # residual(2025, Jan) = 14 - (12 + 2*0.5)  = 14 - 13 = +1
        residuals = compute_residuals(by_year, baseline, yoy_slope=2.0)
        self.assertAlmostEqual(residuals[2024][0], -1.0)
        self.assertAlmostEqual(residuals[2025][0], 1.0)

    def test_compute_residuals_none_passthrough(self):
        """Months with None in by_year produce None residuals."""
        by_year: dict[int, list[float | None]] = {
            2025: [None] * 12,
        }
        baseline: list[float | None] = [None] * 12
        residuals = compute_residuals(by_year, baseline, yoy_slope=0.0)
        self.assertIsNone(residuals[2025][0])


# ─── TestRenderYoYSection ─────────────────────────────────────────────────────


class TestRenderYoYSection(unittest.TestCase):
    """Tests for render_yoy_section() HTML renderer."""

    def _by_year_two(self) -> dict[int, list[float | None]]:
        return {
            2024: [1.0, 2.0, 3.0] + [None] * 9,
            2025: [4.0, 5.0, 6.0] + [None] * 9,
        }

    def _by_year_one(self) -> dict[int, list[float | None]]:
        return {
            2025: [4.0, 5.0, 6.0] + [None] * 9,
        }

    def _deltas_two(self) -> list[tuple]:
        return [("Jan", 1.0, 4.0, 300.0), ("Feb", 2.0, 5.0, 150.0), ("Mar", 3.0, 6.0, 100.0)]

    def test_render_yoy_section_two_years_has_canvas(self):
        """Two years of data: HTML contains a canvas element."""
        html = render_yoy_section(
            metric_key="commits_per_engineer",
            metric_label="Commits per Engineer",
            by_year=self._by_year_two(),
            deltas=self._deltas_two(),
            baseline=[None] * 12,
            residuals={},
        )
        self.assertIn("<canvas", html)

    def test_render_yoy_section_two_years_has_delta_table(self):
        """Two years of data: HTML contains a YoY delta table."""
        html = render_yoy_section(
            metric_key="commits_per_engineer",
            metric_label="Commits per Engineer",
            by_year=self._by_year_two(),
            deltas=self._deltas_two(),
            baseline=[None] * 12,
            residuals={},
        )
        # Delta table must include month names and column headers
        self.assertIn("Jan", html)
        self.assertIn("Feb", html)

    def test_render_yoy_section_empty_state_one_year(self):
        """One year only → empty-state placeholder card, no canvas."""
        html = render_yoy_section(
            metric_key="commits_per_engineer",
            metric_label="Commits per Engineer",
            by_year=self._by_year_one(),
            deltas=[],
            baseline=[None] * 12,
            residuals={},
        )
        # Must NOT contain a canvas element
        self.assertNotIn("<canvas", html)
        # Must contain some placeholder text
        self.assertIn("Year-over-Year", html)

    def test_render_yoy_section_no_scatter(self):
        """Correlation scatter has been removed — no canvas for scatter, no Pearson r."""
        html = render_yoy_section(
            metric_key="commits_per_engineer",
            metric_label="Commits per Engineer",
            by_year=self._by_year_two(),
            deltas=self._deltas_two(),
            baseline=[None] * 12,
            residuals={2024: [None] * 12, 2025: [None] * 12},
        )
        self.assertNotIn("r&#8239;=", html)
        self.assertNotIn("yoy-corr-", html)


# ─── TestOverviewIncludesYoY ──────────────────────────────────────────────────


class TestOverviewIncludesYoY(unittest.TestCase):
    """Integration: render_overview_page with 2-year data includes YoY heading."""

    def _make_repo_json(self, repo: str, month: str, commits: int = 10) -> dict:
        return {
            "meta": {"repo": repo, "month": month, "category": "product"},
            "commits": {"total": commits, "per_contributor": {f"u{i}": 1 for i in range(commits)}},
            "mrs": {"total": 3},
            "lines_changed": {"added": 300, "removed": 100},
            "commit_classification": {"feature": 4},
        }

    def test_overview_includes_yoy_heading_two_years(self):
        """render_overview_page with 2-year months list includes 'Year-over-Year' heading."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            prod_dir = base_dir / "products" / "alpha"
            prod_dir.mkdir(parents=True)
            (base_dir / "repos.yaml").write_text("products:\n  alpha:\n    display_name: Alpha\n")
            # Write data for 2024 and 2025
            for month in ["2024-01", "2024-02", "2024-03", "2025-01", "2025-02", "2025-03"]:
                commits = 10
                (prod_dir / f"{month}_repo-a.json").write_text(
                    json.dumps(self._make_repo_json("repo-a", month, commits=commits))
                )
            all_months = ["2024-01", "2024-02", "2024-03", "2025-01", "2025-02", "2025-03"]
            html = render_overview_page(["alpha"], {"alpha": "Alpha"}, base_dir, all_months)

        self.assertIn("Year-over-Year", html)

    def test_overview_no_yoy_heading_one_year(self):
        """render_overview_page with only 1 year of data still includes YoY section (empty state)."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            prod_dir = base_dir / "products" / "alpha"
            prod_dir.mkdir(parents=True)
            (base_dir / "repos.yaml").write_text("products:\n  alpha:\n    display_name: Alpha\n")
            for month in ["2025-01", "2025-02", "2025-03"]:
                (prod_dir / f"{month}_repo-a.json").write_text(json.dumps(self._make_repo_json("repo-a", month)))
            html = render_overview_page(["alpha"], {"alpha": "Alpha"}, base_dir, ["2025-01", "2025-02", "2025-03"])

        # Even with one year, the section heading should be present
        self.assertIn("Year-over-Year", html)


# ─── TestCorrelationChart ─────────────────────────────────────────────────────


class TestCorrelationChart(unittest.TestCase):
    """Tests for AI adoption correlation scatter rendering."""

    def _make_metrics(self, month: str, commits: int = 10) -> dict:
        return {
            "meta": {"repo": "repo-a", "month": month, "category": "product"},
            "commits": {"total": commits, "per_contributor": {f"u{i}": 1 for i in range(commits)}},
            "mrs": {"total": 3},
            "lines_changed": {"added": 200, "removed": 100},
            "commit_classification": {"feature": 4},
        }

    def test_no_correlation_scatter_in_overview(self):
        """Correlation scatter has been removed — Pearson r must not appear in overview HTML."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            prod_dir = base_dir / "products" / "alpha"
            prod_dir.mkdir(parents=True)
            (base_dir / "repos.yaml").write_text("products:\n  alpha:\n    display_name: Alpha\n")
            for month in ["2024-01", "2024-02", "2025-01", "2025-02"]:
                (prod_dir / f"{month}_repo-a.json").write_text(json.dumps(self._make_metrics(month, commits=10)))
            html = render_overview_page(
                ["alpha"], {"alpha": "Alpha"}, base_dir, ["2024-01", "2024-02", "2025-01", "2025-02"]
            )

        self.assertNotIn("r&#8239;=", html)
        self.assertNotIn("yoy-corr-", html)


if __name__ == "__main__":
    unittest.main()
