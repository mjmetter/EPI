from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta
from urllib.error import URLError
from urllib.request import Request, urlopen

# ─── JIRA API CLIENT ─────────────────────────────────────────────────────────


def fetch_jira_issue_types(
    ticket_ids: list[str],
    jira_instance: str,
    jira_token_env: str,
) -> dict[str, str]:
    """Batch-fetch Jira issue types for a list of ticket IDs.

    Returns a dict mapping ticket ID → issue type name (e.g., "PROJ-123" → "Bug").
    Tickets that fail to resolve are silently omitted.
    Sub-tasks are resolved to their parent's type (one level).
    """
    token = os.environ.get(jira_token_env, "")
    if not token or not jira_instance:
        return {}

    # Ensure token is base64-encoded (email:api_token format)
    # If it already contains a colon, base64-encode it; otherwise assume pre-encoded.
    if ":" in token:
        token_b64 = base64.b64encode(token.encode()).decode()
    else:
        token_b64 = token

    result: dict[str, str] = {}
    for i, tid in enumerate(ticket_ids):
        if i > 0 and len(ticket_ids) > 20:
            time.sleep(0.05)  # rate limiting for large batches

        url = f"{jira_instance.rstrip('/')}/rest/api/2/issue/{tid}?fields=issuetype,parent"
        try:
            req = Request(
                url,
                headers={
                    "Authorization": f"Basic {token_b64}",
                    "Accept": "application/json",
                },
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

            issue_type = data.get("fields", {}).get("issuetype", {}).get("name", "")

            # Sub-tasks: resolve to parent type (one level)
            if issue_type == "Sub-task":
                parent_type = (
                    data.get("fields", {}).get("parent", {}).get("fields", {}).get("issuetype", {}).get("name", "")
                )
                if parent_type:
                    issue_type = parent_type

            if issue_type:
                result[tid] = issue_type

        except (URLError, json.JSONDecodeError, OSError) as e:
            print(f"  Warning: Jira lookup failed for {tid}: {e}", file=sys.stderr)
            continue

    return result


# ─── LINEAR API CLIENT ────────────────────────────────────────────────────────

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# Linear has no fixed "issue type" enum like Jira's issuetype field — teams
# label issues freely. Match label names against common keywords and normalize
# to the same vocabulary as JIRA_TYPE_MAP (Bug/Story/Epic) so downstream
# classification logic stays tracker-agnostic.
_LINEAR_LABEL_KEYWORDS: list[tuple[str, str]] = [
    ("bug", "Bug"),
    ("feature", "Story"),
    ("story", "Story"),
    ("epic", "Epic"),
]


def fetch_linear_issue_types(
    ticket_ids: list[str],
    token_env: str,
) -> dict[str, str]:
    """Batch-fetch Linear issue labels for a list of ticket identifiers (e.g. "ENG-123").

    Returns a dict mapping ticket ID → normalized type name ("Bug"/"Story"/"Epic"),
    matching fetch_jira_issue_types' output vocabulary. Issues with no matching
    label keyword, or that fail to resolve, are silently omitted.
    """
    token = os.environ.get(token_env, "")
    if not token:
        return {}

    result: dict[str, str] = {}
    for i, tid in enumerate(ticket_ids):
        if i > 0 and len(ticket_ids) > 20:
            time.sleep(0.05)  # rate limiting for large batches

        payload = {
            "query": "query($id: String!) { issue(id: $id) { labels { nodes { name } } } }",
            "variables": {"id": tid},
        }
        try:
            req = Request(
                LINEAR_GRAPHQL_URL,
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": token,
                    "Content-Type": "application/json",
                },
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

            issue = (data.get("data") or {}).get("issue")
            if not issue:
                continue

            label_names = [n["name"].lower() for n in issue.get("labels", {}).get("nodes", [])]
            for keyword, mapped_type in _LINEAR_LABEL_KEYWORDS:
                if any(keyword in name for name in label_names):
                    result[tid] = mapped_type
                    break

        except (URLError, json.JSONDecodeError, OSError) as e:
            print(f"  Warning: Linear lookup failed for {tid}: {e}", file=sys.stderr)
            continue

    return result


# ─── GITLAB API CLIENTS ───────────────────────────────────────────────────────


def fetch_group_projects(
    gitlab_instance: str,
    token_env: str,
    group_path: str,
    active_months: int = 6,
) -> list[dict[str, str]]:
    """
    Return a list of active projects in a GitLab group.

    Each item is a dict with keys: name, namespace, project_id, ssh_url, http_url.
    Projects whose last_activity_at is older than active_months are excluded.
    """
    token = os.environ.get(token_env, "") if token_env else os.environ.get("GITLAB_TOKEN", "")
    if not token:
        print(f"  Warning: no token found in env var '{token_env}' — cannot fetch group projects", file=sys.stderr)
        return []

    cutoff = datetime.utcnow() - timedelta(days=active_months * 30)
    encoded_path = group_path.replace("/", "%2F")
    page = 1
    projects = []

    while True:
        url = (
            f"{gitlab_instance}/api/v4/groups/{encoded_path}/projects"
            f"?include_subgroups=true&per_page=100&page={page}&archived=false"
        )
        try:
            req = Request(url, headers={"PRIVATE-TOKEN": token})
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except (URLError, json.JSONDecodeError, OSError) as e:
            print(f"  GitLab API error fetching group projects: {e}", file=sys.stderr)
            break

        if not isinstance(data, list) or not data:
            break

        for p in data:
            last_activity = p.get("last_activity_at", "")
            if last_activity:
                try:
                    ts = datetime.strptime(last_activity[:19], "%Y-%m-%dT%H:%M:%S")
                    if ts < cutoff:
                        continue
                except ValueError:
                    pass
            projects.append(
                {
                    "name": p["path"],
                    "namespace": p["path_with_namespace"],
                    "project_id": str(p["id"]),
                    "http_url": p["http_url_to_repo"],
                    "ssh_url": p.get("ssh_url_to_repo", ""),
                }
            )

        page += 1
        if len(data) < 100:
            break

    return projects


def fetch_instance_projects(
    gitlab_instance: str,
    token_env: str,
    active_months: int = 6,
    exclude_groups: list[str] | None = None,
) -> list[dict[str, str]]:
    """
    Return a list of active projects across an entire GitLab instance.

    Discovers all projects the token has access to, excluding archived projects
    and projects whose namespace starts with any path in exclude_groups.
    Projects whose last_activity_at is older than active_months are excluded.
    """
    token = os.environ.get(token_env, "") if token_env else os.environ.get("GITLAB_TOKEN", "")
    if not token:
        print(f"  Warning: no token found in env var '{token_env}' — cannot fetch instance projects", file=sys.stderr)
        return []

    exclude_groups = exclude_groups or []
    cutoff = datetime.utcnow() - timedelta(days=active_months * 30)
    page = 1
    projects = []

    while True:
        url = f"{gitlab_instance}/api/v4/projects?membership=true&archived=false&per_page=100&page={page}"
        try:
            req = Request(url, headers={"PRIVATE-TOKEN": token})
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except (URLError, json.JSONDecodeError, OSError) as e:
            print(f"  GitLab API error fetching instance projects: {e}", file=sys.stderr)
            break

        if not isinstance(data, list) or not data:
            break

        for p in data:
            # Filter by activity
            last_activity = p.get("last_activity_at", "")
            if last_activity:
                try:
                    ts = datetime.strptime(last_activity[:19], "%Y-%m-%dT%H:%M:%S")
                    if ts < cutoff:
                        continue
                except ValueError:
                    pass

            # Filter out excluded groups
            ns_path = p.get("namespace", {}).get("full_path", "")
            if any(ns_path == grp or ns_path.startswith(grp + "/") for grp in exclude_groups):
                continue

            projects.append(
                {
                    "name": p["path"],
                    "namespace": p["path_with_namespace"],
                    "project_id": str(p["id"]),
                    "http_url": p["http_url_to_repo"],
                    "ssh_url": p.get("ssh_url_to_repo", ""),
                }
            )

        page += 1
        if len(data) < 100:
            break

    return projects


def fetch_mrs_created(
    start: str,
    end: str,
    gitlab_instance: str = "",
    token_env: str = "",
    project_id: str = "",
) -> dict[str, object]:
    """Fetch MRs created in the period from GitLab API (if credentials available).

    Counts by creation date (stable — doesn't change when MR merges) and includes
    all states (open, merged, closed) so in-flight work is visible immediately.
    Paginates through all results and collects MR titles for classification.
    """
    token = os.environ.get(token_env, "") if token_env else os.environ.get("GITLAB_TOKEN", "")
    instance = gitlab_instance or os.environ.get("GITLAB_INSTANCE", "")
    project_id = project_id or os.environ.get("GITLAB_PROJECT_ID", "")

    if not all([token, instance, project_id]):
        return {"total": 0, "per_engineer": 0.0, "gitlab_api_used": False}

    all_mrs = []
    page = 1

    while True:
        url = (
            f"{instance}/api/v4/projects/{project_id}/merge_requests"
            f"?state=all&created_after={start}T00:00:00Z&created_before={end}T00:00:00Z"
            f"&per_page=100&page={page}"
        )

        try:
            req = Request(url, headers={"PRIVATE-TOKEN": token})
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                if not isinstance(data, list) or not data:
                    break
                all_mrs.extend(data)
                if len(data) < 100:
                    break
                page += 1
        except (URLError, json.JSONDecodeError, OSError) as e:
            print(f"  GitLab API error: {e}", file=sys.stderr)
            break

    titles = [mr.get("title", "") for mr in all_mrs]
    return {
        "total": len(all_mrs),
        "per_engineer": 0.0,
        "gitlab_api_used": True,
        "titles": titles,
    }


def fetch_mr_ai_usage(
    start: str,
    end: str,
    gitlab_instance: str = "",
    token_env: str = "",
    project_id: str = "",
) -> dict[str, int]:
    """Fetch MR AI-usage label data from GitLab API."""
    token = os.environ.get(token_env, "") if token_env else os.environ.get("GITLAB_TOKEN", "")
    instance = gitlab_instance or os.environ.get("GITLAB_INSTANCE", "")
    project_id = project_id or os.environ.get("GITLAB_PROJECT_ID", "")

    result = {"total_mrs": 0, "none": 0, "ai_augmented": 0, "agentic": 0, "na": 0}

    if not all([token, instance, project_id]):
        return result

    page = 1
    all_mrs = []
    while True:
        url = (
            f"{instance}/api/v4/projects/{project_id}/merge_requests"
            f"?state=merged&updated_after={start}T00:00:00Z&updated_before={end}T00:00:00Z"
            f"&per_page=100&page={page}"
        )
        try:
            req = Request(url, headers={"PRIVATE-TOKEN": token})
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                if not isinstance(data, list) or not data:
                    break
                all_mrs.extend(data)
                if len(data) < 100:
                    break
                page += 1
        except (URLError, json.JSONDecodeError, OSError) as e:
            print(f"  GitLab API error fetching MR AI-usage: {e}", file=sys.stderr)
            break

    result["total_mrs"] = len(all_mrs)

    for mr in all_mrs:
        # Check labels for AI-usage indicators
        labels = [
            lbl.lower() if isinstance(lbl, str) else lbl.get("name", "").lower() for lbl in (mr.get("labels") or [])
        ]
        if "agentic" in labels:
            result["agentic"] += 1
        elif "ai-augmented" in labels or "ai_augmented" in labels:
            result["ai_augmented"] += 1
        elif "n/a" in labels or "na" in labels:
            result["na"] += 1
        else:
            result["none"] += 1

    return result
