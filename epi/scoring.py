from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from epi.bot import is_bot
from epi.config import load_repos_config

# ─── EPI BAND DEFINITIONS ─────────────────────────────────────────────────────
#
# Each metric has bands: Elite (90-100), Happy (70-89), Acceptable (50-69), Concerning (0-49)
# Bands are defined as (lower_threshold, upper_threshold) for the metric value.
# "direction" indicates whether higher is better ("higher") or lower is better ("lower").

METRIC_BANDS = {
    # Delivery Velocity
    "deployment_frequency": {
        "direction": "higher",
        "bands": [
            {"name": "Concerning", "min_val": 0, "max_val": 1, "min_score": 0, "max_score": 49},
            {"name": "Acceptable", "min_val": 1, "max_val": 4, "min_score": 50, "max_score": 69},
            {"name": "Happy", "min_val": 4, "max_val": 8, "min_score": 70, "max_score": 89},
            {"name": "Elite", "min_val": 8, "max_val": 20, "min_score": 90, "max_score": 100},
        ],
    },
    "mrs_per_engineer": {
        "direction": "higher",
        "bands": [
            {"name": "Concerning", "min_val": 0, "max_val": 2, "min_score": 0, "max_score": 49},
            {"name": "Acceptable", "min_val": 2, "max_val": 4, "min_score": 50, "max_score": 69},
            {"name": "Happy", "min_val": 4, "max_val": 8, "min_score": 70, "max_score": 89},
            {"name": "Elite", "min_val": 8, "max_val": 16, "min_score": 90, "max_score": 100},
        ],
    },
    "features_shipped": {
        # Trend metric — scored as count, but thresholds are product-relative
        "direction": "higher",
        "bands": [
            {"name": "Concerning", "min_val": 0, "max_val": 5, "min_score": 0, "max_score": 49},
            {"name": "Acceptable", "min_val": 5, "max_val": 15, "min_score": 50, "max_score": 69},
            {"name": "Happy", "min_val": 15, "max_val": 30, "min_score": 70, "max_score": 89},
            {"name": "Elite", "min_val": 30, "max_val": 60, "min_score": 90, "max_score": 100},
        ],
    },
    # Delivery Quality
    "change_failure_rate": {
        "direction": "lower",  # Lower is better
        "bands": [
            {"name": "Elite", "min_val": 0, "max_val": 5, "min_score": 90, "max_score": 100},
            {"name": "Happy", "min_val": 5, "max_val": 15, "min_score": 70, "max_score": 89},
            {"name": "Acceptable", "min_val": 15, "max_val": 30, "min_score": 50, "max_score": 69},
            {"name": "Concerning", "min_val": 30, "max_val": 100, "min_score": 0, "max_score": 49},
        ],
    },
    "post_release_defect_rate": {
        "direction": "lower",
        "bands": [
            {"name": "Elite", "min_val": 0, "max_val": 10, "min_score": 90, "max_score": 100},
            {"name": "Happy", "min_val": 10, "max_val": 20, "min_score": 70, "max_score": 89},
            {"name": "Acceptable", "min_val": 20, "max_val": 35, "min_score": 50, "max_score": 69},
            {"name": "Concerning", "min_val": 35, "max_val": 100, "min_score": 0, "max_score": 49},
        ],
    },
    "rework_rate": {
        "direction": "lower",
        "bands": [
            {"name": "Elite", "min_val": 0, "max_val": 15, "min_score": 90, "max_score": 100},
            {"name": "Happy", "min_val": 15, "max_val": 25, "min_score": 70, "max_score": 89},
            {"name": "Acceptable", "min_val": 25, "max_val": 35, "min_score": 50, "max_score": 69},
            {"name": "Concerning", "min_val": 35, "max_val": 100, "min_score": 0, "max_score": 49},
        ],
    },
    "commit_intentionality": {
        # % of commits classified as feature, bugfix, or test (not "other" / maintenance).
        # Measures whether commits represent deliberate, purposeful work.
        # Pre-AI teams: ~30-40% typical. AI-mature teams: 40-60%+.
        "direction": "higher",
        "bands": [
            {"name": "Concerning", "min_val": 0, "max_val": 25, "min_score": 0, "max_score": 49},
            {"name": "Acceptable", "min_val": 25, "max_val": 40, "min_score": 50, "max_score": 69},
            {"name": "Happy", "min_val": 40, "max_val": 55, "min_score": 70, "max_score": 89},
            {"name": "Elite", "min_val": 55, "max_val": 100, "min_score": 90, "max_score": 100},
        ],
    },
    # Engineering Efficiency
    "lead_time_days": {
        "direction": "lower",
        "bands": [
            {"name": "Elite", "min_val": 0, "max_val": 3, "min_score": 90, "max_score": 100},
            {"name": "Happy", "min_val": 3, "max_val": 7, "min_score": 70, "max_score": 89},
            {"name": "Acceptable", "min_val": 7, "max_val": 21, "min_score": 50, "max_score": 69},
            {"name": "Concerning", "min_val": 21, "max_val": 60, "min_score": 0, "max_score": 49},
        ],
    },
    "cycle_delivery_accuracy": {
        "direction": "higher",
        "bands": [
            {"name": "Concerning", "min_val": 0, "max_val": 55, "min_score": 0, "max_score": 49},
            {"name": "Acceptable", "min_val": 55, "max_val": 70, "min_score": 50, "max_score": 69},
            {"name": "Happy", "min_val": 70, "max_val": 85, "min_score": 70, "max_score": 89},
            {"name": "Elite", "min_val": 85, "max_val": 100, "min_score": 90, "max_score": 100},
        ],
    },
    # Engineering Health
    "mttr_hours": {
        "direction": "lower",
        "bands": [
            {"name": "Elite", "min_val": 0, "max_val": 1, "min_score": 90, "max_score": 100},
            {"name": "Happy", "min_val": 1, "max_val": 4, "min_score": 70, "max_score": 89},
            {"name": "Acceptable", "min_val": 4, "max_val": 12, "min_score": 50, "max_score": 69},
            {"name": "Concerning", "min_val": 12, "max_val": 48, "min_score": 0, "max_score": 49},
        ],
    },
    "bus_factor": {
        "direction": "higher",
        "bands": [
            {"name": "Concerning", "min_val": 1, "max_val": 2, "min_score": 0, "max_score": 49},
            {"name": "Acceptable", "min_val": 2, "max_val": 3, "min_score": 50, "max_score": 69},
            {"name": "Happy", "min_val": 3, "max_val": 4, "min_score": 70, "max_score": 89},
            {"name": "Elite", "min_val": 4, "max_val": 10, "min_score": 90, "max_score": 100},
        ],
    },
    "knowledge_distribution": {
        "direction": "higher",
        "bands": [
            {"name": "Concerning", "min_val": 0, "max_val": 40, "min_score": 0, "max_score": 49},
            {"name": "Acceptable", "min_val": 40, "max_val": 60, "min_score": 50, "max_score": 69},
            {"name": "Happy", "min_val": 60, "max_val": 80, "min_score": 70, "max_score": 89},
            {"name": "Elite", "min_val": 80, "max_val": 100, "min_score": 90, "max_score": 100},
        ],
    },
}


# ─── BOT DETECTION ────────────────────────────────────────────────────────────
#
_MANUAL_SOURCE_METRICS: frozenset[str] = frozenset(
    {
        "deployment_frequency",
        "change_failure_rate",
        "post_release_defect_rate",
        "lead_time_days",
        "cycle_delivery_accuracy",
        "mttr_hours",
    }
)


def score_metric(metric_name: str, value: float) -> dict:
    """Score a single metric value against its bands using linear interpolation."""
    if metric_name not in METRIC_BANDS:
        return {"score": None, "band": "Unknown", "error": f"Unknown metric: {metric_name}"}

    config: dict[str, Any] = METRIC_BANDS[metric_name]
    bands: list[dict[str, Any]] = config["bands"]

    # For lower-is-better metrics, bands are ordered Elite→Concerning (low→high values)
    # For higher-is-better metrics, bands are ordered Concerning→Elite (low→high values)
    for band in bands:
        # For boundary values (e.g., bus_factor=3 at Acceptable/Happy boundary),
        # prefer the better band: higher band for higher-is-better, lower for lower-is-better.
        if config["direction"] == "higher":
            # Use exclusive upper bound so boundary goes to the next (better) band
            if band != bands[-1]:
                in_band = band["min_val"] <= value < band["max_val"]
            else:
                in_band = band["min_val"] <= value <= band["max_val"]
        else:
            # Use exclusive lower bound so boundary goes to the previous (better) band
            if band != bands[0]:
                in_band = band["min_val"] < value <= band["max_val"]
            else:
                in_band = band["min_val"] <= value <= band["max_val"]
        if in_band:
            # Linear interpolation within band
            val_range = band["max_val"] - band["min_val"]
            if val_range == 0:
                score = band["max_score"]
            else:
                val_fraction = (value - band["min_val"]) / val_range
                # For lower-is-better: higher value within band = lower score
                if config["direction"] == "lower":
                    score = band["max_score"] - val_fraction * (band["max_score"] - band["min_score"])
                else:
                    score = band["min_score"] + val_fraction * (band["max_score"] - band["min_score"])
            return {
                "score": round(score, 1),
                "band": band["name"],
                "value": value,
            }

    # Value outside all bands — clamp to nearest
    if config["direction"] == "higher":
        if value <= bands[0]["min_val"]:
            return {"score": 0, "band": "Concerning", "value": value}
        return {"score": 100, "band": "Elite", "value": value}
    else:
        if value <= bands[0]["min_val"]:
            return {"score": 100, "band": "Elite", "value": value}
        return {"score": 0, "band": "Concerning", "value": value}


def load_git_metrics(product_dir: Path, month: str) -> dict:
    """Load git metrics JSON for a product/month. Tries aggregated and per-repo files."""
    # Try aggregated file first
    agg_file = product_dir / f"{month}.json"
    if agg_file.exists():
        with open(agg_file) as f:
            return json.load(f)  # type: ignore[no-any-return]

    # Try individual repo files and aggregate
    repo_files = sorted(product_dir.glob(f"{month}_*.json"))
    if not repo_files:
        return {}

    # Aggregate multiple repo metrics
    aggregated: dict[str, Any] = {
        "commits": {"total": 0, "active_contributors": 0, "per_contributor": {}},
        "tickets": {"unique_count": 0, "ids": []},
        "lines_changed": {"added": 0, "removed": 0, "net": 0},
        "focus_score": {"focused_commits": 0, "total_scored": 0},
        "commit_classification": {
            "feature": 0,
            "bugfix": 0,
            "test": 0,
            "tooling": 0,
            "maintenance": 0,
            "docs": 0,
            "other": 0,
        },
        "bus_factor": {"value": 0},
        "knowledge_distribution": {"multi_contributor_modules": 0, "total_modules": 0},
        "mrs": {"total": 0},
        "mr_classification": None,
    }

    all_contributors = set()
    all_contributors_human = set()
    all_tickets = set()

    for rf in repo_files:
        with open(rf) as f:
            data = json.load(f)

        # Skip support repos from EPI aggregation
        if data.get("meta", {}).get("category", "product") == "support":
            continue

        agg = aggregated
        agg["commits"]["total"] += data.get("commits", {}).get("total", 0)
        for author, count in data.get("commits", {}).get("per_contributor", {}).items():
            agg["commits"]["per_contributor"][author] = agg["commits"]["per_contributor"].get(author, 0) + count
            all_contributors.add(author)
            if not is_bot(author):
                all_contributors_human.add(author)

        for tid in data.get("tickets", {}).get("ids", []):
            all_tickets.add(tid)

        lc = data.get("lines_changed", {})
        agg["lines_changed"]["added"] += lc.get("added", 0)
        agg["lines_changed"]["removed"] += lc.get("removed", 0)

        fs = data.get("focus_score", {})
        agg["focus_score"]["focused_commits"] += fs.get("focused_commits", 0)
        agg["focus_score"]["total_scored"] += fs.get("total_scored", 0)

        cc = data.get("commit_classification", {})
        for cat in agg["commit_classification"]:
            agg["commit_classification"][cat] += cc.get(cat, 0)

        mc = data.get("mr_classification")
        if mc:
            if agg["mr_classification"] is None:
                agg["mr_classification"] = {
                    "feature": 0,
                    "bugfix": 0,
                    "test": 0,
                    "tooling": 0,
                    "maintenance": 0,
                    "docs": 0,
                    "other": 0,
                }
            for cat in agg["mr_classification"]:
                agg["mr_classification"][cat] += mc.get(cat, 0)

        kd = data.get("knowledge_distribution", {})
        agg["knowledge_distribution"]["multi_contributor_modules"] += kd.get("multi_contributor_modules", 0)
        agg["knowledge_distribution"]["total_modules"] += kd.get("total_modules", 0)

        agg["mrs"]["total"] += data.get("mrs", data.get("mrs_merged", {})).get("total", 0)

        # Bus factor: take the minimum across active repos (skip inactive ones)
        bf_data = data.get("bus_factor", {})
        bf = bf_data.get("value", 0)
        trail_commits = bf_data.get("total_trailing_commits", 0)
        if trail_commits > 0 and bf > 0:
            agg["bus_factor"]["total_trailing_commits"] = (
                agg["bus_factor"].get("total_trailing_commits", 0) + trail_commits
            )
            if agg["bus_factor"]["value"] == 0:
                agg["bus_factor"]["value"] = bf
            else:
                agg["bus_factor"]["value"] = min(agg["bus_factor"]["value"], bf)

    aggregated["commits"]["active_contributors"] = len(all_contributors)
    aggregated["commits"]["active_contributors_human"] = len(all_contributors_human)

    # Identify which accounts were filtered as bots (for transparency)
    bot_names = sorted(all_contributors - all_contributors_human)
    aggregated["commits"]["bot_accounts"] = bot_names

    # Build human-only per_contributor dict
    aggregated["commits"]["per_contributor_human"] = {
        k: v for k, v in aggregated["commits"]["per_contributor"].items() if not is_bot(k)
    }

    aggregated["tickets"]["ids"] = sorted(all_tickets)
    aggregated["tickets"]["unique_count"] = len(all_tickets)
    aggregated["lines_changed"]["net"] = aggregated["lines_changed"]["added"] - aggregated["lines_changed"]["removed"]

    # Recalculate focus score percentage
    fs = aggregated["focus_score"]
    if fs["total_scored"] > 0:
        fs["percentage"] = round(fs["focused_commits"] / fs["total_scored"] * 100, 1)
    else:
        fs["percentage"] = 0.0

    # MRs per engineer — compute both all-inclusive and human-only
    active = aggregated["commits"]["active_contributors"]
    active_human = aggregated["commits"]["active_contributors_human"]
    mrs = aggregated["mrs"]["total"]
    aggregated["mrs"]["per_engineer"] = round(mrs / active, 1) if active > 0 else 0.0
    aggregated["mrs"]["per_engineer_human"] = round(mrs / active_human, 1) if active_human > 0 else 0.0

    # Rework rate — always use commit-level classification
    cc = aggregated["commit_classification"]
    fb = cc["feature"] + cc["bugfix"]
    aggregated["rework_rate"] = {
        "percentage": round(cc["bugfix"] / fb * 100, 1) if fb > 0 else 0.0,
        "bugfix_count": cc["bugfix"],
        "feature_plus_bugfix": fb,
        "source": "commit",
    }

    # Knowledge distribution percentage
    kd = aggregated["knowledge_distribution"]
    if kd["total_modules"] > 0:
        kd["percentage"] = round(kd["multi_contributor_modules"] / kd["total_modules"] * 100, 1)
    else:
        kd["percentage"] = 0.0

    return aggregated


def _load_manual_yaml(product_dir: Path, month: str) -> dict:
    """Load a single manual input YAML file (no carry-forward)."""
    manual_file = product_dir / f"manual-{month}.yaml"
    if not manual_file.exists():
        return {}
    with open(manual_file) as f:
        return yaml.safe_load(f) or {}


def load_manual_input(product_dir: Path, month: str) -> dict:
    """Load manual input YAML for a product/month, with cycle carry-forward.

    If the current month has no cycle data (cycle_items_committed is missing or
    None/0), look back up to 2 previous months for a manual file that does.
    Only cycle fields (cycle, cycle_items_committed, cycle_items_delivered) are
    carried forward — never monthly metrics like deployment_frequency.
    """
    current = _load_manual_yaml(product_dir, month)

    # If current month already has cycle data, return as-is
    if current.get("cycle_items_committed"):
        return current

    # Look back up to 2 months for cycle data
    prev = month
    for _ in range(2):
        prev = _previous_month(prev)
        prev_data = _load_manual_yaml(product_dir, prev)
        if prev_data.get("cycle_items_committed"):
            current.setdefault("cycle", prev_data.get("cycle"))
            current["cycle_items_committed"] = prev_data["cycle_items_committed"]
            current["cycle_items_delivered"] = prev_data.get("cycle_items_delivered")
            current["cycle_carried_forward"] = True
            break

    return current


def extract_metric_values(git_data: dict, manual_data: dict) -> tuple:
    """Extract all 12 metric values from git + manual data sources.

    Returns (metrics, confidence) where:
    - metrics: dict mapping metric name to value (None if unavailable)
    - confidence: dict mapping metric name to confidence factor (0.0-1.0)
      Used to down-weight metrics with small sample sizes.
    """
    metrics = {}
    confidence = {}  # default 1.0 for all metrics unless overridden

    # From manual input — use None when manual data is absent
    deploy_freq = manual_data.get("deployment_frequency")
    metrics["deployment_frequency"] = deploy_freq

    features = manual_data.get("features_shipped")
    if features is None:
        ticket_count = git_data.get("tickets", {}).get("unique_count", 0)
        features = ticket_count if ticket_count > 0 else None
    metrics["features_shipped"] = features

    rollbacks = manual_data.get("rollbacks")
    if deploy_freq is not None and deploy_freq > 0:
        # Treat null rollbacks as 0: no reported rollbacks in a month with known deployments = 0% CFR.
        metrics["change_failure_rate"] = round((rollbacks or 0) / deploy_freq * 100, 1)
    else:
        metrics["change_failure_rate"] = None

    defects = manual_data.get("post_release_defects")
    if features is not None and features > 0 and defects is not None:
        metrics["post_release_defect_rate"] = round(defects / features * 100, 1)
    else:
        metrics["post_release_defect_rate"] = None

    lead_time = manual_data.get("lead_time_median_days")
    metrics["lead_time_days"] = lead_time

    committed = manual_data.get("cycle_items_committed")
    delivered = manual_data.get("cycle_items_delivered")
    if committed is not None and committed > 0 and delivered is not None:
        metrics["cycle_delivery_accuracy"] = round(delivered / committed * 100, 1)
        # Confidence-weighted: small sample sizes get less weight
        # 15 items = full confidence; fewer items = proportionally less
        confidence["cycle_delivery_accuracy"] = min(1.0, committed / 15.0)
    else:
        metrics["cycle_delivery_accuracy"] = None

    mttr = manual_data.get("mttr_hours")
    metrics["mttr_hours"] = mttr

    # From git data — use None when underlying data is insufficient
    if git_data:
        mrs_total = git_data.get("mrs", git_data.get("mrs_merged", {})).get("total", 0)
        # Use human-only contributor count for scoring (falls back to all if unavailable)
        active_human = git_data.get("commits", {}).get("active_contributors_human")
        active_all = git_data.get("commits", {}).get("active_contributors", 0)
        active = active_human if active_human is not None else active_all
        metrics["mrs_per_engineer"] = round(mrs_total / active, 1) if active > 0 else None
        # Also store the all-inclusive value for dual display
        metrics["mrs_per_engineer_all"] = round(mrs_total / active_all, 1) if active_all > 0 else None

        rr = git_data.get("rework_rate", {})
        metrics["rework_rate"] = rr.get("percentage", 0.0) if rr.get("feature_plus_bugfix", 0) > 0 else None

        bf = git_data.get("bus_factor", {})
        metrics["bus_factor"] = bf.get("value", 0) if bf.get("total_trailing_commits", 0) > 0 else None

        kd = git_data.get("knowledge_distribution", {})
        metrics["knowledge_distribution"] = kd.get("percentage", 0.0) if kd.get("total_modules", 0) > 0 else None

        cc = git_data.get("commit_classification", {})
        total_cc = sum(cc.values())
        if total_cc > 0:
            intentional = cc.get("feature", 0) + cc.get("bugfix", 0) + cc.get("test", 0)
            metrics["commit_intentionality"] = round(intentional / total_cc * 100, 1)
        else:
            metrics["commit_intentionality"] = None
    else:
        metrics["mrs_per_engineer"] = None
        metrics["rework_rate"] = None
        metrics["bus_factor"] = None
        metrics["knowledge_distribution"] = None
        metrics["commit_intentionality"] = None

    return metrics, confidence


def score_repos(product: str, month: str, base_dir: Path) -> list:
    """Score individual repos for a product/month.

    Reads per-repo JSON files (products/{product}/{month}_*.json) and
    scores band-scoreable metrics for each repo. Also loads health and
    manual-health data if available.
    """
    product_dir = base_dir / "products" / product
    if not product_dir.exists():
        return []

    repo_files = sorted(product_dir.glob(f"{month}_*.json"))
    if not repo_files:
        return []

    repos = []
    for rf in repo_files:
        with open(rf) as f:
            data = json.load(f)

        meta = data.get("meta", {})
        repo_name = meta.get("repo", rf.stem.split("_", 1)[-1])

        commits_data = data.get("commits", {})
        total_commits = commits_data.get("total", 0)
        active_contributors = commits_data.get("active_contributors", 0)
        per_contributor = commits_data.get("per_contributor", {})

        # Compute per-repo human-only contributor counts
        per_contributor_human = {k: v for k, v in per_contributor.items() if not is_bot(k)}
        active_contributors_human = len(per_contributor_human)
        bot_accounts = sorted(k for k in per_contributor if is_bot(k))

        mrs_data = data.get("mrs", data.get("mrs_merged", {}))
        mrs_total = mrs_data.get("total", 0)

        classification = data.get("commit_classification", {})
        rework_fb = classification.get("feature", 0) + classification.get("bugfix", 0)
        rework_pct = round(classification.get("bugfix", 0) / rework_fb * 100, 1) if rework_fb > 0 else 0.0

        bus_data = data.get("bus_factor", {})
        bus_value = bus_data.get("value", 0)
        bus_trailing = bus_data.get("total_trailing_commits", 0)

        kd_data = data.get("knowledge_distribution", {})
        kd_pct = kd_data.get("percentage", 0.0)
        kd_modules = kd_data.get("total_modules", 0)

        focus_data = data.get("focus_score", {})
        focus_pct = focus_data.get("percentage", 0.0)
        lines_changed = data.get("lines_changed", {})
        tickets = data.get("tickets", {})

        # Score band-scoreable metrics
        scored_metrics = {}
        if rework_fb > 0:
            scored_metrics["rework_rate"] = score_metric("rework_rate", rework_pct)
        if bus_trailing > 0 and bus_value > 0:
            scored_metrics["bus_factor"] = score_metric("bus_factor", bus_value)
        if kd_modules > 0:
            scored_metrics["knowledge_distribution"] = score_metric("knowledge_distribution", kd_pct)
        total_cc = sum(classification.values())
        if total_cc > 0:
            intentional_pct = round(
                (classification.get("feature", 0) + classification.get("bugfix", 0) + classification.get("test", 0))
                / total_cc
                * 100,
                1,
            )
            scored_metrics["commit_intentionality"] = score_metric("commit_intentionality", intentional_pct)
        else:
            intentional_pct = None

        # Load health data if available
        health_file = product_dir / f"health-{month}_{repo_name}.json"
        health = None
        if health_file.exists():
            with open(health_file) as f:
                health = json.load(f)

        # Load manual health data if available
        manual_health_file = product_dir / f"manual-health-{month}_{repo_name}.yaml"
        manual_health = None
        if manual_health_file.exists():
            with open(manual_health_file) as f:
                manual_health = yaml.safe_load(f) or None

        repo_result = {
            "name": repo_name,
            "category": meta.get("category", "product"),
            "branch": meta.get("branch", ""),
            "gitlab_instance": meta.get("gitlab_instance", ""),
            "namespace": meta.get("namespace", ""),
            "project_id": meta.get("project_id", ""),
            "inactive": total_commits == 0,
            "commits": total_commits,
            "active_contributors": active_contributors,
            "active_contributors_human": active_contributors_human,
            "bot_accounts": bot_accounts,
            "per_contributor": per_contributor,
            "mrs": mrs_total,
            "rework_rate": rework_pct if rework_fb > 0 else None,
            "bus_factor": bus_value if bus_trailing > 0 else None,
            "knowledge_distribution": kd_pct if kd_modules > 0 else None,
            "focus_score": focus_pct,
            "commit_intentionality": intentional_pct,
            "commit_classification": classification,
            "lines_changed": lines_changed,
            "tickets": tickets,
            "scored_metrics": scored_metrics,
            "health": health,
            "manual_health": manual_health,
        }
        repos.append(repo_result)

    return repos


def score_product(product: str, month: str, base_dir: Path, repos_config: dict | None = None) -> dict:
    """Score a single product for a given month.

    Returns a flat per-metric result — no composite EPI score, no component rollup.
    Each metric carries: value, score, band, prev_value, prev_band, direction, is_manual.
    """
    product_dir = base_dir / "products" / product

    if not product_dir.exists():
        return {"error": f"Product directory not found: {product_dir}"}

    # Get display name from central repos.yaml
    if repos_config is None:
        repos_config = load_repos_config(base_dir)

    product_config = repos_config.get(product, {})
    display_name = product_config.get("display_name", product.upper())

    # Load data
    git_data = load_git_metrics(product_dir, month)
    manual_data = load_manual_input(product_dir, month)

    # Load per-repo data
    repos = score_repos(product, month, base_dir)

    if not git_data and not manual_data:
        return {
            "product": product,
            "display_name": display_name,
            "month": month,
            "error": "No data found (neither git metrics JSON nor manual input YAML)",
        }

    # Extract metric values for current month
    metric_values, _metric_confidence = extract_metric_values(git_data, manual_data)

    # Load previous month data for comparison
    prev_month = _previous_month(month)
    prev_data_file = product_dir / f"{prev_month}.json"
    prev_manual_file = product_dir / f"manual-{prev_month}.yaml"
    prev_repo_files = list(product_dir.glob(f"{prev_month}_*.json"))

    prev_metric_values: dict[str, Any] = {}
    if prev_data_file.exists() or prev_manual_file.exists() or prev_repo_files:
        prev_git = load_git_metrics(product_dir, prev_month)
        prev_manual = load_manual_input(product_dir, prev_month)
        prev_metric_values, _ = extract_metric_values(prev_git, prev_manual)

    # Build flat per-metric result dict — only metrics tracked in METRIC_BANDS
    metrics_out: dict[str, dict[str, Any]] = {}
    for metric_name in METRIC_BANDS:
        value = metric_values.get(metric_name)
        if value is None:
            continue

        scored = score_metric(metric_name, value)
        band = scored.get("band", "N/A")

        # Previous month comparison
        prev_value = prev_metric_values.get(metric_name)
        prev_band: str | None = None
        if prev_value is not None:
            prev_scored = score_metric(metric_name, prev_value)
            prev_band = prev_scored.get("band", "N/A")

        # Direction of change vs previous month
        direction: str | None = None
        if prev_value is not None and value is not None:
            config_dir = METRIC_BANDS[metric_name]["direction"]
            delta = value - prev_value
            if abs(delta) < 1e-9:
                direction = "unchanged"
            elif (config_dir == "higher" and delta > 0) or (config_dir == "lower" and delta < 0):
                direction = "improved"
            else:
                direction = "declined"

        is_manual = metric_name in _MANUAL_SOURCE_METRICS or (
            metric_name == "features_shipped" and manual_data.get("features_shipped") is not None
        )

        metrics_out[metric_name] = {
            "value": value,
            "score": scored.get("score"),
            "band": band,
            "prev_value": prev_value,
            "prev_band": prev_band,
            "direction": direction,
            "is_manual": is_manual,
        }

    return {
        "product": product,
        "display_name": display_name,
        "month": month,
        "metrics": metrics_out,
        "raw_metrics": metric_values,
        "cycle_info": {
            "cycle": manual_data.get("cycle"),
            "carried_forward": manual_data.get("cycle_carried_forward", False),
            "committed": manual_data.get("cycle_items_committed"),
            "delivered": manual_data.get("cycle_items_delivered"),
        },
        "data_sources": {
            "git_metrics": bool(git_data),
            "manual_input": bool(manual_data),
        },
        "repos": repos,
    }


def _previous_month(month: str) -> str:
    """Get the previous month string (YYYY-MM)."""
    year, mon = int(month[:4]), int(month[5:7])
    if mon == 1:
        return f"{year - 1}-12"
    return f"{year}-{mon - 1:02d}"


def _band_threshold_str(metric_name: str, band_name: str, bands_by_name: dict[str, Any], direction: str) -> str:
    """Return a human-readable threshold string for one band of a metric."""
    b = bands_by_name.get(band_name)
    if not b:
        return "—"
    lo = _metric_value_str(metric_name, b["min_val"])
    hi = _metric_value_str(metric_name, b["max_val"])
    if direction == "higher":
        if band_name == "Concerning":
            return f"< {hi}"
        if band_name == "Elite":
            return f">= {lo}"
        return f"{lo}–{hi}"
    else:
        if band_name == "Elite":
            return f"< {hi}"
        if band_name == "Concerning":
            return f"> {lo}"
        return f"{lo}–{hi}"


def _metric_value_str(metric_name: str, value: Any) -> str:
    """Format a metric value with units for display."""
    if value is None:
        return "—"
    val_str = f"{value:.1f}" if isinstance(value, float) else str(value)
    if "rate" in metric_name or "accuracy" in metric_name or metric_name == "knowledge_distribution":
        val_str += "%"
    elif metric_name == "mttr_hours":
        val_str += "h"
    elif metric_name == "lead_time_days":
        val_str += "d"
    elif metric_name == "deployment_frequency":
        val_str += "/mo"
    elif metric_name == "mrs_per_engineer":
        val_str += "/eng"
    return val_str


def _render_band_legend_rows(metric_keys: list[str]) -> list[str]:
    """Return markdown table rows (no header) for the Band Thresholds legend."""
    rows = []
    for mname in metric_keys:
        mconfig: dict[str, Any] = METRIC_BANDS[mname]
        display = mname.replace("_", " ").title()
        bands_by_name: dict[str, Any] = {b["name"]: b for b in mconfig["bands"]}
        direction: str = mconfig["direction"]
        concerning = _band_threshold_str(mname, "Concerning", bands_by_name, direction)
        acceptable = _band_threshold_str(mname, "Acceptable", bands_by_name, direction)
        happy = _band_threshold_str(mname, "Happy", bands_by_name, direction)
        elite = _band_threshold_str(mname, "Elite", bands_by_name, direction)
        rows.append(f"| {display} | {concerning} | {acceptable} | {happy} | {elite} |")
    return rows


def generate_product_report(result: dict) -> str:
    """Generate detailed per-product markdown report.

    Shows a per-metric table with band context and month-over-month comparison.
    No composite EPI score — each metric stands on its own.
    """
    if "error" in result and "metrics" not in result:
        return f"# Metrics Report: {result.get('display_name', result['product'])}\n\n**Error:** {result['error']}\n"

    lines = []
    p = result

    lines.append(f"# Metrics Report: {p['display_name']} — {p['month']}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Per-metric table
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value | Band | vs Previous | Source |")
    lines.append("|--------|-------|------|-------------|--------|")

    for metric_name, m in p.get("metrics", {}).items():
        display = metric_name.replace("_", " ").title()
        val_str = _metric_value_str(metric_name, m.get("value"))
        band = m.get("band", "N/A")

        prev_value = m.get("prev_value")
        prev_band = m.get("prev_band")
        direction = m.get("direction")

        if prev_value is None:
            vs_prev = "—"
        else:
            prev_val_str = _metric_value_str(metric_name, prev_value)
            dir_arrow = {"improved": "↑", "declined": "↓", "unchanged": "→"}.get(direction or "", "")
            band_change = ""
            if prev_band and prev_band != band:
                band_change = f" ({prev_band} → {band})"
            vs_prev = f"{dir_arrow} {prev_val_str}{band_change}".strip()

        source = "manual" if m.get("is_manual") else "git"
        lines.append(f"| {display} | {val_str} | {band} | {vs_prev} | {source} |")

    lines.append("")

    # Cycle info if available
    ci = p.get("cycle_info", {})
    if ci.get("cycle") or ci.get("committed"):
        lines.append("## Cycle Info")
        lines.append("")
        if ci.get("cycle"):
            carried = " *(carried forward)*" if ci.get("carried_forward") else ""
            lines.append(f"- Cycle: **{ci['cycle']}**{carried}")
        if ci.get("committed") is not None:
            delivered = ci.get("delivered", "—")
            pct = round(delivered / ci["committed"] * 100, 1) if delivered else "—"
            lines.append(f"- Items committed: {ci['committed']} / delivered: {delivered} ({pct}%)")
        lines.append("")

    # Band legend — thresholds from METRIC_BANDS
    lines.append("## Band Thresholds")
    lines.append("")
    lines.append("Bands: **Elite** (90–100) · **Happy** (70–89) · **Acceptable** (50–69) · **Concerning** (0–49)")
    lines.append("")
    lines.append("| Metric | Concerning | Acceptable | Happy | Elite |")
    lines.append("|--------|------------|------------|-------|-------|")
    lines.extend(_render_band_legend_rows(list(METRIC_BANDS)))
    lines.append("")

    # Data sources
    lines.append("---")
    lines.append("")
    ds = p.get("data_sources", {})
    lines.append(f"- Git metrics: {'Available' if ds.get('git_metrics') else 'Not available'}")
    lines.append(f"- Manual input: {'Available' if ds.get('manual_input') else 'Not available'}")
    lines.append("")
    lines.append(f"*Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}*")

    return "\n".join(lines)


def generate_board_report(results: list, month: str) -> str:
    """Generate board-level summary across all products.

    Produces a product-x-metric matrix with band indicators and a
    "metrics that moved band this month" callout. No composite EPI column.
    """
    # Collect all metric keys that appear across all products (ordered by METRIC_BANDS)
    all_metric_keys = [k for k in METRIC_BANDS if any(k in r.get("metrics", {}) for r in results)]

    lines = []
    lines.append(f"# Engineering Metrics — {month}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Product x metric matrix table
    lines.append("## Metric Matrix")
    lines.append("")

    # Build header
    short_names = {
        "deployment_frequency": "Deploy Freq",
        "mrs_per_engineer": "MRs/Eng",
        "features_shipped": "Features",
        "commit_intentionality": "Intentionality",
        "change_failure_rate": "CFR",
        "post_release_defect_rate": "Defect Rate",
        "rework_rate": "Rework",
        "lead_time_days": "Lead Time",
        "cycle_delivery_accuracy": "Cycle Acc.",
        "mttr_hours": "MTTR",
        "bus_factor": "Bus Factor",
        "knowledge_distribution": "Know. Dist.",
    }

    header_cols = ["Product"] + [short_names.get(k, k.replace("_", " ").title()) for k in all_metric_keys]
    sep_cols = ["-" * max(len(h), 3) for h in header_cols]
    lines.append("| " + " | ".join(header_cols) + " |")
    lines.append("| " + " | ".join(sep_cols) + " |")

    for r in results:
        if "error" in r and "metrics" not in r:
            row = [r.get("display_name", r["product"])] + ["—"] * len(all_metric_keys)
            lines.append("| " + " | ".join(row) + " |")
            continue

        row = [r["display_name"]]
        for metric_name in all_metric_keys:
            m = r.get("metrics", {}).get(metric_name)
            if m is None:
                row.append("—")
            else:
                val_str = _metric_value_str(metric_name, m.get("value"))
                band = m.get("band", "N/A")
                band_indicator = {"Elite": "★", "Happy": "✓", "Acceptable": "△", "Concerning": "✗"}.get(band, "?")
                row.append(f"{band_indicator} {val_str}")
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")
    lines.append("*Band: ★ Elite · ✓ Happy · △ Acceptable · ✗ Concerning*")
    lines.append("")

    # Metrics that moved band this month
    band_moves: list[str] = []
    for r in results:
        if "metrics" not in r:
            continue
        for metric_name, m in r["metrics"].items():
            prev_band = m.get("prev_band")
            curr_band = m.get("band")
            if prev_band and curr_band and prev_band != curr_band and curr_band not in ("N/A",):
                direction = m.get("direction", "")
                arrow = "↑" if direction == "improved" else "↓"
                display = metric_name.replace("_", " ").title()
                band_moves.append(f"- {r['display_name']} · {display}: {prev_band} → {curr_band} {arrow}")

    if band_moves:
        lines.append("## Metrics That Moved Band")
        lines.append("")
        lines.extend(band_moves)
        lines.append("")

    # Per-product highlights
    lines.append("## Per-Product Highlights")
    lines.append("")
    for r in results:
        if "metrics" not in r:
            lines.append(f"**{r.get('display_name', r['product'])}** — no data")
            lines.append("")
            continue

        lines.append(f"### {r['display_name']}")
        lines.append("")

        scored = [(k, m) for k, m in r["metrics"].items() if m.get("score") is not None]
        if scored:
            best = max(scored, key=lambda x: x[1]["score"])  # type: ignore[index]
            worst = min(scored, key=lambda x: x[1]["score"])  # type: ignore[index]
            best_display = best[0].replace("_", " ").title()
            worst_display = worst[0].replace("_", " ").title()
            lines.append(f"- **Strength:** {best_display} — {best[1]['band']} ({best[1]['score']:.0f})")
            lines.append(f"- **Risk:** {worst_display} — {worst[1]['band']} ({worst[1]['score']:.0f})")
        else:
            lines.append("- No scored metrics available")
        lines.append("")

    # Legend
    lines.append("## Band Thresholds")
    lines.append("")
    lines.append("| Metric | Concerning | Acceptable | Happy | Elite |")
    lines.append("|--------|------------|------------|-------|-------|")
    lines.extend(_render_band_legend_rows(all_metric_keys))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}*")

    return "\n".join(lines)


def main() -> None:
    """Parse arguments, score products, and generate EPI reports."""
    parser = argparse.ArgumentParser(description="EPI Scoring Engine")
    parser.add_argument("--product", help="Product to score (e.g., app_a, app_b)")
    parser.add_argument("--all", action="store_true", help="Score all products")
    parser.add_argument("--month", required=True, help="Month to score (YYYY-MM)")
    parser.add_argument("--base-dir", default=None, help="Base epi-data directory (default: script directory)")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Output format")
    parser.add_argument("--output-dir", default=None, help="Directory for report files (default: base-dir/reports)")

    args = parser.parse_args()

    if not args.product and not args.all:
        parser.error("Either --product or --all is required")

    base_dir = Path(args.base_dir) if args.base_dir else Path(__file__).resolve().parent.parent
    output_dir = Path(args.output_dir) if args.output_dir else base_dir / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)

    repos_config = load_repos_config(base_dir)

    products = []
    if args.all:
        # Use products from repos.yaml; fall back to scanning directories
        if repos_config:
            products = sorted(repos_config.keys())
        else:
            products_dir = base_dir / "products"
            if products_dir.exists():
                products = sorted([d.name for d in products_dir.iterdir() if d.is_dir()])
    else:
        products = [args.product]

    if not products:
        print("No products found.", file=sys.stderr)
        sys.exit(1)

    results = []
    for product in products:
        print(f"Scoring {product} for {args.month}...", file=sys.stderr)
        result = score_product(product, args.month, base_dir, repos_config)
        results.append(result)

        if args.format == "json":
            print(json.dumps(result, indent=2))
        else:
            report = generate_product_report(result)
            report_file = output_dir / f"EPI_{args.month}_{product}.md"
            with open(report_file, "w") as f:
                f.write(report)
            print(f"  Written: {report_file}", file=sys.stderr)

    # Generate board report if scoring multiple products
    if len(results) > 1:
        if args.format == "json":
            board = {
                "month": args.month,
                "products": results,
            }
            print(json.dumps(board, indent=2))
        else:
            board_report = generate_board_report(results, args.month)
            board_file = output_dir / f"EPI_{args.month}.md"
            with open(board_file, "w") as f:
                f.write(board_report)
            print(f"  Board report: {board_file}", file=sys.stderr)
