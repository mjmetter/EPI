"""Convert incident CSV exports into EPI manual-input YAML files.

Usage:
  python3 import-incidents-csv.py --csv incidents.csv --product app_a --year 2026
  python3 import-incidents-csv.py --csv incidents.csv --product app_a --month 2026-01
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

SEVERITY_MAP = {
    "P0": "sev1",
    "P1": "sev2",
    "P2": "sev3",
}


def parse_duration_to_hours(raw: str) -> float | None:
    """Parse a human-written duration string into hours.

    Handles: 1h, 43m, 30min, 8days, 25d, 3.5h, 10m, 1d, ongoing, empty.
    Returns None for unparseable/empty/ongoing values.
    """
    if not raw:
        return None
    raw = raw.strip().lower()
    if raw in ("ongoing", "n/a", "-", ""):
        return None

    # Try patterns from most specific to least
    # Days: 8days, 25d, 1d
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:days?|d)", raw)
    if m:
        return float(m.group(1)) * 24.0

    # Hours: 1h, 3.5h
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?", raw)
    if m:
        return float(m.group(1))

    # Minutes: 43m, 30min, 10m
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:min(?:utes?)?|m)", raw)
    if m:
        return float(m.group(1)) / 60.0

    return None


def parse_month_from_occurrence(occurrence: str) -> str | None:
    """Extract YYYY-MM from an occurrence date like 'January 9, 2026'."""
    months = {
        "january": "01",
        "february": "02",
        "march": "03",
        "april": "04",
        "may": "05",
        "june": "06",
        "july": "07",
        "august": "08",
        "september": "09",
        "october": "10",
        "november": "11",
        "december": "12",
    }
    m = re.match(r"(\w+)\s+\d+,?\s+(\d{4})", occurrence.strip())
    if m:
        month_name = m.group(1).lower()
        year = m.group(2)
        if month_name in months:
            return f"{year}-{months[month_name]}"
    return None


def read_incidents(csv_path: Path) -> list:
    """Read CSV and return list of incident dicts."""
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            # Skip empty rows
            if not any(v.strip() for v in row.values() if v):
                continue
            rows.append(row)
    return rows


def group_by_month(incidents: list, year_filter: str | None, month_filter: str | None) -> dict:
    """Group incidents by YYYY-MM, applying optional filters."""
    groups = defaultdict(list)
    for inc in incidents:
        # Determine month from occurrence date
        month_key = parse_month_from_occurrence(inc.get("occurence", ""))
        if not month_key:
            # Fallback: use Month column + year from occurrence or filter
            month_num = inc.get("Month", "").strip()
            if month_num and year_filter:
                month_key = f"{year_filter}-{int(month_num):02d}"
            else:
                print(f"  WARNING: Could not determine month for: {inc.get('Issue title', '?')}", file=sys.stderr)
                continue

        # Apply filters
        if month_filter and month_key != month_filter:
            continue
        if year_filter and not month_key.startswith(year_filter):
            continue

        groups[month_key].append(inc)
    return dict(sorted(groups.items()))


def compute_month_stats(incidents: list) -> dict:
    """Compute severity counts and average MTTR for a month's incidents.

    MTTR is calculated from P0+P1 incidents only (service-impacting).
    P2 durations are excluded — they reflect prioritization, not response speed.
    """
    sev_counts = {"sev1": 0, "sev2": 0, "sev3": 0}
    mttr_durations = []  # P0+P1 only
    notes_parts = []

    for inc in incidents:
        classification = inc.get("classification", "").strip().upper()
        sev_key = SEVERITY_MAP.get(classification)
        if sev_key:
            sev_counts[sev_key] += 1

        duration = parse_duration_to_hours(inc.get("impact", ""))
        # Only P0/P1 contribute to MTTR (DORA: time to restore service)
        if duration is not None and classification in ("P0", "P1"):
            mttr_durations.append(duration)

        title = inc.get("Issue title", "").strip()
        comment = inc.get("comments", "").strip()
        if title:
            entry = f"- [{classification}] {title}: {comment}" if comment else f"- [{classification}] {title}"
            notes_parts.append(entry)

    mttr = round(sum(mttr_durations) / len(mttr_durations), 1) if mttr_durations else 0.0
    notes = "\n".join(notes_parts)

    return {
        "sev1": sev_counts["sev1"],
        "sev2": sev_counts["sev2"],
        "sev3": sev_counts["sev3"],
        "mttr_hours": mttr,
        "notes": notes,
        "total": sum(sev_counts.values()),
        "durations_count": len(mttr_durations),
    }


def generate_yaml(product: str, month: str, stats: dict) -> str:
    """Generate manual-input YAML content."""
    # Escape notes for YAML multiline
    notes_block = stats["notes"].replace("\n", "\n    ")

    return f"""# EPI Manual Input — {product} {month}
# Auto-generated by import-incidents-csv.py
# Source: incidents CSV export

product: "{product}"
month: "{month}"
submitted_by: "import-incidents-csv.py"
submitted_date: ""

# --- Delivery Velocity (not populated by incident data) ---
deployment_frequency: null    # Fill manually or via CI data
features_shipped: null        # Fill manually or via your issue tracker (Jira, Linear, etc.)

# --- Delivery Quality (not populated by incident data) ---
rollbacks: null               # Fill manually
post_release_defects: null    # Fill manually

# --- Engineering Efficiency (not populated by incident data) ---
lead_time_median_days: null   # Fill manually or via git+CI
cycle_items_committed: null   # Fill from sprint planning
cycle_items_delivered: null   # Fill from sprint review

# --- Engineering Health (from incident CSV) ---
incidents:
  sev1: {stats["sev1"]}                     # P0 — Critical
  sev2: {stats["sev2"]}                     # P1 — Major
  sev3: {stats["sev3"]}                     # P2 — Minor

mttr_hours: {stats["mttr_hours"]}  # MTTR — P0+P1 only ({stats["durations_count"]} incidents)

# --- Notes ---
notes: |
    {notes_block}
"""


def main():
    """Parse arguments and import incident data from CSV."""
    parser = argparse.ArgumentParser(description="Convert incident CSV to EPI manual-input YAML")
    parser.add_argument("--csv", required=True, help="Path to incidents CSV file")
    parser.add_argument("--product", required=True, help="Product name (e.g., app_a)")
    parser.add_argument("--year", help="Filter to year (e.g., 2026)")
    parser.add_argument("--month", help="Filter to specific month (e.g., 2026-01)")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    product_dir = Path("products") / args.product
    if not product_dir.exists():
        product_dir.mkdir(parents=True, exist_ok=True)
        print(f"  Created directory: {product_dir}", file=sys.stderr)

    # Read and group incidents
    incidents = read_incidents(csv_path)
    print(f"  Read {len(incidents)} incidents from {csv_path}", file=sys.stderr)

    groups = group_by_month(incidents, args.year, args.month)

    if not groups:
        print("  WARNING: No incidents matched the filters.", file=sys.stderr)
        sys.exit(0)

    # Generate YAML for each month
    for month_key, month_incidents in groups.items():
        stats = compute_month_stats(month_incidents)
        yaml_content = generate_yaml(args.product, month_key, stats)

        out_path = product_dir / f"manual-{month_key}.yaml"
        out_path.write_text(yaml_content)

        total = stats["total"]
        print(
            f"  {month_key}: {total} incidents "
            f"(P0={stats['sev1']}, P1={stats['sev2']}, P2={stats['sev3']}), "
            f"MTTR={stats['mttr_hours']}h "
            f"→ {out_path}",
            file=sys.stderr,
        )

    print(f"\n  Done. Generated {len(groups)} file(s).", file=sys.stderr)
