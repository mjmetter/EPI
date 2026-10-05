"""collect-dora-metrics — Measure the DORA metrics from deployment and incident events

Usage:
    collect-dora-metrics --product app_a --month 2026-03 [--base-dir DIR] [--dry-run]
    record-dora-event --product app_a deployment --repo service-one --sha "$CI_COMMIT_SHA"
    record-dora-event --product app_a incident --id INC-7 --action opened --severity sev1

Replaces hand-entered DORA inputs with measured values. Writes them into the
same products/<product>/manual-YYYY-MM.yaml that score-epi already reads, so the
scoring engine is unchanged. Fields it measured are listed under `measured:`,
which stops the dashboard from flagging those metrics as manual. Fields it does
not measure (features_shipped, cycle_*, notes, ...) are left untouched.

Measured fields:
  deployment_frequency   successful production deployments in the month
  rollbacks              deployments that were a rollback or a hotfix
                         (score-epi derives change_failure_rate = rollbacks / deployments)
  lead_time_median_days  median time from commit (author date) to the deployment that shipped it
  incidents.sev1..sev3   incidents opened in the month, by severity
  mttr_hours             mean time to restore over resolved sev1 + sev2 incidents

Event sources, configured per product in repos.yaml under `dora:`:
  gitlab   Deployments API (an environment, default "production") and incidents
           (issues of type "incident"). Lead time uses the compare API.
  events   products/<product>/dora-events.jsonl, appended by `record-dora-event`
           from any CI/CD pipeline or incident tool. Lead time uses the local
           clone at the repo's `path`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from epi.api.gitlab import (
    fetch_compare_commits,
    fetch_deployments,
    fetch_group_projects,
    fetch_incident_issues,
    fetch_instance_projects,
)
from epi.collectors.git_metrics import month_range, run_git
from epi.config import load_repos_config

UTC = timezone.utc  # noqa: UP017 — datetime.UTC needs Python 3.11; we support 3.9+
EVENTS_FILE = "dora-events.jsonl"
SEVERITIES = ("sev1", "sev2", "sev3")
DEFAULT_HOTFIX_PATTERN = r"^hotfix[/_-]"
DEFAULT_LOOKBACK_DAYS = 90

# GitLab incident `severity` field → EPI severity
_GITLAB_SEVERITY = {"CRITICAL": "sev1", "HIGH": "sev2", "MEDIUM": "sev3", "LOW": "sev3"}
# Labels such as "severity::1", "sev2", "S1", "P0"
_SEVERITY_LABEL = re.compile(r"^(?:severity::\s*|sev\s*|s)([1-4])$|^p([0-3])$", re.IGNORECASE)

# Manual-YAML fields this collector can fill in
DEPLOYMENT_FIELDS = ("deployment_frequency", "rollbacks", "lead_time_median_days")
INCIDENT_FIELDS = ("incidents", "mttr_hours")


@dataclass(frozen=True)
class Deployment:
    """One successful deployment of a repo to the tracked environment."""

    repo: str
    sha: str
    finished_at: datetime
    ref: str = ""
    rollback: bool = False  # explicitly flagged as a rollback by the event source


@dataclass(frozen=True)
class Incident:
    """One incident; resolved_at is None while it is still open."""

    id: str
    severity: str
    opened_at: datetime
    resolved_at: datetime | None = None


# (repo, from_sha, to_sha) → {commit sha: author time} for from_sha..to_sha, merge
# commits excluded, or None when the range cannot be read
CommitTimes = Callable[[str, str, str], "dict[str, datetime] | None"]


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (GitLab or event log) into an aware UTC datetime."""
    ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """Return [start, end) of a YYYY-MM month as aware UTC datetimes."""
    start, end = month_range(month)
    return parse_timestamp(start + "T00:00:00Z"), parse_timestamp(end + "T00:00:00Z")


# ─── METRIC COMPUTATION ──────────────────────────────────────────────────────


def compute_deployment_metrics(
    deployments: list[Deployment],
    month: str,
    commit_times: CommitTimes,
    hotfix_pattern: str = DEFAULT_HOTFIX_PATTERN,
) -> dict:
    """Compute deployment_frequency, rollbacks and lead_time_median_days for a month.

    `deployments` may include deployments from before the month: they are not
    counted, but provide the history needed to detect rollbacks (a redeploy of
    an older SHA) and the previous SHA that bounds each deployment's commits.
    """
    start, end = month_bounds(month)
    hotfix_re = re.compile(hotfix_pattern) if hotfix_pattern else None

    by_repo: dict[str, list[Deployment]] = {}
    for d in sorted(deployments, key=lambda d: d.finished_at):
        by_repo.setdefault(d.repo, []).append(d)

    count = 0
    failures = 0
    lead_times_h: list[float] = []

    for repo, history in by_repo.items():
        seen_shas: set[str] = set()
        shipped: set[str] = set()  # commits already credited to an earlier deployment
        prev: Deployment | None = None
        for d in history:
            in_month = start <= d.finished_at < end
            # Redeploying an older SHA (not a retry of the live one) is a rollback
            redeploy_of_old = d.sha in seen_shas and prev is not None and d.sha != prev.sha
            is_rollback = d.rollback or redeploy_of_old
            is_hotfix = bool(hotfix_re and d.ref and hotfix_re.search(d.ref))

            if in_month:
                count += 1
                if is_rollback or is_hotfix:
                    failures += 1
                if prev is not None and not is_rollback and d.sha != prev.sha:
                    commits = commit_times(repo, prev.sha, d.sha) or {}
                    for sha, authored in commits.items():
                        if sha not in shipped:
                            shipped.add(sha)
                            lead_times_h.append(max(0.0, (d.finished_at - authored).total_seconds() / 3600))

            seen_shas.add(d.sha)
            prev = d

    lead_time_days = round(statistics.median(lead_times_h) / 24, 2) if lead_times_h else None
    return {
        "deployment_frequency": count,
        "rollbacks": failures,
        "lead_time_median_days": lead_time_days,
        "lead_time_commits": len(lead_times_h),
    }


def compute_incident_metrics(incidents: list[Incident], month: str) -> dict:
    """Count incidents opened in the month by severity and compute MTTR.

    MTTR covers sev1 + sev2 only (service-impacting), like import-incidents-csv.
    It is 0.0 when there were no such incidents (scored as Elite) and None when
    there were some but none is resolved yet.
    """
    start, end = month_bounds(month)
    counts = dict.fromkeys(SEVERITIES, 0)
    restore_h: list[float] = []
    unresolved = 0

    for inc in incidents:
        if not (start <= inc.opened_at < end):
            continue
        counts[inc.severity] += 1
        if inc.severity in ("sev1", "sev2"):
            if inc.resolved_at is None:
                unresolved += 1
            else:
                restore_h.append(max(0.0, (inc.resolved_at - inc.opened_at).total_seconds() / 3600))

    if restore_h:
        mttr: float | None = round(statistics.mean(restore_h), 1)
    elif unresolved:
        mttr = None
    else:
        mttr = 0.0

    return {"incidents": counts, "mttr_hours": mttr, "unresolved_incidents": unresolved}


# ─── EVENT LOG SOURCE ────────────────────────────────────────────────────────


def append_event(product_dir: Path, event: dict) -> Path:
    """Append one event as a JSON line to the product's event log."""
    product_dir.mkdir(parents=True, exist_ok=True)
    path = product_dir / EVENTS_FILE
    with path.open("a") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")
    return path


def read_events(product_dir: Path) -> list[dict]:
    """Read the product's event log, skipping blank and malformed lines."""
    path = product_dir / EVENTS_FILE
    if not path.exists():
        return []
    events = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"  Warning: skipping malformed line {lineno} in {path}", file=sys.stderr)
    return events


def deployments_from_events(events: list[dict], environment: str) -> list[Deployment]:
    """Build deployments from `deployment` events for one environment."""
    out = []
    for e in events:
        if e.get("type") != "deployment" or e.get("environment", "production") != environment:
            continue
        try:
            out.append(
                Deployment(
                    repo=str(e["repo"]),
                    sha=str(e["sha"]),
                    finished_at=parse_timestamp(e["timestamp"]),
                    ref=str(e.get("ref", "")),
                    rollback=bool(e.get("rollback", False)),
                )
            )
        except (KeyError, ValueError) as err:
            print(f"  Warning: skipping invalid deployment event {e}: {err}", file=sys.stderr)
    return out


def incidents_from_events(events: list[dict]) -> list[Incident]:
    """Fold `incident` opened/resolved events into incidents, keyed by id."""
    opened: dict[str, Incident] = {}
    resolved: dict[str, datetime] = {}
    for e in events:
        if e.get("type") != "incident":
            continue
        try:
            inc_id = str(e["id"])
            ts = parse_timestamp(e["timestamp"])
            if e.get("action") == "opened":
                severity = e.get("severity", "sev3")
                if severity not in SEVERITIES:
                    raise ValueError(f"unknown severity '{severity}'")
                opened[inc_id] = Incident(id=inc_id, severity=severity, opened_at=ts)
            elif e.get("action") == "resolved":
                resolved[inc_id] = ts
        except (KeyError, ValueError) as err:
            print(f"  Warning: skipping invalid incident event {e}: {err}", file=sys.stderr)

    return [
        Incident(id=i.id, severity=i.severity, opened_at=i.opened_at, resolved_at=resolved.get(i.id))
        for i in opened.values()
    ]


def local_repo_paths(product_config: dict, product: str) -> dict[str, str]:
    """Map repo name → local clone path: explicit `path`s, else clone_dir/<name> (as collect-git-metrics clones)."""
    clone_dir = os.path.expanduser(product_config.get("clone_dir", f"~/.epi-clones/{product}"))
    paths: dict[str, str] = {}
    if os.path.isdir(clone_dir):
        for entry in os.listdir(clone_dir):
            paths[entry] = os.path.join(clone_dir, entry)
    for source in product_config.get("sources", [product_config]):
        for r in source.get("repos", []):
            if r.get("path"):
                paths[r["name"]] = os.path.expanduser(r["path"])
    return paths


def local_commit_times(repo_paths: dict[str, str]) -> CommitTimes:
    """Commit-time lookup backed by local clones (repo name → path)."""

    def lookup(repo: str, from_sha: str, to_sha: str) -> dict[str, datetime] | None:
        path = repo_paths.get(repo)
        if not path or not os.path.isdir(path):
            return None
        out = run_git(["log", "--no-merges", "--format=%H %at", f"{from_sha}..{to_sha}"], path)
        commits = {}
        for line in out.splitlines():
            sha, _, ts = line.partition(" ")
            if ts.isdigit():
                commits[sha] = datetime.fromtimestamp(int(ts), tz=UTC)
        return commits

    return lookup


# ─── GITLAB SOURCE ───────────────────────────────────────────────────────────


def gitlab_severity(issue: dict, default: str = "sev3") -> str:
    """Map a GitLab incident to sev1..sev3 from its severity labels or severity field."""
    for label in issue.get("labels") or []:
        name = label if isinstance(label, str) else label.get("name", "")
        m = _SEVERITY_LABEL.match(name.strip())
        if m:
            level = int(m.group(1)) if m.group(1) else int(m.group(2)) + 1
            return SEVERITIES[min(level, 3) - 1]
    return _GITLAB_SEVERITY.get(str(issue.get("severity", "")).upper(), default)


def gitlab_commit_times(projects: dict[str, tuple[str, str, str]]) -> CommitTimes:
    """Commit-time lookup backed by the GitLab compare API (repo name → instance, token env, project id)."""

    def lookup(repo: str, from_sha: str, to_sha: str) -> dict[str, datetime] | None:
        if repo not in projects:
            return None
        instance, token_env, project_id = projects[repo]
        commits = fetch_compare_commits(instance, token_env, project_id, from_sha, to_sha)
        if commits is None:
            return None
        return {
            c["id"]: parse_timestamp(c["authored_date"])
            for c in commits
            if c.get("id") and c.get("authored_date") and len(c.get("parent_ids") or []) <= 1
        }

    return lookup


def non_product_repos(product_config: dict) -> set[str]:
    """Support and excluded repos: their deployments are not product deployments."""
    return set(product_config.get("exclude_repos", [])) | set(product_config.get("support_repos", []))


def resolve_projects(product_config: dict) -> list[dict]:
    """List the product's GitLab projects (name, project_id, instance, token_env).

    Mirrors collect-git-metrics' product layouts (explicit repos, groups, multi-
    source, instance-wide) without cloning anything.
    """
    skip = non_product_repos(product_config)
    projects: list[dict] = []

    def add(name: str, project_id: str, instance: str, token_env: str) -> None:
        if name not in skip:
            projects.append({"name": name, "project_id": project_id, "instance": instance, "token_env": token_env})

    def expand(source: dict) -> None:
        instance = source.get("gitlab_instance", "")
        token_env = source.get("token_env", "")
        for r in source.get("repos", []):
            add(r["name"], str(r.get("project_id", "")), instance, token_env)
        for group in source.get("groups", []):
            for p in fetch_group_projects(instance, token_env, group.get("path", ""), group.get("active_months", 6)):
                add(p["name"], p["project_id"], instance, token_env)

    if "sources" in product_config:
        for source in product_config["sources"]:
            expand(source)
    elif product_config.get("scope") == "instance":
        instance = product_config.get("gitlab_instance", "")
        token_env = product_config.get("token_env", "")
        for p in fetch_instance_projects(
            instance, token_env, product_config.get("active_months", 6), product_config.get("exclude_groups", [])
        ):
            add(p["name"], p["project_id"], instance, token_env)
    else:
        expand(product_config)

    return projects


def collect_gitlab_deployments(
    projects: list[dict], environment: str, since: datetime, until: datetime
) -> list[Deployment] | None:
    """Fetch successful deployments for all projects. None if any project could not be read."""
    deployments: list[Deployment] = []
    for p in projects:
        if not p["project_id"]:
            print(f"  Warning: repo '{p['name']}' has no project_id — skipping its deployments", file=sys.stderr)
            continue
        raw = fetch_deployments(
            p["instance"],
            p["token_env"],
            p["project_id"],
            environment,
            since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        if raw is None:
            return None
        for d in raw:
            if not d.get("sha") or not d.get("finished_at"):
                continue
            ref = (d.get("deployable") or {}).get("ref") or d.get("ref") or ""
            deployments.append(Deployment(p["name"], d["sha"], parse_timestamp(d["finished_at"]), ref))
    return deployments


def collect_gitlab_incidents(
    instance: str, token_env: str, project_id: str, month: str, default_severity: str
) -> list[Incident] | None:
    """Fetch GitLab incidents opened in the month."""
    start, end = month_range(month)
    raw = fetch_incident_issues(instance, token_env, project_id, f"{start}T00:00:00Z", f"{end}T00:00:00Z")
    if raw is None:
        return None
    return [
        Incident(
            id=str(issue.get("iid", issue.get("id", ""))),
            severity=gitlab_severity(issue, default_severity),
            opened_at=parse_timestamp(issue["created_at"]),
            resolved_at=parse_timestamp(issue["closed_at"]) if issue.get("closed_at") else None,
        )
        for issue in raw
        if issue.get("created_at")
    ]


# ─── MANUAL YAML MERGE ───────────────────────────────────────────────────────

_TEMPLATE_KEYS = (
    "product",
    "month",
    "submitted_by",
    "submitted_date",
    "deployment_frequency",
    "features_shipped",
    "rollbacks",
    "post_release_defects",
    "lead_time_median_days",
    "cycle",
    "cycle_items_committed",
    "cycle_items_delivered",
    "incidents",
    "mttr_hours",
    "notes",
)


def merge_measured(existing: dict, product: str, month: str, measured: dict, details: dict, today: str) -> dict:
    """Merge measured fields into a manual-input dict, keeping everything else.

    Warns when a hand-entered value (one not measured by a previous run) is
    replaced with a different measured value.
    """
    if existing:
        data = dict(existing)
    else:
        data = dict.fromkeys(_TEMPLATE_KEYS)
        data.update(product=product, month=month, submitted_by="collect-dora-metrics", submitted_date=today)

    previously_measured = set(existing.get("measured") or [])
    for key, value in measured.items():
        old = existing.get(key)
        if key not in previously_measured and old not in (None, {}, value):
            print(f"  Note: replacing hand-entered {key}={old!r} with measured {value!r}", file=sys.stderr)
        data[key] = value

    data["measured"] = sorted(previously_measured | set(measured))
    data["dora_collection"] = {**(existing.get("dora_collection") or {}), **details, "collected_at": today}
    return data


def write_manual_yaml(path: Path, product: str, month: str, data: dict) -> None:
    """Write the manual-input YAML with a short provenance header."""
    header = (
        f"# EPI Manual Input — {product} {month}\n"
        f"# Fields listed under `measured` were filled in by collect-dora-metrics;\n"
        f"# re-running it overwrites them. Edit the other fields by hand as usual.\n\n"
    )
    path.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False))


# ─── ORCHESTRATION ───────────────────────────────────────────────────────────


def collect_product(product: str, product_config: dict, month: str, product_dir: Path) -> tuple[dict, dict]:
    """Measure DORA fields for one product/month. Returns (measured_fields, details)."""
    dora = product_config.get("dora") or {}
    deploy_conf = dora.get("deployments", {"source": "gitlab"})
    incident_conf = dora.get("incidents") or {}
    measured: dict = {}
    details: dict = {}
    events: list[dict] | None = None

    start, end = month_bounds(month)

    if deploy_conf and deploy_conf.get("source"):
        source = deploy_conf["source"]
        environment = deploy_conf.get("environment", "production")
        since = start - timedelta(days=deploy_conf.get("lookback_days", DEFAULT_LOOKBACK_DAYS))

        deployments: list[Deployment] | None
        commit_times: CommitTimes
        if source == "gitlab":
            projects = resolve_projects(product_config)
            deployments = collect_gitlab_deployments(projects, environment, since, end)
            commit_times = gitlab_commit_times(
                {p["name"]: (p["instance"], p["token_env"], p["project_id"]) for p in projects}
            )
        elif source == "events":
            events = read_events(product_dir)
            skip = non_product_repos(product_config)
            deployments = [
                d
                for d in deployments_from_events(events, environment)
                if since <= d.finished_at < end and d.repo not in skip
            ]
            commit_times = local_commit_times(local_repo_paths(product_config, product))
        else:
            print(f"  Error: unknown deployments source '{source}' (use gitlab or events)", file=sys.stderr)
            deployments = None

        if deployments is None:
            print("  Deployments: could not be read — leaving deployment fields unchanged", file=sys.stderr)
        else:
            stats = compute_deployment_metrics(
                deployments, month, commit_times, deploy_conf.get("hotfix_ref_pattern", DEFAULT_HOTFIX_PATTERN)
            )
            for key in DEPLOYMENT_FIELDS:
                if stats[key] is not None:
                    measured[key] = stats[key]
            details["deployments"] = {
                "source": source,
                "environment": environment,
                "lead_time_commits": stats["lead_time_commits"],
            }
            print(
                f"  Deployments ({source}, {environment}): {stats['deployment_frequency']} deployed, "
                f"{stats['rollbacks']} rollback/hotfix, lead time median "
                f"{stats['lead_time_median_days']} days over {stats['lead_time_commits']} commits",
                file=sys.stderr,
            )

    if incident_conf.get("source"):
        source = incident_conf["source"]
        incidents: list[Incident] | None
        if source == "gitlab":
            incidents = collect_gitlab_incidents(
                incident_conf.get("gitlab_instance", product_config.get("gitlab_instance", "")),
                incident_conf.get("token_env", product_config.get("token_env", "")),
                str(incident_conf.get("project_id", "")),
                month,
                incident_conf.get("default_severity", "sev3"),
            )
        elif source == "events":
            incidents = incidents_from_events(events if events is not None else read_events(product_dir))
        else:
            print(f"  Error: unknown incidents source '{source}' (use gitlab or events)", file=sys.stderr)
            incidents = None

        if incidents is None:
            print("  Incidents: could not be read — leaving incident fields unchanged", file=sys.stderr)
        else:
            stats = compute_incident_metrics(incidents, month)
            measured["incidents"] = stats["incidents"]
            if stats["mttr_hours"] is not None:
                measured["mttr_hours"] = stats["mttr_hours"]
            details["incidents"] = {"source": source, "unresolved_sev1_sev2": stats["unresolved_incidents"]}
            sev = stats["incidents"]
            print(
                f"  Incidents ({source}): sev1={sev['sev1']} sev2={sev['sev2']} sev3={sev['sev3']}, "
                f"MTTR {stats['mttr_hours']}h ({stats['unresolved_incidents']} sev1/sev2 still open)",
                file=sys.stderr,
            )

    return measured, details


def _default_base_dir() -> str:
    return str(Path(__file__).resolve().parent.parent.parent)


def main() -> None:
    """Measure DORA fields for a product/month and merge them into its manual YAML."""
    parser = argparse.ArgumentParser(description="Measure DORA metrics from deployment and incident events")
    parser.add_argument("--product", required=True, help="Product name from repos.yaml")
    parser.add_argument("--month", required=True, help="Month to collect (YYYY-MM)")
    parser.add_argument("--base-dir", default=None, help="Base epi-data directory (default: script directory)")
    parser.add_argument("--dry-run", action="store_true", help="Print the result instead of writing the YAML")
    args = parser.parse_args()

    if not re.match(r"^\d{4}-\d{2}$", args.month):
        print("Error: --month must be YYYY-MM format", file=sys.stderr)
        sys.exit(1)

    base_dir = args.base_dir or _default_base_dir()
    config = load_repos_config(base_dir)
    if args.product not in config:
        print(f"Error: product '{args.product}' not found in repos.yaml", file=sys.stderr)
        print(f"Available products: {', '.join(sorted(config.keys()))}", file=sys.stderr)
        sys.exit(1)

    product_dir = Path(base_dir) / "products" / args.product
    print(f"Collecting DORA metrics for {args.product} {args.month}...", file=sys.stderr)
    measured, details = collect_product(args.product, config[args.product], args.month, product_dir)

    if not measured:
        print("Nothing measured — manual YAML left unchanged.", file=sys.stderr)
        sys.exit(1)

    path = product_dir / f"manual-{args.month}.yaml"
    existing: dict = {}
    if path.exists():
        existing = yaml.safe_load(path.read_text()) or {}
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    data = merge_measured(existing, args.product, args.month, measured, details, today)

    if args.dry_run:
        print(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        return

    product_dir.mkdir(parents=True, exist_ok=True)
    write_manual_yaml(path, args.product, args.month, data)
    print(f"Wrote {path}", file=sys.stderr)


def record_event_main() -> None:
    """Append a deployment or incident event to a product's DORA event log (for CI/CD hooks)."""
    parser = argparse.ArgumentParser(description="Record a DORA deployment or incident event")
    parser.add_argument("--product", required=True, help="Product name from repos.yaml")
    parser.add_argument("--base-dir", default=None, help="Base epi-data directory (default: script directory)")
    parser.add_argument("--timestamp", default="", help="ISO-8601 event time (default: now, UTC)")
    sub = parser.add_subparsers(dest="kind", required=True)

    dep = sub.add_parser("deployment", help="A successful deployment")
    dep.add_argument("--repo", required=True, help="Repo name as listed in repos.yaml")
    dep.add_argument("--sha", required=True, help="Deployed commit SHA")
    dep.add_argument("--ref", default="", help="Branch or tag deployed (hotfix refs count as failures)")
    dep.add_argument("--environment", default="production")
    dep.add_argument("--rollback", action="store_true", help="This deployment rolls back a failed change")

    inc = sub.add_parser("incident", help="An incident being opened or resolved")
    inc.add_argument("--id", required=True, help="Incident identifier, shared by its opened/resolved events")
    inc.add_argument("--action", required=True, choices=["opened", "resolved"])
    inc.add_argument("--severity", choices=SEVERITIES, help="Required when --action opened")

    args = parser.parse_args()
    timestamp = args.timestamp or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        parse_timestamp(timestamp)
    except ValueError:
        parser.error(f"--timestamp is not ISO-8601: {timestamp}")

    event: dict
    if args.kind == "deployment":
        event = {
            "type": "deployment",
            "repo": args.repo,
            "sha": args.sha,
            "ref": args.ref,
            "environment": args.environment,
            "rollback": args.rollback,
            "timestamp": timestamp,
        }
    else:
        if args.action == "opened" and not args.severity:
            parser.error("--severity is required when --action opened")
        event = {"type": "incident", "id": args.id, "action": args.action, "timestamp": timestamp}
        if args.severity:
            event["severity"] = args.severity

    path = append_event(Path(args.base_dir or _default_base_dir()) / "products" / args.product, event)
    print(f"Recorded {args.kind} event → {path}", file=sys.stderr)
