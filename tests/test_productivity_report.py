"""Unit tests for generate-productivity-report.py."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

import epi.reports.productivity as _mod
from epi.reports.productivity import (
    _pearson,
    compute_contributors_human,
    compute_per_engineer_metrics,
    discover_months,
    discover_repos,
    load_ai_spend,
    load_product_data,
    render_overview_page,
    render_product_page,
    render_repo_page,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class TestComputeContributorsHuman(unittest.TestCase):
    """Test compute_contributors_human filters bots correctly."""

    def test_all_humans(self):
        per_contributor = {"alice": 30, "bob": 20, "carol": 15}
        self.assertEqual(compute_contributors_human(per_contributor), 3)

    def test_empty(self):
        self.assertEqual(compute_contributors_human({}), 0)

    def test_bot_exact_match_filtered(self):
        # BOT_EXACT from score-epi.py includes "jenkins", "devops", "ci.cd"
        per_contributor = {"alice": 30, "jenkins": 50}
        result = compute_contributors_human(per_contributor)
        self.assertEqual(result, 1)  # only alice

    def test_bot_pattern_filtered(self):
        # BOT_PATTERNS: project_\d+_bot_, group_\d+_bot_, \d+\+dependabot\[bot\]
        per_contributor = {"alice": 10, "project_123_bot_abc": 5, "12345+dependabot[bot]": 3}
        result = compute_contributors_human(per_contributor)
        self.assertEqual(result, 1)  # only alice

    def test_all_bots_returns_zero(self):
        # BOT_EXACT: "jenkins", "devops"; BOT_PATTERNS: project_\d+_bot_
        per_contributor = {"jenkins": 50, "devops": 20}
        self.assertEqual(compute_contributors_human(per_contributor), 0)

    def test_fixture_data_no_bots(self):
        """Fixture 2025-10_repo-a.json has 5 human contributors."""
        fixture_file = FIXTURES / "products" / "alpha" / "2025-10_repo-a.json"
        with open(fixture_file) as f:
            data = json.load(f)
        per_contributor = data["commits"]["per_contributor"]
        self.assertEqual(compute_contributors_human(per_contributor), 5)


class TestComputePerEngineerMetrics(unittest.TestCase):
    """Test compute_per_engineer_metrics divides correctly and handles edge cases."""

    def _make_data(
        self,
        commits_total: int = 80,
        mrs_total: int = 25,
        lines_added: int = 2000,
        lines_removed: int = 1000,
        feature_commits: int = 30,
        per_contributor: dict | None = None,
    ) -> dict:
        if per_contributor is None:
            per_contributor = {"alice": 40, "bob": 40}
        return {
            "commits": {"total": commits_total, "per_contributor": per_contributor},
            "mrs": {"total": mrs_total},
            "lines_changed": {"added": lines_added, "removed": lines_removed},
            "commit_classification": {"feature": feature_commits},
        }

    def test_basic_division(self):
        # 2 human contributors; lines changed = 2000 + 1000 = 3000; avg commit size = 3000 / 80
        data = self._make_data(
            commits_total=80,
            mrs_total=25,
            lines_added=2000,
            lines_removed=1000,
            feature_commits=30,
            per_contributor={"alice": 40, "bob": 40},
        )
        metrics = compute_per_engineer_metrics(data)
        self.assertAlmostEqual(metrics["commits_per_engineer"], 40.0)
        self.assertAlmostEqual(metrics["mrs_per_engineer"], 12.5)
        self.assertAlmostEqual(metrics["lines_per_engineer"], 1500.0)  # 3000 / 2
        self.assertAlmostEqual(metrics["features_per_engineer"], 15.0)
        self.assertAlmostEqual(metrics["avg_commit_size"], 37.5)  # 3000 / 80

    def test_avg_commit_size_none_when_no_commits(self):
        data = self._make_data(commits_total=0, per_contributor={"alice": 1})
        metrics = compute_per_engineer_metrics(data)
        self.assertIsNone(metrics["avg_commit_size"])

    def test_zero_contributors_returns_none(self):
        """Zero active human contributors — all metrics should be None (not crash)."""
        data = self._make_data(per_contributor={})
        metrics = compute_per_engineer_metrics(data)
        self.assertIsNone(metrics["commits_per_engineer"])
        self.assertIsNone(metrics["mrs_per_engineer"])
        self.assertIsNone(metrics["lines_per_engineer"])
        self.assertIsNone(metrics["features_per_engineer"])

    def test_all_bots_returns_none(self):
        # "jenkins" is in BOT_EXACT from score-epi.py
        data = self._make_data(per_contributor={"jenkins": 99})
        metrics = compute_per_engineer_metrics(data)
        self.assertIsNone(metrics["commits_per_engineer"])

    def test_legacy_mrs_merged_key(self):
        """Old schema used mrs_merged instead of mrs — must be read as fallback."""
        data = self._make_data()
        data.pop("mrs", None)
        data["mrs_merged"] = {"total": 10}
        metrics = compute_per_engineer_metrics(data)
        self.assertAlmostEqual(metrics["mrs_per_engineer"], 5.0)  # 10 / 2

    def test_fixture_data(self):
        """Spot-check against 2025-10_repo-a.json fixture."""
        fixture_file = FIXTURES / "products" / "alpha" / "2025-10_repo-a.json"
        with open(fixture_file) as f:
            data = json.load(f)
        # 5 human contributors
        metrics = compute_per_engineer_metrics(data)
        self.assertAlmostEqual(metrics["commits_per_engineer"], 16.0)  # 80 / 5
        self.assertAlmostEqual(metrics["mrs_per_engineer"], 5.0)  # 25 / 5
        self.assertAlmostEqual(metrics["lines_per_engineer"], 1400.0)  # (5000 + 2000) / 5
        self.assertAlmostEqual(metrics["features_per_engineer"], 6.0)  # 30 / 5

    def test_missing_sections_default_zero(self):
        """Missing JSON sections should not crash — default to 0 numerator."""
        data: dict = {"commits": {"total": 10, "per_contributor": {"alice": 10}}}
        metrics = compute_per_engineer_metrics(data)
        self.assertAlmostEqual(metrics["commits_per_engineer"], 10.0)
        self.assertAlmostEqual(metrics["mrs_per_engineer"], 0.0)
        self.assertAlmostEqual(metrics["lines_per_engineer"], 0.0)
        self.assertAlmostEqual(metrics["features_per_engineer"], 0.0)


class TestDiscoverMonths(unittest.TestCase):
    """Test discover_months finds correct sorted months from directory."""

    def test_fixture_alpha(self):
        """Alpha fixture has months 2025-10, 2025-11, 2025-12, 2026-01."""
        months = discover_months(FIXTURES / "products" / "alpha")
        self.assertEqual(months, ["2025-10", "2025-11", "2025-12", "2026-01"])

    def test_sorted_ascending(self):
        with tempfile.TemporaryDirectory() as d:
            dirpath = Path(d)
            (dirpath / "2026-03_repo-x.json").touch()
            (dirpath / "2026-01_repo-x.json").touch()
            (dirpath / "2026-02_repo-x.json").touch()
            months = discover_months(dirpath)
        self.assertEqual(months, ["2026-01", "2026-02", "2026-03"])

    def test_no_duplicates(self):
        """Multiple repos in same month produce one entry per month."""
        with tempfile.TemporaryDirectory() as d:
            dirpath = Path(d)
            (dirpath / "2026-01_repo-a.json").touch()
            (dirpath / "2026-01_repo-b.json").touch()
            (dirpath / "2026-02_repo-a.json").touch()
            months = discover_months(dirpath)
        self.assertEqual(months, ["2026-01", "2026-02"])

    def test_ignores_non_month_files(self):
        """Health files, manual YAMLs, etc. should not be included."""
        with tempfile.TemporaryDirectory() as d:
            dirpath = Path(d)
            (dirpath / "2026-01_repo-a.json").touch()
            (dirpath / "health-2026-01_repo-a.json").touch()
            (dirpath / "manual-2026-01.yaml").touch()
            months = discover_months(dirpath)
        self.assertEqual(months, ["2026-01"])

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as d:
            months = discover_months(Path(d))
        self.assertEqual(months, [])


class TestDiscoverRepos(unittest.TestCase):
    """Test discover_repos finds correct repo names from per-repo JSON files."""

    def test_fixture_alpha_month_2025_12(self):
        """2025-12 in alpha has repo-a and repo-c."""
        repos = discover_repos(FIXTURES / "products" / "alpha", "2025-12")
        self.assertEqual(sorted(repos), ["repo-a", "repo-c"])

    def test_fixture_alpha_month_2025_10(self):
        """2025-10 in alpha has only repo-a."""
        repos = discover_repos(FIXTURES / "products" / "alpha", "2025-10")
        self.assertEqual(repos, ["repo-a"])

    def test_excludes_health_files(self):
        """health-{month}_{repo}.json files must not appear as repos."""
        with tempfile.TemporaryDirectory() as d:
            dirpath = Path(d)
            (dirpath / "2026-01_repo-a.json").touch()
            (dirpath / "health-2026-01_repo-a.json").touch()
            repos = discover_repos(dirpath, "2026-01")
        self.assertEqual(repos, ["repo-a"])

    def test_no_repos_for_month(self):
        """Month with no matching files returns empty list."""
        repos = discover_repos(FIXTURES / "products" / "alpha", "2099-01")
        self.assertEqual(repos, [])

    def test_sorted_output(self):
        with tempfile.TemporaryDirectory() as d:
            dirpath = Path(d)
            (dirpath / "2026-02_zebra.json").touch()
            (dirpath / "2026-02_alpha.json").touch()
            repos = discover_repos(dirpath, "2026-02")
        self.assertEqual(repos, ["alpha", "zebra"])


class TestSupportRepoExclusion(unittest.TestCase):
    """Support repos (meta.category == 'support') must not appear in product drilldown."""

    def _make_repo_json(self, repo: str, month: str, category: str = "product") -> dict:
        return {
            "meta": {"repo": repo, "month": month, "category": category},
            "commits": {"total": 10, "per_contributor": {"alice": 10}},
            "mrs": {"total": 3},
            "lines_changed": {"added": 300, "removed": 200},
            "commit_classification": {"feature": 4},
        }

    def test_support_repo_excluded_from_product_page_charts(self):
        """render_product_page must not include support repos as chart series."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            product_dir = base_dir / "products" / "testprod"
            product_dir.mkdir(parents=True)

            # Write one product repo and one support repo
            (product_dir / "2026-01_core.json").write_text(
                json.dumps(self._make_repo_json("core", "2026-01", category="product"))
            )
            (product_dir / "2026-01_infra-support.json").write_text(
                json.dumps(self._make_repo_json("infra-support", "2026-01", category="support"))
            )

            html = render_product_page("testprod", "Test Prod", base_dir, ["2026-01"])

        # The product repo should appear as a chart dataset label
        self.assertIn('"core"', html)
        # The support repo must NOT appear as a chart dataset label
        self.assertNotIn('"infra-support"', html)

    def test_support_repo_in_collapsed_section_not_main_table(self):
        """Support repo must appear only in the collapsed support section, not in the main table
        or charts. The 'Show N support repos' button must be present."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            product_dir = base_dir / "products" / "testprod"
            product_dir.mkdir(parents=True)

            (product_dir / "2026-01_core.json").write_text(
                json.dumps(self._make_repo_json("core", "2026-01", category="product"))
            )
            (product_dir / "2026-01_infra-support.json").write_text(
                json.dumps(self._make_repo_json("infra-support", "2026-01", category="support"))
            )

            html = render_product_page("testprod", "Test Prod", base_dir, ["2026-01"])

        # Product repo appears as a normal link
        self.assertIn("productivity_testprod_core.html", html)
        # Support repo appears only inside the collapsed support section
        self.assertIn("productivity_testprod_infra-support.html", html)
        self.assertIn("Show 1 support repo", html)
        # Support repo must NOT appear as a chart dataset
        self.assertNotIn('"infra-support"', html)


class TestLegendVisibility(unittest.TestCase):
    """Product detail page hides the legend; overview keeps it."""

    def _make_repo_json(self, repo: str, month: str) -> dict:
        return {
            "meta": {"repo": repo, "month": month, "category": "product"},
            "commits": {"total": 10, "per_contributor": {"alice": 10}},
            "mrs": {"total": 3},
            "lines_changed": {"added": 300, "removed": 200},
            "commit_classification": {"feature": 4},
        }

    def test_product_page_legend_hidden(self):
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            product_dir = base_dir / "products" / "testprod"
            product_dir.mkdir(parents=True)
            (product_dir / "2026-01_core.json").write_text(json.dumps(self._make_repo_json("core", "2026-01")))
            html = render_product_page("testprod", "Test Prod", base_dir, ["2026-01"])
        self.assertIn("display: false", html)

    def test_overview_page_product_colors_on_drilldown_links(self):
        """Overview uses per-product colors on the drilldown links."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            product_dir = base_dir / "products" / "testprod"
            product_dir.mkdir(parents=True)
            (product_dir / "2026-01_core.json").write_text(json.dumps(self._make_repo_json("core", "2026-01")))
            # Write repos.yaml so load_repos_config works
            (base_dir / "repos.yaml").write_text("products:\n  testprod:\n    display_name: Test Prod\n")
            html = render_overview_page(["testprod"], {"testprod": "Test Prod"}, base_dir, ["2026-01"])
        # First product uses COLOURS[0] — verify its color appears on the drilldown link
        self.assertIn(_mod.COLOURS[0], html)
        # Engineering Metrics section should render with legend shown for product lines
        self.assertIn("Engineering Metrics", html)

    def test_overview_shows_engineering_metrics_section(self):
        """Overview renders the Engineering Metrics section with individual metric charts."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            (base_dir / "products" / "testprod").mkdir(parents=True)
            (base_dir / "products" / "testprod" / "2026-01_core.json").write_text(
                json.dumps(self._make_repo_json("core", "2026-01"))
            )
            (base_dir / "repos.yaml").write_text("products:\n  testprod:\n    display_name: Test Prod\n")
            html = render_overview_page(["testprod"], {"testprod": "Test Prod"}, base_dir, ["2026-01"])

        self.assertIn("Engineering Metrics", html)
        # Repo detail metrics (lines_per_engineer etc.) must not appear on overview
        for metric in _mod.REPO_DETAIL_METRICS:
            self.assertNotIn(f"chart-{metric['key'].replace('_', '-')}", html)


def _make_repo_json_full(
    repo: str,
    month: str,
    category: str = "product",
    commits: int = 10,
    mrs: int = 3,
    lines_added: int = 300,
    lines_removed: int = 100,
) -> dict:
    return {
        "meta": {"repo": repo, "month": month, "category": category},
        "commits": {"total": commits, "per_contributor": {"alice": commits}},
        "mrs": {"total": mrs},
        "lines_changed": {"added": lines_added, "removed": lines_removed},
        "commit_classification": {"feature": 4},
    }


class TestLoadProductData(unittest.TestCase):
    """load_product_data aggregates repos, skips support repos, and handles mrs_merged fallback."""

    def test_aggregates_two_repos(self):
        with tempfile.TemporaryDirectory() as base:
            product_dir = Path(base)
            (product_dir / "2026-01_repo-a.json").write_text(
                json.dumps(_make_repo_json_full("repo-a", "2026-01", commits=20, mrs=4))
            )
            (product_dir / "2026-01_repo-b.json").write_text(
                json.dumps(_make_repo_json_full("repo-b", "2026-01", commits=10, mrs=2))
            )
            result = load_product_data(product_dir, "2026-01")

        self.assertEqual(result["commits"]["total"], 30)
        self.assertEqual(result["mrs"]["total"], 6)

    def test_support_repo_skipped(self):
        with tempfile.TemporaryDirectory() as base:
            product_dir = Path(base)
            (product_dir / "2026-01_core.json").write_text(
                json.dumps(_make_repo_json_full("core", "2026-01", commits=20, mrs=4))
            )
            (product_dir / "2026-01_infra.json").write_text(
                json.dumps(_make_repo_json_full("infra", "2026-01", category="support", commits=999, mrs=999))
            )
            result = load_product_data(product_dir, "2026-01")

        # Support repo's commits/mrs must not appear
        self.assertEqual(result["commits"]["total"], 20)
        self.assertEqual(result["mrs"]["total"], 4)

    def test_mrs_merged_fallback(self):
        """Old-schema repos using mrs_merged key are correctly counted."""
        with tempfile.TemporaryDirectory() as base:
            product_dir = Path(base)
            data = _make_repo_json_full("repo-a", "2026-01", mrs=0)
            del data["mrs"]
            data["mrs_merged"] = {"total": 7}
            (product_dir / "2026-01_repo-a.json").write_text(json.dumps(data))
            result = load_product_data(product_dir, "2026-01")

        self.assertEqual(result["mrs"]["total"], 7)

    def test_per_contributor_aggregated_across_repos(self):
        with tempfile.TemporaryDirectory() as base:
            product_dir = Path(base)
            data_a = _make_repo_json_full("repo-a", "2026-01")
            data_a["commits"]["per_contributor"] = {"alice": 10, "bob": 5}
            data_b = _make_repo_json_full("repo-b", "2026-01")
            data_b["commits"]["per_contributor"] = {"alice": 3, "carol": 8}
            (product_dir / "2026-01_repo-a.json").write_text(json.dumps(data_a))
            (product_dir / "2026-01_repo-b.json").write_text(json.dumps(data_b))
            result = load_product_data(product_dir, "2026-01")

        self.assertEqual(result["commits"]["per_contributor"]["alice"], 13)
        self.assertEqual(result["commits"]["per_contributor"]["bob"], 5)
        self.assertEqual(result["commits"]["per_contributor"]["carol"], 8)

    def test_health_files_skipped(self):
        with tempfile.TemporaryDirectory() as base:
            product_dir = Path(base)
            (product_dir / "2026-01_repo-a.json").write_text(
                json.dumps(_make_repo_json_full("repo-a", "2026-01", commits=5))
            )
            (product_dir / "health-2026-01_repo-a.json").write_text(json.dumps({"ai_adoption": {}}))
            result = load_product_data(product_dir, "2026-01")

        self.assertEqual(result["commits"]["total"], 5)


class TestRenderRepoPage(unittest.TestCase):
    """render_repo_page produces a complete HTML page for a single repo drilldown."""

    def _write_repo_json(self, product_dir: Path, month: str, repo: str, **kwargs: int) -> None:
        product_dir.mkdir(parents=True, exist_ok=True)
        data = _make_repo_json_full(repo, month, **kwargs)
        (product_dir / f"{month}_{repo}.json").write_text(json.dumps(data))

    def test_contains_repo_name_in_output(self):
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            product_dir = base_dir / "products" / "testprod"
            self._write_repo_json(product_dir, "2026-01", "my-repo")
            html = render_repo_page("my-repo", "testprod", "Test Prod", base_dir, ["2026-01"])

        self.assertIn("my-repo", html)

    def test_contains_all_metric_chart_ids(self):
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            product_dir = base_dir / "products" / "testprod"
            self._write_repo_json(product_dir, "2026-01", "repo-x")
            html = render_repo_page("repo-x", "testprod", "Test Prod", base_dir, ["2026-01"])

        # Top 2 primary metrics
        for metric in _mod.OVERVIEW_METRICS:
            self.assertIn(f"chart-{metric['key'].replace('_', '-')}", html)
        # Bottom 3 detail metrics
        for metric in _mod.REPO_DETAIL_METRICS:
            self.assertIn(f"chart-{metric['key'].replace('_', '-')}", html)

    def test_repo_page_has_rework_rate_chart(self):
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            product_dir = base_dir / "products" / "testprod"
            product_dir.mkdir(parents=True, exist_ok=True)
            # 10 commits total, 3 bugfix → rework rate = 30.0 %
            data = _make_repo_json_full("repo-x", "2026-01", commits=10, mrs=2)
            data["commit_classification"]["bugfix"] = 3
            (product_dir / "2026-01_repo-x.json").write_text(json.dumps(data))
            html = render_repo_page("repo-x", "testprod", "Test Prod", base_dir, ["2026-01"])

        self.assertIn('id="chart-rework-rate"', html)
        self.assertIn("30.0", html)  # rework rate value rendered in the JS dataset

    def test_activity_section_present_when_data_exists(self):
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            product_dir = base_dir / "products" / "testprod"
            self._write_repo_json(product_dir, "2026-01", "repo-x", commits=20)
            html = render_repo_page("repo-x", "testprod", "Test Prod", base_dir, ["2026-01"])

        self.assertIn("Activity Breakdown", html)
        self.assertIn("Top Contributors", html)

    def test_breadcrumb_links_overview_and_product(self):
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            product_dir = base_dir / "products" / "testprod"
            self._write_repo_json(product_dir, "2026-01", "repo-x")
            html = render_repo_page("repo-x", "testprod", "Test Prod", base_dir, ["2026-01"])

        self.assertIn("productivity.html", html)
        self.assertIn("productivity_testprod.html", html)

    def test_contributor_name_is_escaped(self):
        """Git author names with HTML special chars must be escaped in the output."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            product_dir = base_dir / "products" / "testprod"
            product_dir.mkdir(parents=True, exist_ok=True)
            data = _make_repo_json_full("repo-x", "2026-01")
            data["commits"]["per_contributor"] = {"alice<b>xss</b>": 10}
            data["commits"]["total"] = 10
            (product_dir / "2026-01_repo-x.json").write_text(json.dumps(data))
            html = render_repo_page("repo-x", "testprod", "Test Prod", base_dir, ["2026-01"])

        # Raw tag must not appear in the contributor span; escaped form must be present
        self.assertNotIn(">alice<b>", html)
        self.assertIn("alice&lt;b&gt;xss&lt;/b&gt;", html)

    def test_no_data_renders_without_crash(self):
        """A repo that exists in months list but has no JSON file should not crash."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            (base_dir / "products" / "testprod").mkdir(parents=True, exist_ok=True)
            html = render_repo_page("ghost-repo", "testprod", "Test Prod", base_dir, ["2026-01"])

        self.assertIn("ghost-repo", html)


class TestPearson(unittest.TestCase):
    """_pearson(xs, ys) — Pearson r helper."""

    def test_perfect_positive_correlation(self):
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [2.0, 4.0, 6.0, 8.0]
        r = _pearson(xs, ys)
        assert r is not None
        self.assertAlmostEqual(r, 1.0, places=6)

    def test_perfect_negative_correlation(self):
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [8.0, 6.0, 4.0, 2.0]
        r = _pearson(xs, ys)
        assert r is not None
        self.assertAlmostEqual(r, -1.0, places=6)

    def test_none_pairs_ignored(self):
        xs = [1.0, None, 3.0, 4.0]
        ys = [2.0, 4.0, None, 8.0]
        # Only the pair (1.0, 2.0) and (4.0, 8.0) are valid — still < 3 pairs → None
        self.assertIsNone(_pearson(xs, ys))

    def test_fewer_than_three_valid_pairs_returns_none(self):
        self.assertIsNone(_pearson([1.0, 2.0], [3.0, 4.0]))

    def test_constant_series_returns_none(self):
        # denominator is zero when one series has no variance
        xs = [5.0, 5.0, 5.0, 5.0]
        ys = [1.0, 2.0, 3.0, 4.0]
        self.assertIsNone(_pearson(xs, ys))

    def test_all_none_returns_none(self):
        self.assertIsNone(_pearson([None, None, None], [None, None, None]))


class TestAiAdoptionOverlay(unittest.TestCase):
    """Repo pages render an AI Adoption placeholder and health detail section when data is present."""

    def _make_repo_json(self, repo: str, month: str) -> dict:
        return {
            "meta": {"repo": repo, "month": month, "category": "product"},
            "commits": {"total": 10, "per_contributor": {"alice": 10}},
            "mrs": {"total": 3},
            "lines_changed": {"added": 300, "removed": 100},
            "commit_classification": {"feature": 4},
        }

    def _make_health_json(self, repo: str, month: str, rate: float) -> dict:
        return {
            "meta": {"repo": repo, "month": month},
            "ai_adoption": {"ai_assisted_commit_rate": rate},
        }

    def test_ai_readiness_adoption_section_shown_when_health_present(self):
        """The AI Readiness & Adoption section appears when health data is present."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            prod_dir = base_dir / "products" / "alpha"
            prod_dir.mkdir(parents=True)
            (prod_dir / "2026-01_repo-a.json").write_text(json.dumps(self._make_repo_json("repo-a", "2026-01")))
            (prod_dir / "health-2026-01_repo-a.json").write_text(
                json.dumps(self._make_health_json("repo-a", "2026-01", 25.0))
            )
            html = render_repo_page("repo-a", "alpha", "Alpha", base_dir, ["2026-01"])

        self.assertIn("AI Readiness", html)
        self.assertIn('id="chart-rai-ai-assisted-commit-rate"', html)

    def test_ai_readiness_charts_rendered_from_dict_values(self):
        """ai_readiness metrics are dicts {value, source} — verify dict unwrapping renders charts."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            prod_dir = base_dir / "products" / "alpha"
            prod_dir.mkdir(parents=True)
            (prod_dir / "2026-01_repo-a.json").write_text(json.dumps(self._make_repo_json("repo-a", "2026-01")))
            health = {
                "meta": {"repo": "repo-a", "month": "2026-01"},
                "ai_adoption": {"ai_assisted_commit_rate": 25.0},
                "ai_readiness": {
                    "ticket_reference_rate": {"value": 80.0, "source": "automated"},
                    "feature_test_coupling": {"value": 60.0, "source": "automated"},
                },
            }
            (prod_dir / "health-2026-01_repo-a.json").write_text(json.dumps(health))
            html = render_repo_page("repo-a", "alpha", "Alpha", base_dir, ["2026-01"])

        self.assertIn('id="chart-rai-ticket-reference-rate"', html)
        self.assertIn('id="chart-rai-feature-test-coupling"', html)

    def test_health_section_rendered_when_health_present(self):
        """AI Readiness section appears in the month detail when health data is present."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            prod_dir = base_dir / "products" / "alpha"
            prod_dir.mkdir(parents=True)
            (prod_dir / "2026-01_repo-a.json").write_text(json.dumps(self._make_repo_json("repo-a", "2026-01")))
            (prod_dir / "health-2026-01_repo-a.json").write_text(
                json.dumps(self._make_health_json("repo-a", "2026-01", 42.5))
            )
            html = render_repo_page("repo-a", "alpha", "Alpha", base_dir, ["2026-01"])

        self.assertIn("AI Readiness", html)

    def test_health_section_absent_when_health_missing(self):
        """AI Readiness section is not rendered when no health file exists."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            prod_dir = base_dir / "products" / "alpha"
            prod_dir.mkdir(parents=True)
            (prod_dir / "2026-01_repo-a.json").write_text(json.dumps(self._make_repo_json("repo-a", "2026-01")))
            html = render_repo_page("repo-a", "alpha", "Alpha", base_dir, ["2026-01"])

        self.assertNotIn("AI Readiness", html)

    def test_month_slider_present_when_data_exists(self):
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            prod_dir = base_dir / "products" / "alpha"
            prod_dir.mkdir(parents=True)
            (prod_dir / "2026-01_repo-a.json").write_text(json.dumps(self._make_repo_json("repo-a", "2026-01")))
            html = render_repo_page("repo-a", "alpha", "Alpha", base_dir, ["2026-01"])

        self.assertIn("month-slider", html)
        self.assertNotIn("month-pill", html)

    def test_real_chart_rendered_when_ai_commits_present(self):
        """When health data has ai_assisted_commits, a real canvas replaces the placeholder."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            prod_dir = base_dir / "products" / "alpha"
            prod_dir.mkdir(parents=True)
            (prod_dir / "2026-01_repo-a.json").write_text(json.dumps(self._make_repo_json("repo-a", "2026-01")))
            health = {"meta": {"repo": "repo-a", "month": "2026-01"}, "ai_adoption": {"ai_assisted_commit_rate": 25.0}}
            (prod_dir / "health-2026-01_repo-a.json").write_text(json.dumps(health))
            html = render_repo_page("repo-a", "alpha", "Alpha", base_dir, ["2026-01"])

        self.assertIn('id="chart-rai-ai-assisted-commit-rate"', html)
        self.assertNotIn("Coming soon", html)

    def test_ai_adoption_chart_absent_when_no_rate_data(self):
        """Without ai_assisted_commit_rate in health data, the AI adoption chart is not rendered."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            prod_dir = base_dir / "products" / "alpha"
            prod_dir.mkdir(parents=True)
            (prod_dir / "2026-01_repo-a.json").write_text(json.dumps(self._make_repo_json("repo-a", "2026-01")))
            health = {"meta": {"repo": "repo-a", "month": "2026-01"}, "ai_adoption": {"litellm_spend_usd": 12.5}}
            (prod_dir / "health-2026-01_repo-a.json").write_text(json.dumps(health))
            html = render_repo_page("repo-a", "alpha", "Alpha", base_dir, ["2026-01"])

        self.assertNotIn('id="chart-rai-ai-assisted-commit-rate"', html)

    def test_correlation_label_not_shown(self):
        """Pearson r correlation is not rendered — AI adoption chart is a placeholder for now."""
        months = ["2026-01", "2026-02", "2026-03"]
        line_counts = [200, 400, 600]
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            prod_dir = base_dir / "products" / "alpha"
            prod_dir.mkdir(parents=True)
            for i, (m, lines) in enumerate(zip(months, line_counts), start=1):  # noqa: B905
                repo_json = self._make_repo_json("repo-a", m)
                repo_json["lines_changed"] = {"added": lines, "removed": 0}
                (prod_dir / f"{m}_repo-a.json").write_text(json.dumps(repo_json))
                (prod_dir / f"health-{m}_repo-a.json").write_text(
                    json.dumps(self._make_health_json("repo-a", m, float(i * 10)))
                )
            html = render_repo_page("repo-a", "alpha", "Alpha", base_dir, months)

        self.assertNotIn("r&#8239;=&#8239;", html)
        self.assertNotIn("corrLabel", html)


class TestChatIntegration(unittest.TestCase):
    """Chat panel must appear in all three page types."""

    def _make_repo_json(self, repo: str, month: str) -> dict:
        return {
            "meta": {"repo": repo, "month": month, "category": "product"},
            "commits": {"total": 10, "per_contributor": {"alice": 10}},
            "mrs": {"total": 3},
            "lines_changed": {"added": 300, "removed": 100},
            "commit_classification": {"feature": 4},
        }

    def test_overview_page_has_chat_button(self):
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            (base_dir / "products" / "alpha").mkdir(parents=True)
            (base_dir / "products" / "alpha" / "2026-01_repo-a.json").write_text(
                json.dumps(self._make_repo_json("repo-a", "2026-01"))
            )
            (base_dir / "repos.yaml").write_text("products:\n  alpha:\n    display_name: Alpha\n")
            html = render_overview_page(["alpha"], {"alpha": "Alpha"}, base_dir, ["2026-01"])

        self.assertIn("chat-toggle", html)
        self.assertIn("chat-panel", html)
        self.assertIn("__prodChat", html)

    def test_product_page_has_chat_button(self):
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            (base_dir / "products" / "alpha").mkdir(parents=True)
            (base_dir / "products" / "alpha" / "2026-01_repo-a.json").write_text(
                json.dumps(self._make_repo_json("repo-a", "2026-01"))
            )
            html = render_product_page("alpha", "Alpha", base_dir, ["2026-01"])

        self.assertIn("chat-toggle", html)
        self.assertIn("__prodChat", html)

    def test_repo_page_has_chat_button(self):
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            (base_dir / "products" / "alpha").mkdir(parents=True)
            (base_dir / "products" / "alpha" / "2026-01_repo-a.json").write_text(
                json.dumps(self._make_repo_json("repo-a", "2026-01"))
            )
            html = render_repo_page("repo-a", "alpha", "Alpha", base_dir, ["2026-01"])

        self.assertIn("chat-toggle", html)
        self.assertIn("__prodChat", html)

    def test_system_prompt_contains_page_context(self):
        """The injected system prompt must include the product name so the AI has context."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            (base_dir / "products" / "alpha").mkdir(parents=True)
            (base_dir / "products" / "alpha" / "2026-01_repo-a.json").write_text(
                json.dumps(self._make_repo_json("repo-a", "2026-01"))
            )
            html = render_product_page("alpha", "Alpha", base_dir, ["2026-01"])

        # product name must appear inside the serialised context JSON baked into the systemPrompt,
        # not just in the page title — the JSON is double-encoded so inner quotes are escaped
        self.assertIn('\\"product\\": \\"Alpha\\"', html)
        self.assertIn("systemPrompt", html)


class TestUpdateBadgesExcludesTrendlines(unittest.TestCase):
    """updateBadges must ignore trendline datasets to avoid inflating YoY percentage.

    When a product is toggled off, only its primary dataset gets ds.hidden = true.
    The corresponding trendline dataset (label ending ' trend') stays at hidden=undefined,
    so it passes the !ds.hidden check and pollutes the from/to averages.
    """

    def test_visible_filter_excludes_trend_datasets(self):
        # The fix: filter also rejects datasets whose label ends with ' trend'
        self.assertIn("endsWith(' trend')", _mod._JS_UPDATE_BADGES)

    def test_overview_page_js_contains_trend_exclusion(self):
        """The overview page HTML must embed the fixed updateBadges visible filter."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            (base_dir / "products" / "alpha").mkdir(parents=True)
            (base_dir / "products" / "alpha" / "2026-01_repo-a.json").write_text(
                json.dumps(_make_repo_json_full("repo-a", "2026-01"))
            )
            (base_dir / "repos.yaml").write_text("products:\n  alpha:\n    display_name: Alpha\n")
            html = render_overview_page(["alpha"], {"alpha": "Alpha"}, base_dir, ["2026-01"])

        # This conjunction only appears inside the fixed updateBadges visible filter
        self.assertIn(
            "entry[1].isDatasetVisible(i) && !(ds.label && ds.label.endsWith(' trend')) && ds.yAxisID !== 'y1'", html
        )


class TestProductToggleHidesTrendline(unittest.TestCase):
    """Toggling a product off must also hide its matching trendline dataset."""

    def _make_repo_json(self, repo: str, month: str) -> dict:
        return {
            "meta": {"repo": repo, "month": month, "category": "product"},
            "commits": {"total": 10, "per_contributor": {"alice": 10}},
            "mrs": {"total": 3},
            "lines_changed": {"added": 300, "removed": 100},
            "commit_classification": {"feature": 4},
        }

    def test_overview_product_toggle_hides_trendline(self):
        """The product toggle JS must hide the trendline whose label matches the product."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            for prod in ("alpha", "beta"):
                (base_dir / "products" / prod).mkdir(parents=True)
                (base_dir / "products" / prod / "2026-01_repo-a.json").write_text(
                    json.dumps(self._make_repo_json("repo-a", "2026-01"))
                )
            (base_dir / "repos.yaml").write_text(
                "products:\n  alpha:\n    display_name: Alpha\n  beta:\n    display_name: Beta\n"
            )
            html = render_overview_page(["alpha", "beta"], {"alpha": "Alpha", "beta": "Beta"}, base_dir, ["2026-01"])

        # The toggle handler must propagate hidden to the matching trendline.
        # This pattern only appears inside the toggle handler after the fix — not
        # in trendDatasets construction (which uses ds.label + ' trend' differently).
        self.assertIn("if (d.label === trendLabel) chart.setDatasetVisibility(i, show)", html)

    def test_product_page_repo_toggle_hides_trendline(self):
        """The repo toggle JS on the product page must also hide the matching trendline."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            product_dir = base_dir / "products" / "testprod"
            product_dir.mkdir(parents=True)
            (product_dir / "2026-01_repo-a.json").write_text(json.dumps(self._make_repo_json("repo-a", "2026-01")))
            html = render_product_page("testprod", "Test Prod", base_dir, ["2026-01"])

        self.assertIn("if (d.label === trendLabel) chart.setDatasetVisibility(i, show)", html)


class TestOverviewSpendOverlay(unittest.TestCase):
    """AI adoption data from ai-spend.yaml should appear in the AI Tool Adoption section."""

    def _make_repo_json(self, repo: str, month: str) -> dict:
        return {
            "meta": {"repo": repo, "month": month, "category": "product"},
            "commits": {"total": 10, "per_contributor": {"alice": 10}},
            "mrs": {"total": 2},
            "lines_changed": {"added": 100, "removed": 50},
            "commit_classification": {"feature": 4},
        }

    def test_active_user_chart_rendered_when_yaml_has_data(self):
        """When ai-spend.yaml has active user data, AI Tool Adoption chart is rendered."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            (base_dir / "products" / "testprod").mkdir(parents=True)
            (base_dir / "products" / "testprod" / "2026-01_repo-a.json").write_text(
                json.dumps(self._make_repo_json("repo-a", "2026-01"))
            )
            (base_dir / "ai-spend.yaml").write_text(
                'cursor_active_users:\n  "2026-01": 130\nlitellm_active_users:\n  "2026-01": 50\n'
            )

            html = render_overview_page(["testprod"], {"testprod": "Test Prod"}, base_dir, ["2026-01"])

        self.assertIn("AI Tool Adoption", html)
        self.assertIn("Cursor Active Users", html)
        self.assertIn("LiteLLM Active Users", html)

    def test_no_spend_overlay_on_charts(self):
        """Spend dollar overlays are no longer shown on metric charts — they were removed."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            (base_dir / "products" / "testprod").mkdir(parents=True)
            (base_dir / "products" / "testprod" / "2026-01_repo-a.json").write_text(
                json.dumps(self._make_repo_json("repo-a", "2026-01"))
            )
            (base_dir / "ai-spend.yaml").write_text('litellm:\n  "2026-01": 5000\ncursor:\n  "2026-01": 15.0\n')

            html = render_overview_page(["testprod"], {"testprod": "Test Prod"}, base_dir, ["2026-01"])

        # Spend $ overlays were tied to the old two-chart layout which was removed
        self.assertNotIn('id="spend-litellm-chart-', html)
        self.assertNotIn('id="spend-cursor-chart-', html)

    def test_load_ai_spend_null_values_do_not_corrupt_other_series(self):
        """Null entries in ai-spend.yaml (e.g. unreliable cursor months) must not silently wipe all data."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            (base_dir / "ai-spend.yaml").write_text(
                "cursor:\n"
                "  2025-08: ~\n"
                "  2025-09: ~\n"
                "  2026-01: 51201\n"
                "litellm_active_users:\n"
                "  2026-01: 116\n"
                "  2026-02: 128\n"
            )
            result = load_ai_spend(base_dir)

        self.assertIn("cursor", result)
        self.assertNotIn("2025-08", result["cursor"], "null cursor months must be omitted")
        self.assertEqual(result["cursor"]["2026-01"], 51201)
        self.assertIn("litellm_active_users", result, "unrelated series must survive null values in another series")
        self.assertEqual(result["litellm_active_users"]["2026-01"], 116)


class TestPerRepoCursorSpend(unittest.TestCase):
    """Repo-detail AI Adoption chart shows only AI commits — no spend series."""

    def _make_metrics_json(self) -> dict:
        return {
            "meta": {"repo": "repo-a", "month": "2026-01", "category": "product"},
            "commits": {"total": 5, "per_contributor": {"alice": 5}},
            "mrs": {"total": 1},
            "lines_changed": {"added": 50, "removed": 20},
            "commit_classification": {"feature": 2},
        }

    def _make_health_json(self, litellm: float = 10.0, cursor: float = 25.0) -> dict:
        return {
            "meta": {"repo": "repo-a", "month": "2026-01", "product": "testprod", "category": "product"},
            "ai_readiness": {},
            "ai_adoption": {
                "co_authored_commits": {"total": 0, "by_tool": {}},
                "co_authored_rate": 0.0,
                "mr_ai_usage": {"total_mrs": 0, "none": 0, "ai_augmented": 0, "agentic": 0, "na": 0},
                "ai_assisted_mr_rate": 0.0,
                "ai_assisted_commits": 5,
                "ai_assisted_commit_rate": 1.0,
                "litellm_spend_usd": litellm,
                "cursor_spend_usd": cursor,
            },
        }

    def test_no_spend_series_on_repo_page(self):
        """Health JSON with litellm_spend_usd and cursor_spend_usd must not produce spend series in HTML."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            (base_dir / "products" / "testprod").mkdir(parents=True)
            (base_dir / "products" / "testprod" / "2026-01_repo-a.json").write_text(
                json.dumps(self._make_metrics_json())
            )
            (base_dir / "products" / "testprod" / "health-2026-01_repo-a.json").write_text(
                json.dumps(self._make_health_json(litellm=999.0, cursor=25.0))
            )
            html = render_repo_page("repo-a", "testprod", "Test Prod", base_dir, ["2026-01"])

        self.assertNotIn("LiteLLM Spend (USD)", html)
        self.assertNotIn("Cursor Spend (USD)", html)

    def test_ai_adoption_chart_single_axis_on_repo_page(self):
        """AI Adoption chart on repo page must be single-axis (no dual-axis chart call)."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            (base_dir / "products" / "testprod").mkdir(parents=True)
            (base_dir / "products" / "testprod" / "2026-01_repo-a.json").write_text(
                json.dumps(self._make_metrics_json())
            )
            (base_dir / "products" / "testprod" / "health-2026-01_repo-a.json").write_text(
                json.dumps(self._make_health_json())
            )
            html = render_repo_page("repo-a", "testprod", "Test Prod", base_dir, ["2026-01"])

        # Single-axis chart does not define a y1 axis; dual-axis does
        # The chart-rai-ai-assisted-commit-rate canvas must be rendered (has data)
        self.assertIn('id="chart-rai-ai-assisted-commit-rate"', html)
        self.assertNotIn("Coming soon", html)
        # Dual-axis charts define a 'y1' scale in the Chart options object.
        # Single-axis charts do not. Find the <script> block that owns chart-rai-ai-assisted-commit-rate
        # and confirm no y1 scale is defined there.
        ai_scripts = [
            b
            for b in re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
            if "chart-rai-ai-assisted-commit-rate" in b
        ]
        self.assertEqual(len(ai_scripts), 1, "Expected exactly one script block for chart-rai-ai-assisted-commit-rate")
        # 'y1' as a scale key looks like: scales: { ... y1: { — check for that pattern
        self.assertNotIn('"y1":', ai_scripts[0])

    def test_no_spend_series_but_chart_still_renders_on_repo_page(self):
        """Health JSON with spend values must not produce spend datasets — chart renders AI commits only."""
        with tempfile.TemporaryDirectory() as base:
            base_dir = Path(base)
            (base_dir / "products" / "testprod").mkdir(parents=True)
            (base_dir / "products" / "testprod" / "2026-01_repo-a.json").write_text(
                json.dumps(self._make_metrics_json())
            )
            (base_dir / "products" / "testprod" / "health-2026-01_repo-a.json").write_text(
                json.dumps(self._make_health_json(litellm=10.0, cursor=25.0))
            )
            html = render_repo_page("repo-a", "testprod", "Test Prod", base_dir, ["2026-01"])

        # Spend series labels must NOT appear
        self.assertNotIn("Cursor Spend (USD)", html)
        self.assertNotIn("LiteLLM Spend (USD)", html)
        # The AI commits chart must still render (not "Coming soon")
        self.assertIn('id="chart-rai-ai-assisted-commit-rate"', html)
        self.assertNotIn("Coming soon", html)


if __name__ == "__main__":
    unittest.main()
