from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ─── MODULE-LEVEL CACHES ─────────────────────────────────────────────────────

_cursor_cache: dict[str, dict[str, int]] | None = None
_cursor_spend_cache: dict[tuple[str, str], dict[str, float] | None] = {}


def _spend_disk_cache_path(start: str, end: str) -> Path:
    return Path(tempfile.gettempdir()) / f"epi_cursor_spend_{start}_{end}.json"


# ─── CURSOR API CLIENTS ──────────────────────────────────────────────────────


def fetch_cursor_commits(start_date: str, end_date: str) -> dict[str, dict[str, int]]:
    """Bulk-fetch Cursor AI code attribution for all commits in the date range.

    Returns dict[commit_hash] -> {tab_added, tab_deleted, composer_added,
    composer_deleted, non_ai_added, non_ai_deleted, total_added, total_deleted}.

    Automatically splits ranges longer than 30 days into sequential 30-day chunks
    (the Cursor API rejects ranges > 30 days with HTTP 400).
    """
    token = os.environ.get("CURSOR_API_TOKEN", "")
    if not token:
        return {}

    # Split ranges > 30 days into 30-day chunks and merge results
    start_d = date.fromisoformat(start_date)
    end_d = date.fromisoformat(end_date)
    if (end_d - start_d).days > 30:
        result: dict[str, dict[str, int]] = {}
        chunk_start = start_d
        while chunk_start < end_d:
            chunk_end = min(chunk_start + timedelta(days=30), end_d)
            result.update(fetch_cursor_commits(chunk_start.isoformat(), chunk_end.isoformat()))
            chunk_start = chunk_end
        return result

    auth = base64.b64encode(f"{token}:".encode()).decode()
    base_url = "https://api.cursor.com/analytics/ai-code/commits"
    result = {}
    page = 1
    page_size = 100

    while True:
        url = f"{base_url}?startDate={start_date}&endDate={end_date}&page={page}&pageSize={page_size}"
        try:
            req = Request(
                url,
                headers={
                    "Authorization": f"Basic {auth}",
                    "Accept": "application/json",
                },
            )
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except HTTPError as e:
            if e.code == 429:
                print(f"  Cursor API rate-limited (page {page}), waiting 10s...", file=sys.stderr)
                time.sleep(10)
                continue  # retry same page
            print(f"  Cursor API error (page {page}): {e}", file=sys.stderr)
            break
        except (URLError, json.JSONDecodeError, OSError) as e:
            print(f"  Cursor API error (page {page}): {e}", file=sys.stderr)
            break

        commits = data if isinstance(data, list) else data.get("items", data.get("data", data.get("commits", [])))
        if not commits:
            break

        for c in commits:
            sha = c.get("sha", c.get("commitHash", ""))
            if not sha:
                continue
            result[sha] = {
                "tab_added": c.get("tabLinesAdded", 0),
                "tab_deleted": c.get("tabLinesDeleted", 0),
                "composer_added": c.get("composerLinesAdded", 0),
                "composer_deleted": c.get("composerLinesDeleted", 0),
                "non_ai_added": c.get("nonAiLinesAdded", 0),
                "non_ai_deleted": c.get("nonAiLinesDeleted", 0),
                "total_added": c.get("tabLinesAdded", 0) + c.get("composerLinesAdded", 0) + c.get("nonAiLinesAdded", 0),
                "total_deleted": (
                    c.get("tabLinesDeleted", 0) + c.get("composerLinesDeleted", 0) + c.get("nonAiLinesDeleted", 0)
                ),
            }

        if len(commits) < page_size:
            break
        page += 1
        time.sleep(1)  # rate-limit: 1s between pages

    if result:
        print(f"  Cursor API: fetched {len(result)} commit(s) attribution data", file=sys.stderr)
    return result


def get_cursor_cache(start: str, end: str) -> dict[str, dict[str, int]]:
    """Return cached Cursor commit data, fetching once per pipeline run."""
    global _cursor_cache
    if _cursor_cache is None:
        _cursor_cache = fetch_cursor_commits(start, end)
    return _cursor_cache


def fetch_cursor_spend_by_user(start: str, end: str) -> dict[str, float] | None:
    """Return {email → USD} Cursor spend for all team members in the date range.

    Uses the Cursor admin API /teams/filtered-usage-events (POST, epoch ms dates).
    Results are cached to disk so concurrent product runs share one API fetch.
    Returns None when CURSOR_API_TOKEN is not set or on API error.
    """
    global _cursor_spend_cache
    cache_key = (start, end)
    if cache_key in _cursor_spend_cache:
        return _cursor_spend_cache[cache_key]

    token = os.environ.get("CURSOR_API_TOKEN", "")
    if not token:
        _cursor_spend_cache[cache_key] = None
        return None

    # Check disk cache — survives across process boundaries within a pipeline run
    disk_path = _spend_disk_cache_path(start, end)
    if disk_path.exists():
        try:
            disk_data = json.loads(disk_path.read_text())
            cached_result: dict[str, float] | None = disk_data if isinstance(disk_data, dict) else None
            _cursor_spend_cache[cache_key] = cached_result
            count = len(cached_result) if cached_result else 0
            print(f"  Cursor spend API: loaded {count} user(s) from disk cache", file=sys.stderr)
            return cached_result
        except (json.JSONDecodeError, OSError):
            pass  # fall through to live API call

    def _to_epoch_ms(date_str: str) -> int:
        return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)  # noqa: UP017

    auth = base64.b64encode(f"{token}:".encode()).decode()
    url = "https://api.cursor.com/teams/filtered-usage-events"
    start_ms = _to_epoch_ms(start)
    end_ms = _to_epoch_ms(end)
    result: dict[str, float] = {}
    page = 1
    page_size = 100

    while True:
        payload = json.dumps({"startDate": start_ms, "endDate": end_ms, "page": page, "pageSize": page_size}).encode()
        try:
            req = Request(
                url,
                data=payload,
                headers={
                    "Authorization": f"Basic {auth}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except HTTPError as e:
            if e.code in (429, 503):
                print(f"  Cursor spend API transient error {e.code} (page {page}), waiting 15s...", file=sys.stderr)
                time.sleep(15)
                continue
            print(f"  Cursor spend API error (page {page}): {e}", file=sys.stderr)
            break
        except (URLError, json.JSONDecodeError, OSError) as e:
            print(f"  Cursor spend API error (page {page}): {e}", file=sys.stderr)
            break

        events = (
            data
            if isinstance(data, list)
            else (
                data.get("usageEvents", data.get("items", data.get("data", data.get("events", []))))
                if isinstance(data, dict)
                else []
            )
        )
        if not events:
            break
        for ev in events:
            email = (ev.get("userEmail") or "").strip().lower()
            charged = float(ev.get("chargedCents") or 0.0)
            if email:
                result[email] = result.get(email, 0.0) + charged
        if len(events) < page_size:
            break
        page += 1
        time.sleep(1)

    if result:
        print(f"  Cursor spend API: {len(result)} user(s), total ${sum(result.values()) / 100:.2f}", file=sys.stderr)
    else:
        print("  Cursor spend API: no spend data returned (empty events or all chargedCents=0)", file=sys.stderr)
    usd_result: dict[str, float] | None = {k: round(v / 100, 4) for k, v in result.items()} if result else None

    if usd_result is not None:
        with contextlib.suppress(OSError):
            disk_path.write_text(json.dumps(usd_result))

    _cursor_spend_cache[cache_key] = usd_result
    return usd_result
