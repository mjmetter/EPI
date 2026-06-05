from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ─── MODULE-LEVEL CACHE ───────────────────────────────────────────────────────

_litellm_spend_cache: dict[tuple[str, str], dict[str, float] | None] = {}


def _spend_disk_cache_path(start: str, end: str) -> Path:
    return Path(tempfile.gettempdir()) / f"epi_litellm_spend_{start}_{end}.json"


# ─── LITELLM API CLIENT ───────────────────────────────────────────────────────


def fetch_litellm_spend_by_user(start: str, end: str) -> dict[str, float] | None:
    """Return cached LiteLLM spend per user (email → USD) for the given date range.

    Results are cached to disk so concurrent product runs share one API fetch.
    Returns None when LITELLM_BASE_URL / LITELLM_API_KEY are not set or on API error.
    """
    global _litellm_spend_cache
    cache_key = (start, end)
    if cache_key in _litellm_spend_cache:
        return _litellm_spend_cache[cache_key]

    instance = os.environ.get("LITELLM_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("LITELLM_API_KEY", "")
    if not instance or not api_key:
        return None

    # Check disk cache — survives across process boundaries within a pipeline run
    disk_path = _spend_disk_cache_path(start, end)
    if disk_path.exists():
        try:
            disk_data = json.loads(disk_path.read_text())
            cached_result: dict[str, float] | None = disk_data if isinstance(disk_data, dict) else None
            _litellm_spend_cache[cache_key] = cached_result
            count = len(cached_result) if cached_result else 0
            print(f"  LiteLLM spend API: loaded {count} user(s) from disk cache", file=sys.stderr)
            return cached_result
        except (json.JSONDecodeError, OSError):
            pass  # fall through to live API call

    # /global/spend/users was removed in newer LiteLLM versions; fall back to
    # /global/spend/report which aggregates by API key but may also carry user_id.
    url = f"{instance}/global/spend/report?start_date={start}&end_date={end}"
    try:
        req = Request(url, headers={"Authorization": f"Bearer {api_key}"})
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except HTTPError as exc:
        body = ""
        with contextlib.suppress(Exception):
            body = exc.read().decode(errors="replace")
        print(f"  LiteLLM spend API error: {exc} — {body[:200]}", file=sys.stderr)
        return None
    except (URLError, json.JSONDecodeError, OSError) as exc:
        print(f"  LiteLLM spend API error: {exc}", file=sys.stderr)
        return None

    result: dict[str, float] = {}
    for entry in data if isinstance(data, list) else []:
        # Prefer user_id (email) when present; fall back to api_key identifier.
        user_id = (entry.get("user_id") or entry.get("api_key") or "").strip().lower()
        cost = float(entry.get("total_cost") or 0.0)
        if user_id:
            result[user_id] = result.get(user_id, 0.0) + cost

    if result:
        print(f"  LiteLLM spend API: {len(result)} user(s), total ${sum(result.values()):.2f}", file=sys.stderr)
    else:
        print("  LiteLLM spend API: no spend data returned", file=sys.stderr)

    usd_result: dict[str, float] | None = result if result else None

    if usd_result is not None:
        with contextlib.suppress(OSError):
            disk_path.write_text(json.dumps(usd_result))

    _litellm_spend_cache[cache_key] = usd_result
    return usd_result
