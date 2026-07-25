"""Unit tests for Jira-based commit classification."""

import json
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import ClassVar
from unittest.mock import patch

from epi.api.gitlab import fetch_jira_issue_types
from epi.collectors.git_metrics import (
    ISSUE_TYPE_MAP,
    build_commit_ticket_map,
    classify_commits,
    classify_mrs,
)


class TestJiraTypeMap(unittest.TestCase):
    """Verify the Jira type → classification mapping."""

    def test_bug_maps_to_bugfix(self):
        self.assertEqual(ISSUE_TYPE_MAP["Bug"], "bugfix")

    def test_story_maps_to_feature(self):
        self.assertEqual(ISSUE_TYPE_MAP["Story"], "feature")

    def test_epic_maps_to_feature(self):
        self.assertEqual(ISSUE_TYPE_MAP["Epic"], "feature")

    def test_task_not_mapped(self):
        self.assertNotIn("Task", ISSUE_TYPE_MAP)


class TestClassifyCommitsWithJira(unittest.TestCase):
    """Test classify_commits with Jira type overrides."""

    def test_bug_ticket_classifies_as_bugfix(self):
        commits = [{"subject": "PROJ-123 Update login page", "hash": "abc"}]
        ticket_types = {"PROJ-123": "Bug"}
        result = classify_commits(commits, ticket_types=ticket_types)
        self.assertEqual(result["bugfix"], 1)
        self.assertEqual(result["feature"], 0)

    def test_story_ticket_classifies_as_feature(self):
        commits = [{"subject": "PROJ-456 Fix the dashboard layout", "hash": "def"}]
        ticket_types = {"PROJ-456": "Story"}
        result = classify_commits(commits, ticket_types=ticket_types)
        self.assertEqual(result["feature"], 1)
        # "Fix" in subject would normally be bugfix — Jira type wins
        self.assertEqual(result["bugfix"], 0)

    def test_epic_ticket_classifies_as_feature(self):
        commits = [{"subject": "PROJ-10 Refactor user module", "hash": "ghi"}]
        ticket_types = {"PROJ-10": "Epic"}
        result = classify_commits(commits, ticket_types=ticket_types)
        self.assertEqual(result["feature"], 1)
        self.assertEqual(result["maintenance"], 0)

    def test_task_falls_through_to_regex(self):
        """Task type is not in ISSUE_TYPE_MAP, so regex takes over."""
        commits = [{"subject": "PROJ-789 Fix login bug", "hash": "jkl"}]
        ticket_types = {"PROJ-789": "Task"}
        result = classify_commits(commits, ticket_types=ticket_types)
        # "Fix" + "bug" should be caught by regex as bugfix
        self.assertEqual(result["bugfix"], 1)
        self.assertEqual(result["feature"], 0)

    def test_unknown_type_falls_through_to_regex(self):
        commits = [{"subject": "PROJ-100 Add new endpoint", "hash": "mno"}]
        ticket_types = {"PROJ-100": "Initiative"}  # not in ISSUE_TYPE_MAP
        result = classify_commits(commits, ticket_types=ticket_types)
        # "Add" should be caught by regex as feature
        self.assertEqual(result["feature"], 1)

    def test_no_jira_types_uses_regex_only(self):
        commits = [{"subject": "fix: resolve crash on startup", "hash": "pqr"}]
        result = classify_commits(commits, ticket_types=None)
        self.assertEqual(result["bugfix"], 1)

    def test_mixed_jira_and_regex(self):
        """Some commits have Jira types, others fall to regex."""
        commits = [
            {"subject": "PROJ-1 Update readme", "hash": "a1"},  # Jira: Bug → bugfix
            {"subject": "feat: add dark mode", "hash": "a2"},  # no ticket → regex: feature
            {"subject": "PROJ-2 Cleanup old code", "hash": "a3"},  # Jira: Story → feature
            {"subject": "PROJ-3 Some task work", "hash": "a4"},  # Jira: Task → regex fallback
        ]
        ticket_types = {"PROJ-1": "Bug", "PROJ-2": "Story", "PROJ-3": "Task"}
        result = classify_commits(commits, ticket_types=ticket_types)
        self.assertEqual(result["bugfix"], 1)  # PROJ-1
        self.assertEqual(result["feature"], 2)  # dark mode + PROJ-2
        # PROJ-3 "Some task work" has no regex match → other
        self.assertEqual(result["other"], 1)

    def test_commit_with_unknown_ticket_falls_to_regex(self):
        """Ticket in subject but not in ticket_types dict — regex fallback."""
        commits = [{"subject": "PROJ-999 fix: resolve bug", "hash": "xyz"}]
        ticket_types = {"PROJ-1": "Bug"}  # PROJ-999 not present
        result = classify_commits(commits, ticket_types=ticket_types)
        self.assertEqual(result["bugfix"], 1)  # regex catches "fix" + "bug"

    def test_empty_jira_types_dict_uses_regex(self):
        commits = [{"subject": "feat: new feature", "hash": "abc"}]
        result = classify_commits(commits, ticket_types={})
        # Empty dict is falsy, so regex path taken
        self.assertEqual(result["feature"], 1)


class TestClassifyMrsWithJira(unittest.TestCase):
    """Test classify_mrs passes ticket_types through."""

    def test_mr_titles_with_jira_types(self):
        titles = ["PROJ-10 Fix production crash", "PROJ-20 Add new dashboard"]
        ticket_types = {"PROJ-10": "Bug", "PROJ-20": "Story"}
        result = classify_mrs(titles, ticket_types=ticket_types)
        self.assertEqual(result["bugfix"], 1)
        self.assertEqual(result["feature"], 1)

    def test_mr_titles_without_jira_types(self):
        titles = ["fix: resolve timeout", "feat: add caching"]
        result = classify_mrs(titles)
        self.assertEqual(result["bugfix"], 1)
        self.assertEqual(result["feature"], 1)


class TestBuildCommitTicketMap(unittest.TestCase):
    """Test build_commit_ticket_map."""

    def test_commits_with_tickets(self):
        commits = [
            {"subject": "PROJ-123 Fix bug", "hash": "aaa"},
            {"subject": "PROJ-456 Add feature", "hash": "bbb"},
        ]
        result = build_commit_ticket_map(commits)
        self.assertEqual(result, {"aaa": "PROJ-123", "bbb": "PROJ-456"})

    def test_commits_without_tickets(self):
        commits = [
            {"subject": "fix: resolve crash", "hash": "ccc"},
            {"subject": "chore: cleanup", "hash": "ddd"},
        ]
        result = build_commit_ticket_map(commits)
        self.assertEqual(result, {})

    def test_mixed_commits(self):
        commits = [
            {"subject": "PROJ-1 Update login", "hash": "eee"},
            {"subject": "refactor: clean up utils", "hash": "fff"},
        ]
        result = build_commit_ticket_map(commits)
        self.assertEqual(result, {"eee": "PROJ-1"})

    def test_multiple_tickets_uses_first(self):
        commits = [
            {"subject": "PROJ-1 PROJ-2 Fix both issues", "hash": "ggg"},
        ]
        result = build_commit_ticket_map(commits)
        self.assertEqual(result, {"ggg": "PROJ-1"})


class _MockJiraHandler(BaseHTTPRequestHandler):
    """HTTP handler that returns mock Jira API responses."""

    # Class-level dict: ticket_id → response payload
    responses: ClassVar[dict] = {}

    def do_GET(self):
        # Extract ticket ID from path: /rest/api/2/issue/PROJ-123?fields=...
        parts = self.path.split("?")[0].split("/")
        ticket_id = parts[-1] if parts else ""

        if ticket_id in self.responses:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.responses[ticket_id]).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress request logs during tests


class TestFetchJiraIssueTypes(unittest.TestCase):
    """Test fetch_jira_issue_types with a mock HTTP server."""

    @classmethod
    def setUpClass(cls):
        _MockJiraHandler.responses = {
            "PROJ-1": {
                "fields": {
                    "issuetype": {"name": "Bug"},
                }
            },
            "PROJ-2": {
                "fields": {
                    "issuetype": {"name": "Story"},
                }
            },
            "PROJ-3": {
                "fields": {
                    "issuetype": {"name": "Task"},
                }
            },
            "PROJ-4": {
                "fields": {
                    "issuetype": {"name": "Sub-task"},
                    "parent": {
                        "fields": {
                            "issuetype": {"name": "Bug"},
                        }
                    },
                }
            },
            "PROJ-5": {
                "fields": {
                    "issuetype": {"name": "Epic"},
                }
            },
        }
        cls.server = HTTPServer(("127.0.0.1", 0), _MockJiraHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_resolves_bug_and_story(self):
        with patch.dict("os.environ", {"TEST_JIRA_TOKEN": "user@test.com:fake-token"}):
            result = fetch_jira_issue_types(
                ["PROJ-1", "PROJ-2"],
                f"http://127.0.0.1:{self.port}",
                "TEST_JIRA_TOKEN",
            )
        self.assertEqual(result["PROJ-1"], "Bug")
        self.assertEqual(result["PROJ-2"], "Story")

    def test_resolves_task(self):
        with patch.dict("os.environ", {"TEST_JIRA_TOKEN": "user@test.com:fake-token"}):
            result = fetch_jira_issue_types(
                ["PROJ-3"],
                f"http://127.0.0.1:{self.port}",
                "TEST_JIRA_TOKEN",
            )
        self.assertEqual(result["PROJ-3"], "Task")

    def test_subtask_resolved_to_parent(self):
        with patch.dict("os.environ", {"TEST_JIRA_TOKEN": "user@test.com:fake-token"}):
            result = fetch_jira_issue_types(
                ["PROJ-4"],
                f"http://127.0.0.1:{self.port}",
                "TEST_JIRA_TOKEN",
            )
        # Sub-task's parent is Bug
        self.assertEqual(result["PROJ-4"], "Bug")

    def test_epic_resolved(self):
        with patch.dict("os.environ", {"TEST_JIRA_TOKEN": "user@test.com:fake-token"}):
            result = fetch_jira_issue_types(
                ["PROJ-5"],
                f"http://127.0.0.1:{self.port}",
                "TEST_JIRA_TOKEN",
            )
        self.assertEqual(result["PROJ-5"], "Epic")

    def test_unknown_ticket_omitted(self):
        with patch.dict("os.environ", {"TEST_JIRA_TOKEN": "user@test.com:fake-token"}):
            result = fetch_jira_issue_types(
                ["PROJ-999"],
                f"http://127.0.0.1:{self.port}",
                "TEST_JIRA_TOKEN",
            )
        self.assertNotIn("PROJ-999", result)

    def test_no_token_returns_empty(self):
        with patch.dict("os.environ", {}, clear=False):
            # Ensure the env var is not set
            import os

            os.environ.pop("MISSING_TOKEN", None)
            result = fetch_jira_issue_types(
                ["PROJ-1"],
                f"http://127.0.0.1:{self.port}",
                "MISSING_TOKEN",
            )
        self.assertEqual(result, {})

    def test_no_instance_returns_empty(self):
        with patch.dict("os.environ", {"TEST_JIRA_TOKEN": "user@test.com:fake-token"}):
            result = fetch_jira_issue_types(
                ["PROJ-1"],
                "",
                "TEST_JIRA_TOKEN",
            )
        self.assertEqual(result, {})

    def test_batch_of_multiple_tickets(self):
        with patch.dict("os.environ", {"TEST_JIRA_TOKEN": "user@test.com:fake-token"}):
            result = fetch_jira_issue_types(
                ["PROJ-1", "PROJ-2", "PROJ-3", "PROJ-4", "PROJ-5"],
                f"http://127.0.0.1:{self.port}",
                "TEST_JIRA_TOKEN",
            )
        self.assertEqual(len(result), 5)
        self.assertEqual(result["PROJ-4"], "Bug")  # Sub-task → parent Bug


if __name__ == "__main__":
    unittest.main()
