"""collect-repo-health — Collect AI Readiness and AI Adoption metrics per repo

Usage:
    collect-repo-health --product app_a --month 2026-03
    collect-repo-health --repo /path/to/repo --month 2026-03

Collects two categories of automated metrics:
  AI Readiness: ticket reference rate, feature-test coupling, test-to-code ratio,
                commit body rate, file existence checks (README, CLAUDE.md, CI)
  AI Adoption:  co-authored commits with tool breakdown, MR AI-usage labels

Output: products/{product}/health-YYYY-MM_{repo}.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml

from epi.api.cursor import _cursor_spend_cache, fetch_cursor_spend_by_user, get_cursor_cache
from epi.api.gitlab import fetch_mr_ai_usage
from epi.config import load_repos_config, load_yaml_file

# ─── GIT HELPERS ─────────────────────────────────────────────────────────────


def run_git(args: list[str], cwd: str, default: str = "") -> str:
    """Run a git command and return stdout."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return default
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return default


def month_range(month_str: str) -> tuple[str, str]:
    """Return (start_date, end_date) for a YYYY-MM string.

    end_date is the exclusive first day of the following month.
    fetch_cursor_commits handles splitting if the range exceeds 30 days.
    """
    year, mon = int(month_str[:4]), int(month_str[5:7])
    start = date(year, mon, 1)
    end = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def detect_default_branch(repo_path: str) -> str:
    """Detect the default branch of a repo."""
    ref = run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], repo_path)
    if ref:
        return ref.replace("refs/remotes/origin/", "")
    for branch in ["main", "master", "paas", "develop"]:
        result = run_git(["rev-parse", "--verify", f"origin/{branch}"], repo_path)
        if result:
            return branch
    return "main"


# ─── AI READINESS METRICS ───────────────────────────────────────────────────

TICKET_PATTERN = re.compile(r"[A-Z]+-\d+")


def collect_ai_readiness(repo_path: str, branch: str, start: str, end: str) -> dict:
    """Collect AI readiness metrics from git history and file system."""

    # Get non-merge commits for the month
    log = run_git(
        ["log", branch, f"--after={start}", f"--before={end}", "--format=%H|%ae|%s|%b|END_BODY", "--no-merges"],
        repo_path,
    )

    commits = []
    if log:
        # Parse commits (body may contain newlines)
        raw_parts = log.split("|END_BODY")
        for part in raw_parts:
            part = part.strip()
            if not part or "|" not in part:
                continue
            # First line after stripping starts with hash|email|subject
            lines = part.split("\n")
            first_line = lines[0].strip()
            pieces = first_line.split("|", 3)
            if len(pieces) >= 3:
                body = pieces[3] if len(pieces) > 3 else ""
                # Include rest of lines as body continuation
                if len(lines) > 1:
                    body += "\n".join(lines[1:])
                commits.append(
                    {
                        "hash": pieces[0],
                        "email": pieces[1],
                        "subject": pieces[2],
                        "body": body.strip(),
                    }
                )

    total_commits = len(commits)

    # 1. Ticket reference rate
    ticket_commits = sum(1 for c in commits if TICKET_PATTERN.search(c["subject"]))
    ticket_reference_rate = round(ticket_commits / total_commits * 100, 1) if total_commits > 0 else 0.0

    # 2. Feature-test coupling
    # Check which commits touch test files
    feature_pattern = re.compile(
        r"feat|(?<!\w)add(?!\w)|implement|(?<!\w)new(?!\w)" r"|support|enable|introduce|extend|improve|increase",
        re.I,
    )
    test_file_pattern = re.compile(r"(test|spec|__tests__)", re.I)

    feature_commits_with_tests = 0
    total_feature_commits = 0

    for c in commits:
        if not feature_pattern.search(c["subject"]):
            continue
        total_feature_commits += 1

        # Check if this commit also touches test files
        files = run_git(["diff-tree", "--no-commit-id", "--name-only", "-r", c["hash"]], repo_path)
        if files:
            file_list = files.split("\n")
            has_test = any(test_file_pattern.search(f) for f in file_list)
            if has_test:
                feature_commits_with_tests += 1

    feature_test_coupling = (
        round(feature_commits_with_tests / total_feature_commits * 100, 1) if total_feature_commits > 0 else 0.0
    )

    # 3. Test-to-code ratio
    test_files_changed = 0
    prod_files_changed = 0
    for c in commits:
        files = run_git(["diff-tree", "--no-commit-id", "--name-only", "-r", c["hash"]], repo_path)
        if files:
            for f in files.split("\n"):
                if test_file_pattern.search(f):
                    test_files_changed += 1
                else:
                    prod_files_changed += 1

    test_to_code_ratio = round(test_files_changed / prod_files_changed, 2) if prod_files_changed > 0 else 0.0

    # 4. Commit body rate — % of commits that include a non-empty body paragraph
    body_commits = sum(1 for c in commits if c["body"])
    commit_body_rate = round(body_commits / total_commits * 100, 1) if total_commits > 0 else 0.0

    # 5. File existence checks
    readme_exists = any(
        os.path.exists(os.path.join(repo_path, f)) for f in ["README.md", "README.rst", "README.txt", "README"]
    )
    claude_md_exists = os.path.exists(os.path.join(repo_path, "CLAUDE.md"))
    ci_config_exists = any(
        os.path.exists(os.path.join(repo_path, f)) for f in [".gitlab-ci.yml", "Jenkinsfile", ".github/workflows"]
    )
    # Check for Claude skills (.claude/skills/*/SKILL.md) or Cursor rules (.cursor/rules/*.mdc)
    claude_skills_dir = os.path.join(repo_path, ".claude", "skills")
    has_claude_skills = (
        any(
            os.path.isfile(os.path.join(claude_skills_dir, d, "SKILL.md"))
            for d in os.listdir(claude_skills_dir)
            if os.path.isdir(os.path.join(claude_skills_dir, d))
        )
        if os.path.isdir(claude_skills_dir)
        else False
    )
    cursor_rules_dir = os.path.join(repo_path, ".cursor", "rules")
    has_cursor_rules = (
        any(
            f.endswith(".mdc")
            for f in os.listdir(cursor_rules_dir)
            if os.path.isfile(os.path.join(cursor_rules_dir, f))
        )
        if os.path.isdir(cursor_rules_dir)
        else False
    )
    skills_defined = has_claude_skills or has_cursor_rules

    return {
        "ticket_reference_rate": {"value": ticket_reference_rate, "source": "automated"},
        "feature_test_coupling": {"value": feature_test_coupling, "source": "automated"},
        "test_to_code_ratio": {"value": test_to_code_ratio, "source": "automated"},
        "commit_body_rate": {"value": commit_body_rate, "source": "automated"},
        "readme_exists": readme_exists,
        "claude_md_exists": claude_md_exists,
        "ci_config_exists": ci_config_exists,
        "skills_defined": skills_defined,
    }


# ─── AI ADOPTION METRICS ────────────────────────────────────────────────────

# Matches both the standard git trailer ("Co-Authored-By:") and a
# parenthetical footer ("(co-authored with <tool>)").
CO_AUTHOR_PATTERN = re.compile(r"Co-[Aa]uthored-[Bb]y:|co-authored with", re.I)
TOOL_PATTERNS = {
    "claude": re.compile(r"claude|anthropic", re.I),
    "copilot": re.compile(r"copilot|github", re.I),
    "cursor": re.compile(r"cursor", re.I),
    "cody": re.compile(r"cody|sourcegraph", re.I),
}


def collect_ai_adoption(
    repo_path: str,
    branch: str,
    start: str,
    end: str,
    gitlab_instance: str = "",
    token_env: str = "",
    project_id: str = "",
) -> dict:
    """Collect AI adoption metrics from git and GitLab API."""

    # 1. Co-authored commits (from git trailers)
    log = run_git(
        ["log", branch, f"--after={start}", f"--before={end}", "--format=%H|%b|END_ENTRY", "--no-merges"],
        repo_path,
    )

    total_commits_raw = run_git(
        ["log", branch, f"--after={start}", f"--before={end}", "--format=%H", "--no-merges"],
        repo_path,
    )
    total_commits = len(total_commits_raw.split("\n")) if total_commits_raw else 0

    co_authored_total = 0
    by_tool: dict[str, int] = Counter()
    commit_hashes = []
    co_authored_shas: set[str] = set()

    if log:
        entries = log.split("|END_ENTRY")
        for entry in entries:
            entry = entry.strip()
            if not entry or "|" not in entry:
                continue
            parts = entry.split("|", 1)
            commit_hash = parts[0].strip()
            if commit_hash:
                commit_hashes.append(commit_hash)
            body = parts[1] if len(parts) > 1 else ""

            if CO_AUTHOR_PATTERN.search(body):
                co_authored_total += 1
                co_authored_shas.add(commit_hash)
                # Detect which tool
                detected = False
                for tool_name, pattern in TOOL_PATTERNS.items():
                    if pattern.search(body):
                        by_tool[tool_name] += 1
                        detected = True
                        break
                if not detected:
                    by_tool["other"] += 1

    co_authored_rate = round(co_authored_total / total_commits * 100, 1) if total_commits > 0 else 0.0

    # 2. MR AI-usage labels (from GitLab API)
    mr_ai_usage = fetch_mr_ai_usage(start, end, gitlab_instance, token_env, project_id)

    # Calculate AI-assisted MR rate
    mr_total = mr_ai_usage.get("total_mrs", 0)
    mr_augmented = mr_ai_usage.get("ai_augmented", 0)
    mr_agentic = mr_ai_usage.get("agentic", 0)
    ai_assisted_mr_rate = round((mr_augmented + mr_agentic) / mr_total * 100, 1) if mr_total > 0 else 0.0

    # 3. Cursor line-level attribution
    cursor_data = get_cursor_cache(start, end)
    cursor_stats = {
        "commits_matched": 0,
        "tab_lines_added": 0,
        "composer_lines_added": 0,
        "non_ai_lines_added": 0,
        "total_lines_added": 0,
    }
    cursor_ai_shas: set[str] = set()
    for h in commit_hashes:
        if h in cursor_data:
            c = cursor_data[h]
            cursor_stats["commits_matched"] += 1
            cursor_stats["tab_lines_added"] += c["tab_added"]
            cursor_stats["composer_lines_added"] += c["composer_added"]
            cursor_stats["non_ai_lines_added"] += c["non_ai_added"]
            cursor_stats["total_lines_added"] += c["total_added"]
            if c["tab_added"] + c["composer_added"] > 0:
                cursor_ai_shas.add(h)

    # 4. Combined AI-assisted commit rate (union of both signals)
    ai_assisted_shas = co_authored_shas | cursor_ai_shas
    ai_assisted_commits = len(ai_assisted_shas)
    ai_assisted_commit_rate = round(ai_assisted_commits / total_commits * 100, 1) if total_commits > 0 else 0.0

    # 5. Per-tool spend for engineers who contributed to this repo in the period
    raw_emails = run_git(["log", branch, "--format=%ae", f"--after={start}", f"--before={end}"], repo_path)
    contributor_emails = {e.strip().lower() for e in raw_emails.splitlines() if e.strip()}
    # LiteLLM per-user spend requires Enterprise tier (/global/spend/report).
    # Disabled until an Enterprise license is in place.
    litellm_spend_usd: float | None = None
    cursor_spend_data = fetch_cursor_spend_by_user(start, end)
    cursor_spend_usd: float | None = (
        round(sum(cursor_spend_data.get(e, 0.0) for e in contributor_emails), 4)
        if cursor_spend_data is not None
        else None
    )

    result = {
        "co_authored_commits": {
            "total": co_authored_total,
            "by_tool": dict(by_tool),
        },
        "co_authored_rate": co_authored_rate,
        "mr_ai_usage": mr_ai_usage,
        "ai_assisted_mr_rate": ai_assisted_mr_rate,
        "ai_assisted_commits": ai_assisted_commits,
        "ai_assisted_commit_rate": ai_assisted_commit_rate,
        "litellm_spend_usd": litellm_spend_usd,
        "cursor_spend_usd": cursor_spend_usd,
    }

    if cursor_stats["commits_matched"] > 0:
        result["cursor_line_attribution"] = cursor_stats

    return result


def resolve_gitlab_info(repo_path: str) -> tuple[str, str]:
    """Auto-detect gitlab_instance and project namespace from git remote URL."""
    remote = run_git(["remote", "get-url", "origin"], repo_path)
    if not remote:
        return "", ""
    if remote.startswith("git@") or remote.startswith("ssh://"):
        match = re.search(r"@([^:/]+)[:/](.+?)(?:\.git)?$", remote)
        if match:
            return f"https://{match.group(1)}", match.group(2)
    elif remote.startswith("http"):
        # Strip oauth2 token if present
        clean = re.sub(r"://[^@]+@", "://", remote)
        match = re.search(r"(https?://[^/]+)/(.+?)(?:\.git)?$", clean)
        if match:
            return match.group(1), match.group(2)
    return "", ""


def resolve_project_id(repo_path: str, gitlab_instance: str, token_env: str) -> str:
    """Resolve GitLab project ID from a cloned repo's remote URL."""
    remote = run_git(["remote", "get-url", "origin"], repo_path)
    if not remote:
        return ""

    # Extract namespace/project from remote URL
    # SSH:  git@githost.example.com:org/group/repo.git
    # HTTPS: https://githost.example.com/org/group/repo.git
    path = ""
    if remote.startswith("git@") or remote.startswith("ssh://"):
        match = re.search(r":(.+?)(?:\.git)?$", remote)
        if match:
            path = match.group(1)
    elif remote.startswith("http"):
        # Strip instance URL prefix and .git suffix
        for prefix in [gitlab_instance, gitlab_instance.rstrip("/")]:
            if remote.startswith(prefix):
                path = remote[len(prefix) :].strip("/").removesuffix(".git")
                break
        if not path:
            match = re.search(r"https?://[^/]+/(.+?)(?:\.git)?$", remote)
            if match:
                path = match.group(1)
        # Strip oauth2 token from path if present
        if "@" in path.split("/")[0]:
            path = "/".join(path.split("/")[1:]) if "/" in path else path

    if not path:
        return ""

    # Look up project by namespace path
    token = os.environ.get(token_env, "") if token_env else os.environ.get("GITLAB_TOKEN", "")
    if not token or not gitlab_instance:
        return ""

    encoded_path = path.replace("/", "%2F")
    url = f"{gitlab_instance}/api/v4/projects/{encoded_path}"
    try:
        req = Request(url, headers={"PRIVATE-TOKEN": token})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return str(data.get("id", ""))
    except (URLError, json.JSONDecodeError, OSError):
        return ""


# ─── MAIN COLLECTION ────────────────────────────────────────────────────────


def collect_repo_health(
    repo_path: str,
    month: str,
    branch: str = "",
    gitlab_instance: str = "",
    token_env: str = "",
    project_id: str = "",
    product: str = "",
    output_path: str = "",
    category: str = "product",
) -> dict | None:
    """Collect health metrics for a single repo."""
    repo_path = os.path.abspath(os.path.expanduser(repo_path))
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        print(f"Error: {repo_path} is not a git repository", file=sys.stderr)
        return None

    start, end = month_range(month)
    branch = branch or detect_default_branch(repo_path)
    repo_name = os.path.basename(repo_path)

    print(f"Collecting health metrics for {repo_name} ({month}, branch: {branch})...", file=sys.stderr)

    # AI Readiness
    print("  Collecting AI readiness metrics...", file=sys.stderr)
    ai_readiness = collect_ai_readiness(repo_path, branch, start, end)

    # AI Adoption
    print("  Collecting AI adoption metrics...", file=sys.stderr)
    ai_adoption = collect_ai_adoption(
        repo_path,
        branch,
        start,
        end,
        gitlab_instance,
        token_env,
        project_id,
    )

    output = {
        "meta": {
            "repo": repo_name,
            "month": month,
            "product": product,
            "category": category,
            "collected_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "ai_readiness": ai_readiness,
        "ai_adoption": ai_adoption,
    }

    json_str = json.dumps(output, indent=2)

    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w") as f:
            f.write(json_str)
        print(f"  Output written to {output_path}", file=sys.stderr)
    else:
        print(json_str)

    return output


def main() -> None:
    """Parse arguments and collect repo health metrics."""
    parser = argparse.ArgumentParser(description="Collect EPI repo health metrics")
    parser.add_argument("--repo", help="Path to a single git repository")
    parser.add_argument("--product", help="Product name from repos.yaml")
    parser.add_argument("--month", required=True, help="Month to collect (YYYY-MM)")
    parser.add_argument("--branch", default="", help="Branch to analyse")
    parser.add_argument("--output", default="", help="Output JSON file (single-repo mode)")
    parser.add_argument("--base-dir", default=None, help="Base epi-data directory")
    parser.add_argument(
        "--gitlab-instance", default="", help="GitLab instance URL (auto-detected from git remote if omitted)"
    )
    parser.add_argument("--project-id", default="", help="GitLab project ID (auto-resolved via API if omitted)")
    parser.add_argument(
        "--repo-name",
        default="",
        help="Collect health only for this named repo within the product (requires --product).",
    )
    args = parser.parse_args()

    if not args.repo and not args.product:
        parser.error("Either --repo or --product is required")

    if args.repo_name and not args.product:
        parser.error("--repo-name requires --product")

    if not re.match(r"^\d{4}-\d{2}$", args.month):
        print("Error: --month must be YYYY-MM format", file=sys.stderr)
        sys.exit(1)

    base_dir = args.base_dir or str(Path(__file__).resolve().parent.parent.parent)

    if args.product:
        config = load_repos_config(base_dir)
        if args.product not in config:
            print(f"Error: product '{args.product}' not found in repos.yaml", file=sys.stderr)
            sys.exit(1)

        product_config = config[args.product]
        product_dir = os.path.join(base_dir, "products", args.product)
        os.makedirs(product_dir, exist_ok=True)

        # Discover repos from per-repo JSON files already collected by collect-git-metrics
        import glob as glob_mod

        repo_jsons = sorted(glob_mod.glob(os.path.join(product_dir, f"{args.month}_*.json")))

        if not repo_jsons:
            print(f"No per-repo JSON files found for {args.product}/{args.month}", file=sys.stderr)
            print("Run collect-git-metrics first.", file=sys.stderr)
            sys.exit(1)

        exclude_repos = set(product_config.get("exclude_repos", []))
        collected = 0
        repo_names_seen: list[str] = []
        for rj in repo_jsons:
            with open(rj) as f:
                data = json.load(f)
            meta = data.get("meta", {})
            repo_name = meta.get("repo", os.path.basename(rj).replace(f"{args.month}_", "").replace(".json", ""))
            if repo_name in exclude_repos:
                print(f"  Skipping excluded repo: {repo_name}", file=sys.stderr)
                continue
            repo_names_seen.append(repo_name)
            if args.repo_name and repo_name != args.repo_name:
                continue
            repo_path = meta.get("repo_path", "")

            # Try to find repo path from git metrics meta or from filesystem.
            # Also run fallback when repo_path is set but stale (e.g. absolute path
            # from a previous full-pipeline workspace that no longer exists).
            if not repo_path or not os.path.isdir(os.path.join(repo_path, ".git")):
                for candidate in [
                    os.path.expanduser(f"~/.epi-clones/{args.product}/{repo_name}"),
                    os.path.join(base_dir, "workspace", args.product, repo_name),
                ]:
                    if os.path.isdir(os.path.join(candidate, ".git")):
                        repo_path = candidate
                        break
                else:
                    # No local clone found — clone on demand so the health-only pipeline
                    # doesn't require a preceding full-pipeline workspace.
                    namespace = meta.get("namespace", "")
                    clone_gi = meta.get("gitlab_instance", "") or product_config.get("gitlab_instance", "")
                    clone_token_env = product_config.get("token_env", "")
                    if "sources" in product_config:
                        for src in product_config["sources"]:
                            if not clone_gi and src.get("gitlab_instance"):
                                clone_gi = src["gitlab_instance"]
                            if src.get("gitlab_instance", "") == clone_gi:
                                clone_token_env = src.get("token_env", "")
                                break
                    clone_token = os.environ.get(clone_token_env, "") if clone_token_env else ""
                    if namespace and clone_gi and clone_token:
                        clone_target = os.path.join(base_dir, "workspace", args.product, repo_name)
                        authed_url = f"{clone_gi}/{namespace}.git".replace("https://", f"https://oauth2:{clone_token}@")
                        branch_for_clone = meta.get("branch", "")
                        # Single-branch avoids enumerating all remote refs — critical for repos
                        # with thousands of open branches (e.g. "work").
                        # Use --shallow-since anchored to the start of the target month so that
                        # historical backfill runs always include the relevant commits regardless
                        # of how active the branch has been since then (depth=100 was too shallow
                        # for long-lived release branches collected months after the fact).
                        p_start, _ = month_range(args.month)
                        clone_cmd = ["git", "clone", "--single-branch", f"--shallow-since={p_start}"]
                        if branch_for_clone:
                            clone_cmd += ["--branch", branch_for_clone]
                        clone_cmd += [authed_url, clone_target]
                        try:
                            subprocess.run(
                                clone_cmd,
                                check=True,
                                capture_output=True,
                                text=True,
                            )
                            repo_path = clone_target
                            print(f"  Cloned {repo_name} on demand", file=sys.stderr)
                        except subprocess.CalledProcessError as exc:
                            print(f"  Could not clone {repo_name}: {exc.stderr}", file=sys.stderr)
                        except OSError as exc:
                            print(f"  Could not clone {repo_name}: {exc}", file=sys.stderr)

            if not repo_path or not os.path.isdir(os.path.join(repo_path, ".git")):
                print(f"  Skipping {repo_name}: repo path not found", file=sys.stderr)
                continue

            branch = args.branch or meta.get("branch", "")

            # Read GitLab connection info from git metrics JSON (stored by collect-git-metrics)
            gitlab_instance = meta.get("gitlab_instance", "")
            project_id = str(meta.get("project_id", ""))
            token_env = ""

            # If git metrics JSON has instance info, resolve the matching token_env
            if gitlab_instance:
                if "sources" in product_config:
                    for source in product_config["sources"]:
                        if source.get("gitlab_instance", "") == gitlab_instance:
                            token_env = source.get("token_env", "")
                            break
                else:
                    token_env = product_config.get("token_env", "")

            # Fallback: read from product config if not in git metrics JSON
            if not gitlab_instance:
                gitlab_instance = product_config.get("gitlab_instance", "")
                token_env = product_config.get("token_env", "")
                if "sources" in product_config:
                    for source in product_config["sources"]:
                        si = source.get("gitlab_instance", "")
                        st = source.get("token_env", "")
                        if si:
                            gitlab_instance = si
                            token_env = st
                            break

            # Fallback: try repos.yaml for project_id
            if not project_id:
                for r in product_config.get("repos", []):
                    if r.get("name") == repo_name:
                        project_id = str(r.get("project_id", ""))
                        break

            # Last resort: resolve project_id from repo's git remote URL via GitLab API
            if not project_id and repo_path and gitlab_instance:
                resolved = resolve_project_id(repo_path, gitlab_instance, token_env)
                if resolved:
                    project_id = resolved
                    print(f"  Resolved project_id={project_id} for {repo_name} via GitLab API", file=sys.stderr)

            repo_category = meta.get("category", "product")
            output_path = os.path.join(product_dir, f"health-{args.month}_{repo_name}.json")
            collect_repo_health(
                repo_path=repo_path,
                month=args.month,
                branch=branch,
                gitlab_instance=gitlab_instance,
                token_env=token_env,
                project_id=project_id,
                product=args.product,
                output_path=output_path,
                category=repo_category,
            )
            collected += 1

        # Exit non-zero when --repo-name was set but matched nothing (prevents silent data gaps)
        if args.repo_name and collected == 0:
            available = ", ".join(sorted(repo_names_seen))
            print(f"Error: --repo-name '{args.repo_name}' not found in product '{args.product}'.", file=sys.stderr)
            print(f"Available repos: {available}", file=sys.stderr)
            sys.exit(1)

        print(f"\nDone. Collected health for {collected} repo(s) for {args.product}.", file=sys.stderr)

        # Write cursor company-level monthly total to ai-spend.yaml
        p_start, p_end = month_range(args.month)
        cursor_cache = _cursor_spend_cache.get((p_start, p_end))
        if cursor_cache is not None:
            company_total = round(sum(cursor_cache.values()), 2)
            spend_path = Path(base_dir) / "ai-spend.yaml"
            try:
                existing = load_yaml_file(spend_path)
                existing.setdefault("cursor", {})[args.month] = company_total
                with open(spend_path, "w") as f:
                    yaml.safe_dump(existing, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
                print(f"  Updated ai-spend.yaml: cursor {args.month} = ${company_total}", file=sys.stderr)
            except Exception as exc:
                print(f"  Could not update ai-spend.yaml: {exc}", file=sys.stderr)

    else:
        # Single repo mode
        repo_path = os.path.abspath(os.path.expanduser(args.repo))
        gitlab_instance = args.gitlab_instance
        project_id = args.project_id

        if not gitlab_instance:
            gitlab_instance, _ = resolve_gitlab_info(repo_path)
            if gitlab_instance:
                print(f"  Auto-detected GitLab instance: {gitlab_instance}", file=sys.stderr)

        token_env = ""  # falls back to GITLAB_TOKEN env var inside fetch_mr_ai_usage

        if gitlab_instance and not project_id:
            project_id = resolve_project_id(repo_path, gitlab_instance, token_env)
            if project_id:
                print(f"  Auto-resolved project ID: {project_id}", file=sys.stderr)

        collect_repo_health(
            repo_path=repo_path,
            month=args.month,
            branch=args.branch,
            gitlab_instance=gitlab_instance,
            token_env=token_env,
            project_id=project_id,
            output_path=args.output,
        )
