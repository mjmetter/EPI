"""Unit tests for Linear-based commit classification."""

import json
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import ClassVar
from unittest.mock import patch

from epi.api import gitlab as gitlab_api
from epi.api.gitlab import fetch_linear_issue_types


class _MockLinearHandler(BaseHTTPRequestHandler):
    """GraphQL handler that returns mock Linear API responses keyed by issue id."""

    # Class-level dict: ticket_id → list of label names
    labels: ClassVar[dict] = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        ticket_id = body["variables"]["id"]

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

        if ticket_id in self.labels:
            payload = {
                "data": {
                    "issue": {
                        "labels": {"nodes": [{"name": n} for n in self.labels[ticket_id]]},
                    }
                }
            }
        else:
            payload = {"data": {"issue": None}}
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, format, *args):
        pass  # suppress request logs during tests


class TestFetchLinearIssueTypes(unittest.TestCase):
    """Test fetch_linear_issue_types with a mock GraphQL server."""

    @classmethod
    def setUpClass(cls):
        _MockLinearHandler.labels = {
            "ENG-1": ["Bug"],
            "ENG-2": ["Feature"],
            "ENG-3": ["Chore"],  # unmapped keyword
            "ENG-4": ["Epic"],
            "ENG-5": ["Story", "Frontend"],
        }
        cls.server = HTTPServer(("127.0.0.1", 0), _MockLinearHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls._orig_url = gitlab_api.LINEAR_GRAPHQL_URL
        gitlab_api.LINEAR_GRAPHQL_URL = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        gitlab_api.LINEAR_GRAPHQL_URL = cls._orig_url
        cls.server.shutdown()

    def test_resolves_bug_and_feature(self):
        with patch.dict("os.environ", {"TEST_LINEAR_TOKEN": "fake-key"}):
            result = fetch_linear_issue_types(["ENG-1", "ENG-2"], "TEST_LINEAR_TOKEN")
        self.assertEqual(result["ENG-1"], "Bug")
        self.assertEqual(result["ENG-2"], "Story")  # "feature" keyword normalizes to Story

    def test_unmapped_label_omitted(self):
        with patch.dict("os.environ", {"TEST_LINEAR_TOKEN": "fake-key"}):
            result = fetch_linear_issue_types(["ENG-3"], "TEST_LINEAR_TOKEN")
        self.assertNotIn("ENG-3", result)

    def test_epic_resolved(self):
        with patch.dict("os.environ", {"TEST_LINEAR_TOKEN": "fake-key"}):
            result = fetch_linear_issue_types(["ENG-4"], "TEST_LINEAR_TOKEN")
        self.assertEqual(result["ENG-4"], "Epic")

    def test_multiple_labels_matches_first_keyword_hit(self):
        with patch.dict("os.environ", {"TEST_LINEAR_TOKEN": "fake-key"}):
            result = fetch_linear_issue_types(["ENG-5"], "TEST_LINEAR_TOKEN")
        self.assertEqual(result["ENG-5"], "Story")

    def test_unknown_ticket_omitted(self):
        with patch.dict("os.environ", {"TEST_LINEAR_TOKEN": "fake-key"}):
            result = fetch_linear_issue_types(["ENG-999"], "TEST_LINEAR_TOKEN")
        self.assertNotIn("ENG-999", result)

    def test_no_token_returns_empty(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("MISSING_TOKEN", None)
            result = fetch_linear_issue_types(["ENG-1"], "MISSING_TOKEN")
        self.assertEqual(result, {})

    def test_batch_of_multiple_tickets(self):
        with patch.dict("os.environ", {"TEST_LINEAR_TOKEN": "fake-key"}):
            result = fetch_linear_issue_types(["ENG-1", "ENG-2", "ENG-3", "ENG-4", "ENG-5"], "TEST_LINEAR_TOKEN")
        self.assertEqual(len(result), 4)  # ENG-3 omitted (unmapped label)


class TestClassifyCommitsWithLinear(unittest.TestCase):
    """Test that classify_commits correctly consumes Linear-sourced ticket_types."""

    def test_bug_ticket_classifies_as_bugfix(self):
        from epi.collectors.git_metrics import classify_commits

        commits = [{"subject": "ENG-1 Update login page", "hash": "abc"}]
        ticket_types = {"ENG-1": "Bug"}  # as normalized by fetch_linear_issue_types
        result = classify_commits(commits, ticket_types=ticket_types)
        self.assertEqual(result["bugfix"], 1)
        self.assertEqual(result["feature"], 0)


if __name__ == "__main__":
    unittest.main()
