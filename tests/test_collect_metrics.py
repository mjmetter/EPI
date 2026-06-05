"""Unit tests for collect-git-metrics.py — ensure_repo_cloned timeout handling."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

import epi.collectors.git_metrics as _gm
from epi.collectors.git_metrics import ensure_repo_cloned

_PROJECT = {
    "name": "repo-a",
    "namespace": "org/group/repo-a",
    "http_url": "https://githost.example.com/org/group/repo-a.git",
    "project_id": "999",
}


class TestEnsureRepoClonedTimeout(unittest.TestCase):
    """ensure_repo_cloned must return None (not raise) when git times out."""

    def test_clone_timeout_returns_none(self):
        """A TimeoutExpired during git clone must return None, not propagate."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git clone", timeout=300)):
            result = ensure_repo_cloned(_PROJECT, "/tmp/epi-clones", "GITLAB_TOKEN")
        self.assertIsNone(result)

    def test_fetch_timeout_returns_none(self):
        """A TimeoutExpired during git fetch must return None, not propagate."""
        # Simulate an already-cloned repo so the fetch branch is taken
        with (
            patch("os.path.isdir", return_value=True),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git fetch", timeout=120)),
        ):
            result = ensure_repo_cloned(_PROJECT, "/tmp/epi-clones", "GITLAB_TOKEN")
        self.assertIsNone(result)

    def test_clone_error_still_returns_none(self):
        """A non-zero exit code during git clone still returns None (regression guard)."""
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stderr = b"fatal: repository not found"
        with patch("subprocess.run", return_value=mock_result):
            result = ensure_repo_cloned(_PROJECT, "/tmp/epi-clones", "GITLAB_TOKEN")
        self.assertIsNone(result)


# ─── REPO NAME FILTER TESTS ──────────────────────────────────────────────────


class TestRepoNameFilter(unittest.TestCase):
    """--repo-name flag filters product collection to a single named repo."""

    def _base_argv(self, extra: list[str] | None = None) -> list[str]:
        args = ["collect-git-metrics", "--product", "alpha", "--month", "2026-02"]
        return args + (extra or [])

    def _product_config(self) -> dict:
        return {
            "alpha": {
                "gitlab_instance": "https://githost.example.com",
                "token_env": "GITLAB_TOKEN",
                "repos": [
                    {"name": "repo-a", "path": "/fake/repo-a", "branch": "main"},
                    {"name": "other-repo", "path": "/fake/other", "branch": "main"},
                ],
                "exclude_repos": [],
            }
        }

    def test_repo_name_filters_valid_tuples(self):
        """Only the matching repo's collect_single_repo is called when --repo-name is set."""
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "products", "alpha"))

            with (
                patch("sys.argv", self._base_argv(["--repo-name", "repo-a", "--base-dir", tmpdir])),
                patch.object(_gm, "load_repos_config", return_value=self._product_config()),
                patch.object(_gm, "collect_single_repo") as mock_collect,
            ):
                _gm.main()

        mock_collect.assert_called_once()
        actual_path = mock_collect.call_args[1]["repo_path"]
        self.assertIn("repo-a", actual_path)

    def test_repo_name_no_match_exits_nonzero(self):
        """sys.exit(1) and available names printed to stderr when --repo-name matches nothing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "products", "alpha"))

            stderr_capture = StringIO()
            with (
                patch("sys.argv", self._base_argv(["--repo-name", "nonexistent", "--base-dir", tmpdir])),
                patch.object(_gm, "load_repos_config", return_value=self._product_config()),
                patch("sys.stderr", stderr_capture),
                self.assertRaises(SystemExit) as cm,
            ):
                _gm.main()

        self.assertEqual(cm.exception.code, 1)
        stderr_output = stderr_capture.getvalue()
        self.assertIn("repo-a", stderr_output)
        self.assertIn("other-repo", stderr_output)

    def test_repo_name_without_product_errors(self):
        """--repo-name without --product must cause an argparse error (non-zero exit)."""
        with (
            patch("sys.argv", ["collect-git-metrics", "--month", "2026-02", "--repo-name", "repo-a"]),
            self.assertRaises(SystemExit) as cm,
        ):
            _gm.main()
        self.assertNotEqual(cm.exception.code, 0)

    def test_repo_name_forces_suffixed_filename(self):
        """Even when filtered to one repo, --repo-name produces YYYY-MM_{name}.json (not YYYY-MM.json)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "products", "alpha"))

            with (
                patch("sys.argv", self._base_argv(["--repo-name", "repo-a", "--base-dir", tmpdir])),
                patch.object(_gm, "load_repos_config", return_value=self._product_config()),
                patch.object(_gm, "collect_single_repo") as mock_collect,
            ):
                _gm.main()

        mock_collect.assert_called_once()
        output_path = mock_collect.call_args[1]["output_path"]
        self.assertIn("2026-02_repo-a.json", output_path)
        self.assertNotIn("2026-02.json", output_path.replace("2026-02_repo-a.json", ""))
