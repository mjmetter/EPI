"""Import uptime monitoring exports into EPI manual-input YAML health fields.

Reads tab-separated uptime data (Check Name / Uptime / Downtime / Outages / Response Time)
from a file or stdin and computes MTTR + incident counts, then writes them to
products/<product>/manual-<month>.yaml.

Usage:
  # From a saved file:
  python3 import-uptime.py --product app_a --month 2026-01 --input uptime.txt

  # Paste directly (Ctrl+D to finish):
  python3 import-uptime.py --product app_a --month 2026-01

  # Exclude noisy internal checks:
  python3 import-uptime.py --product app_a --month 2025-12 \\
      --input uptime.txt --exclude "Noisy Check Name"
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import yaml

# ─── PARSING ──────────────────────────────────────────────────────────────────


def parse_downtime_min(s: str) -> float:
    """Parse 'Xh Ym Zs' downtime string → total minutes (float).

    Examples: '0h 32m 00s' → 32.0, '62h 53m 45s' → 3773.75, '0h 01m 58s' → 1.967
    """
    h = int(m.group(1)) if (m := re.search(r"(\d+)h", s)) else 0
    mins = int(m.group(1)) if (m := re.search(r"(\d+)m", s)) else 0
    secs = int(m.group(1)) if (m := re.search(r"(\d+)s", s)) else 0
    return h * 60.0 + mins + secs / 60.0


def parse_uptime_tsv(text: str) -> list[dict]:
    """Parse tab-separated uptime export text → list of row dicts.

    Skips the header row (CHECK NAME / UPTIME / ...) and blank lines.
    """
    rows = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name = parts[0].strip()
        if not name or name.upper().startswith("CHECK NAME"):
            continue
        try:
            outages = int(parts[3].strip())
        except ValueError:
            continue
        rows.append(
            {
                "name": name,
                "uptime": parts[1].strip(),
                "downtime_raw": parts[2].strip(),
                "outages": outages,
            }
        )
    return rows


# ─── EXCLUSION ────────────────────────────────────────────────────────────────


def should_exclude(name: str, uptime: str, extra_patterns: list[str]) -> bool:
    """Return True if this check should be excluded from MTTR calculation.

    Auto-excludes:
      - Checks marked '(non-alerting)' — explicitly informational only
      - Checks with 0.00% uptime — dead / decommissioned monitors
    Also excludes any names matching extra_patterns (case-insensitive substring).
    """
    name_lower = name.lower()
    if "(non-alerting)" in name_lower:
        return True
    if uptime.strip() == "0.00%":
        return True
    return any(p.lower() in name_lower for p in extra_patterns)


# ─── COMPUTATION ──────────────────────────────────────────────────────────────


def compute_health(rows: list[dict], extra_excludes: list[str]) -> dict:
    """Compute MTTR and incident totals from parsed uptime rows.

    Returns:
      total_downtime_min: float — sum of included downtime in minutes
      total_incidents:    int   — sum of included outage counts
      mttr_hours:         float | None — total_downtime_min / total_incidents / 60
      included_count:     int   — number of checks used
      excluded_count:     int   — number of checks skipped
      top_checks:         list  — top 5 checks by downtime (for notes)
    """
    included: list[tuple[str, float, int]] = []  # (name, downtime_min, outages)
    excluded_count = 0

    for row in rows:
        if should_exclude(row["name"], row["uptime"], extra_excludes):
            excluded_count += 1
            continue
        dt_min = parse_downtime_min(row["downtime_raw"])
        included.append((row["name"], dt_min, row["outages"]))

    total_min = sum(dt for _, dt, _ in included)
    total_inc = sum(inc for _, _, inc in included)
    mttr_h = round(total_min / total_inc / 60, 2) if total_inc > 0 else None

    top = sorted(included, key=lambda x: x[1], reverse=True)[:5]

    return {
        "total_downtime_min": total_min,
        "total_incidents": total_inc,
        "mttr_hours": mttr_h,
        "included_count": len(included),
        "excluded_count": excluded_count,
        "top_checks": top,
    }


# ─── YAML I/O ─────────────────────────────────────────────────────────────────

_NULL = "null"


def _fmt(v: object) -> str:
    return _NULL if v is None else str(v)


def _load_existing(path: Path) -> dict:
    """Load existing YAML to preserve non-health fields."""
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


def write_yaml(path: Path, product: str, month: str, health: dict, today: str, preserve: dict) -> None:
    """Write (or overwrite) the manual YAML, preserving non-health fields."""
    p = preserve

    def _v(key: str) -> str:
        val = p.get(key)
        return _NULL if val is None else str(val)

    # Incident breakdown: all counted checks go to sev3 (monitoring alerts)
    sev3 = health["total_incidents"]
    mttr = health["mttr_hours"]

    # Build notes
    top = health["top_checks"]
    top_lines = "\n    ".join(f"{name}: {dt:.0f}m / {inc} incident{'s' if inc != 1 else ''}" for name, dt, inc in top)
    notes = (
        f"MTTR derived from uptime monitoring data ({health['included_count']} checks, "
        f"{health['excluded_count']} excluded).\n"
        f"    Top checks by downtime:\n"
        f"    {top_lines}\n"
        f"    Total: {health['total_downtime_min']:.0f} min downtime, "
        f"{health['total_incidents']} incidents. All sev3 (monitoring alerts)."
    )

    existing_notes = p.get("notes", "")
    if existing_notes and not str(existing_notes).startswith("MTTR derived"):
        # Preserve hand-written notes that aren't from a previous import
        notes = str(existing_notes).strip() + "\n    " + notes

    if health["total_incidents"] > 0:
        mttr_line = (
            f"mttr_hours: {_fmt(mttr)}    "
            f"# {health['total_downtime_min']:.0f} min / {health['total_incidents']} incidents"
            f" = {health['total_downtime_min'] / health['total_incidents']:.1f} min avg\n\n"
        )
    else:
        mttr_line = "mttr_hours: null\n\n"

    path.write_text(
        f"# EPI Manual Input — {product} {month}\n"
        f"# Health fields auto-populated by import-uptime.py\n\n"
        f'product: "{product}"\n'
        f'month: "{month}"\n'
        f'submitted_by: "michael.metternich"\n'
        f'submitted_date: "{today}"\n\n'
        f"# --- Delivery Velocity ---\n"
        f"deployment_frequency: {_v('deployment_frequency')}\n"
        f"features_shipped: {_v('features_shipped')}\n\n"
        f"# --- Delivery Quality ---\n"
        f"rollbacks: {_v('rollbacks')}\n"
        f"post_release_defects: {_v('post_release_defects')}\n\n"
        f"# --- Engineering Efficiency ---\n"
        f"lead_time_median_days: {_v('lead_time_median_days')}\n"
        f"cycle: {_v('cycle')}\n"
        f"cycle_items_committed: {_v('cycle_items_committed')}\n"
        f"cycle_items_delivered: {_v('cycle_items_delivered')}\n\n"
        f"# --- Engineering Health ---\n"
        f"incidents:\n"
        f"  sev1: {_v('incidents.sev1') if 'incidents' not in p else _fmt(p.get('incidents', {}).get('sev1'))}\n"
        f"  sev2: {_v('incidents.sev2') if 'incidents' not in p else _fmt(p.get('incidents', {}).get('sev2'))}\n"
        f"  sev3: {_fmt(sev3)}          # {sev3} monitoring alerts\n\n" + mttr_line + f"# --- Notes ---\n"
        f"notes: |\n"
        f"    {notes}\n"
    )


# ─── MAIN ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Import uptime monitoring exports into EPI manual YAML health fields")
    parser.add_argument("--product", required=True, help="Product name (e.g., app_a)")
    parser.add_argument("--month", required=True, help="Month in YYYY-MM format (e.g., 2026-01)")
    parser.add_argument("--input", help="Path to TSV file (default: stdin)")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Extra check name pattern to exclude (can repeat)",
    )
    args = parser.parse_args()

    # Validate month format
    if not re.fullmatch(r"\d{4}-\d{2}", args.month):
        print(f"ERROR: --month must be YYYY-MM, got: {args.month}", file=sys.stderr)
        sys.exit(1)

    # Read input
    if args.input:
        text = Path(args.input).read_text()
    else:
        if sys.stdin.isatty():
            print("Paste uptime data below (Ctrl+D when done):", file=sys.stderr)
        text = sys.stdin.read()

    rows = parse_uptime_tsv(text)
    if not rows:
        print("ERROR: No data rows found. Check input format.", file=sys.stderr)
        sys.exit(1)

    health = compute_health(rows, args.exclude)

    # Report
    print(
        f"  Parsed {len(rows)} checks: {health['included_count']} included, {health['excluded_count']} excluded",
        file=sys.stderr,
    )
    print(
        f"  Total downtime: {health['total_downtime_min']:.1f} min | "
        f"Incidents: {health['total_incidents']} | "
        f"MTTR: {health['mttr_hours']}h",
        file=sys.stderr,
    )

    # Write YAML
    product_dir = Path("products") / args.product
    if not product_dir.exists():
        print(f"ERROR: Product directory not found: {product_dir}", file=sys.stderr)
        sys.exit(1)

    out_path = product_dir / f"manual-{args.month}.yaml"
    existing = _load_existing(out_path)
    write_yaml(out_path, args.product, args.month, health, str(date.today()), existing)
    print(f"  Written → {out_path}", file=sys.stderr)
