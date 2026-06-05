"""Unit tests for score-epi.py scoring engine."""

import unittest
from pathlib import Path

from epi.bot import is_bot
from epi.config import load_repos_config
from epi.scoring import (
    METRIC_BANDS,
    _previous_month,
    extract_metric_values,
    generate_board_report,
    generate_product_report,
    load_git_metrics,
    load_manual_input,
    score_metric,
    score_product,
    score_repos,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class TestScoreMetric(unittest.TestCase):
    """Test score_metric band boundaries and interpolation."""

    def test_higher_is_better_elite(self):
        # deployment_frequency: Elite band is 8-20
        result = score_metric("deployment_frequency", 10)
        self.assertEqual(result["band"], "Elite")
        self.assertGreaterEqual(result["score"], 90)
        self.assertLessEqual(result["score"], 100)

    def test_higher_is_better_concerning(self):
        result = score_metric("deployment_frequency", 0.5)
        self.assertEqual(result["band"], "Concerning")
        self.assertLess(result["score"], 50)

    def test_lower_is_better_elite(self):
        # change_failure_rate: Elite is 0-5%
        result = score_metric("change_failure_rate", 2)
        self.assertEqual(result["band"], "Elite")
        self.assertGreaterEqual(result["score"], 90)

    def test_lower_is_better_concerning(self):
        result = score_metric("change_failure_rate", 50)
        self.assertEqual(result["band"], "Concerning")
        self.assertLess(result["score"], 50)

    def test_band_boundary_exact(self):
        # At boundary between Concerning and Acceptable for deployment_frequency
        # min_val=1 is the start of Acceptable band
        result = score_metric("deployment_frequency", 1)
        self.assertIn(result["band"], ("Concerning", "Acceptable"))

    def test_unknown_metric(self):
        result = score_metric("nonexistent_metric", 42)
        self.assertIsNone(result["score"])
        self.assertEqual(result["band"], "Unknown")

    def test_interpolation_midpoint(self):
        # mrs_per_engineer Happy band: 4-8, scores 70-89
        result = score_metric("mrs_per_engineer", 6)
        self.assertEqual(result["band"], "Happy")
        self.assertGreater(result["score"], 75)
        self.assertLess(result["score"], 85)

    def test_value_above_max(self):
        # deployment_frequency above Elite max (20) should clamp to 100
        result = score_metric("deployment_frequency", 25)
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["band"], "Elite")

    def test_lower_is_better_above_max(self):
        # change_failure_rate above 100 should clamp to 0
        result = score_metric("change_failure_rate", 120)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["band"], "Concerning")


class TestScoreProduct(unittest.TestCase):
    """Test score_product with fixture data."""

    @classmethod
    def setUpClass(cls):
        cls.repos_config = load_repos_config(FIXTURES)

    def test_score_product_full(self):
        """Alpha with 2025-12 has git + manual data: per-metric results should be populated."""
        result = score_product("alpha", "2025-12", FIXTURES, self.repos_config)
        self.assertNotIn("error", result)
        self.assertIn("metrics", result)

        # All expected metrics should have scores in valid range
        for metric_name, m in result["metrics"].items():
            score = m.get("score")
            self.assertIsNotNone(score, f"Metric {metric_name} should have a score")
            self.assertGreaterEqual(score, 0)
            self.assertLessEqual(score, 100)

    def test_score_product_sparse(self):
        """Beta with only git data (no manual input) should still score what it can."""
        result = score_product("beta", "2025-12", FIXTURES, self.repos_config)
        self.assertIn("metrics", result)

        # Some metrics that come from manual input should be absent from metrics dict
        metrics = result.get("metrics", {})
        self.assertNotIn("deployment_frequency", metrics)
        self.assertNotIn("lead_time_days", metrics)
        self.assertNotIn("mttr_hours", metrics)

        # Git-derived metrics should be present
        raw = result.get("raw_metrics", {})
        self.assertIsNotNone(raw.get("mrs_per_engineer"))
        self.assertIsNotNone(raw.get("rework_rate"))

    def test_score_product_prev_month_comparison(self):
        """Alpha has 2025-11 per-repo data; metrics should have prev_value and prev_band populated."""
        result = score_product("alpha", "2025-12", FIXTURES, self.repos_config)
        # 2025-11 has per-repo git data — at least git-derived metrics should have prev_value
        metrics = result.get("metrics", {})
        git_metrics_with_prev = [name for name, m in metrics.items() if m.get("prev_value") is not None]
        self.assertGreater(
            len(git_metrics_with_prev),
            0,
            "At least one metric should have prev_value from 2025-11 data",
        )
        # Verify prev_value and prev_band are both present on those metrics
        for name in git_metrics_with_prev:
            m = metrics[name]
            self.assertIn("prev_band", m, f"prev_band missing from metrics['{name}']")
            self.assertIsNotNone(m["prev_band"])

    def test_score_product_no_data(self):
        """Nonexistent product should return error."""
        result = score_product("nonexistent", "2025-12", FIXTURES, self.repos_config)
        self.assertIn("error", result)

    def test_score_product_repos(self):
        """Alpha should have per-repo scoring data including support repos."""
        result = score_product("alpha", "2025-12", FIXTURES, self.repos_config)
        repos = result.get("repos", [])
        self.assertEqual(len(repos), 2)  # repo-a + repo-c (support)
        repo_names = [r["name"] for r in repos]
        self.assertIn("repo-a", repo_names)
        self.assertIn("repo-c", repo_names)
        for r in repos:
            self.assertFalse(r["inactive"])
        # Verify category field
        repo_a = next(r for r in repos if r["name"] == "repo-a")
        repo_c = next(r for r in repos if r["name"] == "repo-c")
        self.assertEqual(repo_a["category"], "product")
        self.assertEqual(repo_c["category"], "support")

    def test_metric_bands_valid(self):
        """All scored metric bands should be recognizable."""
        valid_bands = {"Elite", "Happy", "Acceptable", "Concerning", "N/A"}
        result = score_product("alpha", "2025-12", FIXTURES, self.repos_config)
        for metric_name, m in result.get("metrics", {}).items():
            self.assertIn(m["band"], valid_bands, f"Unexpected band on {metric_name}: {m['band']}")

    def test_score_product_no_epi_key(self):
        """score_product() result must not contain an 'epi' key (composite retired)."""
        result = score_product("alpha", "2025-12", FIXTURES, self.repos_config)
        self.assertNotIn("epi", result)

    def test_score_product_no_components_key(self):
        """score_product() result must not contain a 'components' key (composite retired)."""
        result = score_product("alpha", "2025-12", FIXTURES, self.repos_config)
        self.assertNotIn("components", result)

    def test_score_product_metrics_shape(self):
        """result['metrics'] must contain expected keys each with value, band, prev_value, prev_band."""
        result = score_product("alpha", "2025-12", FIXTURES, self.repos_config)
        self.assertIn("metrics", result)
        metrics = result["metrics"]
        expected_keys = {
            "deployment_frequency",
            "mrs_per_engineer",
            "rework_rate",
            "bus_factor",
            "knowledge_distribution",
            "commit_intentionality",
        }
        for key in expected_keys:
            self.assertIn(key, metrics, f"Expected metric key '{key}' in result['metrics']")
            m = metrics[key]
            self.assertIn("value", m, f"'value' missing from metrics['{key}']")
            self.assertIn("band", m, f"'band' missing from metrics['{key}']")
            self.assertIn("prev_value", m, f"'prev_value' missing from metrics['{key}']")
            self.assertIn("prev_band", m, f"'prev_band' missing from metrics['{key}']")


class TestMarkdownReports(unittest.TestCase):
    """Test generate_product_report and generate_board_report output."""

    @classmethod
    def setUpClass(cls):
        cls.repos_config = load_repos_config(FIXTURES)
        cls.result_alpha = score_product("alpha", "2025-12", FIXTURES, cls.repos_config)
        cls.result_beta = score_product("beta", "2025-12", FIXTURES, cls.repos_config)

    def test_generate_product_report_no_composite(self):
        """Product markdown must not contain 'EPI Score' literal (composite retired)."""
        report = generate_product_report(self.result_alpha)
        self.assertNotIn("EPI Score", report)

    def test_generate_board_report_no_composite(self):
        """Board markdown must not contain 'Company Average' or an 'EPI' column header."""
        report = generate_board_report([self.result_alpha, self.result_beta], "2025-12")
        self.assertNotIn("Company Average", report)
        # Ensure there is no standalone EPI column header (pipe-delimited table column)
        self.assertNotIn("| EPI |", report)

    def test_metric_band_legend_included(self):
        """Product markdown must contain at least one band threshold value from METRIC_BANDS."""
        report = generate_product_report(self.result_alpha)
        # Check that at least one concrete threshold value appears (e.g. "Elite" boundary values)
        # deployment_frequency Elite min_val is 8 — should appear as a threshold in the legend
        found = any(
            str(band["min_val"]) in report or str(band["max_val"]) in report
            for bands_config in METRIC_BANDS.values()
            for band in bands_config["bands"]
        )
        self.assertTrue(found, "Expected band threshold values from METRIC_BANDS in the product report")


class TestCycleCarryForward(unittest.TestCase):
    """Test cycle data carry-forward across months."""

    @classmethod
    def setUpClass(cls):
        cls.repos_config = load_repos_config(FIXTURES)
        cls.alpha_dir = FIXTURES / "products" / "alpha"

    def test_no_carry_forward_when_cycle_data_present(self):
        """2025-12 has cycle data — should use it directly, no carry-forward."""
        data = load_manual_input(self.alpha_dir, "2025-12")
        self.assertEqual(data["cycle_items_committed"], 18)
        self.assertEqual(data["cycle_items_delivered"], 15)
        self.assertEqual(data.get("cycle"), "C1")
        self.assertFalse(data.get("cycle_carried_forward", False))

    def test_carry_forward_from_previous_month(self):
        """2026-01 has no cycle data — should carry forward from 2025-12."""
        data = load_manual_input(self.alpha_dir, "2026-01")
        self.assertEqual(data["cycle_items_committed"], 18)
        self.assertEqual(data["cycle_items_delivered"], 15)
        self.assertEqual(data.get("cycle"), "C1")
        self.assertTrue(data.get("cycle_carried_forward"))

    def test_carry_forward_does_not_overwrite_monthly_metrics(self):
        """Carry-forward should only bring cycle fields, not monthly metrics."""
        data = load_manual_input(self.alpha_dir, "2026-01")
        # 2026-01's own deployment_frequency should remain
        self.assertEqual(data["deployment_frequency"], 5)
        # Not the 2025-12 value of 6
        self.assertNotEqual(data["deployment_frequency"], 6)

    def test_cycle_info_in_score_product(self):
        """score_product should include cycle_info in output."""
        result = score_product("alpha", "2026-01", FIXTURES, self.repos_config)
        self.assertIn("cycle_info", result)
        ci = result["cycle_info"]
        self.assertEqual(ci["cycle"], "C1")
        self.assertTrue(ci["carried_forward"])

    def test_cycle_delivery_accuracy_scored_with_carry_forward(self):
        """Carried-forward cycle data should produce a cycle_delivery_accuracy score."""
        result = score_product("alpha", "2026-01", FIXTURES, self.repos_config)
        raw = result.get("raw_metrics", {})
        self.assertIsNotNone(raw.get("cycle_delivery_accuracy"))
        self.assertAlmostEqual(raw["cycle_delivery_accuracy"], 83.3, places=1)

    def test_no_carry_forward_beyond_2_months(self):
        """If no cycle data within 2 months back, no carry-forward should happen."""
        # 2025-10 has no manual file, 2025-11 has no manual file
        # (only 2025-12 has one) so 2025-10 cannot reach any cycle data
        data = load_manual_input(self.alpha_dir, "2025-10")
        self.assertIsNone(data.get("cycle_items_committed"))
        self.assertFalse(data.get("cycle_carried_forward", False))


class TestLoadGitMetrics(unittest.TestCase):
    """Test load_git_metrics loading and multi-repo aggregation."""

    def test_single_repo(self):
        """Load single-repo from fixture — verify key fields."""
        product_dir = FIXTURES / "products" / "beta"
        data = load_git_metrics(product_dir, "2025-12")
        self.assertEqual(data["commits"]["total"], 15)
        self.assertEqual(data["bus_factor"]["value"], 1)
        self.assertIn("percentage", data["rework_rate"])
        self.assertIn("percentage", data["knowledge_distribution"])

    def test_multi_repo_aggregation(self):
        """repo-c is support (excluded from EPI aggregation), only repo-a contributes."""
        product_dir = FIXTURES / "products" / "alpha"
        data = load_git_metrics(product_dir, "2025-12")
        # repo-a only (repo-c is category=support, excluded)
        self.assertEqual(data["commits"]["total"], 120)
        self.assertEqual(data["tickets"]["unique_count"], 22)
        self.assertEqual(data["bus_factor"]["value"], 5)
        self.assertEqual(data["mrs"]["total"], 45)

    def test_multi_repo_knowledge_dist(self):
        """Knowledge distribution: only product repos aggregated (repo-c is support)."""
        product_dir = FIXTURES / "products" / "alpha"
        data = load_git_metrics(product_dir, "2025-12")
        kd = data["knowledge_distribution"]
        # repo-a only (repo-c excluded as support)
        self.assertEqual(kd["multi_contributor_modules"], 15)
        self.assertEqual(kd["total_modules"], 20)

    def test_no_data(self):
        """Nonexistent directory returns empty dict."""
        data = load_git_metrics(FIXTURES / "products" / "nonexistent", "2025-12")
        self.assertEqual(data, {})

    def test_rework_rate_present(self):
        """Rework rate is computed for aggregated data."""
        product_dir = FIXTURES / "products" / "alpha"
        data = load_git_metrics(product_dir, "2025-12")
        rr = data.get("rework_rate", {})
        self.assertIn("percentage", rr)
        self.assertGreater(rr["percentage"], 0)


class TestExtractMetricValues(unittest.TestCase):
    """Test extract_metric_values with various data combinations."""

    def _make_git_data(self, **overrides):
        base = {
            "commits": {"total": 100, "active_contributors": 5},
            "tickets": {"unique_count": 15, "ids": []},
            "mrs": {"total": 30},
            "rework_rate": {"percentage": 20.0, "feature_plus_bugfix": 40},
            "bus_factor": {"value": 3, "total_trailing_commits": 300},
            "knowledge_distribution": {"percentage": 65.0, "total_modules": 10},
        }
        base.update(overrides)
        return base

    def _make_manual_data(self, **overrides):
        base = {
            "deployment_frequency": 6,
            "features_shipped": 20,
            "rollbacks": 1,
            "post_release_defects": 3,
            "lead_time_median_days": 5.0,
            "cycle_items_committed": 18,
            "cycle_items_delivered": 15,
            "mttr_hours": 2.5,
        }
        base.update(overrides)
        return base

    def test_full_data_all_metrics(self):
        """Full git + manual data: all 12 metrics non-None."""
        metrics, _ = extract_metric_values(self._make_git_data(), self._make_manual_data())
        for key in (
            "deployment_frequency",
            "features_shipped",
            "change_failure_rate",
            "post_release_defect_rate",
            "rework_rate",
            "lead_time_days",
            "cycle_delivery_accuracy",
            "mttr_hours",
            "mrs_per_engineer",
            "bus_factor",
            "knowledge_distribution",
        ):
            self.assertIsNotNone(metrics[key], f"{key} should not be None")

    def test_git_only_manual_metrics_none(self):
        """Git-only (no manual): deploy, lead_time, mttr are None."""
        metrics, _ = extract_metric_values(self._make_git_data(), {})
        self.assertIsNone(metrics["deployment_frequency"])
        self.assertIsNone(metrics["lead_time_days"])
        self.assertIsNone(metrics["mttr_hours"])
        self.assertIsNotNone(metrics["mrs_per_engineer"])
        self.assertIsNotNone(metrics["rework_rate"])

    def test_change_failure_rate_derived(self):
        """change_failure_rate = rollbacks / deploy_freq * 100."""
        metrics, _ = extract_metric_values(
            self._make_git_data(),
            self._make_manual_data(deployment_frequency=10, rollbacks=2),
        )
        self.assertAlmostEqual(metrics["change_failure_rate"], 20.0, places=1)

    def test_post_release_defect_rate_derived(self):
        """post_release_defect_rate = defects / features * 100."""
        metrics, _ = extract_metric_values(
            self._make_git_data(),
            self._make_manual_data(features_shipped=50, post_release_defects=5),
        )
        self.assertAlmostEqual(metrics["post_release_defect_rate"], 10.0, places=1)

    def test_cycle_delivery_accuracy_derived(self):
        """cycle_delivery_accuracy = delivered / committed * 100."""
        metrics, _ = extract_metric_values(
            self._make_git_data(),
            self._make_manual_data(cycle_items_committed=20, cycle_items_delivered=17),
        )
        self.assertAlmostEqual(metrics["cycle_delivery_accuracy"], 85.0, places=1)

    def test_confidence_small_sample(self):
        """3 committed items → confidence = 0.2."""
        _, confidence = extract_metric_values(
            self._make_git_data(),
            self._make_manual_data(cycle_items_committed=3, cycle_items_delivered=2),
        )
        self.assertAlmostEqual(confidence["cycle_delivery_accuracy"], 0.2, places=2)

    def test_zero_deploy_freq_no_division_error(self):
        """Zero deploy_freq → change_failure_rate is None."""
        metrics, _ = extract_metric_values(
            self._make_git_data(),
            self._make_manual_data(deployment_frequency=0, rollbacks=1),
        )
        self.assertIsNone(metrics["change_failure_rate"])

    def test_null_rollbacks_with_known_deploy_freq_is_zero_cfr(self):
        """rollbacks=null + deploy_freq>0 → change_failure_rate=0.0 (no rollbacks means 0%)."""
        metrics, _ = extract_metric_values(
            self._make_git_data(),
            self._make_manual_data(deployment_frequency=4, rollbacks=None),
        )
        self.assertAlmostEqual(metrics["change_failure_rate"], 0.0, places=1)

    def test_null_deploy_freq_still_yields_null_cfr(self):
        """rollbacks=null + deploy_freq=null → change_failure_rate=None (can't compute)."""
        metrics, _ = extract_metric_values(
            self._make_git_data(),
            self._make_manual_data(deployment_frequency=None, rollbacks=None),
        )
        self.assertIsNone(metrics["change_failure_rate"])

    def test_features_fallback_to_tickets(self):
        """No manual features_shipped + git tickets → uses ticket count."""
        manual = self._make_manual_data()
        del manual["features_shipped"]
        metrics, _ = extract_metric_values(self._make_git_data(), manual)
        self.assertEqual(metrics["features_shipped"], 15)  # from git tickets


class TestPreviousMonth(unittest.TestCase):
    """Test _previous_month helper."""

    def test_normal_month(self):
        self.assertEqual(_previous_month("2026-03"), "2026-02")

    def test_january_wraps_to_december(self):
        self.assertEqual(_previous_month("2026-01"), "2025-12")

    def test_december(self):
        self.assertEqual(_previous_month("2025-12"), "2025-11")


class TestIsBot(unittest.TestCase):
    """Test is_bot() bot detection function."""

    def test_exact_match_bots(self):
        for name in (
            "jenkins",
            "devops",
            "prodops",
            "ghost",
            "root",
            "ops",
            "ci.cd",
            "hammertime",
            "gitlab.jira",
            "--global",
        ):
            self.assertTrue(is_bot(name), f"{name!r} should be detected as a bot")

    def test_exact_match_case_insensitive(self):
        self.assertTrue(is_bot("Jenkins"))
        self.assertTrue(is_bot("DEVOPS"))
        self.assertTrue(is_bot("Ghost"))

    def test_pattern_bots(self):
        self.assertTrue(is_bot("project_42_bot_deploy"))
        self.assertTrue(is_bot("group_7_bot_cicd"))
        self.assertTrue(is_bot("123+dependabot[bot]"))
        self.assertTrue(is_bot("pulsar.service"))

    def test_human_names_not_flagged(self):
        for name in ("alice", "bob", "carol", "dave", "eve", "frank", "jan.novak", "maria.garcia", "stephan.westen"):
            self.assertFalse(is_bot(name), f"{name!r} should NOT be detected as a bot")

    def test_edge_cases(self):
        # Names that contain 'bot' but aren't bots
        self.assertFalse(is_bot("abbott"))
        self.assertFalse(is_bot("robotics_engineer"))


class TestBotFiltering(unittest.TestCase):
    """Test bot filtering in load_git_metrics and score pipeline."""

    def test_load_git_metrics_dual_counts(self):
        """Gamma fixture has 4 bots — human count should be lower than total."""
        product_dir = FIXTURES / "products" / "gamma"
        data = load_git_metrics(product_dir, "2025-12")

        all_count = data["commits"]["active_contributors"]
        human_count = data["commits"]["active_contributors_human"]
        bot_accounts = data["commits"]["bot_accounts"]

        self.assertEqual(all_count, 8)
        self.assertEqual(human_count, 4)
        self.assertEqual(len(bot_accounts), 4)
        self.assertIn("jenkins", bot_accounts)
        self.assertIn("prodops", bot_accounts)
        self.assertIn("ghost", bot_accounts)
        # project_42_bot_deploy matches pattern
        self.assertTrue(any("project_42" in b for b in bot_accounts))

    def test_per_contributor_human_excludes_bots(self):
        """per_contributor_human should not contain bot accounts."""
        product_dir = FIXTURES / "products" / "gamma"
        data = load_git_metrics(product_dir, "2025-12")

        human_contribs = data["commits"]["per_contributor_human"]
        self.assertNotIn("jenkins", human_contribs)
        self.assertNotIn("prodops", human_contribs)
        self.assertIn("alice", human_contribs)
        self.assertIn("bob", human_contribs)

    def test_mrs_per_engineer_dual_values(self):
        """MRs/engineer should differ between human-only and all-inclusive."""
        product_dir = FIXTURES / "products" / "gamma"
        data = load_git_metrics(product_dir, "2025-12")

        per_eng_all = data["mrs"]["per_engineer"]
        per_eng_human = data["mrs"]["per_engineer_human"]

        # 40 MRs / 8 contributors = 5.0
        self.assertAlmostEqual(per_eng_all, 5.0, places=1)
        # 40 MRs / 4 human contributors = 10.0
        self.assertAlmostEqual(per_eng_human, 10.0, places=1)

    def test_extract_metric_uses_human_count(self):
        """extract_metric_values should use human-only count for mrs_per_engineer."""
        product_dir = FIXTURES / "products" / "gamma"
        git_data = load_git_metrics(product_dir, "2025-12")

        metrics, _ = extract_metric_values(git_data, {})

        # Should use human count (4) not all (8)
        # 40 MRs / 4 humans = 10.0
        self.assertAlmostEqual(metrics["mrs_per_engineer"], 10.0, places=1)
        # All-inclusive value should also be present
        self.assertAlmostEqual(metrics["mrs_per_engineer_all"], 5.0, places=1)

    def test_no_bots_produces_equal_counts(self):
        """Alpha fixture has no bots — human count should equal total count."""
        product_dir = FIXTURES / "products" / "alpha"
        data = load_git_metrics(product_dir, "2025-12")

        all_count = data["commits"]["active_contributors"]
        human_count = data["commits"]["active_contributors_human"]
        bot_accounts = data["commits"]["bot_accounts"]

        self.assertEqual(all_count, human_count)
        self.assertEqual(len(bot_accounts), 0)

    def test_score_repos_includes_bot_fields(self):
        """score_repos should include active_contributors_human and bot_accounts."""
        repos_config = load_repos_config(FIXTURES)
        result = score_product("gamma", "2025-12", FIXTURES, repos_config)
        repos = result.get("repos", [])
        self.assertEqual(len(repos), 1)
        repo = repos[0]
        self.assertIn("active_contributors_human", repo)
        self.assertIn("bot_accounts", repo)
        self.assertEqual(repo["active_contributors"], 8)
        self.assertEqual(repo["active_contributors_human"], 4)


class TestReworkRateCommitLevel(unittest.TestCase):
    """Test that rework_rate always uses commit-level classification."""

    @classmethod
    def setUpClass(cls):
        cls.repos_config = load_repos_config(FIXTURES)
        cls.product_dir = FIXTURES / "products" / "alpha"

    def test_commit_level_used_when_mr_has_no_features(self):
        """Fixture has mr_classification(feature=0, bugfix=5) → MR-based gives 100%.
        Commit-based gives feature=50, bugfix=10 → 16.7%. Commit-level must win."""
        data = load_git_metrics(self.product_dir, "2025-12")
        rr = data["rework_rate"]
        self.assertAlmostEqual(rr["percentage"], 16.7, places=1)

    def test_rework_rate_source_is_commit(self):
        """rework_rate source must always be 'commit', even when mr_classification is present."""
        data = load_git_metrics(self.product_dir, "2025-12")
        self.assertEqual(data["rework_rate"]["source"], "commit")

    def test_bot_heavy_repo_correct_rework(self):
        """Gamma fixture: maintenance commits (mostly from bots) don't inflate the denominator.
        feature=40, bugfix=15 → rework = 15/55 = 27.3%, feature_plus_bugfix = 55."""
        product_dir = FIXTURES / "products" / "gamma"
        data = load_git_metrics(product_dir, "2025-12")
        rr = data["rework_rate"]
        self.assertAlmostEqual(rr["percentage"], 27.3, places=1)
        self.assertEqual(rr["feature_plus_bugfix"], 55)
        self.assertEqual(rr["source"], "commit")


class TestMrsBackwardsCompat(unittest.TestCase):
    """Verify that old JSON files with 'mrs_merged' key still load correctly."""

    def test_legacy_mrs_merged_key_loads(self):
        """score-epi falls back to mrs_merged when mrs key is absent."""
        git_data = {
            "commits": {"total": 100, "active_contributors": 5, "active_contributors_human": 5},
            "tickets": {"unique_count": 15, "ids": []},
            "mrs_merged": {"total": 30},  # old key name
            "rework_rate": {"percentage": 20.0, "feature_plus_bugfix": 40},
            "bus_factor": {"value": 3, "total_trailing_commits": 300},
            "knowledge_distribution": {"percentage": 65.0, "total_modules": 10},
        }
        metrics, _ = extract_metric_values(git_data, {})
        self.assertAlmostEqual(metrics["mrs_per_engineer"], 6.0, places=1)


class TestCategoryFiltering(unittest.TestCase):
    """Test that support repos are excluded from aggregation but appear in repo list."""

    @classmethod
    def setUpClass(cls):
        cls.repos_config = load_repos_config(FIXTURES)

    def test_support_repo_excluded_from_aggregation(self):
        """load_git_metrics should exclude repos with category=support."""
        product_dir = FIXTURES / "products" / "alpha"
        data = load_git_metrics(product_dir, "2025-12")
        # Only repo-a (product) should contribute — 120 commits, not 150
        self.assertEqual(data["commits"]["total"], 120)

    def test_support_repo_in_score_repos(self):
        """score_repos should include support repos with correct category."""
        repos = score_repos("alpha", "2025-12", FIXTURES)
        names = {r["name"]: r["category"] for r in repos}
        self.assertEqual(names["repo-a"], "product")
        self.assertEqual(names["repo-c"], "support")

    def test_product_without_support_repos(self):
        """Beta has no support_repos — all repos treated as product."""
        product_dir = FIXTURES / "products" / "beta"
        data = load_git_metrics(product_dir, "2025-12")
        self.assertEqual(data["commits"]["total"], 15)  # repo-b is product (default)

    def test_default_category_is_product(self):
        """Repos without explicit category in JSON default to product."""
        repos = score_repos("beta", "2025-12", FIXTURES)
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]["category"], "product")


class TestCommitIntentionality(unittest.TestCase):
    """Test commit_intentionality metric computation and scoring."""

    def _git_data_with_classification(self, **cc_overrides):
        cc = {"feature": 20, "bugfix": 5, "test": 5, "tooling": 2, "maintenance": 10, "docs": 1, "other": 7}
        cc.update(cc_overrides)
        return {
            "commits": {"total": sum(cc.values()), "active_contributors": 3, "active_contributors_human": 3},
            "tickets": {"unique_count": 10, "ids": []},
            "mrs": {"total": 15},
            "rework_rate": {"percentage": 20.0, "feature_plus_bugfix": 25},
            "bus_factor": {"value": 2, "total_trailing_commits": 200},
            "knowledge_distribution": {"percentage": 60.0, "total_modules": 5},
            "commit_classification": cc,
        }

    def test_commit_intentionality_computed(self):
        """(feature + bugfix + test) / total * 100."""
        # feature=20, bugfix=5, test=5 -> intentional=30 out of 50 total = 60%
        git_data = self._git_data_with_classification()
        metrics, _ = extract_metric_values(git_data, {})
        self.assertAlmostEqual(metrics["commit_intentionality"], 60.0, places=1)

    def test_commit_intentionality_no_classification(self):
        """No commit_classification → metric is None."""
        git_data = {
            "commits": {"total": 50, "active_contributors": 3, "active_contributors_human": 3},
            "tickets": {"unique_count": 10, "ids": []},
            "mrs": {"total": 15},
            "rework_rate": {"percentage": 20.0, "feature_plus_bugfix": 25},
            "bus_factor": {"value": 2, "total_trailing_commits": 200},
            "knowledge_distribution": {"percentage": 60.0, "total_modules": 5},
        }
        metrics, _ = extract_metric_values(git_data, {})
        self.assertIsNone(metrics["commit_intentionality"])

    def test_commit_intentionality_all_other(self):
        """All 'other' commits → 0% intentionality → Concerning band."""
        git_data = self._git_data_with_classification(
            feature=0, bugfix=0, test=0, other=50, tooling=0, maintenance=0, docs=0
        )
        metrics, _ = extract_metric_values(git_data, {})
        self.assertAlmostEqual(metrics["commit_intentionality"], 0.0, places=1)
        scored = score_metric("commit_intentionality", 0.0)
        self.assertEqual(scored["band"], "Concerning")

    def test_commit_intentionality_elite_band(self):
        """≥ 55% intentional → Elite band."""
        scored = score_metric("commit_intentionality", 60.0)
        self.assertEqual(scored["band"], "Elite")

    def test_commit_intentionality_happy_band(self):
        """40-55% intentional -> Happy band."""
        scored = score_metric("commit_intentionality", 47.0)
        self.assertEqual(scored["band"], "Happy")

    def test_commit_intentionality_acceptable_band(self):
        """25-40% intentional -> Acceptable band."""
        scored = score_metric("commit_intentionality", 33.0)
        self.assertEqual(scored["band"], "Acceptable")

    def test_commit_intentionality_in_metric_bands(self):
        """commit_intentionality must have a bands entry."""
        self.assertIn("commit_intentionality", METRIC_BANDS)


class TestIsManualFlag(unittest.TestCase):
    """Test that per-metric dicts carry the is_manual flag."""

    @classmethod
    def setUpClass(cls):
        cls.repos_config = load_repos_config(FIXTURES)
        cls.result = score_product("alpha", "2025-12", FIXTURES, cls.repos_config)

    def _all_metrics(self):
        yield from self.result["metrics"].items()

    def test_is_manual_present_on_all_metrics(self):
        """Every metric dict must have an is_manual key."""
        for name, mdata in self._all_metrics():
            self.assertIn("is_manual", mdata, f"is_manual missing on {name}")

    def test_manual_metrics_flagged_true(self):
        """Known manual-source metrics must have is_manual=True."""
        manual_metrics = {
            "deployment_frequency",
            "change_failure_rate",
            "post_release_defect_rate",
            "lead_time_days",
            "cycle_delivery_accuracy",
            "mttr_hours",
        }
        for name, mdata in self._all_metrics():
            if name in manual_metrics:
                self.assertTrue(mdata["is_manual"], f"{name} should be is_manual=True")

    def test_git_metrics_flagged_false(self):
        """Git-sourced metrics must have is_manual=False."""
        git_metrics = {
            "mrs_per_engineer",
            "rework_rate",
            "bus_factor",
            "knowledge_distribution",
            "commit_intentionality",
        }
        for name, mdata in self._all_metrics():
            if name in git_metrics:
                self.assertFalse(mdata["is_manual"], f"{name} should be is_manual=False")


class TestCycleInfoCommittedDelivered(unittest.TestCase):
    """Test that cycle_info exposes committed and delivered counts."""

    @classmethod
    def setUpClass(cls):
        cls.repos_config = load_repos_config(FIXTURES)

    def test_cycle_info_has_committed_and_delivered(self):
        """cycle_info must include committed and delivered when manual YAML has them."""
        result = score_product("alpha", "2025-12", FIXTURES, self.repos_config)
        ci = result["cycle_info"]
        self.assertIn("committed", ci)
        self.assertIn("delivered", ci)
        self.assertEqual(ci["committed"], 18)
        self.assertEqual(ci["delivered"], 15)

    def test_cycle_info_none_when_no_manual_data(self):
        """committed and delivered must be None when no manual YAML exists."""
        result = score_product("beta", "2025-12", FIXTURES, self.repos_config)
        ci = result["cycle_info"]
        self.assertIsNone(ci["committed"])
        self.assertIsNone(ci["delivered"])


if __name__ == "__main__":
    unittest.main()
