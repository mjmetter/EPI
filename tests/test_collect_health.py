"""Unit tests for collect-repo-health.py."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import yaml

import epi.api.cursor as _cursor_api
import epi.api.litellm as _litellm_api
import epi.collectors.repo_health as _mod
from epi.api.cursor import fetch_cursor_commits, fetch_cursor_spend_by_user
from epi.api.litellm import fetch_litellm_spend_by_user


def _clear_spend_disk_caches() -> None:
    tmp = Path(tempfile.gettempdir())
    for path in tmp.glob("epi_cursor_spend_*.json"):
        path.unlink(missing_ok=True)
    for path in tmp.glob("epi_litellm_spend_*.json"):
        path.unlink(missing_ok=True)


month_range = _mod.month_range
resolve_gitlab_info = _mod.resolve_gitlab_info
collect_ai_readiness = _mod.collect_ai_readiness
collect_ai_adoption = _mod.collect_ai_adoption


class TestMonthRange(unittest.TestCase):
    """Test month_range date computation."""

    def test_normal_month(self):
        start, end = month_range("2026-03")
        self.assertEqual(start, "2026-03-01")
        self.assertEqual(end, "2026-04-01")

    def test_december_year_boundary(self):
        start, end = month_range("2025-12")
        self.assertEqual(start, "2025-12-01")
        self.assertEqual(end, "2026-01-01")

    def test_january(self):
        start, end = month_range("2026-01")
        self.assertEqual(start, "2026-01-01")
        self.assertEqual(end, "2026-02-01")

    def test_february(self):
        start, end = month_range("2026-02")
        self.assertEqual(start, "2026-02-01")
        self.assertEqual(end, "2026-03-01")


class TestResolveGitlabInfo(unittest.TestCase):
    """Test resolve_gitlab_info with mocked git remote."""

    @patch.object(_mod, "run_git")
    def test_ssh_remote(self, mock_run_git):
        mock_run_git.return_value = "git@githost.example.com:org/project.git"
        instance, namespace = resolve_gitlab_info("/fake/repo")
        self.assertEqual(instance, "https://githost.example.com")
        self.assertEqual(namespace, "org/project")

    @patch.object(_mod, "run_git")
    def test_https_remote(self, mock_run_git):
        mock_run_git.return_value = "https://githost.example.com/org/project.git"
        instance, namespace = resolve_gitlab_info("/fake/repo")
        self.assertEqual(instance, "https://githost.example.com")
        self.assertEqual(namespace, "org/project")

    @patch.object(_mod, "run_git")
    def test_https_with_oauth2(self, mock_run_git):
        mock_run_git.return_value = "https://oauth2:token123@githost.example.com/org/project.git"
        instance, namespace = resolve_gitlab_info("/fake/repo")
        self.assertEqual(instance, "https://githost.example.com")
        self.assertEqual(namespace, "org/project")

    @patch.object(_mod, "run_git")
    def test_no_remote(self, mock_run_git):
        mock_run_git.return_value = ""
        instance, namespace = resolve_gitlab_info("/fake/repo")
        self.assertEqual(instance, "")
        self.assertEqual(namespace, "")


class TestFetchCursorCommits(unittest.TestCase):
    """Test fetch_cursor_commits with mocked HTTP."""

    @patch.dict("os.environ", {"CURSOR_API_TOKEN": "test-token"})
    @patch("epi.api.cursor.urlopen")
    def test_success_list_format(self, mock_urlopen):
        """Test with legacy list response format."""
        response_data = [
            {
                "sha": "abc123",
                "tabLinesAdded": 10,
                "tabLinesDeleted": 2,
                "composerLinesAdded": 5,
                "composerLinesDeleted": 1,
                "nonAiLinesAdded": 20,
                "nonAiLinesDeleted": 3,
            },
            {
                "sha": "def456",
                "tabLinesAdded": 0,
                "tabLinesDeleted": 0,
                "composerLinesAdded": 15,
                "composerLinesDeleted": 5,
                "nonAiLinesAdded": 10,
                "nonAiLinesDeleted": 2,
            },
        ]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = fetch_cursor_commits("2026-03-01", "2026-04-01")
        self.assertEqual(len(result), 2)
        self.assertIn("abc123", result)
        self.assertEqual(result["abc123"]["tab_added"], 10)
        self.assertEqual(result["abc123"]["composer_added"], 5)
        self.assertEqual(result["def456"]["composer_added"], 15)

    @patch.dict("os.environ", {"CURSOR_API_TOKEN": "test-token"})
    @patch("epi.api.cursor.urlopen")
    def test_success_items_format(self, mock_urlopen):
        """Test with actual Cursor API response format (items key)."""
        response_data = {
            "items": [
                {
                    "commitHash": "abc123",
                    "tabLinesAdded": 10,
                    "tabLinesDeleted": 2,
                    "composerLinesAdded": 5,
                    "composerLinesDeleted": 1,
                    "nonAiLinesAdded": 20,
                    "nonAiLinesDeleted": 3,
                },
                {
                    "commitHash": "def456",
                    "tabLinesAdded": 0,
                    "tabLinesDeleted": 0,
                    "composerLinesAdded": 15,
                    "composerLinesDeleted": 5,
                    "nonAiLinesAdded": 10,
                    "nonAiLinesDeleted": 2,
                },
            ],
            "totalCount": 2,
            "page": 1,
            "pageSize": 100,
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = fetch_cursor_commits("2026-03-01", "2026-04-01")
        self.assertEqual(len(result), 2)
        self.assertIn("abc123", result)
        self.assertEqual(result["abc123"]["tab_added"], 10)
        self.assertEqual(result["abc123"]["composer_added"], 5)
        self.assertEqual(result["def456"]["composer_added"], 15)

    @patch.dict("os.environ", {"CURSOR_API_TOKEN": "test-token"})
    @patch("epi.api.cursor.urlopen")
    def test_31_day_month_splits_into_two_calls(self, mock_urlopen):
        """31-day ranges are split into two ≤30-day chunks so neither call hits the API limit."""

        # Each chunk returns one distinct commit
        def side_effect(req, timeout=30):
            url = req.full_url
            mock_resp = MagicMock()
            if "startDate=2026-03-01" in url:
                payload = [
                    {
                        "sha": "chunk1",
                        "tabLinesAdded": 5,
                        "composerLinesAdded": 0,
                        "nonAiLinesAdded": 0,
                        "tabLinesDeleted": 0,
                        "composerLinesDeleted": 0,
                        "nonAiLinesDeleted": 0,
                    }
                ]
            else:
                payload = [
                    {
                        "sha": "chunk2",
                        "tabLinesAdded": 3,
                        "composerLinesAdded": 0,
                        "nonAiLinesAdded": 0,
                        "tabLinesDeleted": 0,
                        "composerLinesDeleted": 0,
                        "nonAiLinesDeleted": 0,
                    }
                ]
            mock_resp.read.return_value = json.dumps(payload).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        mock_urlopen.side_effect = side_effect
        result = fetch_cursor_commits("2026-03-01", "2026-04-01")  # 31-day range

        self.assertEqual(mock_urlopen.call_count, 2)
        self.assertIn("chunk1", result)
        self.assertIn("chunk2", result)

        # Verify the two chunk ranges are both ≤ 30 days
        calls = [c.args[0].full_url for c in mock_urlopen.call_args_list]
        self.assertIn("startDate=2026-03-01", calls[0])
        self.assertIn("endDate=2026-03-31", calls[0])
        self.assertIn("startDate=2026-03-31", calls[1])
        self.assertIn("endDate=2026-04-01", calls[1])

    @patch.dict("os.environ", {}, clear=True)
    def test_no_token(self):
        # Remove CURSOR_API_TOKEN if present
        import os

        os.environ.pop("CURSOR_API_TOKEN", None)
        result = fetch_cursor_commits("2026-03-01", "2026-04-01")
        self.assertEqual(result, {})

    @patch.dict("os.environ", {"CURSOR_API_TOKEN": "test-token"})
    @patch("epi.api.cursor.urlopen", side_effect=URLError("connection failed"))
    def test_api_error(self, mock_urlopen):
        result = fetch_cursor_commits("2026-03-01", "2026-04-01")
        self.assertEqual(result, {})


class TestCollectAiReadiness(unittest.TestCase):
    """Test collect_ai_readiness with mocked git and filesystem."""

    @patch("os.path.isdir")
    @patch("os.path.isfile")
    @patch("os.path.exists")
    @patch.object(_mod, "run_git")
    def test_normal_commits(self, mock_run_git, mock_exists, mock_isfile, mock_isdir):
        # Mock git log: 5 commits, some with tickets, some feature commits
        commits_log = (
            "aaa111|dev@test.com|PROJ-1 feat: add login|body text|END_BODY\n"
            "bbb222|dev@test.com|PROJ-2 fix: button bug||END_BODY\n"
            "ccc333|dev@test.com|feat: new feature||END_BODY\n"
            "ddd444|dev@test.com|PROJ-3 feat: add search|description here|END_BODY\n"
            "eee555|dev@test.com|chore: update deps||END_BODY"
        )

        def git_side_effect(args, cwd, default=""):
            if "log" in args:
                return commits_log
            if "diff-tree" in args:
                commit_hash = args[-1]
                if commit_hash in ("aaa111", "ddd444"):
                    return "src/login.py\ntests/test_login.py"
                return "src/other.py"
            return default

        mock_run_git.side_effect = git_side_effect
        mock_exists.return_value = True  # README etc.
        mock_isfile.return_value = False
        mock_isdir.return_value = False

        result = collect_ai_readiness("/fake/repo", "main", "2026-03-01", "2026-04-01")
        # 3 out of 5 commits have ticket references (PROJ-1, PROJ-2, PROJ-3)
        self.assertEqual(result["ticket_reference_rate"]["value"], 60.0)
        # Feature commits: aaa111 (feat: add login), ccc333 (feat: new feature), ddd444 (feat: add search) = 3
        # With tests: aaa111, ddd444 = 2
        self.assertAlmostEqual(result["feature_test_coupling"]["value"], 66.7, places=1)

    @patch("os.path.isdir")
    @patch("os.path.isfile")
    @patch("os.path.exists")
    @patch.object(_mod, "run_git")
    def test_no_commits(self, mock_run_git, mock_exists, mock_isfile, mock_isdir):
        mock_run_git.return_value = ""
        mock_exists.return_value = False
        mock_isfile.return_value = False
        mock_isdir.return_value = False

        result = collect_ai_readiness("/fake/repo", "main", "2026-03-01", "2026-04-01")
        self.assertEqual(result["ticket_reference_rate"]["value"], 0.0)
        self.assertEqual(result["feature_test_coupling"]["value"], 0.0)
        self.assertNotIn("commit_message_quality", result)
        self.assertEqual(result["commit_body_rate"]["value"], 0.0)

    @patch("os.path.isdir")
    @patch("os.path.isfile")
    @patch("os.path.exists")
    @patch.object(_mod, "run_git")
    def test_file_checks(self, mock_run_git, mock_exists, mock_isfile, mock_isdir):
        mock_run_git.return_value = ""

        def exists_side_effect(path):
            return "README.md" in path or "CLAUDE.md" in path

        mock_exists.side_effect = exists_side_effect
        mock_isfile.return_value = False
        mock_isdir.return_value = False

        result = collect_ai_readiness("/fake/repo", "main", "2026-03-01", "2026-04-01")
        self.assertTrue(result["readme_exists"])
        self.assertTrue(result["claude_md_exists"])
        self.assertFalse(result["ci_config_exists"])


class TestCommitBodyRate(unittest.TestCase):
    """Test commit_body_rate metric — % of commits that have a non-empty body."""

    def _make_log(self, commits: list[tuple[str, str, str]]) -> str:
        """Build a git log string from (hash, subject, body) tuples."""
        return "".join(f"{h}|test@test.com|{s}|{b}|END_BODY\n" for h, s, b in commits)

    def _run_git_side_effect(self, log_output: str):
        def side_effect(args, cwd, default=""):
            if "log" in args:
                return log_output
            return ""  # non-log calls (diff-tree etc.) return empty

        return side_effect

    @patch("os.path.isdir", return_value=False)
    @patch("os.path.isfile", return_value=False)
    @patch("os.path.exists", return_value=False)
    @patch.object(_mod, "run_git")
    def test_all_commits_have_bodies(self, mock_run_git, *_):
        """All commits with bodies → 100.0%."""
        log = self._make_log([("h1", "Fix bug", "Fixes the thing"), ("h2", "Refactor db", "Cleans up old code")])
        mock_run_git.side_effect = self._run_git_side_effect(log)
        result = collect_ai_readiness("/fake/repo", "main", "2026-03-01", "2026-04-01")
        self.assertEqual(result["commit_body_rate"]["value"], 100.0)
        self.assertNotIn("commit_message_quality", result)

    @patch("os.path.isdir", return_value=False)
    @patch("os.path.isfile", return_value=False)
    @patch("os.path.exists", return_value=False)
    @patch.object(_mod, "run_git")
    def test_half_commits_have_bodies(self, mock_run_git, *_):
        """Half commits with bodies → 50.0%."""
        log = self._make_log([("h1", "Fix bug", "Fixes the thing"), ("h2", "Refactor db", "")])
        mock_run_git.side_effect = self._run_git_side_effect(log)
        result = collect_ai_readiness("/fake/repo", "main", "2026-03-01", "2026-04-01")
        self.assertEqual(result["commit_body_rate"]["value"], 50.0)

    @patch("os.path.isdir", return_value=False)
    @patch("os.path.isfile", return_value=False)
    @patch("os.path.exists", return_value=False)
    @patch.object(_mod, "run_git")
    def test_no_commits_have_bodies(self, mock_run_git, *_):
        """No commits with bodies → 0.0%."""
        log = self._make_log([("h1", "Fix bug", ""), ("h2", "Refactor db", "")])
        mock_run_git.side_effect = self._run_git_side_effect(log)
        result = collect_ai_readiness("/fake/repo", "main", "2026-03-01", "2026-04-01")
        self.assertEqual(result["commit_body_rate"]["value"], 0.0)

    @patch("os.path.isdir", return_value=False)
    @patch("os.path.isfile", return_value=False)
    @patch("os.path.exists", return_value=False)
    @patch.object(_mod, "run_git")
    def test_commit_message_quality_absent(self, mock_run_git, *_):
        """commit_message_quality key must not appear in results."""
        log = self._make_log([("h1", "Fix bug", "Some body")])
        mock_run_git.side_effect = self._run_git_side_effect(log)
        result = collect_ai_readiness("/fake/repo", "main", "2026-03-01", "2026-04-01")
        self.assertNotIn("commit_message_quality", result)
        self.assertIn("commit_body_rate", result)


class TestCollectAiAdoption(unittest.TestCase):
    """Test collect_ai_adoption with mocked dependencies."""

    @patch.object(_mod, "get_cursor_cache", return_value={})
    @patch.object(
        _mod, "fetch_mr_ai_usage", return_value={"total_mrs": 0, "none": 0, "ai_augmented": 0, "agentic": 0, "na": 0}
    )
    @patch.object(_mod, "run_git")
    def test_co_authored_detection(self, mock_run_git, mock_mr, mock_cursor):
        total_log = "hash1\nhash2\nhash3\nhash4\nhash5"
        body_log = (
            "hash1|Co-Authored-By: Claude <claude@anthropic.com>|END_ENTRY\n"
            "hash2|Regular commit body|END_ENTRY\n"
            "hash3|Co-Authored-By: copilot <noreply@github.com>|END_ENTRY\n"
            "hash4||END_ENTRY\n"
            "hash5||END_ENTRY"
        )

        def git_side_effect(args, cwd, default=""):
            if "--format=%H|%b|END_ENTRY" in " ".join(args):
                return body_log
            if "--format=%H" in " ".join(args):
                return total_log
            return default

        mock_run_git.side_effect = git_side_effect

        result = collect_ai_adoption("/fake/repo", "main", "2026-03-01", "2026-04-01")
        self.assertEqual(result["co_authored_commits"]["total"], 2)
        self.assertEqual(result["co_authored_rate"], 40.0)
        self.assertEqual(result["co_authored_commits"]["by_tool"].get("claude", 0), 1)
        self.assertEqual(result["co_authored_commits"]["by_tool"].get("copilot", 0), 1)

    def test_co_author_pattern_matches_parenthetical_footer(self):
        self.assertTrue(_mod.CO_AUTHOR_PATTERN.search("(co-authored with Claude Code)"))
        self.assertTrue(_mod.CO_AUTHOR_PATTERN.search("(Co-Authored with Claude Code)"))
        self.assertTrue(_mod.CO_AUTHOR_PATTERN.search("Co-Authored-By: Claude Code <noreply@anthropic.com>"))
        self.assertFalse(_mod.CO_AUTHOR_PATTERN.search("regular commit message"))

    @patch.object(_mod, "get_cursor_cache", return_value={})
    @patch.object(
        _mod, "fetch_mr_ai_usage", return_value={"total_mrs": 0, "none": 0, "ai_augmented": 0, "agentic": 0, "na": 0}
    )
    @patch.object(_mod, "run_git")
    def test_co_authored_parenthetical_footer_counted(self, mock_run_git, mock_mr, mock_cursor):
        total_log = "hash1\nhash2\nhash3"
        body_log = (
            "hash1|(co-authored with Claude Code)|END_ENTRY\n"
            "hash2|Regular commit body|END_ENTRY\n"
            "hash3|(co-authored with GitHub Copilot)|END_ENTRY"
        )

        def git_side_effect(args, cwd, default=""):
            if "--format=%H|%b|END_ENTRY" in " ".join(args):
                return body_log
            if "--format=%H" in " ".join(args):
                return total_log
            return default

        mock_run_git.side_effect = git_side_effect

        result = collect_ai_adoption("/fake/repo", "main", "2026-03-01", "2026-04-01")
        self.assertEqual(result["co_authored_commits"]["total"], 2)
        self.assertEqual(result["co_authored_rate"], 66.7)
        self.assertEqual(result["co_authored_commits"]["by_tool"].get("claude", 0), 1)
        self.assertEqual(result["co_authored_commits"]["by_tool"].get("copilot", 0), 1)

    @patch.object(_mod, "get_cursor_cache", return_value={})
    @patch.object(
        _mod, "fetch_mr_ai_usage", return_value={"total_mrs": 0, "none": 0, "ai_augmented": 0, "agentic": 0, "na": 0}
    )
    @patch.object(_mod, "run_git")
    def test_no_co_authored(self, mock_run_git, mock_mr, mock_cursor):
        total_log = "hash1\nhash2"
        body_log = "hash1|Regular body|END_ENTRY\nhash2||END_ENTRY"

        def git_side_effect(args, cwd, default=""):
            if "--format=%H|%b|END_ENTRY" in " ".join(args):
                return body_log
            if "--format=%H" in " ".join(args):
                return total_log
            return default

        mock_run_git.side_effect = git_side_effect

        result = collect_ai_adoption("/fake/repo", "main", "2026-03-01", "2026-04-01")
        self.assertEqual(result["co_authored_rate"], 0.0)
        self.assertEqual(result["co_authored_commits"]["total"], 0)

    @patch.object(
        _mod, "fetch_mr_ai_usage", return_value={"total_mrs": 0, "none": 0, "ai_augmented": 0, "agentic": 0, "na": 0}
    )
    @patch.object(_mod, "run_git")
    def test_cursor_attribution(self, mock_run_git, mock_mr):
        total_log = "abc123\ndef456"
        body_log = "abc123||END_ENTRY\ndef456||END_ENTRY"

        def git_side_effect(args, cwd, default=""):
            if "--format=%H|%b|END_ENTRY" in " ".join(args):
                return body_log
            if "--format=%H" in " ".join(args):
                return total_log
            return default

        mock_run_git.side_effect = git_side_effect

        cursor_data = {
            "abc123": {
                "tab_added": 10,
                "tab_deleted": 2,
                "composer_added": 5,
                "composer_deleted": 1,
                "non_ai_added": 20,
                "non_ai_deleted": 3,
                "total_added": 35,
                "total_deleted": 6,
            },
        }

        with patch.object(_mod, "get_cursor_cache", return_value=cursor_data):
            result = collect_ai_adoption("/fake/repo", "main", "2026-03-01", "2026-04-01")

        self.assertIn("cursor_line_attribution", result)
        self.assertEqual(result["cursor_line_attribution"]["commits_matched"], 1)
        self.assertEqual(result["cursor_line_attribution"]["tab_lines_added"], 10)
        self.assertEqual(result["cursor_line_attribution"]["composer_lines_added"], 5)


class TestCombinedAiAdoption(unittest.TestCase):
    """Test ai_assisted_commit_rate — union of co-authored trailers and Cursor AI lines."""

    NO_MR: ClassVar[dict] = {"total_mrs": 0, "none": 0, "ai_augmented": 0, "agentic": 0, "na": 0}

    @staticmethod
    def _git_side_effect(total_log: str, body_log: str):
        def side_effect(args, cwd, default=""):
            if "--format=%H|%b|END_ENTRY" in " ".join(args):
                return body_log
            if "--format=%H" in " ".join(args):
                return total_log
            return default

        return side_effect

    @patch.object(_mod, "fetch_mr_ai_usage", return_value=NO_MR)
    @patch.object(_mod, "run_git")
    def test_both_signals_same_commit(self, mock_run_git, mock_mr):
        """Commit with BOTH a trailer and Cursor AI lines counts once."""
        mock_run_git.side_effect = self._git_side_effect(
            "aaa111", "aaa111|Co-Authored-By: Claude <claude@anthropic.com>|END_ENTRY"
        )
        cursor = {
            "aaa111": {
                "tab_added": 10,
                "tab_deleted": 0,
                "composer_added": 5,
                "composer_deleted": 0,
                "non_ai_added": 20,
                "non_ai_deleted": 0,
                "total_added": 35,
                "total_deleted": 0,
            }
        }
        with patch.object(_mod, "get_cursor_cache", return_value=cursor):
            result = collect_ai_adoption("/fake", "main", "2026-03-01", "2026-04-01")
        self.assertEqual(result["ai_assisted_commits"], 1)
        self.assertEqual(result["ai_assisted_commit_rate"], 100.0)

    @patch.object(_mod, "get_cursor_cache", return_value={})
    @patch.object(_mod, "fetch_mr_ai_usage", return_value=NO_MR)
    @patch.object(_mod, "run_git")
    def test_trailer_only(self, mock_run_git, mock_mr, mock_cursor):
        """Only co-authored trailer, no Cursor data."""
        mock_run_git.side_effect = self._git_side_effect(
            "aaa111\nbbb222",
            "aaa111|Co-Authored-By: Claude <claude@anthropic.com>|END_ENTRY\nbbb222|regular|END_ENTRY",
        )
        result = collect_ai_adoption("/fake", "main", "2026-03-01", "2026-04-01")
        self.assertEqual(result["ai_assisted_commits"], 1)
        self.assertEqual(result["ai_assisted_commit_rate"], 50.0)

    @patch.object(_mod, "fetch_mr_ai_usage", return_value=NO_MR)
    @patch.object(_mod, "run_git")
    def test_cursor_only(self, mock_run_git, mock_mr):
        """Only Cursor AI lines, no trailer."""
        mock_run_git.side_effect = self._git_side_effect(
            "aaa111\nbbb222", "aaa111|regular|END_ENTRY\nbbb222|regular|END_ENTRY"
        )
        cursor = {
            "aaa111": {
                "tab_added": 10,
                "tab_deleted": 0,
                "composer_added": 0,
                "composer_deleted": 0,
                "non_ai_added": 5,
                "non_ai_deleted": 0,
                "total_added": 15,
                "total_deleted": 0,
            }
        }
        with patch.object(_mod, "get_cursor_cache", return_value=cursor):
            result = collect_ai_adoption("/fake", "main", "2026-03-01", "2026-04-01")
        self.assertEqual(result["ai_assisted_commits"], 1)
        self.assertEqual(result["ai_assisted_commit_rate"], 50.0)

    @patch.object(_mod, "get_cursor_cache", return_value={})
    @patch.object(_mod, "fetch_mr_ai_usage", return_value=NO_MR)
    @patch.object(_mod, "run_git")
    def test_neither_signal(self, mock_run_git, mock_mr, mock_cursor):
        """No trailer, no Cursor data — rate is 0."""
        mock_run_git.side_effect = self._git_side_effect(
            "aaa111\nbbb222", "aaa111|regular|END_ENTRY\nbbb222||END_ENTRY"
        )
        result = collect_ai_adoption("/fake", "main", "2026-03-01", "2026-04-01")
        self.assertEqual(result["ai_assisted_commits"], 0)
        self.assertEqual(result["ai_assisted_commit_rate"], 0.0)

    @patch.object(_mod, "fetch_mr_ai_usage", return_value=NO_MR)
    @patch.object(_mod, "run_git")
    def test_mixed_scenario(self, mock_run_git, mock_mr):
        """5 commits: 1 has both signals, 1 trailer-only, 1 cursor-only, 2 neither → 60%."""
        mock_run_git.side_effect = self._git_side_effect(
            "aaa111\nbbb222\nccc333\nddd444\neee555",
            "aaa111|Co-Authored-By: Claude <claude@anthropic.com>|END_ENTRY\n"
            "bbb222|Co-Authored-By: copilot <noreply@github.com>|END_ENTRY\n"
            "ccc333|regular|END_ENTRY\n"
            "ddd444|regular|END_ENTRY\n"
            "eee555||END_ENTRY",
        )
        cursor = {
            "aaa111": {
                "tab_added": 5,
                "tab_deleted": 0,
                "composer_added": 3,
                "composer_deleted": 0,
                "non_ai_added": 2,
                "non_ai_deleted": 0,
                "total_added": 10,
                "total_deleted": 0,
            },
            "ccc333": {
                "tab_added": 8,
                "tab_deleted": 0,
                "composer_added": 0,
                "composer_deleted": 0,
                "non_ai_added": 4,
                "non_ai_deleted": 0,
                "total_added": 12,
                "total_deleted": 0,
            },
        }
        with patch.object(_mod, "get_cursor_cache", return_value=cursor):
            result = collect_ai_adoption("/fake", "main", "2026-03-01", "2026-04-01")
        self.assertEqual(result["ai_assisted_commits"], 3)
        self.assertEqual(result["ai_assisted_commit_rate"], 60.0)

    @patch.object(_mod, "fetch_mr_ai_usage", return_value=NO_MR)
    @patch.object(_mod, "run_git")
    def test_cursor_zero_ai_lines_not_counted(self, mock_run_git, mock_mr):
        """Cursor-matched commit with 0 AI lines should NOT count as AI-assisted."""
        mock_run_git.side_effect = self._git_side_effect("aaa111", "aaa111|regular|END_ENTRY")
        cursor = {
            "aaa111": {
                "tab_added": 0,
                "tab_deleted": 0,
                "composer_added": 0,
                "composer_deleted": 0,
                "non_ai_added": 30,
                "non_ai_deleted": 5,
                "total_added": 30,
                "total_deleted": 5,
            }
        }
        with patch.object(_mod, "get_cursor_cache", return_value=cursor):
            result = collect_ai_adoption("/fake", "main", "2026-03-01", "2026-04-01")
        self.assertEqual(result["ai_assisted_commits"], 0)
        self.assertEqual(result["ai_assisted_commit_rate"], 0.0)

    @patch.object(_mod, "fetch_mr_ai_usage", return_value=NO_MR)
    @patch.object(_mod, "run_git")
    def test_existing_fields_unchanged(self, mock_run_git, mock_mr):
        """New fields don't break existing co_authored_rate or cursor_line_attribution."""
        mock_run_git.side_effect = self._git_side_effect(
            "aaa111\nbbb222",
            "aaa111|Co-Authored-By: Claude <claude@anthropic.com>|END_ENTRY\nbbb222|regular|END_ENTRY",
        )
        cursor = {
            "bbb222": {
                "tab_added": 10,
                "tab_deleted": 0,
                "composer_added": 0,
                "composer_deleted": 0,
                "non_ai_added": 5,
                "non_ai_deleted": 0,
                "total_added": 15,
                "total_deleted": 0,
            }
        }
        with patch.object(_mod, "get_cursor_cache", return_value=cursor):
            result = collect_ai_adoption("/fake", "main", "2026-03-01", "2026-04-01")
        # Existing fields
        self.assertEqual(result["co_authored_rate"], 50.0)
        self.assertEqual(result["co_authored_commits"]["total"], 1)
        self.assertIn("cursor_line_attribution", result)
        self.assertEqual(result["cursor_line_attribution"]["commits_matched"], 1)
        # New combined fields
        self.assertEqual(result["ai_assisted_commits"], 2)
        self.assertEqual(result["ai_assisted_commit_rate"], 100.0)


class TestCollectAiAdoptionUsesAllBranches(unittest.TestCase):
    """collect_ai_adoption uses single-branch clone scope — git log must NOT pass --all."""

    @patch.object(_mod, "get_cursor_cache", return_value={})
    @patch.object(
        _mod, "fetch_mr_ai_usage", return_value={"total_mrs": 0, "none": 0, "ai_augmented": 0, "agentic": 0, "na": 0}
    )
    @patch.object(_mod, "run_git")
    def test_git_log_does_not_use_all(self, mock_run_git, _mock_mr, _mock_cursor):
        """git log calls must not pass --all — scope is the single cloned branch."""
        mock_run_git.return_value = ""  # no commits — we only care about the args

        collect_ai_adoption("/repo", "main", "2026-01-01", "2026-02-01")

        for call in mock_run_git.call_args_list:
            args = call[0][0]  # first positional arg is the git argv list
            if "log" not in args:
                continue
            self.assertNotIn("--all", args, "git log must not use --all when cloned single-branch")

    @patch.object(_mod, "get_cursor_cache", return_value={})
    @patch.object(
        _mod, "fetch_mr_ai_usage", return_value={"total_mrs": 0, "none": 0, "ai_augmented": 0, "agentic": 0, "na": 0}
    )
    @patch.object(_mod, "run_git")
    def test_denominator_counts_all_branch_commits(self, mock_run_git, _mock_mr, _mock_cursor):
        """ai_assisted_commit_rate denominator must include commits on all branches.

        Scenario: 10 commits exist across all branches, 1 is AI-assisted.
        With --all the rate should be 10%, not a higher rate from a smaller default-branch count.
        """
        # First call (log with bodies): returns 1 co-authored commit
        co_authored_log = "abc123|Co-authored-by: Claude <claude@anthropic.com>|END_ENTRY"
        # Second call (log for total count): returns 10 SHAs representing all-branch commits
        all_shas = "\n".join(f"sha{i:03d}" for i in range(10))

        mock_run_git.side_effect = [co_authored_log, all_shas, ""]

        result = collect_ai_adoption("/repo", "main", "2026-01-01", "2026-02-01")

        self.assertEqual(result["ai_assisted_commits"], 1)
        self.assertEqual(result["ai_assisted_commit_rate"], 10.0)


class TestFetchLiteLLMSpend(unittest.TestCase):
    """fetch_litellm_spend_by_user returns per-user spend or None when not configured."""

    def setUp(self):
        _litellm_api._litellm_spend_cache.clear()
        _clear_spend_disk_caches()

    def tearDown(self):
        _litellm_api._litellm_spend_cache.clear()
        _clear_spend_disk_caches()

    def test_returns_none_when_env_not_set(self):
        with patch.dict("os.environ", {}, clear=True):
            result = fetch_litellm_spend_by_user("2026-03-01", "2026-04-01")
        self.assertIsNone(result)

    def test_returns_none_on_api_error(self):
        env = {"LITELLM_BASE_URL": "https://litellm.example.com", "LITELLM_API_KEY": "key"}
        with patch.dict("os.environ", env), patch("epi.api.litellm.urlopen", side_effect=URLError("timeout")):
            result = fetch_litellm_spend_by_user("2026-03-01", "2026-04-01")
        self.assertIsNone(result)

    def test_returns_email_to_cost_mapping(self):
        response_data = [
            {"user_id": "alice@example.com", "total_cost": 5.0},
            {"user_id": "bob@example.com", "total_cost": 3.0},
        ]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        env = {"LITELLM_BASE_URL": "https://litellm.example.com", "LITELLM_API_KEY": "key"}
        with patch.dict("os.environ", env), patch("epi.api.litellm.urlopen", return_value=mock_resp):
            result = fetch_litellm_spend_by_user("2026-03-01", "2026-04-01")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["alice@example.com"], 5.0)
        self.assertAlmostEqual(result["bob@example.com"], 3.0)

    def test_uses_global_spend_report_endpoint(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([]).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        env = {"LITELLM_BASE_URL": "https://litellm.example.com", "LITELLM_API_KEY": "key"}
        with patch.dict("os.environ", env), patch("epi.api.litellm.urlopen", return_value=mock_resp) as mock_open:
            fetch_litellm_spend_by_user("2026-03-01", "2026-04-01")
        called_url = mock_open.call_args[0][0].full_url
        self.assertIn("/global/spend/report", called_url)
        self.assertNotIn("/global/spend/users", called_url)

    def test_falls_back_to_api_key_when_no_user_id(self):
        response_data = [
            {"api_key": "sk-alice-key", "total_cost": 4.0},
            {"api_key": "sk-bob-key", "total_cost": 2.0},
        ]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        env = {"LITELLM_BASE_URL": "https://litellm.example.com", "LITELLM_API_KEY": "key"}
        with patch.dict("os.environ", env), patch("epi.api.litellm.urlopen", return_value=mock_resp):
            result = fetch_litellm_spend_by_user("2026-03-01", "2026-04-01")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["sk-alice-key"], 4.0)
        self.assertAlmostEqual(result["sk-bob-key"], 2.0)

    def test_caches_result(self):
        response_data = [{"user_id": "alice@example.com", "total_cost": 5.0}]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        env = {"LITELLM_BASE_URL": "https://litellm.example.com", "LITELLM_API_KEY": "key"}
        with patch.dict("os.environ", env), patch("epi.api.litellm.urlopen", return_value=mock_resp) as mock_open:
            fetch_litellm_spend_by_user("2026-03-01", "2026-04-01")
            fetch_litellm_spend_by_user("2026-03-01", "2026-04-01")
        self.assertEqual(mock_open.call_count, 1)


class TestCollectAiAdoptionLiteLLMSpend(unittest.TestCase):
    """collect_ai_adoption includes litellm_spend_usd from contributor email intersection."""

    NO_MR: ClassVar[dict] = {"total_mrs": 0, "none": 0, "ai_augmented": 0, "agentic": 0, "na": 0}

    def setUp(self):
        _litellm_api._litellm_spend_cache.clear()

    def tearDown(self):
        _litellm_api._litellm_spend_cache.clear()

    @patch.object(_mod, "get_cursor_cache", return_value={})
    @patch.object(_mod, "fetch_mr_ai_usage")
    @patch.object(_mod, "run_git")
    def test_litellm_spend_none_when_no_api_configured(self, mock_run_git, mock_mr, _mock_cursor):
        mock_mr.return_value = self.NO_MR
        mock_run_git.return_value = ""
        with patch.dict("os.environ", {}, clear=True):
            result = collect_ai_adoption("/fake/repo", "main", "2026-03-01", "2026-04-01")
        self.assertIsNone(result["litellm_spend_usd"])

    @patch.object(_mod, "get_cursor_cache", return_value={})
    @patch.object(_mod, "fetch_mr_ai_usage")
    @patch.object(_mod, "run_git")
    def test_litellm_spend_always_none(self, mock_run_git, mock_mr, _mock_cursor):
        # LiteLLM spend requires Enterprise tier and is disabled.
        mock_mr.return_value = self.NO_MR
        mock_run_git.return_value = ""
        result = collect_ai_adoption("/fake/repo", "main", "2026-03-01", "2026-04-01")
        self.assertIsNone(result["litellm_spend_usd"])


class TestMainStaleRepoPath(unittest.TestCase):
    """main() must fall back to workspace/{product}/{repo} when meta.repo_path is a stale absolute path."""

    def _write_metrics_json(self, path: str, repo_path: str, repo_name: str = "repo-b") -> None:
        with open(path, "w") as f:
            json.dump(
                {
                    "meta": {
                        "repo": repo_name,
                        "repo_path": repo_path,
                        "branch": "main",
                        "gitlab_instance": "https://githost.example.com",
                        "project_id": "123",
                        "category": "product",
                    }
                },
                f,
            )

    def test_stale_repo_path_falls_back_to_workspace_clone(self):
        """When meta.repo_path is a non-empty stale path, main() must find the
        repo cloned at workspace/{product}/{repo} and call collect_repo_health."""
        import os
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create product dir with a fake git metrics JSON that has a stale repo_path
            product_dir = os.path.join(tmpdir, "products", "alpha")
            os.makedirs(product_dir)
            stale_path = "/old-jenkins-workspace/workspace/alpha/repo-b"
            self._write_metrics_json(os.path.join(product_dir, "2026-03_repo-b.json"), stale_path)

            # Simulate Jenkinsfile.health cloning the repo into workspace/{product}/{repo}
            workspace_clone = os.path.join(tmpdir, "workspace", "alpha", "repo-b")
            os.makedirs(os.path.join(workspace_clone, ".git"))

            product_config = {
                "alpha": {
                    "gitlab_instance": "https://githost.example.com",
                    "token_env": "GITLAB_TOKEN",
                    "repos": [],
                    "exclude_repos": [],
                }
            }

            with (
                patch(
                    "sys.argv",
                    ["collect-repo-health.py", "--product", "alpha", "--month", "2026-03", "--base-dir", tmpdir],
                ),
                patch.object(_mod, "load_repos_config", return_value=product_config),
                patch.object(_mod, "collect_repo_health") as mock_collect,
            ):
                _mod.main()

            mock_collect.assert_called_once()
            actual_path = mock_collect.call_args[1]["repo_path"]
            self.assertEqual(actual_path, os.path.abspath(workspace_clone))

    def test_valid_repo_path_used_directly(self):
        """When meta.repo_path points to an existing git dir, it should be used as-is."""
        import os
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmpdir:
            product_dir = os.path.join(tmpdir, "products", "alpha")
            os.makedirs(product_dir)

            # The repo_path in the JSON is valid (has a .git dir)
            valid_path = os.path.join(tmpdir, "some", "cloned", "repo-b")
            os.makedirs(os.path.join(valid_path, ".git"))
            self._write_metrics_json(os.path.join(product_dir, "2026-03_repo-b.json"), valid_path)

            product_config = {
                "alpha": {
                    "gitlab_instance": "https://githost.example.com",
                    "token_env": "GITLAB_TOKEN",
                    "repos": [],
                    "exclude_repos": [],
                }
            }

            with (
                patch(
                    "sys.argv",
                    ["collect-repo-health.py", "--product", "alpha", "--month", "2026-03", "--base-dir", tmpdir],
                ),
                patch.object(_mod, "load_repos_config", return_value=product_config),
                patch.object(_mod, "collect_repo_health") as mock_collect,
            ):
                _mod.main()

            mock_collect.assert_called_once()
            actual_path = mock_collect.call_args[1]["repo_path"]
            self.assertEqual(actual_path, os.path.abspath(valid_path))


class TestCloneOnDemand(unittest.TestCase):
    """main() must clone repos on demand when no local copy exists in the health-only pipeline."""

    def _write_metrics_json(self, path: str, repo_name: str = "repo-b") -> None:
        with open(path, "w") as f:
            json.dump(
                {
                    "meta": {
                        "repo": repo_name,
                        "repo_path": "",
                        "branch": "main",
                        "gitlab_instance": "https://githost.example.com",
                        "project_id": "123",
                        "namespace": "org/repo-b",
                        "category": "product",
                    }
                },
                f,
            )

    def test_clones_on_demand_when_no_local_copy(self):
        """When no local clone exists, main() should git clone the repo and call collect_repo_health."""
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            product_dir = os.path.join(tmpdir, "products", "alpha")
            os.makedirs(product_dir)
            self._write_metrics_json(os.path.join(product_dir, "2026-03_repo-b.json"))

            clone_target = os.path.join(tmpdir, "workspace", "alpha", "repo-b")

            def fake_clone(cmd, **kwargs):
                os.makedirs(os.path.join(clone_target, ".git"), exist_ok=True)
                return MagicMock(returncode=0)

            product_config = {
                "alpha": {
                    "gitlab_instance": "https://githost.example.com",
                    "token_env": "GITLAB_TOKEN",
                    "repos": [],
                    "exclude_repos": [],
                }
            }

            with (
                patch(
                    "sys.argv",
                    ["collect-repo-health.py", "--product", "alpha", "--month", "2026-03", "--base-dir", tmpdir],
                ),
                patch.object(_mod, "load_repos_config", return_value=product_config),
                patch.object(_mod, "collect_repo_health") as mock_collect,
                patch.dict("os.environ", {"GITLAB_TOKEN": "test-token"}),
                patch("subprocess.run", side_effect=fake_clone) as mock_subprocess,
            ):
                _mod.main()

            mock_subprocess.assert_called_once()
            clone_cmd = mock_subprocess.call_args[0][0]
            self.assertIn("clone", clone_cmd)
            self.assertIn("oauth2:test-token@githost.example.com", " ".join(clone_cmd))
            mock_collect.assert_called_once()
            actual_path = mock_collect.call_args[1]["repo_path"]
            self.assertEqual(actual_path, os.path.abspath(clone_target))

    def test_skips_when_clone_token_missing(self):
        """When the token env var is absent, no clone attempt is made and repo is skipped."""
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            product_dir = os.path.join(tmpdir, "products", "alpha")
            os.makedirs(product_dir)
            self._write_metrics_json(os.path.join(product_dir, "2026-03_repo-b.json"))

            product_config = {
                "alpha": {
                    "gitlab_instance": "https://githost.example.com",
                    "token_env": "GITLAB_TOKEN",
                    "repos": [],
                    "exclude_repos": [],
                }
            }

            env_without_token = {k: v for k, v in __import__("os").environ.items() if k != "GITLAB_TOKEN"}

            with (
                patch(
                    "sys.argv",
                    ["collect-repo-health.py", "--product", "alpha", "--month", "2026-03", "--base-dir", tmpdir],
                ),
                patch.object(_mod, "load_repos_config", return_value=product_config),
                patch.object(_mod, "collect_repo_health") as mock_collect,
                patch.dict("os.environ", env_without_token, clear=True),
                patch("subprocess.run") as mock_subprocess,
            ):
                _mod.main()

            mock_subprocess.assert_not_called()
            mock_collect.assert_not_called()

    def test_skips_when_clone_fails(self):
        """When git clone fails, the repo is skipped (no collect_repo_health call)."""
        import os
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            product_dir = os.path.join(tmpdir, "products", "alpha")
            os.makedirs(product_dir)
            self._write_metrics_json(os.path.join(product_dir, "2026-03_repo-b.json"))

            product_config = {
                "alpha": {
                    "gitlab_instance": "https://githost.example.com",
                    "token_env": "GITLAB_TOKEN",
                    "repos": [],
                    "exclude_repos": [],
                }
            }

            with (
                patch(
                    "sys.argv",
                    ["collect-repo-health.py", "--product", "alpha", "--month", "2026-03", "--base-dir", tmpdir],
                ),
                patch.object(_mod, "load_repos_config", return_value=product_config),
                patch.object(_mod, "collect_repo_health") as mock_collect,
                patch.dict("os.environ", {"GITLAB_TOKEN": "test-token"}),
                patch("subprocess.run", side_effect=subprocess.CalledProcessError(128, "git", stderr="not found")),
            ):
                _mod.main()

            mock_collect.assert_not_called()


class TestFetchCursorSpend(unittest.TestCase):
    """fetch_cursor_spend_by_user returns per-email USD spend or None when not configured."""

    def setUp(self):
        _cursor_api._cursor_spend_cache.clear()
        _clear_spend_disk_caches()

    def tearDown(self):
        _cursor_api._cursor_spend_cache.clear()
        _clear_spend_disk_caches()

    def test_returns_none_when_no_token(self):
        with patch.dict("os.environ", {}, clear=True):
            result = fetch_cursor_spend_by_user("2026-03-01", "2026-04-01")
        self.assertIsNone(result)

    def test_returns_none_on_api_error(self):
        with (
            patch.dict("os.environ", {"CURSOR_API_TOKEN": "tok"}),
            patch("epi.api.cursor.urlopen", side_effect=URLError("timeout")),
        ):
            result = fetch_cursor_spend_by_user("2026-03-01", "2026-04-01")
        self.assertIsNone(result)

    def test_returns_email_to_usd_mapping(self):
        events = [
            {"userEmail": "alice@example.com", "chargedCents": 500},
            {"userEmail": "bob@example.com", "chargedCents": 300},
            {"userEmail": "alice@example.com", "chargedCents": 200},
        ]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(events).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with (
            patch.dict("os.environ", {"CURSOR_API_TOKEN": "tok"}),
            patch("epi.api.cursor.urlopen", return_value=mock_resp),
        ):
            result = fetch_cursor_spend_by_user("2026-03-01", "2026-04-01")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["alice@example.com"], 7.0)  # (500+200)/100
        self.assertAlmostEqual(result["bob@example.com"], 3.0)  # 300/100

    def test_post_body_uses_epoch_ms(self):
        """Verify the POST body sends epoch milliseconds, not ISO strings."""
        import json as _json

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"[]"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        captured: list = []

        def capture_urlopen(req, timeout=30):
            captured.append(req)
            return mock_resp

        with (
            patch.dict("os.environ", {"CURSOR_API_TOKEN": "tok"}),
            patch("epi.api.cursor.urlopen", side_effect=capture_urlopen),
        ):
            fetch_cursor_spend_by_user("2026-03-01", "2026-04-01")

        self.assertTrue(captured, "urlopen should have been called")
        body = _json.loads(captured[0].data)
        self.assertIsInstance(body["startDate"], int)
        self.assertIsInstance(body["endDate"], int)
        self.assertGreater(body["startDate"], 1_000_000_000_000)  # epoch ms > 1T
        self.assertGreater(body["endDate"], body["startDate"])

    def test_caches_result(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"[]"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with (
            patch.dict("os.environ", {"CURSOR_API_TOKEN": "tok"}),
            patch("epi.api.cursor.urlopen", return_value=mock_resp) as mock_open,
        ):
            fetch_cursor_spend_by_user("2026-03-01", "2026-04-01")
            fetch_cursor_spend_by_user("2026-03-01", "2026-04-01")
        self.assertEqual(mock_open.call_count, 1)


class TestCollectAiAdoptionCursorSpend(unittest.TestCase):
    """collect_ai_adoption includes cursor_spend_usd from contributor email intersection."""

    NO_MR: ClassVar[dict] = {"total_mrs": 0, "none": 0, "ai_augmented": 0, "agentic": 0, "na": 0}

    def setUp(self):
        _cursor_api._cursor_spend_cache.clear()
        _clear_spend_disk_caches()

    def tearDown(self):
        _cursor_api._cursor_spend_cache.clear()
        _clear_spend_disk_caches()

    def _git_side_effect(self, shas: str, log_bodies: str, emails: str = ""):
        def side_effect(args, cwd, default=""):
            joined = " ".join(args)
            if "--format=%H|%b|END_ENTRY" in joined:
                return log_bodies
            if "--format=%H" in joined:
                return shas
            if "--format=%ae" in joined:
                return emails
            return default

        return side_effect

    @patch.object(_mod, "get_cursor_cache", return_value={})
    @patch.object(_mod, "fetch_mr_ai_usage")
    @patch.object(_mod, "fetch_cursor_spend_by_user")
    @patch.object(_mod, "run_git")
    def test_cursor_spend_usd_sums_contributor_emails(self, mock_run_git, mock_spend, mock_mr, _mock_cursor):
        mock_mr.return_value = self.NO_MR
        mock_spend.return_value = {
            "alice@example.com": 20.0,
            "bob@example.com": 10.0,
            "other@example.com": 5.0,
        }
        mock_run_git.side_effect = self._git_side_effect(
            "hash1", "hash1||END_ENTRY", "alice@example.com\nbob@example.com\n"
        )
        result = collect_ai_adoption("/fake/repo", "main", "2026-03-01", "2026-04-01")
        self.assertAlmostEqual(result["cursor_spend_usd"], 30.0)

    @patch.object(_mod, "get_cursor_cache", return_value={})
    @patch.object(_mod, "fetch_mr_ai_usage")
    @patch.object(_mod, "run_git")
    def test_cursor_spend_usd_none_when_no_token(self, mock_run_git, mock_mr, _mock_cursor):
        mock_mr.return_value = self.NO_MR
        mock_run_git.side_effect = self._git_side_effect("", "", "")
        with patch.dict("os.environ", {}, clear=True):
            result = collect_ai_adoption("/fake/repo", "main", "2026-03-01", "2026-04-01")
        self.assertIsNone(result["cursor_spend_usd"])


class TestAiSpendYamlWrite(unittest.TestCase):
    """main() writes cursor company-level monthly total to ai-spend.yaml after a product run."""

    def setUp(self) -> None:
        _cursor_api._cursor_spend_cache.clear()

    def tearDown(self) -> None:
        _cursor_api._cursor_spend_cache.clear()

    def _write_metrics_json(self, path: str, repo_path: str, repo_name: str = "repo-a") -> None:
        with open(path, "w") as f:
            json.dump(
                {
                    "meta": {
                        "repo": repo_name,
                        "repo_path": repo_path,
                        "branch": "main",
                        "gitlab_instance": "",
                        "project_id": "",
                        "category": "product",
                    }
                },
                f,
            )

    def _product_config(self, tmpdir: str) -> dict:
        return {
            "testprod": {
                "gitlab_instance": "",
                "token_env": "",
                "repos": [],
                "exclude_repos": [],
            }
        }

    def test_cursor_total_written_to_yaml_after_product_run(self):
        """When _cursor_spend_cache is populated, main() sums it and writes cursor.{month} to ai-spend.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            product_dir = os.path.join(tmpdir, "products", "testprod")
            os.makedirs(product_dir)
            repo_path = os.path.join(tmpdir, "workspace", "testprod", "repo-a")
            os.makedirs(os.path.join(repo_path, ".git"))
            self._write_metrics_json(os.path.join(product_dir, "2026-01_repo-a.json"), repo_path)

            # Pre-populate the module-level cache with spend data
            _cursor_api._cursor_spend_cache[("2026-01-01", "2026-02-01")] = {
                "alice@x.com": 12.5,
                "bob@x.com": 7.5,
            }

            with (
                patch(
                    "sys.argv",
                    ["collect-repo-health", "--product", "testprod", "--month", "2026-01", "--base-dir", tmpdir],
                ),
                patch.object(_mod, "load_repos_config", return_value=self._product_config(tmpdir)),
                patch.object(_mod, "collect_repo_health"),
            ):
                _mod.main()

            spend_path = os.path.join(tmpdir, "ai-spend.yaml")
            self.assertTrue(os.path.exists(spend_path), "ai-spend.yaml must be created")
            with open(spend_path) as f:
                data = yaml.safe_load(f)
            self.assertIn("cursor", data)
            self.assertAlmostEqual(data["cursor"]["2026-01"], 20.0)

    def test_cursor_yaml_write_preserves_existing_litellm(self):
        """Writing cursor entry must not overwrite an existing litellm section in ai-spend.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            product_dir = os.path.join(tmpdir, "products", "testprod")
            os.makedirs(product_dir)
            repo_path = os.path.join(tmpdir, "workspace", "testprod", "repo-a")
            os.makedirs(os.path.join(repo_path, ".git"))
            self._write_metrics_json(os.path.join(product_dir, "2026-01_repo-a.json"), repo_path)

            # Pre-seed ai-spend.yaml with litellm data
            spend_path = os.path.join(tmpdir, "ai-spend.yaml")
            with open(spend_path, "w") as f:
                f.write('litellm:\n  "2026-01": 1234.5\n')

            _cursor_api._cursor_spend_cache[("2026-01-01", "2026-02-01")] = {"alice@x.com": 50.0}

            with (
                patch(
                    "sys.argv",
                    ["collect-repo-health", "--product", "testprod", "--month", "2026-01", "--base-dir", tmpdir],
                ),
                patch.object(_mod, "load_repos_config", return_value=self._product_config(tmpdir)),
                patch.object(_mod, "collect_repo_health"),
            ):
                _mod.main()

            with open(spend_path) as f:
                data = yaml.safe_load(f)
            # Both keys must be present
            self.assertIn("litellm", data)
            self.assertAlmostEqual(data["litellm"]["2026-01"], 1234.5)
            self.assertIn("cursor", data)
            self.assertAlmostEqual(data["cursor"]["2026-01"], 50.0)

    def test_cursor_yaml_not_written_when_cache_empty(self):
        """When _cursor_spend_cache has no entry for the month, ai-spend.yaml must not gain a cursor key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            product_dir = os.path.join(tmpdir, "products", "testprod")
            os.makedirs(product_dir)
            repo_path = os.path.join(tmpdir, "workspace", "testprod", "repo-a")
            os.makedirs(os.path.join(repo_path, ".git"))
            self._write_metrics_json(os.path.join(product_dir, "2026-01_repo-a.json"), repo_path)

            # Ensure cache has no entry for this month
            _cursor_api._cursor_spend_cache.clear()

            with (
                patch(
                    "sys.argv",
                    ["collect-repo-health", "--product", "testprod", "--month", "2026-01", "--base-dir", tmpdir],
                ),
                patch.object(_mod, "load_repos_config", return_value=self._product_config(tmpdir)),
                patch.object(_mod, "collect_repo_health"),
            ):
                _mod.main()

            spend_path = os.path.join(tmpdir, "ai-spend.yaml")
            if os.path.exists(spend_path):
                with open(spend_path) as f:
                    data = yaml.safe_load(f) or {}
                self.assertNotIn("cursor", data)

    def test_cursor_yaml_not_written_in_single_repo_mode(self):
        """In --repo mode (not --product mode), no cursor entry must be written to ai-spend.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = os.path.join(tmpdir, "some-repo")
            os.makedirs(os.path.join(repo_path, ".git"))

            # Populate cache as if collect_ai_adoption ran
            _cursor_api._cursor_spend_cache[("2026-01-01", "2026-02-01")] = {"alice@x.com": 99.0}

            with (
                patch(
                    "sys.argv",
                    ["collect-repo-health", "--repo", repo_path, "--month", "2026-01"],
                ),
                patch.object(_mod, "collect_repo_health"),
            ):
                _mod.main()

            spend_path = os.path.join(tmpdir, "ai-spend.yaml")
            self.assertFalse(os.path.exists(spend_path), "ai-spend.yaml must NOT be created in single-repo mode")


# ─── REPO NAME FILTER TESTS (HEALTH) ────────────────────────────────────────


class TestRepoNameFilterHealth(unittest.TestCase):
    """--repo-name flag filters collect-repo-health product collection to a single named repo."""

    def _write_metrics_json(self, path: str, repo_name: str, repo_path: str = "") -> None:
        with open(path, "w") as f:
            json.dump(
                {
                    "meta": {
                        "repo": repo_name,
                        "repo_path": repo_path,
                        "branch": "main",
                        "gitlab_instance": "https://githost.example.com",
                        "project_id": "123",
                        "category": "product",
                    }
                },
                f,
            )

    def _product_config(self) -> dict:
        return {
            "alpha": {
                "gitlab_instance": "https://githost.example.com",
                "token_env": "GITLAB_TOKEN",
                "repos": [],
                "exclude_repos": [],
            }
        }

    def test_repo_name_skips_nonmatching_repos(self):
        """When --repo-name is set, only the matching repo's collect_repo_health is called."""
        with tempfile.TemporaryDirectory() as tmpdir:
            product_dir = os.path.join(tmpdir, "products", "alpha")
            os.makedirs(product_dir)

            # Two repos: repo-a and repo-b, both with valid local paths
            work_path = os.path.join(tmpdir, "workspace", "alpha", "repo-a")
            aeplus_path = os.path.join(tmpdir, "workspace", "alpha", "repo-b")
            os.makedirs(os.path.join(work_path, ".git"))
            os.makedirs(os.path.join(aeplus_path, ".git"))
            self._write_metrics_json(os.path.join(product_dir, "2026-02_repo-a.json"), "repo-a", work_path)
            self._write_metrics_json(os.path.join(product_dir, "2026-02_repo-b.json"), "repo-b", aeplus_path)

            with (
                patch(
                    "sys.argv",
                    [
                        "collect-repo-health",
                        "--product",
                        "alpha",
                        "--month",
                        "2026-02",
                        "--repo-name",
                        "repo-a",
                        "--base-dir",
                        tmpdir,
                    ],
                ),
                patch.object(_mod, "load_repos_config", return_value=self._product_config()),
                patch.object(_mod, "collect_repo_health") as mock_collect,
            ):
                _mod.main()

        mock_collect.assert_called_once()
        actual_path = mock_collect.call_args[1]["repo_path"]
        self.assertEqual(actual_path, os.path.abspath(work_path))

    def test_repo_name_no_match_exits_nonzero(self):
        """sys.exit(1) when --repo-name matches no repo in the product JSON files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            product_dir = os.path.join(tmpdir, "products", "alpha")
            os.makedirs(product_dir)

            work_path = os.path.join(tmpdir, "workspace", "alpha", "repo-a")
            os.makedirs(os.path.join(work_path, ".git"))
            self._write_metrics_json(os.path.join(product_dir, "2026-02_repo-a.json"), "repo-a", work_path)

            stderr_capture = StringIO()
            with (
                patch(
                    "sys.argv",
                    [
                        "collect-repo-health",
                        "--product",
                        "alpha",
                        "--month",
                        "2026-02",
                        "--repo-name",
                        "nonexistent",
                        "--base-dir",
                        tmpdir,
                    ],
                ),
                patch.object(_mod, "load_repos_config", return_value=self._product_config()),
                patch.object(_mod, "collect_repo_health"),
                patch("sys.stderr", stderr_capture),
                self.assertRaises(SystemExit) as cm,
            ):
                _mod.main()

        self.assertEqual(cm.exception.code, 1)
        stderr_output = stderr_capture.getvalue()
        self.assertIn("repo-a", stderr_output)

    def test_repo_name_without_product_errors(self):
        """--repo-name without --product must cause a non-zero exit."""
        with (
            patch("sys.argv", ["collect-repo-health", "--month", "2026-02", "--repo-name", "repo-a"]),
            self.assertRaises(SystemExit) as cm,
        ):
            _mod.main()
        self.assertNotEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
