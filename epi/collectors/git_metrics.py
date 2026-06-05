"""collect-git-metrics — Collect automatable EPI git metrics for a repo

Usage (single repo):
    collect-git-metrics --repo /path/to/repo --month 2026-03 [--branch main] [--output /path/to/output.json]

Usage (all repos for a product, from repos.yaml):
    collect-git-metrics --product app_a --month 2026-03

Outputs JSON with all automatable git metrics:
  1. Commits (total + per contributor)
  2. MRs merged (via GitLab API, if configured)
  3. Unique tickets (regex on commit messages)
  4. Lines added/removed
  5. Focus score (% commits < 50 lines changed)
  6. Commit classification + rework rate
  7. Bus factor + knowledge distribution (trailing 6 months)

When using --product mode, reads repos.yaml for repo paths, branches, and
GitLab config. Outputs go to products/<product>/YYYY-MM[_reponame].json.

Environment variables (override repos.yaml or used in single-repo mode):
  GITLAB_TOKEN       — GitLab API token
  GITLAB_INSTANCE    — GitLab instance URL
  GITLAB_PROJECT_ID  — GitLab project ID
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from epi.api.gitlab import fetch_group_projects, fetch_instance_projects, fetch_jira_issue_types, fetch_mrs_created
from epi.config import load_repos_config


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
            # Log stderr for debugging (e.g., branch not found, shallow clone issues)
            cmd_str = "git " + " ".join(args[:3]) + ("..." if len(args) > 3 else "")
            stderr = result.stderr.strip()
            if stderr:
                print(f"  [git warning] {cmd_str} (exit {result.returncode}): {stderr}", file=sys.stderr)
            return default
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        print(f"  [git warning] timed out: git {' '.join(args[:3])}", file=sys.stderr)
        return default
    except FileNotFoundError:
        print("  [git warning] git not found in PATH", file=sys.stderr)
        return default


def month_range(month_str: str) -> tuple[str, str]:
    """Return (start_date, end_date) for a YYYY-MM string."""
    year, mon = int(month_str[:4]), int(month_str[5:7])
    start = f"{year}-{mon:02d}-01"
    if mon == 12:
        end = f"{year + 1}-01-01"
    else:
        end = f"{year}-{mon + 1:02d}-01"
    return start, end


def trailing_start(start_date: str, months: int = 6) -> str:
    """Calculate date N months before start_date."""
    year, mon, day = int(start_date[:4]), int(start_date[5:7]), int(start_date[8:10])
    mon -= months
    while mon < 1:
        mon += 12
        year -= 1
    return f"{year}-{mon:02d}-{day:02d}"


def detect_default_branch(repo_path: str) -> str:
    """Detect the default branch of a repo."""
    ref = run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], repo_path)
    if ref:
        return ref.replace("refs/remotes/origin/", "")
    # Fallback: check for common branch names
    for branch in ["main", "master", "paas", "develop"]:
        result = run_git(["rev-parse", "--verify", f"origin/{branch}"], repo_path)
        if result:
            return branch
    return "main"


def collect_commits(repo_path: str, branch: str, start: str, end: str) -> list[dict[str, str]]:
    """Collect commit data for the month."""
    log = run_git(
        ["log", branch, f"--after={start}", f"--before={end}", "--format=%H|%ae|%s", "--no-merges"],
        repo_path,
    )
    commits = []
    if log:
        for line in log.split("\n"):
            if "|" not in line:
                continue
            parts = line.split("|", 2)
            if len(parts) == 3:
                commits.append(
                    {
                        "hash": parts[0],
                        "email": parts[1],
                        "subject": parts[2],
                    }
                )
    return commits


def collect_all_branch_commits(repo_path: str, start: str, end: str) -> list[dict[str, str]]:
    """Collect commits across all branches for the month.

    Uses --all to count real developer commits, which is important for repos
    using squash merges where the target branch only shows squashed commits.
    """
    log = run_git(
        ["log", "--all", f"--after={start}", f"--before={end}", "--format=%H|%ae|%s", "--no-merges"],
        repo_path,
    )
    commits = []
    if log:
        for line in log.split("\n"):
            if "|" not in line:
                continue
            parts = line.split("|", 2)
            if len(parts) == 3:
                commits.append(
                    {
                        "hash": parts[0],
                        "email": parts[1],
                        "subject": parts[2],
                    }
                )
    return commits


def count_per_contributor(commits: list[dict[str, str]]) -> dict[str, int]:
    """Count commits per contributor (email prefix)."""
    counter: dict[str, int] = Counter()
    for c in commits:
        author = c["email"].split("@")[0]
        counter[author] += 1
    return dict(counter)


def extract_tickets(commits: list[dict[str, str]]) -> list[str]:
    """Extract unique Jira-style ticket IDs from commit messages."""
    tickets = set()
    for c in commits:
        found = re.findall(r"[A-Z]+-\d+", c["subject"])
        tickets.update(found)
    return sorted(tickets)


# Jira issue type → classification category mapping.
# Types not listed here fall through to regex classification.
JIRA_TYPE_MAP: dict[str, str] = {
    "Bug": "bugfix",
    "Story": "feature",
    "Epic": "feature",
}


def build_commit_ticket_map(commits: list[dict[str, str]]) -> dict[str, str]:
    """Map each commit hash to its first Jira ticket ID (if any).

    Commits without ticket references are omitted from the result.
    """
    result: dict[str, str] = {}
    for c in commits:
        found = re.findall(r"[A-Z]+-\d+", c["subject"])
        if found:
            result[c["hash"]] = found[0]
    return result


def count_lines_changed(repo_path: str, branch: str, start: str, end: str) -> tuple[int, int]:
    """Count lines added and removed in the month (all branches)."""
    numstat = run_git(
        ["log", "--all", f"--after={start}", f"--before={end}", "--no-merges", "--numstat", "--format="],
        repo_path,
    )
    added, removed = 0, 0
    if numstat:
        for line in numstat.split("\n"):
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0] != "-":
                try:
                    added += int(parts[0])
                    removed += int(parts[1])
                except ValueError:
                    continue
    return added, removed


def calculate_focus_score(repo_path: str, commits: list[dict[str, str]]) -> dict[str, float | int]:
    """Calculate % of commits with < 50 lines changed."""
    focused = 0
    total_scored = 0

    for c in commits:
        stat = run_git(["diff", "--numstat", f"{c['hash']}^..{c['hash']}"], repo_path)
        if not stat:
            continue

        total_changed = 0
        for line in stat.split("\n"):
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0] != "-":
                try:
                    total_changed += int(parts[0]) + int(parts[1])
                except ValueError:
                    continue

        total_scored += 1
        if total_changed < 50:
            focused += 1

    pct = round(focused / total_scored * 100, 1) if total_scored > 0 else 0.0
    return {"percentage": pct, "focused_commits": focused, "total_scored": total_scored}


def classify_commits(
    commits: list[dict[str, str]],
    jira_types: dict[str, str] | None = None,
) -> dict[str, int]:
    """Classify commits by type based on Jira issue types (when available) or subject line keywords.

    When jira_types is provided, commits referencing a known Jira ticket are classified
    by the ticket's issue type (Bug→bugfix, Story/Epic→feature). Tickets with unmapped
    types (e.g., Task) fall through to regex classification.
    """
    classes = {
        "feature": 0,
        "bugfix": 0,
        "test": 0,
        "tooling": 0,
        "maintenance": 0,
        "docs": 0,
        "other": 0,
    }

    # Patterns use word boundaries to avoid substring false positives
    # (e.g., "prefix" matching "fix", "Docker" matching "doc", "latest" matching "test").
    # Priority order: feature first (respects author intent — "feat: fix up login" is a
    # feature), then bugfix, then tooling/docs (specific), then test, then maintenance
    # (broadest patterns, catch-all).
    feature_patterns = re.compile(
        r"\bfeat\b|(?<!\w)add(?!\w)|\bimplement|\bnew\b"
        r"|\bsupport\b|\benable\b|\bintroduce|\bextend|\bimprove|\bincrease",
        re.I,
    )
    bugfix_patterns = re.compile(r"\bfix(?:e[sd])?\b|\bbug|hotfix|\bpatch(?:e[sd])?\b|\bresolve[sd]?\b|\brepair", re.I)
    tooling_patterns = re.compile(r"\bci\b|ci/|\bbuild\b|\btool|infra|helm|\bdocker\b|pipeline|makefile", re.I)
    docs_patterns = re.compile(r"\bdocs?\b|\breadme\b|\bchangelog\b", re.I)
    test_patterns = re.compile(r"\btests?\b|\bspec\b|\bassert", re.I)
    maint_patterns = re.compile(
        r"\brefactor|\bcleanup|\bclean.up|\brename|\breorgani|\brestructur|\bdeprecat|\bupgrade[sd]?\b"
        r"|\bupdate[sd]?\b|\bbump|\bremove[sd]?\b|\bmigrat|\breplace[sd]?\b|\bmove[sd]?\b|\bsimplif"
        r"|\bconsolidat|\balign|\bswitch|\badjust|\boptimiz|\bchang(?:e[sd]?|ing)\b",
        re.I,
    )

    for c in commits:
        s = c["subject"]

        # Try Jira-based classification first
        if jira_types:
            commit_tickets = re.findall(r"[A-Z]+-\d+", s)
            jira_classified = False
            for tid in commit_tickets:
                issue_type = jira_types.get(tid)
                if issue_type:
                    category = JIRA_TYPE_MAP.get(issue_type)
                    if category:
                        classes[category] += 1
                        jira_classified = True
                        break
                    # Unmapped types (Task, etc.) fall through to regex
            if jira_classified:
                continue

        # Regex fallback
        if feature_patterns.search(s):
            classes["feature"] += 1
        elif bugfix_patterns.search(s):
            classes["bugfix"] += 1
        elif tooling_patterns.search(s):
            classes["tooling"] += 1
        elif docs_patterns.search(s):
            classes["docs"] += 1
        elif test_patterns.search(s):
            classes["test"] += 1
        elif maint_patterns.search(s):
            classes["maintenance"] += 1
        else:
            classes["other"] += 1

    return classes


def classify_mrs(titles: list[str], jira_types: dict[str, str] | None = None) -> dict[str, int]:
    """Classify MR titles using the same patterns as classify_commits.

    Wraps titles as commit-like dicts and delegates to classify_commits
    to avoid duplicating regex patterns.
    """
    pseudo_commits = [{"subject": t} for t in titles if t]
    return classify_commits(pseudo_commits, jira_types=jira_types)


def calculate_bus_factor(repo_path: str, branch: str, trail_start: str, end: str) -> dict[str, int]:
    """Calculate bus factor: min contributors covering 80% of commits (trailing 6mo)."""
    log = run_git(
        ["log", branch, f"--after={trail_start}", f"--before={end}", "--format=%ae", "--no-merges"],
        repo_path,
    )
    if not log:
        return {"value": 0, "trailing_months": 6, "total_trailing_commits": 0}

    counter: Counter[str] = Counter()
    for email in log.split("\n"):
        if email:
            author = email.split("@")[0]
            counter[author] += 1

    total = sum(counter.values())
    threshold = int(total * 0.80)
    cumulative = 0
    bus_factor = 0

    for _author, count in counter.most_common():
        cumulative += count
        bus_factor += 1
        if cumulative >= threshold:
            break

    return {"value": bus_factor, "trailing_months": 6, "total_trailing_commits": total}


def calculate_knowledge_distribution(repo_path: str, branch: str, trail_start: str, end: str) -> dict[str, float | int]:
    """Calculate % of top-level modules with >1 contributor (trailing 6mo)."""
    log = run_git(
        [
            "log",
            branch,
            f"--after={trail_start}",
            f"--before={end}",
            "--no-merges",
            "--format=COMMIT|%ae",
            "--name-only",
        ],
        repo_path,
    )
    if not log:
        return {"percentage": 0.0, "multi_contributor_modules": 0, "total_modules": 0}

    module_authors = defaultdict(set)
    current_author = ""

    for line in log.split("\n"):
        if line.startswith("COMMIT|"):
            email = line.split("|", 1)[1]
            current_author = email.split("@")[0]
        elif line and current_author:
            # Top-level module = first directory component
            module = line.split("/")[0] if "/" in line else "(root)"
            module_authors[module].add(current_author)

    total_modules = len(module_authors)
    multi = sum(1 for authors in module_authors.values() if len(authors) > 1)
    pct = round(multi / total_modules * 100, 1) if total_modules > 0 else 0.0

    return {"percentage": pct, "multi_contributor_modules": multi, "total_modules": total_modules}


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
    path = ""
    if remote.startswith("git@") or remote.startswith("ssh://"):
        match = re.search(r":(.+?)(?:\.git)?$", remote)
        if match:
            path = match.group(1)
    elif remote.startswith("http"):
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


def ensure_repo_cloned(project: dict[str, str], clone_dir: str, token_env: str) -> str | None:
    """
    Ensure a repo is cloned (or fetched) in clone_dir/<name>.
    Returns the local path, or None on failure.
    """
    token = os.environ.get(token_env, "") if token_env else os.environ.get("GITLAB_TOKEN", "")
    repo_path = os.path.join(clone_dir, project["name"])

    # Build authenticated URL: https://oauth2:<token>@host/path.git
    http_url = project["http_url"]
    if token and "://" in http_url:
        scheme, rest = http_url.split("://", 1)
        auth_url = f"{scheme}://oauth2:{token}@{rest}"
    else:
        auth_url = http_url

    if os.path.isdir(os.path.join(repo_path, ".git")):
        print(f"  Fetching {project['name']}...", file=sys.stderr)
        try:
            result = subprocess.run(
                ["git", "fetch", "--all", "--prune"],
                cwd=repo_path,
                capture_output=True,
                timeout=120,
            )
            if result.returncode != 0:
                print(f"  Warning: git fetch failed for {project['name']}", file=sys.stderr)
        except subprocess.TimeoutExpired:
            print(f"  Warning: git fetch timed out for {project['name']} — skipping", file=sys.stderr)
            return None
    else:
        print(f"  Cloning {project['namespace']}...", file=sys.stderr)
        os.makedirs(clone_dir, exist_ok=True)
        try:
            result = subprocess.run(
                ["git", "clone", "--no-single-branch", auth_url, repo_path],
                capture_output=True,
                timeout=300,
            )
            if result.returncode != 0:
                err = result.stderr.decode(errors="replace").strip()
                print(f"  Error cloning {project['name']}: {err}", file=sys.stderr)
                return None
        except subprocess.TimeoutExpired:
            print(f"  Warning: git clone timed out for {project['name']} — skipping", file=sys.stderr)
            return None

    return repo_path


def collect_single_repo(
    repo_path: str,
    month: str,
    branch: str = "",
    gitlab_instance: str = "",
    token_env: str = "",
    project_id: str = "",
    namespace: str = "",
    output_path: str = "",
    jira_instance: str = "",
    jira_token_env: str = "",
    category: str = "product",
) -> dict | None:
    """Collect metrics for a single repo. Returns the output dict."""
    repo_path = os.path.abspath(os.path.expanduser(repo_path))
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        print(f"Error: {repo_path} is not a git repository", file=sys.stderr)
        return None

    start, end = month_range(month)
    trail_start_date = trailing_start(start, 6)
    branch = branch or detect_default_branch(repo_path)
    repo_name = os.path.basename(repo_path)

    print(f"Collecting metrics for {repo_name} ({month}, branch: {branch})...", file=sys.stderr)

    # Diagnostic: verify the branch exists and git works in this repo
    branch_check = run_git(["rev-parse", "--verify", branch], repo_path)
    if not branch_check:
        # Try with origin/ prefix
        branch_check = run_git(["rev-parse", "--verify", f"origin/{branch}"], repo_path)
        if branch_check:
            print(f"  WARNING: branch '{branch}' not found locally, using 'origin/{branch}'", file=sys.stderr)
            branch = f"origin/{branch}"
        else:
            print(f"  ERROR: branch '{branch}' does not exist in {repo_path}", file=sys.stderr)
            branches = run_git(["branch", "-a"], repo_path)
            print(f"  Available branches: {branches}", file=sys.stderr)
    total_commits_check = run_git(["rev-list", "--count", branch], repo_path)
    print(f"  Repo verified: branch={branch}, total history={total_commits_check} commits", file=sys.stderr)

    # 1. Commits
    print("  Collecting commits...", file=sys.stderr)
    branch_commits = collect_commits(repo_path, branch, start, end)
    all_commits = collect_all_branch_commits(repo_path, start, end)
    # Use all-branch commits for totals (real developer commit count)
    contrib = count_per_contributor(all_commits)
    active_contributors = len(contrib)
    print(
        f"  Total commits: {len(all_commits)} across all branches, "
        f"{len(branch_commits)} on {branch} ({active_contributors} contributors)",
        file=sys.stderr,
    )

    # 2. Tickets
    print("  Extracting ticket references...", file=sys.stderr)
    tickets = extract_tickets(all_commits)
    print(f"  Unique tickets: {len(tickets)}", file=sys.stderr)

    # 3. Lines changed
    print("  Counting lines changed...", file=sys.stderr)
    lines_added, lines_removed = count_lines_changed(repo_path, branch, start, end)
    print(f"  Lines: +{lines_added} / -{lines_removed}", file=sys.stderr)

    # 4. Focus score (uses branch commits — analyses what lands on integration branch)
    print("  Calculating focus score...", file=sys.stderr)
    focus = calculate_focus_score(repo_path, branch_commits)
    print(
        f"  Focus score: {focus['percentage']}% ({focus['focused_commits']}/{focus['total_scored']} < 50 lines)",
        file=sys.stderr,
    )

    # 5. MRs created (GitLab API, with git fallback) — fetched early for MR-level rework
    print("  Fetching MRs created...", file=sys.stderr)
    mrs = fetch_mrs_created(start, end, gitlab_instance, token_env, project_id)
    if mrs["gitlab_api_used"]:
        print(f"  MRs created: {mrs['total']} (GitLab API)", file=sys.stderr)
    else:
        # Fallback: count commits across all branches as proxy for MRs.
        mrs["total"] = len(all_commits)
        mrs["git_fallback"] = True
        print(f"  MRs created: {mrs['total']} (git fallback — all-branch commit count)", file=sys.stderr)

    # Calculate MRs per engineer
    mrs_total: int = mrs["total"]  # type: ignore[assignment]
    if active_contributors > 0 and mrs_total > 0:
        mrs["per_engineer"] = round(mrs_total / active_contributors, 1)

    # 6. Jira-based classification enrichment (optional)
    jira_types: dict[str, str] = {}
    if jira_instance and jira_token_env:
        print("  Fetching Jira issue types for ticket-based classification...", file=sys.stderr)
        jira_types = fetch_jira_issue_types(tickets, jira_instance, jira_token_env)
        print(f"  Resolved {len(jira_types)} ticket type(s) from Jira", file=sys.stderr)

    # 7. Commit classification (Jira types override regex when available)
    print("  Classifying commits...", file=sys.stderr)
    classification = classify_commits(all_commits, jira_types=jira_types or None)
    print(f"  Classification: {classification}", file=sys.stderr)

    # 8. MR classification (informational — kept for detail page, not used for rework_rate)
    mr_classification = None
    if mrs.get("gitlab_api_used") and mrs.get("titles"):
        mr_titles: list[str] = mrs["titles"]  # type: ignore[assignment]
        mr_classification = classify_mrs(mr_titles, jira_types=jira_types or None)
        print(f"  MR classification: {mr_classification}", file=sys.stderr)

    # Rework rate — always commit-level
    fb = classification["feature"] + classification["bugfix"]
    rework_pct = round(classification["bugfix"] / fb * 100, 1) if fb > 0 else 0.0
    rework_source = "commit"
    rework_fb = fb
    rework_bugfix = classification["bugfix"]
    print(f"  Rework rate: {rework_pct}% (commit-level, {classification['bugfix']}/{fb})", file=sys.stderr)

    # 9. Bus factor
    print("  Calculating bus factor (trailing 6 months)...", file=sys.stderr)
    bus = calculate_bus_factor(repo_path, branch, trail_start_date, end)
    print(f"  Bus factor: {bus['value']}", file=sys.stderr)

    # 10. Knowledge distribution
    print("  Calculating knowledge distribution...", file=sys.stderr)
    knowledge = calculate_knowledge_distribution(repo_path, branch, trail_start_date, end)
    print(f"  Knowledge distribution: {knowledge['percentage']}%", file=sys.stderr)

    # Strip titles from mrs before writing output (large, not needed downstream)
    mrs_output = {k: v for k, v in mrs.items() if k != "titles"}

    # Build output
    output = {
        "meta": {
            "repo": repo_name,
            "month": month,
            "branch": branch,
            "category": category,
            "collected_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "date_range": {"start": start, "end": end},
            "trailing_window": {"start": trail_start_date, "end": end},
            "gitlab_instance": gitlab_instance,
            "project_id": project_id,
            "namespace": namespace,
        },
        "commits": {
            "total": len(all_commits),
            "branch_commits": len(branch_commits),
            "active_contributors": active_contributors,
            "per_contributor": contrib,
        },
        "tickets": {
            "unique_count": len(tickets),
            "ids": tickets,
        },
        "lines_changed": {
            "added": lines_added,
            "removed": lines_removed,
            "net": lines_added - lines_removed,
        },
        "focus_score": focus,
        "commit_classification": classification,
        "classification_meta": {
            "jira_resolved": len(jira_types),
            "total_commits": len(all_commits),
            "jira_classification_rate": round(len(jira_types) / len(all_commits) * 100, 1) if all_commits else 0.0,
        },
        "mr_classification": mr_classification,
        "rework_rate": {
            "percentage": rework_pct,
            "bugfix_count": rework_bugfix,
            "feature_plus_bugfix": rework_fb,
            "source": rework_source,
        },
        "bus_factor": bus,
        "knowledge_distribution": knowledge,
        "mrs": mrs_output,
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
    """Parse arguments and collect git metrics for configured repositories."""
    parser = argparse.ArgumentParser(description="Collect EPI git metrics")
    parser.add_argument("--repo", help="Path to a single git repository")
    parser.add_argument("--product", help="Product name from repos.yaml (collects all its repos)")
    parser.add_argument("--month", required=True, help="Month to collect (YYYY-MM)")
    parser.add_argument("--branch", default="", help="Branch to analyse (default: from repos.yaml or auto-detect)")
    parser.add_argument("--output", default="", help="Output JSON file (single-repo mode only)")
    parser.add_argument("--base-dir", default=None, help="Base epi-data directory (default: script directory)")
    parser.add_argument(
        "--gitlab-instance", default="", help="GitLab instance URL (auto-detected from git remote if omitted)"
    )
    parser.add_argument("--project-id", default="", help="GitLab project ID (auto-resolved via API if omitted)")
    parser.add_argument(
        "--repo-name",
        default="",
        help="Collect only this named repo within the product (requires --product). "
        "Forces suffixed output filename (YYYY-MM_{name}.json).",
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
        # Product mode: read repos.yaml and collect all repos for this product
        config = load_repos_config(base_dir)
        if args.product not in config:
            print(f"Error: product '{args.product}' not found in repos.yaml", file=sys.stderr)
            print(f"Available products: {', '.join(sorted(config.keys()))}", file=sys.stderr)
            sys.exit(1)

        product_config = config[args.product]
        product_dir = os.path.join(base_dir, "products", args.product)
        os.makedirs(product_dir, exist_ok=True)

        exclude_repos = set(product_config.get("exclude_repos", []))
        support_repos = set(product_config.get("support_repos", []))

        def expand_source(source_conf, product_name):
            """Expand a single source config (with gitlab_instance/token_env/groups/repos) into repo entries."""
            src_instance = source_conf.get("gitlab_instance", "")
            src_token_env = source_conf.get("token_env", "")
            repos = []

            # Expand explicit repos
            for r in source_conf.get("repos", []):
                repos.append((r, src_instance, src_token_env))

            # Expand groups
            for group_conf in source_conf.get("groups", []):
                group_path = group_conf.get("path", "")
                group_branch = group_conf.get("branch", "")
                active_months = group_conf.get("active_months", 6)
                clone_dir = os.path.expanduser(product_config.get("clone_dir", f"~/.epi-clones/{product_name}"))

                print(
                    f"Fetching active projects from group '{group_path}' (last {active_months} months)...",
                    file=sys.stderr,
                )
                group_projects = fetch_group_projects(src_instance, src_token_env, group_path, active_months)
                print(f"  Found {len(group_projects)} active project(s)", file=sys.stderr)

                for p in group_projects:
                    if p["name"] in exclude_repos:
                        print(f"  Skipping excluded repo: {p['name']}", file=sys.stderr)
                        continue
                    local_path = ensure_repo_cloned(p, clone_dir, src_token_env)
                    if local_path:
                        repos.append(
                            (
                                {
                                    "name": p["name"],
                                    "namespace": p["namespace"],
                                    "project_id": p["project_id"],
                                    "path": local_path,
                                    "branch": group_branch,
                                },
                                src_instance,
                                src_token_env,
                            )
                        )

            return repos

        # Build list of (repo_config, gitlab_instance, token_env) tuples
        repo_tuples = []

        if "sources" in product_config:
            # Multi-source product (spanning more than one GitLab instance/group)
            for source in product_config["sources"]:
                repo_tuples.extend(expand_source(source, args.product))

        elif product_config.get("scope") == "instance":
            # Instance-wide discovery (all repos on one GitLab instance)
            inst = product_config.get("gitlab_instance", "")
            tok = product_config.get("token_env", "")
            active_months = product_config.get("active_months", 6)
            exclude_groups = product_config.get("exclude_groups", [])
            clone_dir = os.path.expanduser(product_config.get("clone_dir", f"~/.epi-clones/{args.product}"))

            print(
                f"Fetching active projects from instance '{inst}' "
                f"(last {active_months} months, excluding {exclude_groups})...",
                file=sys.stderr,
            )
            instance_projects = fetch_instance_projects(inst, tok, active_months, exclude_groups)
            print(f"  Found {len(instance_projects)} active project(s)", file=sys.stderr)

            for p in instance_projects:
                if p["name"] in exclude_repos:
                    print(f"  Skipping excluded repo: {p['name']}", file=sys.stderr)
                    continue
                local_path = ensure_repo_cloned(p, clone_dir, tok)
                if local_path:
                    repo_tuples.append(
                        (
                            {
                                "name": p["name"],
                                "namespace": p["namespace"],
                                "project_id": p["project_id"],
                                "path": local_path,
                                "branch": "",
                            },
                            inst,
                            tok,
                        )
                    )

        else:
            # Single-instance product (explicit repo list on one GitLab instance)
            repo_tuples.extend(expand_source(product_config, args.product))

        if not repo_tuples:
            print(f"Error: no repos configured or discovered for product '{args.product}'", file=sys.stderr)
            sys.exit(1)

        # Filter to repos with a path configured
        valid_tuples = [(r, gi, te) for r, gi, te in repo_tuples if r.get("path")]
        if not valid_tuples:
            print(f"Error: no repos with 'path' set for product '{args.product}'", file=sys.stderr)
            print("Set the 'path' field in repos.yaml to the local clone path for each repo.", file=sys.stderr)
            sys.exit(1)

        if args.repo_name:
            filtered = [(r, gi, te) for r, gi, te in valid_tuples if r["name"] == args.repo_name]
            if not filtered:
                available = ", ".join(sorted(r["name"] for r, _, _ in valid_tuples))
                print(f"Error: --repo-name '{args.repo_name}' not found in product '{args.product}'.", file=sys.stderr)
                print(f"Available repos: {available}", file=sys.stderr)
                sys.exit(1)
            valid_tuples = filtered

        jira_instance = product_config.get("jira_instance", "")
        jira_token_env = product_config.get("jira_token_env", "")

        for repo_conf, inst, tok in valid_tuples:
            branch = args.branch or repo_conf.get("branch", "")
            project_id = repo_conf.get("project_id", "")
            namespace = repo_conf.get("namespace", "")
            repo_category = "support" if repo_conf["name"] in support_repos else "product"

            # Output naming: single repo = YYYY-MM.json, multi = YYYY-MM_reponame.json.
            # When --repo-name is set always use the suffixed form to preserve the product's
            # multi-repo layout so the downstream score-epi / report steps see the right file.
            if len(valid_tuples) == 1 and not args.repo_name:
                output_path = os.path.join(product_dir, f"{args.month}.json")
            else:
                output_path = os.path.join(product_dir, f"{args.month}_{repo_conf['name']}.json")

            collect_single_repo(
                repo_path=repo_conf["path"],
                month=args.month,
                branch=branch,
                gitlab_instance=inst,
                token_env=tok,
                project_id=project_id,
                namespace=namespace,
                output_path=output_path,
                jira_instance=jira_instance,
                jira_token_env=jira_token_env,
                category=repo_category,
            )

        print(f"\nDone. Collected {len(valid_tuples)} repo(s) for {args.product}.", file=sys.stderr)

    else:
        # Single repo mode
        repo_path = os.path.abspath(os.path.expanduser(args.repo))
        gitlab_instance = args.gitlab_instance
        project_id = args.project_id

        if not gitlab_instance:
            gitlab_instance, _ = resolve_gitlab_info(repo_path)
            if gitlab_instance:
                print(f"  Auto-detected GitLab instance: {gitlab_instance}", file=sys.stderr)

        token_env = ""  # falls back to GITLAB_TOKEN env var inside fetch_mrs_created

        if gitlab_instance and not project_id:
            project_id = resolve_project_id(repo_path, gitlab_instance, token_env)
            if project_id:
                print(f"  Auto-resolved project ID: {project_id}", file=sys.stderr)

        collect_single_repo(
            repo_path=repo_path,
            month=args.month,
            branch=args.branch,
            gitlab_instance=gitlab_instance,
            token_env=token_env,
            project_id=project_id,
            output_path=args.output,
        )
