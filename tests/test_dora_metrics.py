"""Unit tests for collect-dora-metrics / record-dora-event (epi.collectors.dora)."""

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import ClassVar
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import yaml

from epi.collectors import dora
from epi.collectors.dora import Deployment, Incident
from epi.scoring import score_product


def ts(value: str) -> datetime:
    return dora.parse_timestamp(value)


def no_commits(repo, from_sha, to_sha):
    return None


class TestDeploymentMetrics(unittest.TestCase):
    """compute_deployment_metrics: frequency, rollbacks/hotfixes, lead time."""

    def test_counts_only_deployments_in_month(self):
        deps = [
            Deployment("svc", "a", ts("2026-02-27T10:00:00Z")),
            Deployment("svc", "b", ts("2026-03-02T10:00:00Z")),
            Deployment("svc", "c", ts("2026-03-31T23:59:00Z")),
            Deployment("svc", "d", ts("2026-04-01T00:00:00Z")),
        ]
        stats = dora.compute_deployment_metrics(deps, "2026-03", no_commits)
        self.assertEqual(stats["deployment_frequency"], 2)
        self.assertEqual(stats["rollbacks"], 0)

    def test_redeploy_of_older_sha_is_rollback(self):
        deps = [
            Deployment("svc", "a", ts("2026-03-01T10:00:00Z")),
            Deployment("svc", "b", ts("2026-03-02T10:00:00Z")),
            Deployment("svc", "a", ts("2026-03-02T11:00:00Z")),  # rollback to a
        ]
        stats = dora.compute_deployment_metrics(deps, "2026-03", no_commits)
        self.assertEqual(stats["deployment_frequency"], 3)
        self.assertEqual(stats["rollbacks"], 1)

    def test_retry_of_live_sha_is_not_rollback(self):
        deps = [
            Deployment("svc", "a", ts("2026-03-01T10:00:00Z")),
            Deployment("svc", "a", ts("2026-03-01T10:05:00Z")),
        ]
        stats = dora.compute_deployment_metrics(deps, "2026-03", no_commits)
        self.assertEqual(stats["rollbacks"], 0)

    def test_rollback_detected_against_previous_month_history(self):
        deps = [
            Deployment("svc", "a", ts("2026-02-20T10:00:00Z")),
            Deployment("svc", "b", ts("2026-02-25T10:00:00Z")),
            Deployment("svc", "a", ts("2026-03-01T10:00:00Z")),
        ]
        stats = dora.compute_deployment_metrics(deps, "2026-03", no_commits)
        self.assertEqual((stats["deployment_frequency"], stats["rollbacks"]), (1, 1))

    def test_hotfix_ref_and_flagged_rollback_count_as_failures(self):
        deps = [
            Deployment("svc", "a", ts("2026-03-01T10:00:00Z"), ref="main"),
            Deployment("svc", "b", ts("2026-03-02T10:00:00Z"), ref="hotfix/login"),
            Deployment("svc", "c", ts("2026-03-03T10:00:00Z"), rollback=True),
        ]
        stats = dora.compute_deployment_metrics(deps, "2026-03", no_commits)
        self.assertEqual(stats["rollbacks"], 2)

    def test_repos_are_tracked_independently(self):
        # The same SHA in two repos is not a rollback
        deps = [
            Deployment("one", "a", ts("2026-03-01T10:00:00Z")),
            Deployment("two", "b", ts("2026-03-01T11:00:00Z")),
            Deployment("two", "a", ts("2026-03-01T12:00:00Z")),
        ]
        stats = dora.compute_deployment_metrics(deps, "2026-03", no_commits)
        self.assertEqual(stats["rollbacks"], 0)

    def test_lead_time_is_median_commit_to_deploy(self):
        commits = {
            ("a", "b"): {"c1": ts("2026-03-01T00:00:00Z"), "c2": ts("2026-03-02T00:00:00Z")},
            ("b", "c"): {"c3": ts("2026-03-04T00:00:00Z")},
        }
        deps = [
            Deployment("svc", "a", ts("2026-02-28T00:00:00Z")),
            Deployment("svc", "b", ts("2026-03-03T00:00:00Z")),  # c1: 2d, c2: 1d
            Deployment("svc", "c", ts("2026-03-08T00:00:00Z")),  # c3: 4d
        ]
        stats = dora.compute_deployment_metrics(deps, "2026-03", lambda r, f, t: commits.get((f, t)))
        self.assertEqual(stats["lead_time_median_days"], 2.0)
        self.assertEqual(stats["lead_time_commits"], 3)

    def test_lead_time_credits_each_commit_once_after_rollback(self):
        # a → b, rollback to a, then c (which contains b's commit again)
        commits = {
            ("a", "b"): {"c1": ts("2026-03-01T00:00:00Z")},
            ("a", "c"): {"c1": ts("2026-03-01T00:00:00Z"), "c2": ts("2026-03-05T00:00:00Z")},
        }
        calls = []

        def lookup(repo, f, t):
            calls.append((f, t))
            return commits.get((f, t))

        deps = [
            Deployment("svc", "a", ts("2026-02-28T00:00:00Z")),
            Deployment("svc", "b", ts("2026-03-02T00:00:00Z")),
            Deployment("svc", "a", ts("2026-03-02T01:00:00Z")),
            Deployment("svc", "c", ts("2026-03-06T00:00:00Z")),
        ]
        stats = dora.compute_deployment_metrics(deps, "2026-03", lookup)
        self.assertNotIn(("b", "a"), calls)  # no lead time for the rollback itself
        self.assertEqual(stats["lead_time_commits"], 2)  # c1 counted once (at b), c2 at c
        self.assertEqual(stats["lead_time_median_days"], 1.0)

    def test_no_previous_deployment_means_no_lead_time(self):
        deps = [Deployment("svc", "a", ts("2026-03-01T00:00:00Z"))]
        stats = dora.compute_deployment_metrics(deps, "2026-03", lambda r, f, t: {"x": ts("2026-02-01T00:00:00Z")})
        self.assertIsNone(stats["lead_time_median_days"])


class TestIncidentMetrics(unittest.TestCase):
    """compute_incident_metrics: severity counts and MTTR."""

    def test_counts_and_mttr_from_sev1_sev2_only(self):
        incidents = [
            Incident("1", "sev1", ts("2026-03-01T00:00:00Z"), ts("2026-03-01T02:00:00Z")),
            Incident("2", "sev2", ts("2026-03-05T00:00:00Z"), ts("2026-03-05T04:00:00Z")),
            Incident("3", "sev3", ts("2026-03-06T00:00:00Z"), ts("2026-03-09T00:00:00Z")),
            Incident("4", "sev1", ts("2026-02-28T00:00:00Z"), ts("2026-03-01T00:00:00Z")),  # previous month
        ]
        stats = dora.compute_incident_metrics(incidents, "2026-03")
        self.assertEqual(stats["incidents"], {"sev1": 1, "sev2": 1, "sev3": 1})
        self.assertEqual(stats["mttr_hours"], 3.0)

    def test_no_service_impacting_incidents_is_zero_mttr(self):
        stats = dora.compute_incident_metrics([Incident("1", "sev3", ts("2026-03-01T00:00:00Z"))], "2026-03")
        self.assertEqual(stats["mttr_hours"], 0.0)

    def test_only_unresolved_incidents_leaves_mttr_unknown(self):
        stats = dora.compute_incident_metrics([Incident("1", "sev1", ts("2026-03-01T00:00:00Z"))], "2026-03")
        self.assertIsNone(stats["mttr_hours"])
        self.assertEqual(stats["unresolved_incidents"], 1)


class TestEventLog(unittest.TestCase):
    """dora-events.jsonl round trip and folding."""

    def test_append_and_read_round_trip_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            product_dir = Path(tmp) / "products" / "alpha"
            dora.append_event(product_dir, {"type": "deployment", "repo": "svc", "sha": "a", "timestamp": "x"})
            with (product_dir / dora.EVENTS_FILE).open("a") as f:
                f.write("not json\n\n")
            dora.append_event(product_dir, {"type": "incident", "id": "1"})
            events = dora.read_events(product_dir)
        self.assertEqual([e["type"] for e in events], ["deployment", "incident"])

    def test_deployments_filtered_by_environment(self):
        events = [
            {"type": "deployment", "repo": "svc", "sha": "a", "timestamp": "2026-03-01T00:00:00Z"},
            {
                "type": "deployment",
                "repo": "svc",
                "sha": "b",
                "timestamp": "2026-03-02T00:00:00Z",
                "environment": "staging",
            },
            {"type": "deployment", "repo": "svc", "timestamp": "2026-03-03T00:00:00Z"},  # invalid: no sha
            {"type": "incident", "id": "1", "action": "opened", "timestamp": "2026-03-01T00:00:00Z"},
        ]
        deps = dora.deployments_from_events(events, "production")
        self.assertEqual([d.sha for d in deps], ["a"])

    def test_incident_events_fold_into_lifecycle(self):
        events = [
            {
                "type": "incident",
                "id": "7",
                "action": "opened",
                "severity": "sev2",
                "timestamp": "2026-03-01T00:00:00Z",
            },
            {
                "type": "incident",
                "id": "8",
                "action": "opened",
                "severity": "sev1",
                "timestamp": "2026-03-02T00:00:00Z",
            },
            {"type": "incident", "id": "7", "action": "resolved", "timestamp": "2026-03-01T05:00:00Z"},
            {
                "type": "incident",
                "id": "9",
                "action": "opened",
                "severity": "bogus",
                "timestamp": "2026-03-02T00:00:00Z",
            },
        ]
        incidents = {i.id: i for i in dora.incidents_from_events(events)}
        self.assertEqual(set(incidents), {"7", "8"})
        self.assertEqual(incidents["7"].resolved_at, ts("2026-03-01T05:00:00Z"))
        self.assertIsNone(incidents["8"].resolved_at)


class TestLocalCommitTimes(unittest.TestCase):
    """local_commit_times reads author times between two SHAs from a real clone."""

    def test_reads_commits_between_shas_excluding_merges(self):
        with tempfile.TemporaryDirectory() as repo:

            def git(*args, date=None):
                env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com"}
                env |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
                if date:
                    env |= {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
                out = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True)
                return out.stdout.strip()

            git("init", "-q", "-b", "main")
            git("commit", "-q", "--allow-empty", "-m", "base", date="2026-03-01T00:00:00Z")
            base = git("rev-parse", "HEAD")
            git("commit", "-q", "--allow-empty", "-m", "one", date="2026-03-02T00:00:00Z")
            one = git("rev-parse", "HEAD")
            git("commit", "-q", "--allow-empty", "-m", "two", date="2026-03-03T00:00:00Z")
            head = git("rev-parse", "HEAD")

            lookup = dora.local_commit_times({"svc": repo})
            result = lookup("svc", base, head)
            self.assertEqual(result, {one: ts("2026-03-02T00:00:00Z"), head: ts("2026-03-03T00:00:00Z")})
            self.assertIsNone(lookup("unknown", base, head))


class TestGitlabSeverity(unittest.TestCase):
    def test_labels_win_over_field(self):
        self.assertEqual(dora.gitlab_severity({"labels": ["severity::1"], "severity": "LOW"}), "sev1")
        self.assertEqual(dora.gitlab_severity({"labels": ["bug", "S2"]}), "sev2")
        self.assertEqual(dora.gitlab_severity({"labels": ["P0"]}), "sev1")
        self.assertEqual(dora.gitlab_severity({"labels": ["sev4"]}), "sev3")

    def test_field_and_default(self):
        self.assertEqual(dora.gitlab_severity({"severity": "CRITICAL"}), "sev1")
        self.assertEqual(dora.gitlab_severity({"severity": "UNKNOWN"}, default="sev2"), "sev2")


class TestMergeMeasured(unittest.TestCase):
    def test_keeps_hand_entered_fields_and_records_measured(self):
        existing = {
            "product": "alpha",
            "month": "2026-03",
            "features_shipped": 12,
            "cycle_items_committed": 10,
            "deployment_frequency": 4,
            "measured": ["incidents"],
        }
        data = dora.merge_measured(
            existing, "alpha", "2026-03", {"deployment_frequency": 9, "rollbacks": 1}, {"x": 1}, "2026-04-01"
        )
        self.assertEqual(data["features_shipped"], 12)
        self.assertEqual(data["cycle_items_committed"], 10)
        self.assertEqual(data["deployment_frequency"], 9)
        self.assertEqual(data["measured"], ["deployment_frequency", "incidents", "rollbacks"])
        self.assertEqual(data["dora_collection"]["collected_at"], "2026-04-01")

    def test_new_file_gets_template_keys(self):
        data = dora.merge_measured({}, "alpha", "2026-03", {"mttr_hours": 1.5}, {}, "2026-04-01")
        self.assertEqual(data["submitted_by"], "collect-dora-metrics")
        self.assertIn("features_shipped", data)
        self.assertIsNone(data["features_shipped"])
        self.assertEqual(data["mttr_hours"], 1.5)


class TestScoringManualFlag(unittest.TestCase):
    """score-epi stops flagging metrics as manual once their inputs are measured."""

    def _score(self, manual: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            product_dir = Path(tmp) / "products" / "alpha"
            product_dir.mkdir(parents=True)
            (product_dir / "manual-2026-03.yaml").write_text(yaml.safe_dump(manual))
            return score_product("alpha", "2026-03", Path(tmp), repos_config={})["metrics"]

    def test_measured_metrics_not_manual(self):
        metrics = self._score(
            {
                "deployment_frequency": 10,
                "rollbacks": 1,
                "lead_time_median_days": 1.5,
                "mttr_hours": 2.0,
                "measured": ["deployment_frequency", "rollbacks", "lead_time_median_days", "mttr_hours"],
            }
        )
        for name in ("deployment_frequency", "change_failure_rate", "lead_time_days", "mttr_hours"):
            self.assertFalse(metrics[name]["is_manual"], name)

    def test_cfr_stays_manual_when_rollbacks_hand_entered(self):
        metrics = self._score({"deployment_frequency": 10, "rollbacks": 1, "measured": ["deployment_frequency"]})
        self.assertFalse(metrics["deployment_frequency"]["is_manual"])
        self.assertTrue(metrics["change_failure_rate"]["is_manual"])

    def test_without_measured_list_everything_is_manual(self):
        metrics = self._score({"deployment_frequency": 10, "rollbacks": 1, "mttr_hours": 2.0})
        self.assertTrue(metrics["deployment_frequency"]["is_manual"])
        self.assertTrue(metrics["mttr_hours"]["is_manual"])


class _MockGitLabHandler(BaseHTTPRequestHandler):
    """Serves deployments, compare and incident issues for project 1."""

    deployments: ClassVar[list] = []
    compare: ClassVar[dict] = {}
    issues: ClassVar[list] = []

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if query.get("page", ["1"])[0] != "1":
            payload: object = []
        elif url.path == "/api/v4/projects/1/deployments":
            payload = self.deployments
        elif url.path == "/api/v4/projects/1/repository/compare":
            payload = {"commits": self.compare.get((query["from"][0], query["to"][0]), [])}
        elif url.path == "/api/v4/projects/1/issues":
            payload = self.issues
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, format, *args):
        pass


class TestGitLabSource(unittest.TestCase):
    """collect_product end to end against a mock GitLab."""

    @classmethod
    def setUpClass(cls):
        _MockGitLabHandler.deployments = [
            {"sha": "a", "finished_at": "2026-02-27T00:00:00Z", "deployable": {"ref": "main"}},
            {"sha": "b", "finished_at": "2026-03-03T00:00:00Z", "deployable": {"ref": "main"}},
            {"sha": "c", "finished_at": "2026-03-10T00:00:00Z", "deployable": {"ref": "hotfix/x"}},
        ]
        _MockGitLabHandler.compare = {
            ("a", "b"): [
                {"id": "c1", "authored_date": "2026-03-01T00:00:00Z", "parent_ids": ["p"]},
                {"id": "m1", "authored_date": "2026-03-02T23:00:00Z", "parent_ids": ["p", "q"]},  # merge
            ],
            ("b", "c"): [{"id": "c2", "authored_date": "2026-03-09T00:00:00Z", "parent_ids": ["b"]}],
        }
        _MockGitLabHandler.issues = [
            {"iid": 1, "created_at": "2026-03-04T00:00:00Z", "closed_at": "2026-03-04T03:00:00Z", "labels": ["S1"]},
            {"iid": 2, "created_at": "2026-03-05T00:00:00Z", "closed_at": None, "severity": "LOW"},
        ]
        cls.server = HTTPServer(("127.0.0.1", 0), _MockGitLabHandler)
        cls.instance = f"http://127.0.0.1:{cls.server.server_address[1]}"
        Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_collect_product_from_gitlab(self):
        config = {
            "gitlab_instance": self.instance,
            "token_env": "TEST_GITLAB_TOKEN",
            "repos": [{"name": "svc", "project_id": "1"}],
            "dora": {
                "deployments": {"source": "gitlab"},
                "incidents": {"source": "gitlab", "project_id": "1"},
            },
        }
        with patch.dict(os.environ, {"TEST_GITLAB_TOKEN": "t"}):
            measured, details = dora.collect_product("alpha", config, "2026-03", Path("/nonexistent"))

        self.assertEqual(measured["deployment_frequency"], 2)
        self.assertEqual(measured["rollbacks"], 1)  # hotfix/x
        self.assertEqual(measured["lead_time_median_days"], 1.5)  # c1: 2d, c2: 1d (merge excluded)
        self.assertEqual(measured["incidents"], {"sev1": 1, "sev2": 0, "sev3": 1})
        self.assertEqual(measured["mttr_hours"], 3.0)
        self.assertEqual(details["deployments"]["source"], "gitlab")

    def test_missing_token_leaves_fields_unmeasured(self):
        config = {
            "gitlab_instance": self.instance,
            "token_env": "TEST_GITLAB_TOKEN_UNSET",
            "repos": [{"name": "svc", "project_id": "1"}],
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_GITLAB_TOKEN_UNSET", None)
            measured, _ = dora.collect_product("alpha", config, "2026-03", Path("/nonexistent"))
        self.assertEqual(measured, {})


class TestCommandLine(unittest.TestCase):
    """record-dora-event + collect-dora-metrics using the events source."""

    def test_record_events_then_collect_into_manual_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "repos.yaml").write_text(
                yaml.safe_dump(
                    {
                        "products": {
                            "alpha": {
                                "support_repos": ["ci-tools"],
                                "dora": {"deployments": {"source": "events"}, "incidents": {"source": "events"}},
                            }
                        }
                    }
                )
            )
            manual = Path(tmp, "products", "alpha", "manual-2026-03.yaml")
            manual.parent.mkdir(parents=True)
            manual.write_text(yaml.safe_dump({"product": "alpha", "month": "2026-03", "features_shipped": 7}))

            def record(*args):
                with patch("sys.argv", ["record-dora-event", "--product", "alpha", "--base-dir", tmp, *args]):
                    dora.record_event_main()

            record("--timestamp", "2026-03-02T00:00:00Z", "deployment", "--repo", "svc", "--sha", "a")
            record("--timestamp", "2026-03-03T00:00:00Z", "deployment", "--repo", "svc", "--sha", "b", "--rollback")
            record("--timestamp", "2026-03-03T00:00:00Z", "deployment", "--repo", "ci-tools", "--sha", "z")
            record(
                "--timestamp",
                "2026-03-04T00:00:00Z",
                "incident",
                "--id",
                "I1",
                "--action",
                "opened",
                "--severity",
                "sev1",
            )
            record("--timestamp", "2026-03-04T01:30:00Z", "incident", "--id", "I1", "--action", "resolved")

            with patch(
                "sys.argv", ["collect-dora-metrics", "--product", "alpha", "--month", "2026-03", "--base-dir", tmp]
            ):
                dora.main()

            text = manual.read_text()
            data = yaml.safe_load(text)

        self.assertTrue(text.startswith("# EPI Manual Input — alpha 2026-03\n"))

        self.assertEqual(data["features_shipped"], 7)  # untouched
        self.assertEqual(data["deployment_frequency"], 2)  # ci-tools is a support repo
        self.assertEqual(data["rollbacks"], 1)
        self.assertEqual(data["incidents"], {"sev1": 1, "sev2": 0, "sev3": 0})
        self.assertEqual(data["mttr_hours"], 1.5)
        self.assertEqual(data["measured"], ["deployment_frequency", "incidents", "mttr_hours", "rollbacks"])

    def test_incident_opened_requires_severity(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "record-dora-event",
                "--product",
                "alpha",
                "--base-dir",
                tmp,
                "incident",
                "--id",
                "1",
                "--action",
                "opened",
            ]
            with patch("sys.argv", argv), self.assertRaises(SystemExit), patch("sys.stderr"):
                dora.record_event_main()


if __name__ == "__main__":
    unittest.main()
