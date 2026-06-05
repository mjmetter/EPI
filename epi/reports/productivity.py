"""
Productivity Dashboard — standalone HTML report for exec buy-in.

Produces three page types showing 4 per-engineer metrics across all products:
  - reports/productivity.html                          — overview: all products, all months
  - reports/productivity_{product}.html                — product drilldown: one line per repo
  - reports/productivity_{product}_{repo}.html         — repo drilldown: single repo + contributors

Usage:
    python3 generate-productivity-report.py
    python3 generate-productivity-report.py --output-dir public/
    python3 generate-productivity-report.py --products app_a app_b
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from epi.bot import is_bot
from epi.config import load_repos_config

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent.parent

OVERVIEW_METRICS: list[dict[str, str]] = [
    {"key": "commits_per_engineer", "label": "Commits per Engineer"},
    {"key": "mrs_per_engineer", "label": "MRs per Engineer"},
]

REPO_DETAIL_METRICS: list[dict[str, str]] = [
    {"key": "lines_per_engineer", "label": "Lines Changed per Engineer"},
    {"key": "avg_commit_size", "label": "Average Commit Size"},
    {"key": "features_per_engineer", "label": "Feature Commits per Engineer"},
]

# All metric series on repo pages use the same blue so the amber AI adoption overlay stands out.
METRIC_COLOUR = "#60a5fa"  # blue-400

# Colour palette — one per product / per repo (bright, visible on dark backgrounds)
COLOURS = [
    "#60a5fa",  # blue-400
    "#4ade80",  # green-400
    "#fb923c",  # orange-400
    "#f87171",  # red-400
    "#c084fc",  # purple-400
    "#22d3ee",  # cyan-400
    "#fbbf24",  # amber-400
    "#a3e635",  # lime-400
    "#f472b6",  # pink-400
    "#818cf8",  # indigo-400
]

# Month regex — matches {YYYY-MM}_{anything}.json but not health- prefixed
_MONTH_RE = re.compile(r"^(\d{4}-\d{2})_[^/]+\.json$")

# API key for chat — read from env at generation time.
# ANTHROPIC_AUTH_TOKEN is read from the local environment (e.g. an OpenAI-compatible LLM
# proxy). In CI the __LLM_API_KEY__ placeholder is substituted at deploy time so the
# generated HTML can call the chat endpoint without baking a secret into the repo.
_LLM_API_KEY = os.environ.get("ANTHROPIC_AUTH_TOKEN", "__LLM_API_KEY__")

# ─── CHAT CONSTANTS ───────────────────────────────────────────────────────────

_CHAT_CSS = """\
.chat-toggle{position:fixed;bottom:24px;right:24px;z-index:9999;width:44px;height:44px;border-radius:3px;border:none;background:none;color:var(--text);font-size:1.4rem;cursor:pointer;transition:opacity .15s}
.chat-toggle:hover{opacity:.65}
.chat-panel{position:fixed;bottom:80px;right:24px;z-index:9999;width:400px;max-height:520px;display:none;flex-direction:column;background:var(--surface);border:1px solid var(--border);border-radius:3px;overflow:hidden}
.chat-panel.open{display:flex}
.chat-header{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;border-bottom:1px solid var(--border);font-family:'Barlow Condensed',sans-serif;font-weight:700;font-size:0.73rem;letter-spacing:.1em;text-transform:uppercase}
.chat-header button{background:none;border:none;color:var(--muted);cursor:pointer;font-size:0.78rem;padding:2px 6px;font-family:'Barlow Condensed',sans-serif}
.chat-header button:hover{color:var(--text)}
.chat-messages{flex:1;overflow-y:auto;padding:12px 16px;display:flex;flex-direction:column;gap:8px;min-height:200px;max-height:360px}
.chat-msg{padding:8px 12px;border-radius:2px;font-size:0.82rem;line-height:1.5;max-width:85%;word-wrap:break-word;white-space:pre-wrap}
.chat-msg.user{align-self:flex-end;background:var(--accent);color:var(--header-bg)}
.chat-msg.assistant{align-self:flex-start;background:var(--hover-bg);color:var(--text)}
.chat-msg.error{align-self:center;color:#f87171;font-size:0.78rem;font-style:italic}
.chat-input-row{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--border)}
.chat-input-row input{flex:1;border:1px solid var(--border);border-radius:2px;padding:8px 12px;font-size:0.82rem;outline:none;background:var(--bg);color:var(--text)}
.chat-input-row button{border:none;background:var(--accent);color:var(--header-bg);border-radius:2px;padding:8px 14px;font-family:'Barlow Condensed',sans-serif;font-size:0.73rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;cursor:pointer}
.chat-input-row button:disabled{opacity:.5;cursor:not-allowed}"""

_CHAT_HTML = """\
<button class="chat-toggle" id="chat-toggle" onclick="document.getElementById('chat-panel').classList.toggle('open')" title="Ask AI about this data">&#x1F4AC;</button>
<div class="chat-panel" id="chat-panel">
  <div class="chat-header">
    <span>Ask AI</span>
    <div>
      <button onclick="window.__prodChatClear()" title="Clear history">Clear</button>
      <button onclick="document.getElementById('chat-panel').classList.remove('open')" title="Close">&#x2715;</button>
    </div>
  </div>
  <div class="chat-messages" id="chat-messages"></div>
  <div class="chat-input-row">
    <input type="text" id="chat-input" placeholder="Ask about these metrics\u2026" autocomplete="off">
    <button id="chat-send" onclick="window.__prodChatSend()">Send</button>
  </div>
</div>"""

_CHAT_JS = """\
<script>
(function(){
  var cfg = window.__prodChat || {};
  var apiKey = cfg.apiKey || '';
  var systemPrompt = cfg.systemPrompt || '';
  var messages = [];
  var messagesEl = document.getElementById('chat-messages');
  var inputEl = document.getElementById('chat-input');
  var sendBtn = document.getElementById('chat-send');
  var busy = false;

  function markdownToHtml(text) {
    var s = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    s = s.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    s = s.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    s = s.replace(/^# (.+)$/gm, '<h1>$1</h1>');
    s = s.replace(/[*][*][*](.+?)[*][*][*]/g, '<strong><em>$1</em></strong>');
    s = s.replace(/[*][*](.+?)[*][*]/g, '<strong>$1</strong>');
    s = s.replace(/[*](.+?)[*]/g, '<em>$1</em>');
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/^---+$/gm, '<hr>');
    s = s.replace(/((?:^[ \\t]*[-] .+\\n?)+)/gm, function(block) {
      var items = block.replace(/^[ \\t]*[-] (.+)$/gm, '<li>$1</li>');
      return '<ul>' + items + '</ul>';
    });
    s = s.replace(/((?:^\\d+[.] .+\\n?)+)/gm, function(block) {
      var items = block.replace(/^\\d+[.] (.+)$/gm, '<li>$1</li>');
      return '<ol>' + items + '</ol>';
    });
    s = s.replace(/\\n{2,}/g, '</p><p>');
    s = '<p>' + s + '</p>';
    s = s.replace(/<p>\\s*<\\/p>/g, '');
    s = s.replace(/<p>(<(?:h[1-6]|ul|ol|hr)[^>]*>)/g, '$1');
    s = s.replace(/(<\\/(?:h[1-6]|ul|ol)>)<\\/p>/g, '$1');
    s = s.replace(/([^>])\\n([^<])/g, '$1<br>$2');
    return s;
  }

  function addMsg(role, text) {
    var d = document.createElement('div');
    d.className = 'chat-msg ' + role;
    if (role === 'assistant') { d.innerHTML = markdownToHtml(text); } else { d.textContent = text; }
    messagesEl.appendChild(d);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return d;
  }

  if (!apiKey) {
    addMsg('error', 'API key not configured \u2014 chat is unavailable.');
    inputEl.disabled = true;
    sendBtn.disabled = true;
  }

  window.__prodChatClear = function() {
    messages = [];
    messagesEl.innerHTML = '';
    if (!apiKey) addMsg('error', 'API key not configured \u2014 chat is unavailable.');
  };

  window.__prodChatSend = function() {
    if (busy || !apiKey) return;
    var text = inputEl.value.trim();
    if (!text) return;
    inputEl.value = '';
    addMsg('user', text);
    messages.push({role: 'user', content: text});
    if (messages.length > 12) messages = messages.slice(messages.length - 12);
    busy = true;
    sendBtn.disabled = true;
    var assistantDiv = addMsg('assistant', '');
    var fullText = '';
    var allMessages = [{role: 'system', content: systemPrompt}].concat(messages);
    fetch('__LLM_API_ENDPOINT__', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + apiKey},
      body: JSON.stringify({model: 'claude-sonnet-4-6', max_tokens: 1024, stream: true, messages: allMessages})
    }).then(function(resp) {
      if (!resp.ok) return resp.text().then(function(body) { throw new Error('API error ' + resp.status + ': ' + body.slice(0, 200)); });
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf = '';
      function read() {
        reader.read().then(function(result) {
          if (result.done) { finish(); return; }
          buf += decoder.decode(result.value, {stream: true});
          var lines = buf.split('\\n');
          buf = lines.pop();
          for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            if (line.startsWith('data: ')) {
              var payload = line.slice(6).trim();
              if (payload === '[DONE]') { finish(); return; }
              try {
                var evt = JSON.parse(payload);
                var delta = evt.choices && evt.choices[0] && evt.choices[0].delta;
                if (delta && delta.content) {
                  fullText += delta.content;
                  assistantDiv.innerHTML = markdownToHtml(fullText);
                  messagesEl.scrollTop = messagesEl.scrollHeight;
                }
              } catch(e) {}
            }
          }
          read();
        }).catch(function(e) { addMsg('error', 'Stream error: ' + e.message); finish(); });
      }
      read();
    }).catch(function(e) { addMsg('error', e.message); finish(); });

    function finish() {
      busy = false;
      sendBtn.disabled = false;
      if (fullText) messages.push({role: 'assistant', content: fullText});
    }
  };

  inputEl.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); window.__prodChatSend(); }
  });
})();
</script>"""

_PROD_SYSTEM_PROMPT_PREAMBLE = """\
You are an AI assistant for the Engineering Productivity Dashboard.
You help engineering leaders understand productivity trends across product groups.

## Dashboard structure — three tabs

### Tab 1: DORA Metrics
Four groups of metrics, one 13-month trend chart per metric, one line per product pillar.
Solid lines = automated (from git). Dashed lines = manually entered (from monthly YAML — less precise).
Charts marked "manual" in the header rely on manual input and may be incomplete.

**Delivery Velocity**
- Commits per Engineer — non-bot commits / headcount (automated)
- MRs per Engineer — merge requests merged / headcount (automated)
- Deployment Frequency — deployments/month (manual)
- Features Shipped — commits classified as feat/add/implement / headcount (automated)

**Delivery Quality**
- Change Failure Rate — % deployments causing incidents (manual)
- Post-Release Defect Rate — % features with post-release defects (manual, often missing)
- Rework Rate — % commits that revert or redo recent work (automated)

**Engineering Efficiency**
- Lead Time in Days — commit-to-production days (manual, currently missing)
- Cycle Delivery Accuracy — % planned work delivered on time (manual, currently missing)

**Engineering Health**
- MTTR in Hours — mean time to restore after incident (manual, often missing)
- Bus Factor — min engineers whose loss halts a repo (automated)
- Knowledge Distribution — % repos with multiple active contributors (automated)

### Tab 2: AI Tool Adoption
Monthly Active Users chart showing Cursor (IDE) and LiteLLM (Claude proxy) adoption over 13 months.
Data is manually maintained in ai-spend.yaml.

### Tab 3: Year-over-Year
Year-overlay charts (Jan–Dec axis, one line per calendar year) for Commits per Engineer and MRs per Engineer,
plus delta tables showing month-by-month YoY change. Charts update when products are toggled.

## Product groups
Products and their repositories are defined in repos.yaml. Each product group rolls up
one or more repositories; display names come from that config.

## Commit Classification
Commits are categorised by prefix/keyword: feature, bugfix, test, tooling, maintenance, docs, other.

## AI Spend & Adoption Data
- **LiteLLM spend**: total monthly API spend via the LLM proxy (Claude, Sonnet, etc.)
- **Cursor spend**: flat $20/user/month base + overages
- **Active users**: engineers actively using each tool per month (manually maintained in ai-spend.yaml)

## Instructions
- Reference actual values from the current page data when answering
- Distinguish between automated metrics (reliable) and manual metrics (approximate, may be incomplete)
- Explain what trends mean for engineering effectiveness; suggest concrete, actionable improvements
- Be concise — 2–4 sentences unless more detail is requested
- You are read-only — you cannot modify any data"""


def _esc(s: str) -> str:
    """Escape a string for safe inclusion in HTML content or attribute values."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _fmt_num(v: int | None) -> str:
    """Format an integer with thousands separator, or '—' if absent."""
    return f"{v:,}" if isinstance(v, int) else "—"


def _sort_val(v: int | None) -> int:
    """Return value for data-val sort attribute; -1 sentinel sorts None last."""
    return v if v is not None else -1


def _make_dataset(label: str, series: list[float | None], colour: str) -> dict[str, Any]:
    """Return a Chart.js line dataset dict with consistent styling."""
    return {
        "label": label,
        "data": series,
        "borderColor": colour,
        "backgroundColor": colour + "22",
        "tension": 0.3,
        "pointRadius": 4,
        "spanGaps": True,
    }


# ─── CORE DATA FUNCTIONS ──────────────────────────────────────────────────────


def compute_contributors_human(per_contributor: dict[str, Any]) -> int:
    """Count contributors in *per_contributor* that are not bots."""
    return sum(1 for name in per_contributor if not is_bot(name))


def compute_per_engineer_metrics(data: dict[str, Any]) -> dict[str, float | None]:
    """Compute 5 per-engineer metrics from a repo or aggregated JSON data dict.

    Returns None for each metric when there are no active human contributors.
    """
    per_contributor = data.get("commits", {}).get("per_contributor", {})
    n = compute_contributors_human(per_contributor)

    if n == 0:
        return {
            "lines_per_engineer": None,
            "mrs_per_engineer": None,
            "commits_per_engineer": None,
            "features_per_engineer": None,
            "avg_commit_size": None,
        }

    commits_total = data.get("commits", {}).get("total", 0)
    mrs_total = data.get("mrs", data.get("mrs_merged", {})).get("total", 0)
    lc = data.get("lines_changed", {})
    lines_changed = lc.get("added", 0) + lc.get("removed", 0)
    features = data.get("commit_classification", {}).get("feature", 0)

    return {
        "lines_per_engineer": lines_changed / n,
        "mrs_per_engineer": mrs_total / n,
        "commits_per_engineer": commits_total / n,
        "features_per_engineer": features / n,
        "avg_commit_size": lines_changed / commits_total if commits_total > 0 else None,
    }


def discover_months(product_dir: Path) -> list[str]:
    """Return sorted unique YYYY-MM months present in *product_dir*."""
    months: set[str] = set()
    if not product_dir.is_dir():
        return []
    for f in product_dir.iterdir():
        m = _MONTH_RE.match(f.name)
        if m:
            months.add(m.group(1))
    return sorted(months)


def discover_repos(product_dir: Path, month: str) -> list[str]:
    """Return sorted repo names that have data for *month* in *product_dir*.

    Skips health-prefixed files.  Repo name is extracted from {month}_{repo}.json.
    """
    repos: list[str] = []
    prefix = f"{month}_"
    if not product_dir.is_dir():
        return []
    for f in sorted(product_dir.iterdir()):
        if f.name.startswith(prefix) and f.suffix == ".json" and not f.name.startswith("health-"):
            repo_name = f.name[len(prefix) : -len(".json")]
            repos.append(repo_name)
    return repos


def load_repo_data(product_dir: Path, month: str, repo: str) -> dict[str, Any] | None:
    """Load per-repo JSON for *product_dir/{month}_{repo}.json*."""
    try:
        with open(product_dir / f"{month}_{repo}.json") as f:
            return json.load(f)  # type: ignore[no-any-return]
    except FileNotFoundError:
        return None


def load_ai_spend(base_dir: Path) -> dict[str, dict[str, float]]:
    """Load ai-spend.yaml; return {tool_name: {YYYY-MM: amount}}."""
    try:
        import yaml as _yaml

        path = base_dir / "ai-spend.yaml"
        with open(path) as f:
            data = _yaml.safe_load(f) or {}
        return {
            tool: {str(k): float(v) for k, v in months.items() if v is not None}
            for tool, months in data.items()
            if isinstance(months, dict)
        }
    except Exception:
        return {}


def load_health_for_month(product_dir: Path, month: str, repo: str) -> dict[str, Any] | None:
    """Load health-{month}_{repo}.json for a specific month, or None if absent."""
    try:
        with open(product_dir / f"health-{month}_{repo}.json") as f:
            return json.load(f)  # type: ignore[no-any-return]
    except FileNotFoundError:
        return None


def _format_month(m: str) -> str:
    """Format 'YYYY-MM' as 'Mon YYYY' for display (e.g. '2026-03' → 'Mar 2026')."""
    try:
        return datetime.strptime(m, "%Y-%m").strftime("%b %Y")
    except ValueError:
        return m


def _pearson(xs: list[float | None], ys: list[float | None]) -> float | None:
    """Pearson r between two same-length series, ignoring pairs where either value is None."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]  # noqa: B905
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    num = sum((x - mx) * (y - my) for x, y in pairs)
    den = (sum((x - mx) ** 2 for x, _ in pairs) * sum((y - my) ** 2 for _, y in pairs)) ** 0.5
    return num / den if den else None


def discover_months_with_archive(product_dir: Path) -> list[str]:
    """Return sorted unique YYYY-MM months from *product_dir* **and** its archive/<year>/ subdirs.

    Archive layout: products/<p>/archive/<year>/<month>_<repo>.json
    Months found in the archive are merged with those in the product root.
    """
    months: set[str] = set(discover_months(product_dir))
    # archive/<year>/ subdirs
    archive_root = product_dir / "archive"
    if archive_root.is_dir():
        for year_dir in archive_root.iterdir():
            if not year_dir.is_dir():
                continue
            for f in year_dir.iterdir():
                m = _MONTH_RE.match(f.name)
                if m:
                    months.add(m.group(1))
    return sorted(months)


def _product_dir_for_month(product_dir: Path, month: str) -> Path:
    """Return the directory that contains *month* files for *product_dir*.

    Checks root first, then archive/<year>/ subdirs.  Returns the root dir if
    the month is not found in archive (caller handles the empty-glob case).
    """
    if next(product_dir.glob(f"{month}_*.json"), None) is not None:
        return product_dir
    year = month.split("-")[0]
    archive_dir = product_dir / "archive" / year
    if archive_dir.is_dir() and next(archive_dir.glob(f"{month}_*.json"), None) is not None:
        return archive_dir
    return product_dir


def load_product_data(product_dir: Path, month: str) -> dict[str, Any]:
    """Aggregate per-repo JSON files for *product_dir* and *month*.

    Skips support repos (category == 'support'). Returns a dict in the same
    shape as a single per-repo JSON so compute_per_engineer_metrics can use it.
    Also searches archive/<year>/ subdirs when *month* files are not in root.
    """
    actual_dir = _product_dir_for_month(product_dir, month)
    repo_files = sorted(actual_dir.glob(f"{month}_*.json"))
    aggregated: dict[str, Any] = {
        "commits": {"total": 0, "per_contributor": {}},
        "mrs": {"total": 0},
        "lines_changed": {"added": 0, "removed": 0},
        "commit_classification": {"feature": 0},
    }
    for rf in repo_files:
        if rf.name.startswith("health-"):
            continue
        with open(rf) as f:
            data = json.load(f)
        if data.get("meta", {}).get("category") == "support":
            continue
        aggregated["commits"]["total"] += data.get("commits", {}).get("total", 0)
        aggregated["mrs"]["total"] += data.get("mrs", data.get("mrs_merged", {})).get("total", 0)
        aggregated["lines_changed"]["added"] += data.get("lines_changed", {}).get("added", 0)
        aggregated["lines_changed"]["removed"] += data.get("lines_changed", {}).get("removed", 0)
        aggregated["commit_classification"]["feature"] += data.get("commit_classification", {}).get("feature", 0)
        for author, count in data.get("commits", {}).get("per_contributor", {}).items():
            agg_contrib = aggregated["commits"]["per_contributor"]
            agg_contrib[author] = agg_contrib.get(author, 0) + count
    return aggregated


# ─── YoY ANALYSIS FUNCTIONS ───────────────────────────────────────────────────

_MONTH_ABBREVS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def build_yoy_series(
    products: list[str],
    base_dir: Path,
    all_months: list[str],
    metric_key: str,
) -> tuple[dict[int, list[float | None]], list[tuple[str, float, float, float | None]]]:
    """Build year-indexed monthly series and YoY delta rows for *metric_key*.

    Returns:
        by_year   — {year: [Jan_avg, Feb_avg, …, Dec_avg]} with None for absent months.
        deltas    — [(month_abbrev, prior_val, latest_val, pct_change)] for months present
                    in both the most-recent and the prior calendar year; empty if < 2 years.
                    prior_val and latest_val are always non-None (months with no data are skipped).

    Aggregation: for each (year, calendar_month) we average the *metric_key* value across all
    products, matching the existing YoY badge approach.
    """
    # Collect (year, cal_month_idx) → list of product values
    cell_values: dict[tuple[int, int], list[float]] = {}
    for month in all_months:
        try:
            year = int(month.split("-")[0])
            cal_month = int(month.split("-")[1]) - 1  # 0-based
        except (ValueError, IndexError):
            continue
        for product in products:
            product_dir = base_dir / "products" / product
            data = load_product_data(product_dir, month)
            metrics = compute_per_engineer_metrics(data)
            v = metrics.get(metric_key)
            if v is not None:
                cell_values.setdefault((year, cal_month), []).append(v)

    # Build by_year: {year: [None]*12} with averages filled in
    years = sorted({k[0] for k in cell_values})
    by_year: dict[int, list[float | None]] = {yr: [None] * 12 for yr in years}
    for (yr, cm), vals in cell_values.items():
        by_year[yr][cm] = sum(vals) / len(vals)

    if len(years) < 2:
        return by_year, []

    latest_year = years[-1]
    prior_year = years[-2]

    deltas: list[tuple[str, float, float, float | None]] = []
    for cm in range(12):
        prior_val = by_year[prior_year][cm]
        latest_val = by_year[latest_year][cm]
        if prior_val is None or latest_val is None:
            continue
        pct = (latest_val - prior_val) / prior_val * 100 if prior_val != 0 else None
        deltas.append((_MONTH_ABBREVS[cm], prior_val, latest_val, pct))

    return by_year, deltas


def compute_seasonal_baseline(by_year: dict[int, list[float | None]]) -> list[float | None]:
    """Average each calendar month across all available years.

    Returns a 12-element list (Jan-Dec) where each entry is the mean across years
    that have a non-None value, or None if no year has data for that month.
    """
    baseline: list[float | None] = []
    for cm in range(12):
        vals: list[float] = [by_year[yr][cm] for yr in by_year if by_year[yr][cm] is not None]  # type: ignore[misc]
        baseline.append(sum(vals) / len(vals) if vals else None)
    return baseline


def _least_squares_slope(by_year: dict[int, list[float | None]]) -> float:
    """Fit a least-squares slope of annual average vs. year index.

    Returns 0.0 when fewer than 2 years have data.
    """
    year_avgs: list[tuple[float, float]] = []
    for yr, monthly in by_year.items():
        vals = [v for v in monthly if v is not None]
        if vals:
            year_avgs.append((float(yr), sum(vals) / len(vals)))
    if len(year_avgs) < 2:
        return 0.0
    n = len(year_avgs)
    sx = sum(x for x, _ in year_avgs)
    sy = sum(y for _, y in year_avgs)
    sxy = sum(x * y for x, y in year_avgs)
    sx2 = sum(x * x for x, _ in year_avgs)
    denom = n * sx2 - sx * sx
    return (n * sxy - sx * sy) / denom if denom else 0.0


def compute_residuals(
    by_year: dict[int, list[float | None]],
    baseline: list[float | None],
    yoy_slope: float,
) -> dict[int, list[float | None]]:
    """Compute residuals: actual - (baseline + yoy_slope * year_offset).

    year_offset is centred on the mean year so the baseline intercept represents
    the mid-period expected value.

    Returns a dict with the same year keys as *by_year*, each value a 12-element list.
    """
    years = sorted(by_year)
    if not years:
        return {}
    mid_year = sum(years) / len(years)
    residuals: dict[int, list[float | None]] = {}
    for yr in years:
        year_offset = yr - mid_year
        row: list[float | None] = []
        for cm in range(12):
            actual = by_year[yr][cm]
            base = baseline[cm] if cm < len(baseline) else None
            if actual is None or base is None:
                row.append(None)
            else:
                row.append(actual - (base + yoy_slope * year_offset))
        residuals[yr] = row
    return residuals


def render_yoy_section(
    metric_key: str,
    metric_label: str,
    by_year: dict[int, list[float | None]],
    deltas: list[tuple[str, float, float, float | None]],
    baseline: list[float | None],
    residuals: dict[int, list[float | None]],
) -> str:
    """Render the HTML for one metric's Year-over-Year subsection.

    Returns an HTML string containing:
    - Year-overlay line chart (Jan-Dec axis, one line per year + dashed baseline)
    - YoY delta table
    - AI adoption correlation scatter (or insufficient-data card)
    - Empty-state placeholder when < 2 years of data
    """
    safe_key = metric_key.replace("_", "-")

    if len(by_year) < 2:
        return (
            f"<div class='chart-card'>"
            f"<h2>Year-over-Year — {metric_label}</h2>"
            f'<p style=\'color:var(--muted);font-family:"Barlow Condensed",sans-serif;'
            f"font-size:0.85rem;padding:20px 0'>Insufficient data — at least 2 calendar years required. "
            f"Run historical data collection to enable Year-over-Year analysis.</p>"
            f"</div>"
        )

    # ── Year-overlay chart ────────────────────────────────────────────────────
    chart_id = f"yoy-{safe_key}"
    years = sorted(by_year)
    datasets: list[dict[str, Any]] = []
    for i, yr in enumerate(years):
        if i == len(years) - 1:
            colour = "#4ade80"
        elif i == len(years) - 2:
            colour = "#60a5fa"
        else:
            colour = "#8890a4"
        datasets.append(_make_dataset(str(yr), by_year[yr], colour))

    # Seasonal baseline as dashed grey reference line
    has_baseline = any(v is not None for v in baseline)
    if has_baseline:
        datasets.append(
            {
                "label": "Seasonal baseline",
                "data": baseline,
                "borderColor": "#8890a4",
                "backgroundColor": "transparent",
                "borderWidth": 1.5,
                "borderDash": [6, 3],
                "pointRadius": 0,
                "spanGaps": True,
                "tension": 0.3,
            }
        )

    labels_json = json.dumps(_MONTH_ABBREVS)
    datasets_json = json.dumps(datasets)
    overlay_script = f"""<script>
(function() {{
  var ctx = document.getElementById('{chart_id}').getContext('2d');
  window._staticCharts = window._staticCharts || {{}};
  var chart = new Chart(ctx, {{
    type: 'line',
    data: {{ labels: {labels_json}, datasets: {datasets_json} }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ labels: {{ color: '#8890a4', font: {{ size: 11, family: \"'Barlow Condensed', sans-serif\" }} }} }},
        tooltip: {{
          backgroundColor: '#181c25', titleColor: '#eef0f5', bodyColor: '#8890a4',
          borderColor: '#252a36', borderWidth: 1
        }}
      }},
      scales: {{
        x: {{ ticks: {{ color: '#8890a4' }}, grid: {{ color: '#252a36' }} }},
        y: {{ beginAtZero: true, ticks: {{ color: '#8890a4' }}, grid: {{ color: '#252a36' }} }}
      }}
    }}
  }});
  window._staticCharts['{chart_id}'] = chart;
}})();
</script>"""

    overlay_card = (
        f"<div class='chart-card'>"
        f"<div class='chart-card-header'>"
        f"<h2>{metric_label} — Year Overlay</h2>"
        f"</div>"
        f"<div class='chart-wrapper'><canvas id='{chart_id}'></canvas></div>"
        f"</div>"
    )

    # ── Delta table ───────────────────────────────────────────────────────────
    if deltas:
        latest_year = years[-1]
        prior_year = years[-2]
        header_cells = (
            f"<th>Month</th>"
            f"<th class='num'>{prior_year}</th>"
            f"<th class='num'>{latest_year}</th>"
            f"<th class='num'>Δ abs</th>"
            f"<th class='num'>Δ %</th>"
        )
        rows_html = ""
        for month_name, prior, latest, pct in deltas:
            delta_abs = latest - prior
            abs_str = f"{delta_abs:+.1f}"
            pct_str = f"{pct:+.1f}%" if pct is not None else "—"
            color = "#4ade80" if (pct or 0) >= 0 else "#f87171"
            rows_html += (
                f"<tr>"
                f"<td>{month_name}</td>"
                f"<td class='num'>{prior:.1f}</td>"
                f"<td class='num'>{latest:.1f}</td>"
                f"<td class='num' style='color:{color}'>{abs_str}</td>"
                f"<td class='num' style='color:{color}'>{pct_str}</td>"
                f"</tr>"
            )
        delta_table = (
            f"<div class='chart-card' style='margin-top:20px'>"
            f"<h2>{metric_label} — YoY Delta ({prior_year} → {latest_year})</h2>"
            f"<table class='repo-table' style='margin-top:12px'>"
            f"<thead><tr>{header_cells}</tr></thead>"
            f"<tbody>{rows_html}</tbody>"
            f"</table></div>"
        )
    else:
        delta_table = ""

    return overlay_card + overlay_script + delta_table


# ─── HTML HELPERS ─────────────────────────────────────────────────────────────

_CHART_CDN = "https://cdn.jsdelivr.net/npm/chart.js"

_BASE_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@400;500;600;700&family=Barlow:wght@400;500;600&family=Azeret+Mono:wght@400;600;700&display=swap');
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: oklch(12% 0.006 240);
  --surface: oklch(18% 0.008 240);
  --border: oklch(26% 0.010 240);
  --text: oklch(94% 0.003 240);
  --muted: oklch(62% 0.007 240);
  --header-bg: oklch(8% 0.004 240);
  --accent: oklch(88% 0.005 240);
  --hover-bg: oklch(21% 0.009 240);
}
body { background: var(--bg); color: var(--text); font-family: 'Barlow', system-ui, sans-serif; font-size: 14px; }
header { display: flex; align-items: center; justify-content: space-between;
         padding: 14px 24px; border-bottom: 1px solid var(--accent); background: var(--header-bg); }
header h1 { font-family: 'Barlow Condensed', sans-serif; font-size: 1.05rem; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; }
.subtitle { font-family: 'Barlow Condensed', sans-serif; font-size: 0.63rem; text-transform: uppercase; letter-spacing: .14em; color: var(--accent); margin-bottom: 2px; font-weight: 600; }
main { padding: 24px; max-width: 1400px; margin: 0 auto; }
nav a { color: var(--muted); text-decoration: none; font-family: 'Barlow Condensed', sans-serif; font-size: 0.78rem; letter-spacing: .04em; }
nav a:hover { color: var(--text); }
footer { font-family: 'Barlow Condensed', sans-serif; padding: 16px 24px; color: var(--muted); font-size: 0.63rem; letter-spacing: .08em; text-transform: uppercase; border-top: 1px solid var(--border); text-align: left; }
.breadcrumb { display: flex; gap: 8px; align-items: center; margin-bottom: 24px; color: var(--muted); font-family: 'Barlow Condensed', sans-serif; font-size: 0.73rem; letter-spacing: .04em; }
.breadcrumb a { color: var(--muted); text-decoration: none; }
.breadcrumb a:hover { color: var(--accent); }
.breadcrumb span { color: var(--text); }
.charts-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(560px, 1fr)); gap: 20px; }
.chart-card { background: var(--surface); border: 1px solid var(--border); border-radius: 3px; padding: 20px; }
.chart-card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.chart-card-header h2 { font-family: 'Barlow Condensed', sans-serif; font-size: 1rem; font-weight: 700; margin-bottom: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .08em; }
.chart-card h2 { font-family: 'Barlow Condensed', sans-serif; font-size: 1rem; font-weight: 700; margin-bottom: 16px; color: var(--accent); text-transform: uppercase; letter-spacing: .08em; }
.chart-trendline-label { display: flex; align-items: center; gap: 5px; font-family: 'Barlow Condensed', sans-serif; font-size: 0.68rem; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); cursor: pointer; user-select: none; white-space: nowrap; }
.chart-trendline-label input { accent-color: var(--muted); cursor: pointer; }
.chart-wrapper { position: relative; height: 280px; }
.repo-table { width: 100%; border-collapse: collapse; margin-top: 32px; }
.repo-table th { text-align: left; padding: 8px 12px; font-family: 'Barlow Condensed', sans-serif; font-size: 0.63rem; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); border-bottom: 1px solid var(--border); }
.repo-table th.num { text-align: right; }
.repo-table td.num { text-align: right; font-family: 'Azeret Mono', monospace; font-size: 12px; }
.repo-table td { padding: 8px 12px; border-bottom: 1px solid var(--border); color: var(--text); font-size: 13px; transition: background 100ms ease-out; }
.repo-table tr:last-child td { border-bottom: none; }
.repo-table tr:hover td { background: var(--hover-bg); }
.repo-table a { color: var(--accent); text-decoration: underline; text-underline-offset: 2px; }
.repo-table a:hover { color: var(--text); }
.repo-table th.sort-col { cursor: pointer; user-select: none; }
.repo-table th.sort-col:hover { color: var(--text); }
.toggle-btn { background: none; border: 1px solid var(--border); border-radius: 3px; padding: 6px 16px; font-family: 'Barlow Condensed', sans-serif; font-size: 0.8rem; font-weight: 600; letter-spacing: .04em; color: var(--muted); cursor: pointer; transition: color 120ms ease-out, border-color 120ms ease-out; }
.toggle-btn:hover { color: var(--text); border-color: var(--accent); }
.month-nav { display: flex; align-items: center; gap: 16px; margin: 28px 0 0; }
.month-nav-label { font-family: 'Barlow Condensed', sans-serif; font-size: 0.68rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); white-space: nowrap; }
.month-nav-value { font-family: 'Barlow Condensed', sans-serif; font-size: 0.82rem; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; color: var(--accent); min-width: 70px; }
.month-slider { flex: 1; accent-color: var(--accent); cursor: pointer; height: 4px; }
.month-section { display: none; }
.month-section.active { display: block; }
.info-tip { position: relative; display: inline-flex; align-items: center; cursor: default; }
.info-tip > :first-child { font-size: 0.75rem; color: var(--muted); line-height: 1; transition: color 120ms ease-out; }
.info-tip:hover > :first-child, .info-tip:focus > :first-child { color: var(--text); outline: none; }
.info-tip-body { display: none; position: absolute; top: calc(100% + 6px); left: 50%; transform: translateX(-50%); width: 320px; background: var(--surface); border: 1px solid var(--border); border-radius: 3px; padding: 12px 14px; font-family: 'Barlow', sans-serif; font-size: 0.75rem; font-weight: 400; line-height: 1.55; color: var(--text); text-transform: none; letter-spacing: 0; white-space: normal; z-index: 100; pointer-events: none; }
.info-tip-body strong { color: var(--accent); font-weight: 600; }
.info-tip:hover .info-tip-body, .info-tip:focus .info-tip-body { display: block; }
"""

# Shared JS: recomputes YoY badges from currently visible chart datasets.
# Injected into both the overview product-toggle script and the product repo-toggle script.
_JS_UPDATE_BADGES = """\
  function updateBadges() {
    Object.entries(window._charts || {}).forEach(function(entry) {
      var badge = document.getElementById('badge-' + entry[0]);
      if (!badge) return;
      var valEl = badge.querySelector('.badge-val');
      if (!valEl) return;
      var visible = entry[1].data.datasets.filter(function(ds, i) { return entry[1].isDatasetVisible(i) && !(ds.label && ds.label.endsWith(' trend')) && ds.yAxisID !== 'y1'; });
      if (visible.length === 0) { valEl.textContent = ''; return; }
      var fromVals = [], toVals = [];
      visible.forEach(function(ds) {
        for (var i = 0; i < ds.data.length; i++) { if (ds.data[i] != null) { fromVals.push(ds.data[i]); break; } }
        for (var i = ds.data.length - 1; i >= 0; i--) { if (ds.data[i] != null) { toVals.push(ds.data[i]); break; } }
      });
      if (!fromVals.length || !toVals.length) return;
      var from = fromVals.reduce(function(a, b) { return a + b; }, 0) / fromVals.length;
      var to = toVals.reduce(function(a, b) { return a + b; }, 0) / toVals.length;
      if (from === 0) return;
      var pct = (to - from) / from * 100;
      valEl.style.color = pct >= 0 ? '#4ade80' : '#f87171';
      valEl.textContent = (pct >= 0 ? '\u25b2\u00a0+' : '\u25bc\u00a0') + pct.toFixed(1) + '%';
    });
  }"""


def _build_prod_chat_context(page_type: str, **kwargs: Any) -> str:
    """Build a JSON string with page-specific data for the chat system prompt."""
    ctx: dict[str, Any] = {"page_type": page_type}

    if page_type == "overview":
        months = kwargs.get("months", [])
        ctx["months"] = months
        ctx["active_tab"] = "DORA Metrics / AI Tool Adoption / Year-over-Year"
        ctx["products"] = []
        product_metrics = kwargs.get("product_metrics", {})
        scored_metrics = kwargs.get("scored_metrics", {})
        display_names = kwargs.get("display_names", {})
        for product in kwargs.get("products", []):
            name = display_names.get(product, product)
            # Velocity / throughput metrics from git
            git_series: dict[str, list[float | None]] = {}
            for metric in OVERVIEW_METRICS:
                git_series[metric["key"]] = [product_metrics.get((product, m), {}).get(metric["key"]) for m in months]
            # Latest month scored metrics (all 11 DORA metrics)
            latest_scored: dict[str, float | None] = {}
            if months:
                latest_scored = scored_metrics.get((product, months[-1]), {})
            ctx["products"].append({"name": name, "git_metrics": git_series, "latest_dora": latest_scored})
        if kwargs.get("yoy"):
            ctx["yoy_deltas"] = kwargs["yoy"]
        if kwargs.get("spend"):
            ctx["ai_spend"] = kwargs["spend"]

    elif page_type == "product":
        ctx["product"] = kwargs.get("display_name", "")
        ctx["months"] = kwargs.get("months", [])
        ctx["repos"] = []
        repo_metrics = kwargs.get("repo_metrics", {})
        last_month_raw = kwargs.get("last_month_raw", {})
        for repo in kwargs.get("repos", []):
            latest_m = kwargs.get("months", [])
            latest = next(
                (repo_metrics.get((repo, m)) for m in reversed(latest_m) if repo_metrics.get((repo, m))),
                None,
            )
            raw = last_month_raw.get(repo, {})
            ctx["repos"].append(
                {
                    "repo": repo,
                    "latest_metrics": latest,
                    "commits": raw.get("commits", {}).get("total"),
                    "mrs": raw.get("mrs", raw.get("mrs_merged", {})).get("total"),
                }
            )

    elif page_type == "repo":
        ctx["repo"] = kwargs.get("repo", "")
        ctx["product"] = kwargs.get("display_name", "")
        ctx["months"] = kwargs.get("months", [])
        month_metrics = kwargs.get("month_metrics", {})
        ctx["metrics_by_month"] = {m: month_metrics.get(m) for m in kwargs.get("months", [])}
        latest_data = kwargs.get("latest_data")
        if latest_data:
            ctx["commit_classification"] = latest_data.get("commit_classification", {})
            top_contribs = sorted(
                latest_data.get("commits", {}).get("per_contributor", {}).items(),
                key=lambda x: x[1],
                reverse=True,
            )[:5]
            ctx["top_contributors"] = [{"name": n, "commits": c} for n, c in top_contribs if not is_bot(n)]

    return json.dumps(ctx, default=str)


def _chat_config_js(system_prompt: str) -> str:
    """Return a <script> tag injecting the chat config into window.__prodChat."""
    # Replace </ with <\/ so a value containing </script> cannot terminate the block early.
    safe = lambda v: json.dumps(v).replace("</", "<\\/")  # noqa: E731
    return f"<script>window.__prodChat={{apiKey:{safe(_LLM_API_KEY)},systemPrompt:{safe(system_prompt)}}};</script>"


def _html_page(title: str, body: str, month: str = "", show_overview_link: bool = True, chat_context: str = "") -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")  # noqa: UP017
    month_str = month or datetime.now(timezone.utc).strftime("%Y-%m")  # noqa: UP017
    _overview_link = '<a href="productivity.html">← Overview</a>' if show_overview_link else ""
    nav = f"<nav>{_overview_link}</nav>"
    system_prompt = (
        _PROD_SYSTEM_PROMPT_PREAMBLE + "\n\n## Current Page Data\n" + chat_context
        if chat_context
        else _PROD_SYSTEM_PROMPT_PREAMBLE
    )
    chat_config = _chat_config_js(system_prompt)
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — Productivity Dashboard</title>
  <script src="{_CHART_CDN}"></script>
  <style>{_BASE_CSS}
{_CHAT_CSS}</style>
</head>
<body>
<header>
  <div>
    <h1>{title}</h1>
  </div>
  {nav}
</header>
<main>
{body}
</main>
<footer>Generated {generated} &nbsp;·&nbsp; {month_str}</footer>
{chat_config}
{_CHAT_HTML}
{_CHAT_JS}
</body>
</html>"""


def _chart_js(
    chart_id: str,
    labels: list[str],
    datasets: list[dict[str, Any]],
    show_legend: bool = True,
    right_series: list[dict[str, Any]] | None = None,
    static: bool = False,
    y_integer: bool = False,
    y_percent: bool = False,
) -> str:
    """Render a Chart.js line chart as an inline <script> block.

    static=True registers the chart in window._staticCharts instead of window._charts,
    keeping it immune to the product-toggle iterator which only visits window._charts.

    right_series: optional list of overlays on a shared right Y axis.
      Each dict: data (list), label (str), color (str), format ("percent"|"currency"), toggle_id (str).
    """
    all_datasets = list(datasets)
    if right_series:
        for rs in right_series:
            all_datasets.append(
                {
                    "label": rs["label"],
                    "data": rs["data"],
                    "borderColor": rs["color"],
                    "backgroundColor": "transparent",
                    "borderWidth": 1.5,
                    "borderDash": [6, 3],
                    "pointRadius": 2,
                    "pointBackgroundColor": rs["color"],
                    "spanGaps": True,
                    "tension": 0.3,
                    "yAxisID": "y1",
                }
            )

    datasets_json = json.dumps(all_datasets)
    labels_json = json.dumps(labels)
    legend_cfg = (
        "{ labels: { color: '#8890a4', font: { size: 11, family: \"'Barlow Condensed', sans-serif\" } } }"
        if show_legend
        else "{ display: false }"
    )
    if right_series:
        first = right_series[0]
        y1_color = first["color"]
        if first.get("format") == "currency":
            tick_cb = "function(v) { return '$' + (v >= 1000 ? (v/1000).toFixed(1)+'k' : v.toFixed(0)); }"
            scale_range = "beginAtZero: true,"
        elif first.get("format") == "number":
            tick_cb = "function(v) { return Number.isInteger(v) ? v : ''; }"
            scale_range = "beginAtZero: true,"
        else:
            tick_cb = "function(v) { return v + '%'; }"
            scale_range = "min: 0, max: 100,"
        # Axis title: "Users" when all series are number format, "Spend ($)" for currency, else first label
        if all(rs.get("format") == "number" for rs in right_series):
            y1_title = "Users"
        elif len(right_series) > 1 and any(rs.get("format") == "currency" for rs in right_series):
            y1_title = "Spend ($)"
        else:
            y1_title = first["label"]
        y1_scale = f"""y1: {{
        position: 'right',
        {scale_range}
        ticks: {{ color: '{y1_color}', font: {{ size: 10 }}, callback: {tick_cb} }},
        grid: {{ drawOnChartArea: false }},
        title: {{ display: true, text: '{y1_title}', color: '{y1_color}', font: {{ size: 10 }} }}
      }},"""
    else:
        y1_scale = ""

    # Build per-series toggle JS — each series has its own checkbox by toggle_id
    toggle_js_blocks = ""
    if right_series:
        for rs in right_series:
            tid = rs.get("toggle_id", "")
            lbl = rs["label"].replace("'", "\\'")
            if not tid:
                continue
            toggle_js_blocks += f"""
  (function() {{
    var t = document.getElementById('{tid}');
    if (!t) return;
    t.addEventListener('change', function() {{
      var show = t.checked;
      chart.data.datasets.forEach(function(ds, i) {{
        if (ds.label === '{lbl}') chart.setDatasetVisibility(i, show);
      }});
      var anyY1 = chart.data.datasets.some(function(ds, i) {{
        return ds.yAxisID === 'y1' && chart.getDatasetMeta(i).visible;
      }});
      chart.options.scales.y1.display = anyY1;
      chart.update('none');
    }});
  }})();"""

    return f"""<script>
(function() {{
  var ctx = document.getElementById('{chart_id}').getContext('2d');
  window._charts = window._charts || {{}};
  var datasets = {datasets_json};
  // Trendlines for primary axis datasets only (skip AI adoption overlay)
  var trendDatasets = [];
  datasets.forEach(function(ds) {{
    if (ds.yAxisID === 'y1') return;
    var pts = ds.data.map(function(v, i) {{ return {{ x: i, y: v }}; }}).filter(function(p) {{ return p.y !== null && p.y !== undefined; }});
    if (pts.length < 2) return;
    var n = pts.length, sx = 0, sy = 0, sxy = 0, sx2 = 0;
    pts.forEach(function(p) {{ sx += p.x; sy += p.y; sxy += p.x * p.y; sx2 += p.x * p.x; }});
    var denom = n * sx2 - sx * sx;
    if (denom === 0) return;
    var m = (n * sxy - sx * sy) / denom;
    var b = (sy - m * sx) / n;
    var trendData = ds.data.map(function(v, i) {{ return (v !== null && v !== undefined) ? m * i + b : null; }});
    trendDatasets.push({{
      label: ds.label + ' trend',
      data: trendData,
      borderColor: ds.borderColor,
      borderWidth: 1.5,
      borderDash: [4, 4],
      pointRadius: 0,
      spanGaps: true,
      tension: 0,
      backgroundColor: 'transparent',
    }});
  }});
  var chart = new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: {labels_json},
      datasets: datasets.concat(trendDatasets)
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {legend_cfg},
        tooltip: {{
          backgroundColor: '#181c25', titleColor: '#eef0f5', bodyColor: '#8890a4',
          borderColor: '#252a36', borderWidth: 1,
          itemSort: function(a, b) {{ return b.parsed.y - a.parsed.y; }},
          filter: function(item) {{ return !item.dataset.label.endsWith(' trend'); }}
        }}
      }},
      scales: {{
        x: {{ ticks: {{ color: '#8890a4' }}, grid: {{ color: '#252a36' }} }},
        y: {{ beginAtZero: true, {"max: 100," if y_percent else ""}ticks: {{ color: '#8890a4', {("callback: function(v) { return Number.isInteger(v) ? v : ''; }," if y_integer else "callback: function(v) { return v + '%'; }," if y_percent else "")} }}, grid: {{ color: '#252a36' }} }},
        {y1_scale}
      }}
    }}
  }});
  var _reg = window.{("_staticCharts" if static else "_charts")} = window.{("_staticCharts" if static else "_charts")} || {{}};
  _reg['{chart_id}'] = chart;
  var toggle = document.getElementById('trendline-{chart_id}');
  if (toggle) {{
    toggle.addEventListener('change', function() {{
      var show = toggle.checked;
      chart.data.datasets.forEach(function(ds, i) {{
        if (ds.label && ds.label.endsWith(' trend')) {{
          chart.setDatasetVisibility(i, show);
        }}
      }});
      chart.update('none');
    }});
  }}
  {toggle_js_blocks}
}})();
</script>"""


def _chart_js_dual_axis(
    chart_id: str,
    labels: list[str],
    left_datasets: list[dict[str, Any]],
    right_datasets: list[dict[str, Any]],
) -> str:
    """Render a Chart.js line chart with two independent Y-axes (no trendlines)."""
    for ds in left_datasets:
        ds["yAxisID"] = "y"
    for ds in right_datasets:
        ds["yAxisID"] = "y2"
    all_datasets = left_datasets + right_datasets
    datasets_json = json.dumps(all_datasets)
    labels_json = json.dumps(labels)
    y2_color = right_datasets[0].get("borderColor", "#f59e0b") if len(right_datasets) == 1 else "#8890a4"
    return f"""<script>
(function() {{
  var ctx = document.getElementById('{chart_id}').getContext('2d');
  window._charts = window._charts || {{}};
  var chart = new Chart(ctx, {{
    type: 'line',
    data: {{ labels: {labels_json}, datasets: {datasets_json} }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ labels: {{ color: '#8890a4', font: {{ size: 11, family: \"'Barlow Condensed', sans-serif\" }} }} }},
        tooltip: {{
          backgroundColor: '#181c25', titleColor: '#eef0f5', bodyColor: '#8890a4',
          borderColor: '#252a36', borderWidth: 1
        }}
      }},
      scales: {{
        x: {{ ticks: {{ color: '#8890a4' }}, grid: {{ color: '#252a36' }} }},
        y: {{ beginAtZero: true, ticks: {{ color: '#8890a4' }}, grid: {{ color: '#252a36' }} }},
        y2: {{
          position: 'right',
          beginAtZero: true,
          ticks: {{ color: '{y2_color}', font: {{ size: 10 }}, callback: function(v) {{ return '$' + v.toFixed(2); }} }},
          grid: {{ drawOnChartArea: false }},
          title: {{ display: true, text: 'Spend (USD)', color: '{y2_color}', font: {{ size: 10 }} }}
        }}
      }}
    }}
  }});
  window._charts['{chart_id}'] = chart;
}})();
</script>"""


def _bar_chart_js(chart_id: str, labels: list[str], data: list[float], colour: str) -> str:
    """Render a horizontal bar chart for per-contributor commits."""
    data_json = json.dumps(data)
    labels_json = json.dumps(labels)
    return f"""<script>
(function() {{
  var ctx = document.getElementById('{chart_id}').getContext('2d');
  window._charts = window._charts || {{}};
  window._charts['{chart_id}'] = new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: {labels_json},
      datasets: [{{
        label: 'Commits',
        data: {data_json},
        backgroundColor: '{colour}88',
        borderColor: '{colour}',
        borderWidth: 1
      }}]
    }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{ backgroundColor: '#181c25', titleColor: '#eef0f5', bodyColor: '#8890a4',
                   borderColor: '#252a36', borderWidth: 1,
                   itemSort: function(a, b) {{ return b.parsed.y - a.parsed.y; }} }}
      }},
      scales: {{
        x: {{ beginAtZero: true, ticks: {{ color: '#8890a4' }}, grid: {{ color: '#252a36' }} }},
        y: {{ ticks: {{ color: '#8890a4' }}, grid: {{ color: 'transparent' }} }}
      }}
    }}
  }});
}})();
</script>"""


def _stacked_bar_chart_js(chart_id: str, labels: list[str], datasets: list[dict[str, Any]]) -> str:
    """Render a stacked vertical bar chart (e.g. AI spend by tool)."""
    datasets_json = json.dumps(datasets)
    labels_json = json.dumps(labels)
    return f"""<script>
(function() {{
  var ctx = document.getElementById('{chart_id}').getContext('2d');
  window._staticCharts = window._staticCharts || {{}};
  window._staticCharts['{chart_id}'] = new Chart(ctx, {{
    type: 'bar',
    data: {{ labels: {labels_json}, datasets: {datasets_json} }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ labels: {{ color: '#8890a4', font: {{ size: 11, family: "'Barlow Condensed', sans-serif" }} }} }},
        tooltip: {{
          mode: 'index',
          backgroundColor: '#181c25', titleColor: '#eef0f5', bodyColor: '#8890a4',
          borderColor: '#252a36', borderWidth: 1,
          callbacks: {{
            label: function(ctx) {{ return ctx.dataset.label + ': $' + ctx.parsed.y.toLocaleString(); }},
            footer: function(items) {{
              var t = items.reduce(function(s, i) {{ return s + i.parsed.y; }}, 0);
              return 'Total: $' + Math.round(t).toLocaleString();
            }}
          }}
        }}
      }},
      scales: {{
        x: {{ stacked: true, ticks: {{ color: '#8890a4' }}, grid: {{ color: '#252a36' }} }},
        y: {{
          stacked: true, beginAtZero: true,
          ticks: {{ color: '#8890a4', callback: function(v) {{ return '$' + (v >= 1000 ? Math.round(v/1000) + 'k' : v); }} }},
          grid: {{ color: '#252a36' }}
        }}
      }}
    }}
  }});
}})();
</script>"""


def _breadcrumb(*parts: tuple[str, str | None]) -> str:
    """Build a breadcrumb HTML string. parts = (label, href_or_None)."""
    items = []
    for i, (label, href) in enumerate(parts):
        if href and i < len(parts) - 1:
            items.append(f'<a href="{href}">{label}</a>')
        else:
            items.append(f"<span>{label}</span>")
    sep = '<span style="color:var(--muted)">/</span>'
    return f'<div class="breadcrumb">{sep.join(items)}</div>'


def _yoy_badge(
    from_val: float | None,
    to_val: float | None,
    from_month: str,
    to_month: str,
    badge_id: str = "",
    lower_is_better: bool = False,
) -> str:
    """Render a compact period-over-period delta badge below a chart title.

    The outer div carries *badge_id* so JS can update .badge-val on toggle.
    The range label is static; only the value+color span is rewritten by JS.
    Set lower_is_better=True for metrics like rework rate where a decrease is good.
    """
    id_attr = f" id='{badge_id}'" if badge_id else ""
    if from_val is None or to_val is None or from_val == 0:
        # Render placeholder so JS can populate it once data loads
        return (
            f'<div{id_attr} style=\'font-family:"Azeret Mono",monospace;font-size:0.72rem;'
            f"letter-spacing:.02em;margin-top:-8px;margin-bottom:12px'>"
            f"<span class='badge-val'></span>"
            f"<span style='color:var(--muted);font-size:0.65rem;margin-left:8px'>"
            f"{from_month} → {to_month}</span></div>"
        )
    pct = (to_val - from_val) / from_val * 100
    arrow = "▲" if pct >= 0 else "▼"
    is_good = (pct < 0) if lower_is_better else (pct >= 0)
    color = "#4ade80" if is_good else "#f87171"
    sign = "+" if pct >= 0 else ""
    return (
        f'<div{id_attr} style=\'font-family:"Azeret Mono",monospace;font-size:0.72rem;'
        f"letter-spacing:.02em;margin-top:-8px;margin-bottom:12px'>"
        f"<span class='badge-val' style='color:{color}'>{arrow}&nbsp;{sign}{pct:.1f}%</span>"
        f"<span style='color:var(--muted);font-size:0.65rem;margin-left:8px'>"
        f"{from_month} → {to_month}</span></div>"
    )


# ─── ACTIVITY SECTION RENDERER ───────────────────────────────────────────────


def _render_activity_section(data: dict[str, Any]) -> str:
    """Render commit classification bar + top contributors card from a repo JSON dict."""
    classification = data.get("commit_classification", {})
    if not any(classification.values()):
        return ""
    class_total = sum(classification.values()) or 1
    class_colors = {
        "feature": "#4ade80",
        "bugfix": "#f87171",
        "test": "#c084fc",
        "tooling": "#fbbf24",
        "maintenance": "#818cf8",
        "docs": "#60a5fa",
        "other": "#8890a4",
    }

    bar_segments = ""
    legend_items = ""
    for cat in ["feature", "bugfix", "test", "tooling", "maintenance", "docs", "other"]:
        count = classification.get(cat, 0)
        pct = count / class_total * 100
        color = class_colors[cat]
        if count > 0:
            bar_segments += (
                f"<div style='width:{pct:.1f}%;background:{color};height:100%;border-radius:2px'"
                f" title='{cat}: {count} ({pct:.0f}%)'></div>"
            )
        legend_items += (
            f"<span style='display:inline-flex;align-items:center;gap:4px;"
            f'font-family:"Barlow Condensed",sans-serif;font-size:0.75rem;color:var(--muted)\'>'
            f"<span style='width:10px;height:10px;border-radius:2px;background:{color};"
            f"display:inline-block'></span>{cat} {count}</span> "
        )

    per_contrib = data.get("commits", {}).get("per_contributor", {})
    commits_total = data.get("commits", {}).get("total", 0) or 1
    top_contribs = sorted(per_contrib.items(), key=lambda x: x[1], reverse=True)[:10]
    contrib_rows = ""
    for name, count in top_contribs:
        pct = count / commits_total * 100
        bar_color = "var(--muted)" if is_bot(name) else "#60a5fa"
        contrib_rows += (
            f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:4px'>"
            f"<span style='flex:0 0 160px;font-size:0.8rem;overflow:hidden;"
            f"text-overflow:ellipsis;white-space:nowrap'>{_esc(name)}</span>"
            f"<div style='flex:1;height:4px;background:var(--border);border-radius:2px;overflow:hidden'>"
            f"<div style='width:{pct:.1f}%;height:100%;background:{bar_color};border-radius:2px'></div></div>"
            f"<span style='flex:0 0 90px;font-family:\"Azeret Mono\",monospace;font-size:0.75rem;color:var(--muted);text-align:right;white-space:nowrap'>"
            f"{count} ({pct:.0f}%)</span></div>"
        )

    return (
        f"<div class='chart-card' style='margin-top:24px'>"
        f"<h2>Activity Breakdown</h2>"
        f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:24px'>"
        f"<div>"
        f'<div style=\'font-family:"Barlow Condensed",sans-serif;font-size:0.72rem;font-weight:600;letter-spacing:.05em;'
        f"text-transform:uppercase;color:var(--muted);margin-bottom:8px'>Commit Classification</div>"
        f"<div style='display:flex;height:20px;border-radius:2px;overflow:hidden;gap:1px;margin-bottom:8px'>"
        f"{bar_segments}</div>"
        f"<div style='display:flex;flex-wrap:wrap;gap:8px'>{legend_items}</div>"
        f"</div>"
        f"<div>"
        f'<div style=\'font-family:"Barlow Condensed",sans-serif;font-size:0.72rem;font-weight:600;letter-spacing:.05em;'
        f"text-transform:uppercase;color:var(--muted);margin-bottom:8px'>Top Contributors</div>"
        f"<div style='min-height:80px'>{contrib_rows}</div>"
        f"</div>"
        f"</div></div>"
    )


# ─── HEALTH SECTION RENDERER ─────────────────────────────────────────────────


def _render_health_section(health: dict[str, Any], total_commits: int = 0) -> str:
    """Render AI Readiness + AI Adoption cards from a health JSON dict."""

    # ── AI Readiness ──────────────────────────────────────────────────────────
    readiness = health.get("ai_readiness", {})

    def _num(key: str, fmt: str) -> str:
        entry = readiness.get(key)
        v = entry.get("value") if isinstance(entry, dict) else entry
        return fmt.format(v) if v is not None else "—"

    def _bool_icon(key: str) -> str:
        v = readiness.get(key)
        if v is True:
            return '<span style="color:#4ade80">✓</span>'
        if v is False:
            return '<span style="color:#f87171">✗</span>'
        return "—"

    def _tip(text: str) -> str:
        return f"<span class='info-tip' tabindex='0'>&#x24D8;<span class='info-tip-body'>{text}</span></span>"

    def _metric_cell(label: str, value: str, tip: str = "") -> str:
        label_html = (
            f"<span style='display:flex;align-items:center;gap:4px'>{label}{_tip(tip)}</span>" if tip else label
        )
        return (
            f"<div style='padding:12px;background:var(--bg);border-radius:3px'>"
            f"<div style='font-size:0.72rem;font-weight:600;letter-spacing:.03em;"
            f"text-transform:uppercase;color:var(--muted);margin-bottom:4px'>{label_html}</div>"
            f"<div style='font-size:1.3rem;font-weight:800;font-family:\"Azeret Mono\",monospace'>{value}</div>"
            f"</div>"
        )

    readiness_cells = "".join(
        [
            _metric_cell(
                "Jira Ref Rate",
                _num("ticket_reference_rate", "{:.1f}%"),
                "% of commits that reference a Jira ticket in the message.",
            ),
            _metric_cell(
                "Feature-Test Coupling",
                _num("feature_test_coupling", "{:.1f}%"),
                "% of feature commits paired with a test commit in the same push.",
            ),
            _metric_cell(
                "Test-to-Code Ratio",
                _num("test_to_code_ratio", "{:.2f}"),
                "Test files ÷ source files. Higher means better test coverage hygiene.",
            ),
            _metric_cell(
                "Commit Body Rate",
                _num("commit_body_rate", "{:.1f}%"),
                "% of commits that include a descriptive body paragraph.",
            ),
            _metric_cell("README", _bool_icon("readme_exists"), "Repository contains a README file."),
            _metric_cell(
                "CLAUDE.md",
                _bool_icon("claude_md_exists"),
                "Repository contains a CLAUDE.md guidance file for AI agents.",
            ),
            _metric_cell(
                "CI Config", _bool_icon("ci_config_exists"), "Repository contains a CI pipeline configuration file."
            ),
            _metric_cell(
                "Skills",
                _bool_icon("skills_defined"),
                "Repository has Claude Code skill files (.claude/skills/) or Cursor rules (.cursor/rules/).",
            ),
        ]
    )
    readiness_html = (
        f"<div class='chart-card' style='margin-top:24px'>"
        f"<h2>AI Readiness</h2>"
        f"<div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px'>"
        f"{readiness_cells}</div></div>"
    )

    # ── AI Adoption ───────────────────────────────────────────────────────────
    adoption = health.get("ai_adoption", {})
    combined_rate = adoption.get("ai_assisted_commit_rate", 0.0) or 0.0
    combined_count = adoption.get("ai_assisted_commits", 0) or 0
    co = adoption.get("co_authored_commits", {})
    co_total = co.get("total", 0) if isinstance(co, dict) else 0
    by_tool: dict[str, int] = co.get("by_tool", {}) if isinstance(co, dict) else {}
    co_rate = adoption.get("co_authored_rate", 0.0) or 0.0

    # Tool color map (matches EPI dashboard)
    tool_colours = {"claude": "#fbbf24", "cursor": "#60a5fa", "copilot": "#4ade80", "cody": "#c084fc"}
    _fallback_colour = "#8890a4"
    tool_total = sum(by_tool.values()) or 1
    tool_bar = "".join(
        f"<div style='flex:{v / tool_total};background:{tool_colours.get(k.lower(), _fallback_colour)};min-width:2px' title='{k}: {v}'></div>"
        for k, v in by_tool.items()
    )
    tool_legend = " ".join(
        f"<span style='color:{tool_colours.get(k.lower(), _fallback_colour)};font-size:0.75rem'>● {k} {v}</span>"
        for k, v in by_tool.items()
    )

    # Cursor attribution (optional)
    cursor = adoption.get("cursor_line_attribution")
    cursor_html = ""
    if cursor:
        tab = cursor.get("tab_lines_added", 0)
        comp = cursor.get("composer_lines_added", 0)
        manual = cursor.get("non_ai_lines_added", 0)
        matched = cursor.get("commits_matched", 0)
        total_lines = (tab + comp + manual) or 1
        ai_lines = tab + comp
        ai_pct = ai_lines / total_lines * 100
        tab_pct = tab / total_lines * 100
        comp_pct = comp / total_lines * 100
        manual_pct = manual / total_lines * 100
        matched_pct = matched / total_commits * 100 if total_commits > 0 else 0
        cursor_bar = "".join(
            [
                f"<div style='width:{tab_pct:.1f}%;background:#f59e0b;height:100%;border-radius:2px' title='Tab: {tab}'></div>"
                if tab > 0
                else "",
                f"<div style='width:{comp_pct:.1f}%;background:#6366f1;height:100%;border-radius:2px' title='Composer: {comp}'></div>"
                if comp > 0
                else "",
                f"<div style='width:{manual_pct:.1f}%;background:var(--border);height:100%;border-radius:2px' title='Manual: {manual}'></div>"
                if manual > 0
                else "",
            ]
        )
        cursor_html = (
            f"<div>"
            f"<div style='font-size:0.72rem;font-weight:600;letter-spacing:.05em;"
            f"text-transform:uppercase;color:var(--muted);margin-bottom:8px'>Cursor Attribution</div>"
            f"<div style='display:flex;align-items:baseline;gap:8px;margin-bottom:12px'>"
            f"<span style='font-size:2rem;font-weight:800;font-family:\"Azeret Mono\",monospace'>{matched}</span>"
            f"<span style='font-size:0.85rem;color:var(--muted)'>({matched_pct:.0f}% of commits)</span>"
            f"</div>"
            f"<div style='display:flex;height:16px;border-radius:2px;overflow:hidden;gap:1px;margin-bottom:8px'>{cursor_bar}</div>"
            f"<div style='display:flex;align-items:baseline;gap:8px;margin-bottom:8px'>"
            f"<span style='font-size:1.1rem;font-weight:700;font-family:\"Azeret Mono\",monospace'>{ai_pct:.0f}%</span>"
            f"<span style='font-size:0.82rem;color:var(--muted)'>AI-Assisted Lines</span>"
            f"</div>"
            f"<div style='display:flex;flex-wrap:wrap;gap:8px;font-size:0.75rem;color:var(--muted)'>"
            f"<span style='color:#f59e0b'>Tab: {tab:,}</span>"
            f"<span style='color:#6366f1'>Composer: {comp:,}</span>"
            f"<span>Manual: {manual:,}</span>"
            f"</div></div>"
        )

    grid_cols = "1fr 1fr" if cursor_html else "1fr"
    adoption_html = (
        f"<div class='chart-card' style='margin-top:16px'>"
        f"<h2>AI Adoption</h2>"
        f"<div style='display:flex;align-items:baseline;gap:12px;margin-bottom:16px;"
        f"padding-bottom:16px;border-bottom:1px solid var(--border)'>"
        f"<span style='font-size:2.5rem;font-weight:800;font-family:\"Azeret Mono\",monospace'>{combined_rate:.0f}%</span>"
        f"<span style='font-size:0.95rem;color:var(--muted)'>AI-Assisted Commits ({combined_count})</span>"
        f"</div>"
        f"<div style='display:grid;grid-template-columns:{grid_cols};gap:24px'>"
        f"<div>"
        f"<div style='font-size:0.72rem;font-weight:600;letter-spacing:.05em;"
        f"text-transform:uppercase;color:var(--muted);margin-bottom:8px;display:flex;align-items:center;gap:4px'>"
        f"Claude Attribution"
        f"&nbsp;<span class='info-tip' tabindex='0'>&#x24D8;<span class='info-tip-body'>"
        f"Commits where an AI tool co-authored the code, detected via the "
        f"<strong>Co-Authored-By:</strong> git trailer (standard git format) or "
        f"the <strong>(co-authored with Claude Code)</strong> footer written by a "
        f"git commit skill. Tool attribution (Claude, Cursor, Copilot) is "
        f"derived from the tool name in the trailer. Note: this only captures commits "
        f"where the engineer explicitly included the co-author trailer — it undercounts "
        f"actual AI usage compared to Cursor&#39;s commit-level telemetry."
        f"</span></span></div>"
        f"<div style='display:flex;align-items:baseline;gap:8px;margin-bottom:12px'>"
        f"<span style='font-size:2rem;font-weight:800;font-family:\"Azeret Mono\",monospace'>{co_total}</span>"
        f"<span style='font-size:0.85rem;color:var(--muted)'>({co_rate:.1f}% of commits)</span>"
        f"</div>"
        + (
            f"<div style='display:flex;height:16px;border-radius:2px;overflow:hidden;"
            f"gap:1px;margin-bottom:8px'>{tool_bar}</div>"
            f"<div style='display:flex;gap:12px;flex-wrap:wrap'>{tool_legend}</div>"
            if by_tool
            else ""
        )
        + "</div>"
        + cursor_html
        + "</div></div>"
    )

    return readiness_html + adoption_html


# ─── ENGINEERING METRICS SECTION ─────────────────────────────────────────────

# Categories and their metrics, in display order.
_ENGINEERING_METRIC_CATEGORIES: list[dict[str, Any]] = [
    {
        "label": "Delivery Velocity",
        "metrics": ["commits_per_engineer", "mrs_per_engineer", "deployment_frequency", "features_shipped"],
    },
    {
        "label": "Delivery Quality",
        "metrics": ["change_failure_rate", "post_release_defect_rate", "rework_rate"],
    },
    {
        "label": "Engineering Efficiency",
        "metrics": ["lead_time_days", "cycle_delivery_accuracy"],
    },
    {
        "label": "Engineering Health",
        "metrics": ["mttr_hours", "bus_factor", "knowledge_distribution"],
    },
]

# Info tooltips for each category header — explain scope and missing data.
_CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "Delivery Velocity": (
        "<strong>How fast the team ships.</strong> "
        "Commits/Engineer and MRs/Engineer are collected automatically from Git. "
        "Features Shipped is derived from commit classification (feat/add/implement prefixes). "
        "Deployment Frequency requires manual input in the monthly YAML — "
        "it will show &#8212; without it."
    ),
    "Delivery Quality": (
        "<strong>How reliable the output is.</strong> "
        "Rework Rate is automated (reverts and fix-commits). "
        "Change Failure Rate and Post-Release Defect Rate require manual input "
        "(incident tracking and release defect logs) — they will show &#8212; until added to the YAML. "
        "<strong>Currently missing for most products:</strong> Post-Release Defect Rate."
    ),
    "Engineering Efficiency": (
        "<strong>How smoothly work flows from commit to production.</strong> "
        "Both metrics — Lead Time in Days and Cycle Delivery Accuracy — "
        "require manual input (deployment timestamps and sprint planning data). "
        "They will show &#8212; until added to the monthly YAML. "
        "<strong>Currently missing for all products:</strong> Lead Time in Days, "
        "Cycle Delivery Accuracy."
    ),
    "Engineering Health": (
        "<strong>Team sustainability and resilience.</strong> "
        "Bus Factor and Knowledge Distribution are automated from Git "
        "(contributor spread across repos). "
        "MTTR (Mean Time to Restore) requires manual input from incident tracking "
        "and will show &#8212; without it. "
        "<strong>Currently missing for most products:</strong> MTTR Hours."
    ),
}

# Human-readable labels for each metric.
_METRIC_DISPLAY_LABELS: dict[str, str] = {
    "commits_per_engineer": "Commits per Engineer",
    "deployment_frequency": "Deployment Frequency",
    "mrs_per_engineer": "MRs per Engineer",
    "features_shipped": "Features Shipped",
    "commit_intentionality": "Commit Intentionality",
    "change_failure_rate": "Change Failure Rate",
    "post_release_defect_rate": "Post-Release Defect Rate",
    "rework_rate": "Rework Rate",
    "lead_time_days": "Lead Time (Days)",
    "cycle_delivery_accuracy": "Cycle Delivery Accuracy",
    "mttr_hours": "MTTR (Hours)",
    "bus_factor": "Bus Factor",
    "knowledge_distribution": "Knowledge Distribution",
}

# One-sentence descriptions shown in the info tooltip on each metric chart.
_METRIC_DESCRIPTIONS: dict[str, str] = {
    "commits_per_engineer": "Non-bot commits per active engineer per month — measures raw coding velocity.",
    "deployment_frequency": "How often the team ships to production per month — a core DORA lead indicator.",
    "mrs_per_engineer": "Merge requests merged per active engineer per month — measures delivery cadence.",
    "features_shipped": "Commits classified as feature work (feat/add/implement) per engineer per month.",
    "commit_intentionality": (
        "% of commits with a clear intent prefix (feature, bugfix, test) — "
        "higher means more deliberate, purposeful work."
    ),
    "change_failure_rate": "% of deployments that cause an incident or rollback — lower is better.",
    "post_release_defect_rate": "% of features with defects reported after release — lower is better.",
    "rework_rate": "% of commits that revert, fix, or redo recent work — lower indicates higher first-time quality.",
    "lead_time_days": "Average days from commit to production deployment — lower means faster flow.",
    "cycle_delivery_accuracy": (
        "% of planned cycle work delivered on time — measures planning reliability and execution predictability."
    ),
    "mttr_hours": "Mean time to restore service after an incident in hours — lower is better.",
    "bus_factor": "Minimum number of engineers whose absence would halt progress — higher means safer knowledge spread.",
    "knowledge_distribution": (
        "% of repos with more than one active contributor — higher means less single-point-of-failure risk."
    ),
}

# Band colour for reference lines — muted so they don't distract from data lines.
_BAND_LINE_COLOUR = "#3d4354"


def _get_band_threshold_datasets(metric_name: str) -> list[dict[str, Any]]:
    """Return flat reference-line datasets for each band boundary of *metric_name*.

    Each band boundary becomes a horizontal dashed line rendered as a Chart.js
    dataset with pointRadius=0, borderDash=[4,4], no legend entry.  We use the
    band min_val values as thresholds (skipping 0 / the lowest boundary to avoid
    a redundant baseline at zero).
    """
    from epi.scoring import METRIC_BANDS  # local import keeps top-level deps minimal

    if metric_name not in METRIC_BANDS:
        return []

    config: dict[str, Any] = METRIC_BANDS[metric_name]
    bands: list[dict[str, Any]] = list(config["bands"])
    seen: set[float] = set()
    datasets: list[dict[str, Any]] = []

    for band in bands:
        min_val: float = float(band["min_val"])
        max_val: float = float(band["max_val"])
        band_name_str: str = str(band["name"])
        boundary_pairs: list[tuple[float, str]] = [
            (min_val, band_name_str),
            (max_val, band_name_str),
        ]
        for threshold_val, band_name in boundary_pairs:
            # Skip 0 — most metrics start at 0 and a line there is just noise
            if threshold_val == 0 or threshold_val in seen:
                continue
            seen.add(threshold_val)
            datasets.append(
                {
                    "label": f"_band_{metric_name}_{threshold_val}",  # _ prefix = hidden from legend
                    "data": None,  # filled in per-render using month labels length
                    "threshold_val": threshold_val,
                    "band_name": band_name,
                    "borderColor": _BAND_LINE_COLOUR,
                    "backgroundColor": "transparent",
                    "borderWidth": 1,
                    "borderDash": [4, 4],
                    "pointRadius": 0,
                    "spanGaps": True,
                    "tension": 0,
                }
            )

    return datasets


def _render_engineering_metrics_section(
    products: list[str],
    display_names: dict[str, str],
    base_dir: Path,
    months: list[str],
    product_metrics: dict[tuple[str, str], dict[str, float | None]] | None = None,
) -> tuple[str, dict[tuple[str, str], dict[str, float | None]]]:
    """Render the Engineering Metrics section for the overview page.

    Calls score_product() for each (product, month) combination where data exists,
    then renders one Chart.js line chart per metric, grouped by category.  Band
    thresholds are drawn as horizontal dashed reference lines.

    product_metrics: pre-computed per-engineer metrics from load_product_data/
    compute_per_engineer_metrics — used for metrics like commits_per_engineer that
    come from git data rather than the scoring system.

    Returns (html_string, scored_cache) so the caller can reuse scored values
    (e.g. for the chat context) without repeating file reads.
    """
    from epi.scoring import METRIC_BANDS, score_product  # local import — keep deps minimal

    if not products or not months:
        return "", {}

    # ── Collect scored metric values ────────────────────────────────────────
    # Value cache: {(product, month) -> {metric_name -> value | None}}
    scored_cache: dict[tuple[str, str], dict[str, float | None]] = {}
    # Manual flag cache: {metric_name -> True if ANY (product, month) has is_manual=True}
    manual_metrics: set[str] = set()

    # score_product requires repos.yaml — skip the whole section if it's absent
    repos_yaml = base_dir / "repos.yaml"
    if not repos_yaml.exists():
        return "", {}

    # Load repos_config once so score_product doesn't re-read repos.yaml on every call
    try:
        repos_config: dict | None = load_repos_config(base_dir)
    except Exception:
        repos_config = None

    for product in products:
        for month in months:
            product_dir = base_dir / "products" / product
            # Only call score_product when there is something to read
            has_git = bool(list(product_dir.glob(f"{month}_*.json"))) or (product_dir / f"{month}.json").exists()
            has_manual = (product_dir / f"manual-{month}.yaml").exists()
            if not has_git and not has_manual:
                scored_cache[product, month] = {}
                continue
            try:
                result = score_product(product, month, base_dir, repos_config=repos_config)
                raw = result.get("metrics", {})
                scored_cache[product, month] = {k: v.get("value") for k, v in raw.items()}
                for k, v in raw.items():
                    if v.get("is_manual"):
                        manual_metrics.add(k)
            except Exception:
                scored_cache[product, month] = {}

    # ── Build the HTML ────────────────────────────────────────────────────────
    month_labels = [_format_month(m) for m in months]
    html_parts: list[str] = []
    script_parts: list[str] = []

    html_parts.append(
        '<h2 style=\'font-family:"Barlow Condensed",sans-serif;font-size:1.1rem;font-weight:700;'
        "text-transform:uppercase;letter-spacing:.08em;color:var(--accent);"
        "margin-top:40px;margin-bottom:12px'>Engineering Metrics</h2>"
    )
    html_parts.append(
        '<p style=\'font-family:"Barlow",sans-serif;font-size:0.82rem;color:var(--muted);'
        "margin-bottom:24px;line-height:1.55'>DORA-aligned metrics across all products. "
        "Dashed horizontal lines mark band boundaries (Concerning / Acceptable / Happy / Elite).</p>"
    )

    for category in _ENGINEERING_METRIC_CATEGORIES:
        cat_label: str = category["label"]
        cat_metrics: list[str] = category["metrics"]

        cat_desc = _CATEGORY_DESCRIPTIONS.get(cat_label, "")
        cat_tip = (
            f"&nbsp;<span class='info-tip' tabindex='0'>&#x24D8;<span class='info-tip-body'>{cat_desc}</span></span>"
            if cat_desc
            else ""
        )
        html_parts.append(
            f'<h3 style=\'font-family:"Barlow Condensed",sans-serif;font-size:0.88rem;font-weight:700;'
            f"text-transform:uppercase;letter-spacing:.1em;color:var(--muted);"
            f"margin-top:32px;margin-bottom:12px;display:flex;align-items:center;gap:4px'>"
            f"{_esc(cat_label)}{cat_tip}</h3>"
        )

        cat_cards: list[str] = []
        cat_scripts: list[str] = []

        for metric_name in cat_metrics:
            # commits_per_engineer comes from git metrics (product_metrics), not the scoring system
            is_git_metric = metric_name == "commits_per_engineer"
            if not is_git_metric and metric_name not in METRIC_BANDS:
                continue

            metric_label = _METRIC_DISPLAY_LABELS.get(metric_name, metric_name.replace("_", " ").title())

            # Build one dataset per product
            product_datasets: list[dict[str, Any]] = []
            any_data = False
            for pi, product in enumerate(products):
                if is_git_metric and product_metrics is not None:
                    series: list[float | None] = [
                        product_metrics.get((product, m), {}).get(metric_name) for m in months
                    ]
                else:
                    series = [scored_cache.get((product, m), {}).get(metric_name) for m in months]
                if any(v is not None for v in series):
                    any_data = True
                colour = COLOURS[pi % len(COLOURS)]
                is_manual_metric = metric_name in manual_metrics
                ds_base = _make_dataset(display_names.get(product, product), series, colour)
                if is_manual_metric:
                    # Manual data: dashed line with rounded caps to signal approximate values
                    ds_base.update(
                        {
                            "borderDash": [6, 4],
                            "borderCapStyle": "round",
                            "borderWidth": 1.5,
                            "pointRadius": 2,
                            "tension": 0.2,
                        }
                    )
                product_datasets.append(ds_base)

            # Skip the chart entirely when no product has any data for this metric
            if not any_data:
                continue

            # Band reference lines — rendered as extra datasets with constant y value
            # (not applicable for git metrics like commits_per_engineer)
            band_templates = [] if is_git_metric else _get_band_threshold_datasets(metric_name)
            band_datasets: list[dict[str, Any]] = []
            for tmpl in band_templates:
                const_val = tmpl["threshold_val"]
                ds: dict[str, Any] = {
                    "label": "_band_" + str(const_val),
                    "_band_label": tmpl["band_name"],
                    "data": [const_val] * len(months),
                    "borderColor": _BAND_LINE_COLOUR,
                    "backgroundColor": "transparent",
                    "borderWidth": 1,
                    "borderDash": [4, 4],
                    "pointRadius": 0,
                    "spanGaps": True,
                    "tension": 0,
                }
                band_datasets.append(ds)

            chart_id = f"engmet-{metric_name.replace('_', '-')}"
            all_datasets = product_datasets + band_datasets

            # Build the Chart.js script — legend filters out band lines (label starts with _band_)
            datasets_json = json.dumps(all_datasets)
            labels_json = json.dumps(month_labels)
            script = f"""<script>
(function() {{
  var ctx = document.getElementById('{chart_id}').getContext('2d');
  window._charts = window._charts || {{}};
  var chart = new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: {labels_json},
      datasets: {datasets_json}
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      layout: {{ padding: {{ right: 56 }} }},
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          backgroundColor: '#181c25', titleColor: '#eef0f5', bodyColor: '#8890a4',
          borderColor: '#252a36', borderWidth: 1,
          filter: function(item) {{ return !item.dataset.label.startsWith('_band_'); }},
          itemSort: function(a, b) {{ return b.parsed.y - a.parsed.y; }}
        }}
      }},
      scales: {{
        x: {{ ticks: {{ display: false }}, grid: {{ color: '#252a36' }} }},
        y: {{ beginAtZero: true, ticks: {{ color: '#8890a4' }}, grid: {{ color: '#252a36' }} }}
      }}
    }},
    plugins: [{{
      afterDraw: function(chart) {{
        var ctx = chart.ctx;
        var xScale = chart.scales.x;
        var yScale = chart.scales.y;
        if (!xScale || !yScale) return;
        var seen = {{}};
        chart.data.datasets.forEach(function(ds) {{
          if (!ds.label || !ds.label.startsWith('_band_')) return;
          var bandLabel = ds._band_label;
          if (!bandLabel || seen[bandLabel]) return;
          var val = ds.data && ds.data[0];
          if (val == null) return;
          var yPx = yScale.getPixelForValue(val);
          if (yPx < yScale.top || yPx > yScale.bottom) return;
          seen[bandLabel] = true;
          ctx.save();
          ctx.fillStyle = '#8890a4';
          ctx.font = '9px "Barlow Condensed", sans-serif';
          ctx.textAlign = 'left';
          ctx.textBaseline = 'middle';
          ctx.fillText(bandLabel, xScale.right + 4, yPx);
          ctx.restore();
        }});
      }}
    }}]
  }});
  window._charts['{chart_id}'] = chart;
}})();
</script>"""

            tip_text = _esc(_METRIC_DESCRIPTIONS.get(metric_name, ""))
            tip_html = (
                f"&nbsp;<span class='info-tip' tabindex='0'>&#x24D8;"
                f"<span class='info-tip-body'>{tip_text}</span></span>"
                if tip_text
                else ""
            )
            manual_badge = (
                '<span style=\'font-family:"Azeret Mono",monospace;font-size:0.68rem;'
                "color:var(--muted);border:1px solid var(--border);border-radius:2px;"
                "padding:1px 5px;margin-left:6px;letter-spacing:.04em'>manual</span>"
                if metric_name in manual_metrics
                else ""
            )
            cat_cards.append(
                f'<div class="chart-card">'
                f'<div class="chart-card-header">'
                f"<h2>{_esc(metric_label)}{manual_badge}{tip_html}</h2>"
                f"</div>"
                f'<div class="chart-wrapper"><canvas id="{chart_id}"></canvas></div>'
                f"</div>"
            )
            cat_scripts.append(script)

        if cat_cards:
            html_parts.append(f'<div class="charts-grid">{"".join(cat_cards)}</div>')
            script_parts.extend(cat_scripts)

    if len(html_parts) <= 2:
        # Only header + intro paragraph — no actual charts rendered
        return "", scored_cache

    return "".join(html_parts) + "\n".join(script_parts), scored_cache


# ─── PAGE RENDERERS ───────────────────────────────────────────────────────────


def render_overview_page(
    products: list[str],
    display_names: dict[str, str],
    base_dir: Path,
    months: list[str],
    all_months_full: list[str] | None = None,
) -> str:
    """Build productivity.html — all products, one chart per metric, all months.

    all_months_full: unbounded month list (including archive months) for the YoY section.
    When None, *months* is used for both the main charts and the YoY section.
    """
    # Pre-compute metrics once per (product, month) — reused across all 4 metric charts
    product_metrics: dict[tuple[str, str], dict[str, float | None]] = {
        (product, month): compute_per_engineer_metrics(load_product_data(base_dir / "products" / product, month))
        for product in products
        for month in months
    }

    _ai_spend = load_ai_spend(base_dir)
    _litellm_series: list[float | None] = [_ai_spend.get("litellm", {}).get(m) for m in months]
    _has_litellm = any(v is not None for v in _litellm_series)

    # Build Cursor spend series from ai-spend.yaml (written by collect-repo-health).
    _cursor_series: list[float | None] = [_ai_spend.get("cursor", {}).get(m) for m in months]
    _has_cursor = any(v is not None for v in _cursor_series)

    _openai_series: list[float | None] = [_ai_spend.get("openai", {}).get(m) for m in months]
    _has_openai = any(v is not None for v in _openai_series)

    # Active user series from ai-spend.yaml (manually maintained)
    _litellm_users_series: list[float | None] = [_ai_spend.get("litellm_active_users", {}).get(m) for m in months]
    _has_litellm_users = any(v is not None for v in _litellm_users_series)
    _cursor_users_series: list[float | None] = [_ai_spend.get("cursor_active_users", {}).get(m) for m in months]
    _has_cursor_users = any(v is not None for v in _cursor_users_series)

    # Product filter bar — horizontal row of checkboxes that toggle chart datasets
    product_items: list[str] = []
    for pi, p in enumerate(products):
        colour = COLOURS[pi % len(COLOURS)]
        dname = display_names.get(p, p)
        product_items.append(
            f"<span style='display:inline-flex;align-items:center;gap:6px'>"
            f"<input type='checkbox' checked class='product-toggle' data-dataset-idx='{pi}' data-product-key='{p}'"
            f" style='cursor:pointer;accent-color:{colour};width:13px;height:13px'>"
            f"<a href='productivity_{p}.html' style='font-family:\"Barlow Condensed\",sans-serif;"
            f"font-size:0.78rem;font-weight:600;letter-spacing:.04em;color:{colour};"
            f"text-decoration:underline;text-underline-offset:2px;text-decoration-color:{colour}66'>{dname}</a>"
            f"</span>"
        )
    product_filter = (
        f"<div style='display:flex;flex-wrap:wrap;gap:20px;align-items:center;"
        f"padding:12px 0;border-bottom:1px solid var(--border);margin-bottom:4px'>"
        f"{''.join(product_items)}"
        f"</div>"
    )

    _product_toggle_script = (
        """<script>
(function() {
%%BADGES%%
  document.querySelectorAll('.product-toggle').forEach(function(cb) {
    cb.addEventListener('change', function() {
      var idx = parseInt(this.dataset.datasetIdx);
      var show = this.checked;
      Object.values(window._charts || {}).forEach(function(chart) {
        var ds = chart.data.datasets[idx];
        if (ds !== undefined) {
          chart.setDatasetVisibility(idx, show);
          var trendLabel = ds.label + ' trend';
          chart.data.datasets.forEach(function(d, i) {
            if (d.label === trendLabel) chart.setDatasetVisibility(i, show);
          });
          chart.update('none');
        }
      });
      updateBadges();
      if (typeof window._recomputeYoY === 'function') window._recomputeYoY();
    });
  });
})();
</script>"""
    ).replace("%%BADGES%%", _JS_UPDATE_BADGES)

    # ── Tab 1: DORA Metrics ────────────────────────────────────────────────────
    dora_html, eng_scored_cache = _render_engineering_metrics_section(
        products, display_names, base_dir, months, product_metrics
    )

    # ── Tab 2: AI Tool Adoption ────────────────────────────────────────────────
    if _has_litellm_users or _has_cursor_users:
        adoption_datasets = []
        if _has_cursor_users:
            adoption_datasets.append(_make_dataset("Cursor Active Users", _cursor_users_series, "#a78bfa"))
        if _has_litellm_users:
            adoption_datasets.append(_make_dataset("LiteLLM Active Users", _litellm_users_series, "#f59e0b"))
        adoption_chart_id = "chart-ai-adoption-users"
        adoption_script = _chart_js(
            adoption_chart_id, months, adoption_datasets, show_legend=True, static=True, y_integer=True
        )
        ai_html = (
            f'<div class="chart-card" style="margin-top:24px">'
            f'<div class="chart-card-header"><h2>Monthly Active Users</h2></div>'
            f'<div class="chart-wrapper"><canvas id="{adoption_chart_id}"></canvas></div>'
            f"</div>" + adoption_script
        )
        if _has_openai or _has_cursor or _has_litellm:
            spend_datasets: list[dict[str, Any]] = []
            if _has_openai:
                spend_datasets.append(
                    {
                        "label": "OpenAI",
                        "data": _openai_series,
                        "backgroundColor": "#34d39966",
                        "borderColor": "#34d399",
                        "borderWidth": 1,
                    }
                )
            if _has_cursor:
                spend_datasets.append(
                    {
                        "label": "Cursor",
                        "data": _cursor_series,
                        "backgroundColor": "#a78bfa66",
                        "borderColor": "#a78bfa",
                        "borderWidth": 1,
                    }
                )
            if _has_litellm:
                spend_datasets.append(
                    {
                        "label": "LiteLLM",
                        "data": _litellm_series,
                        "backgroundColor": "#f59e0b66",
                        "borderColor": "#f59e0b",
                        "borderWidth": 1,
                    }
                )
            spend_chart_id = "chart-ai-spend"
            ai_html += (
                f'<div class="chart-card" style="margin-top:24px">'
                f'<div class="chart-card-header"><h2>AI Tool Spend</h2></div>'
                f'<div class="chart-wrapper"><canvas id="{spend_chart_id}"></canvas></div>'
                f"</div>" + _stacked_bar_chart_js(spend_chart_id, months, spend_datasets)
            )
    else:
        ai_html = (
            '<p style=\'color:var(--muted);font-family:"Barlow Condensed",sans-serif;'
            "font-size:0.85rem;padding:40px 0'>No active user data yet — "
            "add cursor_active_users / litellm_active_users to ai-spend.yaml.</p>"
        )

    # ── Tab 3: Year-over-Year ──────────────────────────────────────────────────
    _yoy_months = all_months_full if all_months_full is not None else months
    yoy_intro = (
        '<p style=\'font-family:"Barlow",sans-serif;font-size:0.82rem;color:var(--muted);'
        "margin:16px 0 24px;line-height:1.55'>Each line represents one calendar year (Jan–Dec axis). "
        "The dashed grey line shows the seasonal baseline (average across all years).</p>"
    )

    # Per-product per-year series — embedded as JS for client-side YoY recomputation
    yoy_product_data: dict[str, dict[str, dict[int, list[float | None]]]] = {}
    yoy_chart_ids: dict[str, str] = {}
    yoy_html = yoy_intro

    for metric in OVERVIEW_METRICS:
        mkey = metric["key"]
        by_year, deltas = build_yoy_series(products, base_dir, _yoy_months, mkey)
        baseline = compute_seasonal_baseline(by_year)
        yoy_slope = _least_squares_slope(by_year)
        residuals = compute_residuals(by_year, baseline, yoy_slope)

        # Build per-product series for JS recompute
        per_product: dict[str, dict[int, list[float | None]]] = {}
        for p in products:
            p_by_year, _ = build_yoy_series([p], base_dir, _yoy_months, mkey)
            per_product[p] = {yr: vals for yr, vals in p_by_year.items()}
        yoy_product_data[mkey] = per_product

        chart_id = f"yoy-{mkey.replace('_', '-')}"
        yoy_chart_ids[mkey] = chart_id

        yoy_html += render_yoy_section(
            metric_key=mkey,
            metric_label=metric["label"],
            by_year=by_year,
            deltas=deltas,
            baseline=baseline,
            residuals=residuals,
        )

    # Embed per-product YoY data and recompute function for interactive product filter
    yoy_data_json = json.dumps(yoy_product_data)
    yoy_ids_json = json.dumps(yoy_chart_ids)
    product_keys_json = json.dumps(products)
    yoy_recompute_script = f"""<script>
(function() {{
  window._yoyProductData = {yoy_data_json};
  window._yoyChartIds = {yoy_ids_json};
  window._recomputeYoY = function() {{
    var visibleProducts = [];
    document.querySelectorAll('.product-toggle:checked').forEach(function(cb) {{
      var pk = cb.dataset.productKey;
      if (pk) visibleProducts.push(pk);
    }});
    var allProducts = {product_keys_json};
    if (visibleProducts.length === 0) visibleProducts = allProducts.slice();
    Object.entries(window._yoyProductData || {{}}).forEach(function(entry) {{
      var metricKey = entry[0], productData = entry[1];
      var chartId = (window._yoyChartIds || {{}})[metricKey];
      var yoyChart = (window._staticCharts || {{}})[chartId];
      if (!yoyChart) return;
      // Collect all years across products
      var allYears = new Set();
      Object.values(productData).forEach(function(byYear) {{
        Object.keys(byYear).forEach(function(yr) {{ allYears.add(parseInt(yr)); }});
      }});
      yoyChart.data.datasets.forEach(function(ds) {{
        var year = parseInt(ds.label);
        if (!isNaN(year)) {{
          var newData = [];
          for (var cm = 0; cm < 12; cm++) {{
            var vals = visibleProducts.map(function(p) {{
              var m = (productData[p] || {{}})[year];
              return (m && m[cm] !== null && m[cm] !== undefined) ? m[cm] : null;
            }}).filter(function(v) {{ return v !== null; }});
            newData.push(vals.length ? vals.reduce(function(a,b){{return a+b;}},0)/vals.length : null);
          }}
          ds.data = newData;
        }} else if (ds.label === 'Seasonal baseline') {{
          var newBaseline = [];
          for (var cm = 0; cm < 12; cm++) {{
            var yearVals = [];
            allYears.forEach(function(yr) {{
              var vals = visibleProducts.map(function(p) {{
                var m = (productData[p] || {{}})[yr];
                return (m && m[cm] !== null && m[cm] !== undefined) ? m[cm] : null;
              }}).filter(function(v) {{ return v !== null; }});
              if (vals.length) yearVals.push(vals.reduce(function(a,b){{return a+b;}},0)/vals.length);
            }});
            newBaseline.push(yearVals.length ? yearVals.reduce(function(a,b){{return a+b;}},0)/yearVals.length : null);
          }}
          ds.data = newBaseline;
        }}
      }});
      yoyChart.update('none');
    }});
  }};
}})();
</script>"""

    yoy_html += yoy_recompute_script

    # ── Assemble tabs ──────────────────────────────────────────────────────────
    tab_css = """<style>
.tab-nav{display:flex;gap:0;margin:0 -24px;padding:0 24px;
  border-bottom:1px solid rgba(255,255,255,0.15)}
.tab-btn{background:none;border:none;border-bottom:2px solid transparent;
  padding:12px 28px 11px;
  font-family:'Barlow Condensed',sans-serif;font-size:0.9rem;font-weight:700;
  letter-spacing:.08em;text-transform:uppercase;color:var(--muted);cursor:pointer;
  transition:color 120ms ease-out;margin-bottom:-1px}
.tab-btn:hover{color:var(--text)}
.tab-btn.active{color:var(--text);border-bottom-color:var(--text)}
.tab-separator{border:none;border-top:1px solid rgba(255,255,255,0.12);margin:0 -24px 20px}
</style>"""
    tab_js = """<script>
(function(){
  document.querySelectorAll('.tab-btn').forEach(function(btn){
    btn.addEventListener('click',function(){
      document.querySelectorAll('.tab-btn').forEach(function(b){b.classList.remove('active');});
      document.querySelectorAll('.tab-panel').forEach(function(p){p.style.display='none';});
      this.classList.add('active');
      document.getElementById('tab-'+this.dataset.tab).style.display='block';
      setTimeout(function(){
        var all=Object.assign({},window._charts||{},window._staticCharts||{});
        Object.values(all).forEach(function(c){if(c&&c.resize)c.resize();});
      },10);
    });
  });
})();
</script>"""
    tab_nav = (
        '<div class="tab-nav">'
        '<button class="tab-btn active" data-tab="dora">DORA Metrics</button>'
        '<button class="tab-btn" data-tab="ai">AI Tool Adoption</button>'
        '<button class="tab-btn" data-tab="yoy">Year-over-Year</button>'
        "</div>"
    )
    body = (
        tab_css
        + tab_nav
        + "<hr class='tab-separator'>"
        + product_filter
        + _product_toggle_script
        + f'<div class="tab-panel" id="tab-dora">{dora_html}</div>'
        + f'<div class="tab-panel" id="tab-ai" style="display:none">{ai_html}</div>'
        + f'<div class="tab-panel" id="tab-yoy" style="display:none">{yoy_html}</div>'
        + tab_js
    )

    # Build compact yoy context for the AI assistant (delta per metric)
    yoy_context: dict[str, list[tuple[str, float, float]]] = {}
    for metric in OVERVIEW_METRICS:
        mkey = metric["key"]
        _, deltas = build_yoy_series(products, base_dir, _yoy_months, mkey)
        yoy_context[mkey] = [(m, p, latest) for m, p, latest, *_ in deltas]

    month_range = f"{months[0]} → {months[-1]}" if len(months) >= 2 else (months[-1] if months else "")
    spend_context = {
        "litellm_usd": {m: _ai_spend.get("litellm", {}).get(m) for m in months if _ai_spend.get("litellm", {}).get(m)},
        "cursor_usd": {m: _ai_spend.get("cursor", {}).get(m) for m in months if _ai_spend.get("cursor", {}).get(m)},
        "litellm_active_users": {
            m: _ai_spend.get("litellm_active_users", {}).get(m)
            for m in months
            if _ai_spend.get("litellm_active_users", {}).get(m)
        },
        "cursor_active_users": {
            m: _ai_spend.get("cursor_active_users", {}).get(m)
            for m in months
            if _ai_spend.get("cursor_active_users", {}).get(m)
        },
    }
    # Reuse scored cache already built by _render_engineering_metrics_section
    # to avoid re-reading repos.yaml and metric files for the latest month.
    chat_context = _build_prod_chat_context(
        "overview",
        products=products,
        display_names=display_names,
        months=months,
        product_metrics=product_metrics,
        scored_metrics=eng_scored_cache,
        yoy=yoy_context,
        spend=spend_context,
    )
    return _html_page(
        "Engineering Productivity", body, month=month_range, show_overview_link=False, chat_context=chat_context
    )


def render_product_page(
    product: str,
    display_name: str,
    base_dir: Path,
    months: list[str],
) -> str:
    """Build productivity_{product}.html — one line per repo, all months."""
    product_dir = base_dir / "products" / product

    # Collect all repos that appear in any of the months
    all_repos: set[str] = set()
    for month in months:
        all_repos.update(discover_repos(product_dir, month))
    repo_list = sorted(all_repos)

    # Pre-compute metrics once per (repo, month) — reused across all 4 metric charts.
    # Track support repos during load so we can filter them without a second file read.
    # Also capture raw last-month values for the summary table.
    support_repos: set[str] = set()
    repo_metrics: dict[tuple[str, str], dict[str, float | None] | None] = {}
    last_month_raw: dict[str, dict[str, Any]] = {}
    last_month = months[-1] if months else ""
    for repo in repo_list:
        for month in months:
            data = load_repo_data(product_dir, month, repo)
            if data is not None and data.get("meta", {}).get("category") == "support":
                support_repos.add(repo)
            repo_metrics[repo, month] = compute_per_engineer_metrics(data) if data is not None else None
            if month == last_month and data is not None:
                last_month_raw[repo] = data

    repo_list = [r for r in repo_list if r not in support_repos]

    scripts: list[str] = []
    chart_cards: list[str] = []

    for metric in OVERVIEW_METRICS:
        mkey = metric["key"]
        chart_id = f"chart-{mkey.replace('_', '-')}"

        datasets = []
        for ri, repo in enumerate(repo_list):
            series: list[float | None] = [
                repo_metrics[repo, month][mkey] if repo_metrics[repo, month] is not None else None  # type: ignore[index]
                for month in months
            ]
            colour = COLOURS[ri % len(COLOURS)]
            datasets.append(_make_dataset(repo, series, colour))

        badge = ""
        if len(months) >= 2:
            fv = [
                repo_metrics[r, months[0]][mkey]  # type: ignore[index]
                for r in repo_list
                if repo_metrics.get((r, months[0])) is not None and repo_metrics[r, months[0]][mkey] is not None  # type: ignore[index]
            ]
            tv = [
                repo_metrics[r, months[-1]][mkey]  # type: ignore[index]
                for r in repo_list
                if repo_metrics.get((r, months[-1])) is not None and repo_metrics[r, months[-1]][mkey] is not None  # type: ignore[index]
            ]
            fv_f: list[float] = [v for v in fv if isinstance(v, (int, float))]
            tv_f: list[float] = [v for v in tv if isinstance(v, (int, float))]
            badge = _yoy_badge(
                sum(fv_f) / len(fv_f) if fv_f else None,
                sum(tv_f) / len(tv_f) if tv_f else None,
                months[0],
                months[-1],
                badge_id=f"badge-{chart_id}",
            )
        chart_cards.append(
            f'<div class="chart-card">'
            f'<div class="chart-card-header">'
            f"<h2>{metric['label']}</h2>"
            f'<label class="chart-trendline-label">'
            f'<input type="checkbox" id="trendline-{chart_id}" checked>Trendlines</label>'
            f"</div>"
            f"{badge}"
            f'<div class="chart-wrapper"><canvas id="{chart_id}"></canvas></div></div>'
        )
        scripts.append(_chart_js(chart_id, months, datasets, show_legend=False))

    # Repo summary table (last-month raw values)
    def _repo_commits(r: str) -> int:
        return int(last_month_raw.get(r, {}).get("commits", {}).get("total", -1))

    def _table_row(repo: str, dataset_idx: int | None = None) -> str:
        raw = last_month_raw.get(repo, {})
        commits = raw.get("commits", {}).get("total")
        per_contributor = raw.get("commits", {}).get("per_contributor", {})
        contributors = compute_contributors_human(per_contributor) if per_contributor else None
        mrs = raw.get("mrs", raw.get("mrs_merged", {})).get("total")
        lc = raw.get("lines_changed", {})
        lines = (lc.get("added", 0) + lc.get("removed", 0)) if lc else None
        meta = raw.get("meta", {})
        gl_instance = meta.get("gitlab_instance", "")
        gl_namespace = meta.get("namespace", "")
        gl_url = f"{gl_instance}/{gl_namespace}" if gl_instance and gl_namespace else ""
        if gl_url and not gl_url.lower().startswith(("http://", "https://")):
            gl_url = ""
        gl_link = (
            f' <a href="{gl_url}" target="_blank" rel="noopener" title="Open in GitLab"'
            f' style="color:var(--muted);font-size:0.75rem;vertical-align:middle;margin-left:4px;text-decoration:none">&#x2197;</a>'
            if gl_url
            else ""
        )
        link = f'<a href="productivity_{product}_{repo}.html">{repo}</a>{gl_link}'
        if dataset_idx is not None:
            cb_td = (
                f"<td style='width:28px;padding:8px 8px 8px 12px;text-align:center'>"
                f"<input type='checkbox' checked class='repo-toggle' data-dataset-idx='{dataset_idx}'"
                f" style='cursor:pointer;accent-color:var(--accent);width:13px;height:13px'>"
                f"</td>"
            )
        else:
            cb_td = "<td style='width:28px'></td>"
        return (
            f"<tr>{cb_td}<td>{link}</td>"
            f'<td class="num" data-val="{_sort_val(commits)}">{_fmt_num(commits)}</td>'
            f'<td class="num" data-val="{_sort_val(contributors)}">{_fmt_num(contributors)}</td>'
            f'<td class="num" data-val="{_sort_val(mrs)}">{_fmt_num(mrs)}</td>'
            f'<td class="num" data-val="{_sort_val(lines)}">{_fmt_num(lines)}</td>'
            f"</tr>"
        )

    month_label = f"({last_month})" if last_month else ""
    _th_cols = (
        f"<th>Repo</th>"
        f"<th class='num'>Commits {month_label}</th>"
        f"<th class='num'>Contributors {month_label}</th>"
        f"<th class='num'>MRs {month_label}</th>"
        f"<th class='num'>Lines changed {month_label}</th>"
    )
    _th_cols_sortable = (
        f"<th>Repo</th>"
        f"<th class='num sort-col' onclick='sortRepoTable(this,2)'>Commits {month_label}"
        f"<span class='sort-ind'> ▼</span></th>"
        f"<th class='num sort-col' onclick='sortRepoTable(this,3)'>Contributors {month_label}"
        f"<span class='sort-ind' style='color:var(--muted)'> ↕</span></th>"
        f"<th class='num sort-col' onclick='sortRepoTable(this,4)'>MRs {month_label}"
        f"<span class='sort-ind' style='color:var(--muted)'> ↕</span></th>"
        f"<th class='num sort-col' onclick='sortRepoTable(this,5)'>Lines changed {month_label}"
        f"<span class='sort-ind' style='color:var(--muted)'> ↕</span></th>"
    )
    thead = (
        f"<thead><tr>"
        f"<th style='width:28px;text-align:center;padding:8px 8px 8px 12px'>"
        f"<input type='checkbox' id='toggle-all-repos' checked"
        f" style='cursor:pointer;accent-color:var(--accent);width:13px;height:13px'></th>"
        f"{_th_cols_sortable}</tr></thead>"
    )
    thead_plain = f"<thead><tr><th style='width:28px'></th>{_th_cols}</tr></thead>"

    # Partition: active product / inactive product / support
    sorted_product = sorted(repo_list, key=_repo_commits, reverse=True)
    active_rows = [_table_row(r, repo_list.index(r)) for r in sorted_product if _repo_commits(r) > 0]
    inactive_repos = [r for r in sorted_product if _repo_commits(r) <= 0]
    inactive_rows = [_table_row(r, repo_list.index(r)) for r in inactive_repos]
    support_sorted = sorted(support_repos, key=_repo_commits, reverse=True)
    support_rows = [_table_row(r) for r in support_sorted]  # no dataset index — not in charts

    section_label = (
        f'<div style=\'margin-top:32px;margin-bottom:10px;font-family:"Barlow Condensed",sans-serif;'
        f"font-size:0.63rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;"
        f"color:var(--muted)'>Repositories — {last_month}</div>"
    )
    repo_table = (
        f"{section_label}"
        f"<table class='repo-table' style='margin-top:0'>{thead}<tbody>{''.join(active_rows)}</tbody></table>"
    )

    if inactive_rows:
        n = len(inactive_rows)
        lbl_show = f"Show {n} inactive repo{'s' if n != 1 else ''}"
        lbl_hide = "Hide inactive repos"
        repo_table += (
            f"<div style='margin-top:12px;text-align:center'>"
            f"<button id='btn-inactive-{product}' class='toggle-btn' onclick=\""
            f"var t=document.getElementById('inactive-repos-{product}');"
            f"var show=t.style.display==='none';"
            f"t.style.display=show?'':'none';"
            f"this.textContent=show?'{lbl_hide}':'{lbl_show}'\">"
            f"{lbl_show}"
            f"</button></div>"
            f"<div id='inactive-repos-{product}' style='display:none'>"
            f"<table class='repo-table'>{thead_plain}<tbody>{''.join(inactive_rows)}</tbody></table>"
            f"</div>"
        )

    if support_rows:
        n = len(support_rows)
        lbl_show = f"Show {n} support repo{'s' if n != 1 else ''}"
        lbl_hide = "Hide support repos"
        repo_table += (
            f"<div style='margin-top:12px;text-align:center'>"
            f"<button id='btn-support-{product}' class='toggle-btn' onclick=\""
            f"var t=document.getElementById('support-repos-{product}');"
            f"var show=t.style.display==='none';"
            f"t.style.display=show?'':'none';"
            f"this.textContent=show?'{lbl_hide}':'{lbl_show}'\">"
            f"{lbl_show}"
            f"</button></div>"
            f"<div id='support-repos-{product}' style='display:none'>"
            f"<table class='repo-table'>{thead_plain}<tbody>{''.join(support_rows)}</tbody></table>"
            f"</div>"
        )

    _toggle_script = (
        (
            """<script>
(function() {
  var masterCb = document.getElementById('toggle-all-repos');

  function updateMaster() {
    var all = document.querySelectorAll('.repo-toggle');
    var checked = document.querySelectorAll('.repo-toggle:checked');
    if (!masterCb) return;
    if (checked.length === 0) {
      masterCb.checked = false; masterCb.indeterminate = false;
    } else if (checked.length === all.length) {
      masterCb.checked = true; masterCb.indeterminate = false;
    } else {
      masterCb.indeterminate = true;
    }
  }

%%BADGES%%

  document.querySelectorAll('.repo-toggle').forEach(function(cb) {"""
        ).replace("%%BADGES%%", _JS_UPDATE_BADGES)
        + """
    cb.addEventListener('change', function() {
      var idx = parseInt(this.dataset.datasetIdx);
      var show = this.checked;
      Object.values(window._charts || {}).forEach(function(chart) {
        var ds = chart.data.datasets[idx];
        if (ds !== undefined) {
          chart.setDatasetVisibility(idx, show);
          var trendLabel = ds.label + ' trend';
          chart.data.datasets.forEach(function(d, i) {
            if (d.label === trendLabel) chart.setDatasetVisibility(i, show);
          });
          chart.update('none');
        }
      });
      updateMaster();
      updateBadges();
    });
  });

  if (masterCb) {
    masterCb.addEventListener('change', function() {
      var enabled = this.checked;
      document.querySelectorAll('.repo-toggle').forEach(function(cb) { cb.checked = enabled; });
      Object.values(window._charts || {}).forEach(function(chart) {
        chart.data.datasets.forEach(function(ds, i) { chart.setDatasetVisibility(i, enabled); });
        chart.update('none');
      });
      updateBadges();
    });
  }
})();
</script>"""
    )

    bc = _breadcrumb(("Overview", "productivity.html"), (display_name, None))
    body = bc
    body += f'<div class="charts-grid">{"".join(chart_cards)}</div>'
    body += "\n".join(scripts)
    _sort_script = """<script>
(function() {
  var _sortState = {col: 2, asc: false};
  window.sortRepoTable = function(th, colIdx) {
    var tbody = th.closest('table').querySelector('tbody');
    var rows = Array.from(tbody.querySelectorAll('tr'));
    if (_sortState.col === colIdx) {
      _sortState.asc = !_sortState.asc;
    } else {
      _sortState.col = colIdx;
      _sortState.asc = false;
    }
    rows.sort(function(a, b) {
      var av = (function(v) { var p = parseFloat(v); return isNaN(p) ? -1 : p; })(a.cells[colIdx].dataset.val);
      var bv = (function(v) { var p = parseFloat(v); return isNaN(p) ? -1 : p; })(b.cells[colIdx].dataset.val);
      return _sortState.asc ? av - bv : bv - av;
    });
    rows.forEach(function(r) { tbody.appendChild(r); });
    th.closest('table').querySelectorAll('th .sort-ind').forEach(function(s) {
      s.textContent = ' \u2195'; s.style.color = 'var(--muted)';
    });
    var activeInd = th.querySelector('.sort-ind');
    activeInd.textContent = _sortState.asc ? ' \u25b2' : ' \u25bc';
    activeInd.style.color = '';
  };
})();
</script>"""

    if active_rows or inactive_rows or support_rows:
        body += repo_table
        body += _toggle_script
        body += _sort_script

    chat_context = _build_prod_chat_context(
        "product",
        display_name=display_name,
        months=months,
        repos=repo_list,
        repo_metrics=repo_metrics,
        last_month_raw=last_month_raw,
    )
    return _html_page(
        display_name,
        body,
        month=f"{months[0]} → {months[-1]}" if len(months) >= 2 else (months[-1] if months else ""),
        chat_context=chat_context,
    )


def render_repo_page(
    repo: str,
    product: str,
    display_name: str,
    base_dir: Path,
    months: list[str],
) -> str:
    """Build productivity_{product}_{repo}.html — single repo + contributor bar chart."""
    product_dir = base_dir / "products" / product

    # Pre-load each month's data once — reused across all 4 metric charts and activity section.
    month_data: dict[str, dict[str, Any] | None] = {month: load_repo_data(product_dir, month, repo) for month in months}
    month_metrics: dict[str, dict[str, float | None] | None] = {
        month: (compute_per_engineer_metrics(d) if d is not None else None) for month, d in month_data.items()
    }

    # ai_assisted_commit_rate is git-based, unaffected by cursor telemetry bugs.
    month_health: dict[str, dict[str, Any] | None] = {
        month: load_health_for_month(product_dir, month, repo) for month in months
    }
    scripts: list[str] = []
    chart_cards: list[str] = []

    # ── Top row: 2 primary metrics ────────────────────────────────────────────
    for metric in OVERVIEW_METRICS:
        mkey = metric["key"]
        chart_id = f"chart-{mkey.replace('_', '-')}"

        series: list[float | None] = [
            month_metrics[month][mkey] if month_metrics[month] is not None else None  # type: ignore[index]
            for month in months
        ]

        datasets = [_make_dataset(metric["label"], series, METRIC_COLOUR)]
        non_none = [(months[i], v) for i, v in enumerate(series) if v is not None]
        badge = (
            _yoy_badge(
                non_none[0][1],
                non_none[-1][1],
                non_none[0][0],
                non_none[-1][0],
                badge_id=f"badge-{chart_id}",
            )
            if len(non_none) >= 2
            else ""
        )
        chart_cards.append(
            f'<div class="chart-card">'
            f'<div class="chart-card-header">'
            f"<h2>{metric['label']}</h2>"
            f'<label class="chart-trendline-label">'
            f'<input type="checkbox" id="trendline-{chart_id}" checked>Trendlines</label>'
            f"</div>"
            f"{badge}"
            f'<div class="chart-wrapper"><canvas id="{chart_id}"></canvas></div></div>'
        )
        scripts.append(_chart_js(chart_id, months, datasets, show_legend=False))

    # ── Bottom row: 3 detail metrics + AI adoption placeholder ────────────────
    detail_cards: list[str] = []
    detail_scripts: list[str] = []

    for metric in REPO_DETAIL_METRICS:
        mkey = metric["key"]
        chart_id = f"chart-{mkey.replace('_', '-')}"

        series_d: list[float | None] = [
            month_metrics[month][mkey] if month_metrics[month] is not None else None  # type: ignore[index]
            for month in months
        ]

        datasets_d = [_make_dataset(metric["label"], series_d, METRIC_COLOUR)]
        non_none_d = [(months[i], v) for i, v in enumerate(series_d) if v is not None]
        badge_d = (
            _yoy_badge(
                non_none_d[0][1],
                non_none_d[-1][1],
                non_none_d[0][0],
                non_none_d[-1][0],
                badge_id=f"badge-{chart_id}",
            )
            if len(non_none_d) >= 2
            else ""
        )
        detail_cards.append(
            f'<div class="chart-card">'
            f'<div class="chart-card-header">'
            f"<h2>{metric['label']}</h2>"
            f'<label class="chart-trendline-label">'
            f'<input type="checkbox" id="trendline-{chart_id}" checked>Trendlines</label>'
            f"</div>"
            f"{badge_d}"
            f'<div class="chart-wrapper" style="height:200px"><canvas id="{chart_id}"></canvas></div></div>'
        )
        detail_scripts.append(_chart_js(chart_id, months, datasets_d, show_legend=False))

    # Rework rate: % of commits classified as bugfix (reverts/fixes)
    rework_series: list[float | None] = []
    for m in months:
        md = month_data[m]
        if md is None:
            rework_series.append(None)
            continue
        total = md.get("commits", {}).get("total", 0)
        cc = md.get("commit_classification", {})
        bugfix = cc.get("bugfix", 0)
        rework_series.append(round(bugfix / total * 100, 1) if total > 0 else None)
    has_rework = any(v is not None for v in rework_series)
    if has_rework:
        rw_chart_id = "chart-rework-rate"
        rw_ds = _make_dataset("Rework Rate (%)", rework_series, METRIC_COLOUR)
        rw_badge = ""
        rw_non_none = [(months[i], v) for i, v in enumerate(rework_series) if v is not None]
        if len(rw_non_none) >= 2:
            rw_badge = _yoy_badge(
                rw_non_none[0][1],
                rw_non_none[-1][1],
                rw_non_none[0][0],
                rw_non_none[-1][0],
                badge_id=f"badge-{rw_chart_id}",
                lower_is_better=True,
            )
        detail_cards.append(
            f'<div class="chart-card">'
            f'<div class="chart-card-header">'
            f'<h2>Rework Rate&nbsp;<span class="info-tip" tabindex="0">&#x24D8;'
            f'<span class="info-tip-body">% of commits classified as bugfix or fix — '
            f"a proxy for rework. Higher means more time spent fixing rather than building.</span></span></h2>"
            f"</div>"
            f"{rw_badge}"
            f'<div class="chart-wrapper" style="height:200px"><canvas id="{rw_chart_id}"></canvas></div></div>'
        )
        detail_scripts.append(_chart_js(rw_chart_id, months, [rw_ds], show_legend=False, y_percent=True))
    else:
        detail_cards.append(
            '<div class="chart-card">'
            '<div class="chart-card-header"><h2>Rework Rate</h2></div>'
            '<div style="height:200px;display:flex;align-items:center;justify-content:center;'
            "color:var(--muted);font-family:'Barlow Condensed',sans-serif;"
            'font-size:0.85rem;letter-spacing:.06em;text-transform:uppercase">No data</div>'
            "</div>"
        )

    # ── AI Readiness & Adoption trend charts ─────────────────────────────────
    _rai_metrics: list[tuple[str, str, str, str]] = [
        (
            "ai_adoption",
            "ai_assisted_commit_rate",
            "AI Adoption (%)",
            "% of commits in this repo where an AI tool (Cursor, Copilot, Claude) contributed code, "
            "based on the <strong>Co-Authored-By</strong> git trailer or a co-author footer. "
            "Undercounts real adoption — only captures commits where the engineer explicitly added "
            "the co-author trailer.",
        ),
        (
            "ai_readiness",
            "ticket_reference_rate",
            "Jira Ref Rate (%)",
            "% of non-merge commits whose subject line contains a Jira ticket reference "
            "(e.g. PROJ-123). Measures how consistently the team links code changes to "
            "planned work. Calculated from git log over the month.",
        ),
        (
            "ai_readiness",
            "feature_test_coupling",
            "Feature-Test Coupling (%)",
            "% of commits classified as <strong>feature</strong> work that also touch a test file "
            "in the same commit. Measures whether new features are delivered with tests. "
            "A file is counted as a test if its path contains 'test', 'spec', or '__tests__'.",
        ),
        (
            "ai_readiness",
            "test_to_code_ratio",
            "Test-to-Code Ratio",
            "Ratio of test-file changes to production-file changes across all commits in the month. "
            "A value of 1.0 means equal churn in test and production files; below 0.5 suggests "
            "tests are being neglected relative to production code.",
        ),
        (
            "ai_readiness",
            "commit_body_rate",
            "Commit Body Rate (%)",
            "% of non-merge commits that include a non-empty body paragraph (text after the subject "
            "line). Measures commit message quality — a body should explain <em>why</em> a change "
            "was made, not just what. Calculated from git log over the month.",
        ),
    ]
    rai_cards: list[str] = []
    rai_scripts: list[str] = []
    for section_key, metric_key, label, tooltip in _rai_metrics:
        series_rai: list[float | None] = []
        for m in months:
            raw = (month_health[m] or {}).get(section_key, {}).get(metric_key)
            # ai_readiness values are {"value": x, "source": "..."}; ai_adoption are plain floats
            if isinstance(raw, dict):
                raw = raw.get("value")
            series_rai.append(raw)
        if not any(v is not None for v in series_rai):
            continue
        cid = f"chart-rai-{metric_key.replace('_', '-')}"
        use_percent = metric_key != "test_to_code_ratio"
        ds_rai = _make_dataset(label, series_rai, METRIC_COLOUR)
        tip_html = (
            f"&nbsp;<span class='info-tip' tabindex='0'>&#x24D8;<span class='info-tip-body'>{tooltip}</span></span>"
        )
        rai_cards.append(
            f'<div class="chart-card">'
            f'<div class="chart-card-header"><h2>{_esc(label)}{tip_html}</h2></div>'
            f'<div class="chart-wrapper" style="height:180px"><canvas id="{cid}"></canvas></div></div>'
        )
        rai_scripts.append(_chart_js(cid, months, [ds_rai], show_legend=False, y_percent=use_percent))
    rai_section = ""
    if rai_cards:
        rai_section = (
            '<h2 style=\'font-family:"Barlow Condensed",sans-serif;font-size:0.88rem;font-weight:700;'
            "text-transform:uppercase;letter-spacing:.1em;color:var(--muted);"
            "margin-top:32px;margin-bottom:12px'>AI Readiness &amp; Adoption Trends</h2>"
            f'<div class="charts-grid">{"".join(rai_cards)}</div>' + "\n".join(rai_scripts)
        )

    latest_data = next((month_data[m] for m in reversed(months) if month_data[m] is not None), None)

    bc = _breadcrumb(
        ("Overview", "productivity.html"),
        (display_name, f"productivity_{product}.html"),
        (repo, None),
    )
    body = bc
    body += f'<div class="charts-grid">{"".join(chart_cards)}</div>'
    body += "\n".join(scripts)
    body += (
        f'<div class="charts-grid" style="grid-template-columns:repeat(4,minmax(0,1fr));margin-top:20px">'
        f"{''.join(detail_cards)}</div>"
    )
    body += "\n".join(detail_scripts)
    body += rai_section

    # ── Month-by-month detail: activity breakdown + AI health ─────────────────
    months_with_detail = [m for m in months if month_data[m] is not None or month_health[m] is not None]

    if months_with_detail:
        last_idx = len(months_with_detail) - 1
        default_month = months_with_detail[last_idx]
        months_json = json.dumps(months_with_detail)

        body += (
            f"<div class='month-nav'>"
            f"<span class='month-nav-label'>Month</span>"
            f"<input type='range' class='month-slider' id='month-slider'"
            f" min='0' max='{last_idx}' value='{last_idx}' step='1'>"
            f"<span class='month-nav-value' id='month-slider-label'>{_format_month(default_month)}</span>"
            f"</div>"
        )

        # One hidden section per month
        for m in months_with_detail:
            data_m = month_data[m]
            health_m = month_health[m]
            section_inner = ""
            if data_m:
                section_inner += _render_activity_section(data_m)
            if health_m:
                total_commits_m = data_m.get("commits", {}).get("total", 0) if data_m else 0
                section_inner += _render_health_section(health_m, total_commits=total_commits_m)
            if not section_inner:
                section_inner = (
                    "<p style='color:var(--muted);padding:20px 0;"
                    'font-family:"Barlow Condensed",sans-serif;font-size:0.85rem\'>'
                    "No detailed data available for this month.</p>"
                )
            active_cls = " active" if m == default_month else ""
            body += f"<div id='month-section-{m}' class='month-section{active_cls}'>{section_inner}</div>"

        body += f"""<script>
(function() {{
  var months = {months_json};
  var slider = document.getElementById('month-slider');
  var label = document.getElementById('month-slider-label');
  var monthLabels = {json.dumps([_format_month(m) for m in months_with_detail])};
  function selectIdx(i) {{
    document.querySelectorAll('.month-section').forEach(function(el) {{ el.classList.remove('active'); }});
    var sec = document.getElementById('month-section-' + months[i]);
    if (sec) sec.classList.add('active');
    label.textContent = monthLabels[i];
  }}
  slider.addEventListener('input', function() {{ selectIdx(parseInt(slider.value, 10)); }});
}})();
</script>"""

    chat_context = _build_prod_chat_context(
        "repo",
        repo=repo,
        display_name=display_name,
        months=months,
        month_metrics=month_metrics,
        latest_data=latest_data,
    )
    return _html_page(
        f"{display_name} / {repo}",
        body,
        month=f"{months[0]} → {months[-1]}" if len(months) >= 2 else (months[-1] if months else ""),
        chat_context=chat_context,
    )


def _is_support_repo(product_dir: Path, repo: str, months: list[str]) -> bool:
    """Return True if this repo carries meta.category == 'support' in its most recent available month."""
    for month in reversed(months):
        data = load_repo_data(product_dir, month, repo)
        if data is not None:
            return bool(data.get("meta", {}).get("category") == "support")
    return False


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate Productivity Dashboard HTML reports")
    parser.add_argument("--output-dir", default="reports", help="Output directory (default: reports/)")
    parser.add_argument(
        "--products",
        nargs="+",
        help="Limit to specific products (default: all from repos.yaml)",
    )
    parser.add_argument(
        "--base-dir",
        default=None,
        help="Directory containing repos.yaml and products/ (default: repo root). "
        "Use --base-dir examples to render the bundled synthetic dataset.",
    )
    args = parser.parse_args(argv)

    base_dir = Path(args.base_dir).resolve() if args.base_dir else BASE_DIR

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load product list and display names
    repos_config = load_repos_config(base_dir)
    all_products = sorted(repos_config.keys())
    products = args.products if args.products else all_products
    display_names = {p: repos_config[p].get("display_name", p) for p in all_products}

    # Discover all months across selected products (root only, for main charts)
    all_months_set: set[str] = set()
    all_months_full_set: set[str] = set()
    for product in products:
        product_dir = base_dir / "products" / product
        all_months_set.update(discover_months(product_dir))
        all_months_full_set.update(discover_months_with_archive(product_dir))
    months = sorted(all_months_set)[-13:]
    all_months_full = sorted(all_months_full_set)

    if not months:
        print("No data found in products/ — nothing to generate.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(months)} months: {months[0]} → {months[-1]}")
    print(f"Full history (incl. archive): {len(all_months_full)} months")
    print(f"Products: {', '.join(products)}")

    # ── Overview page ──────────────────────────────────────────────────────────
    html = render_overview_page(products, display_names, base_dir, months, all_months_full=all_months_full)
    out = output_dir / "productivity.html"
    out.write_text(html, encoding="utf-8")
    print(f"  Written: {out}")

    # ── Per-product and per-repo pages ─────────────────────────────────────────
    for product in products:
        product_dir = base_dir / "products" / product
        product_months = discover_months(product_dir)[-13:]
        if not product_months:
            continue
        display_name = display_names.get(product, product)

        html = render_product_page(product, display_name, base_dir, product_months)
        out = output_dir / f"productivity_{product}.html"
        out.write_text(html, encoding="utf-8")
        print(f"  Written: {out}")

        # Per-repo pages — exclude support repos (same policy as EPI dashboard)
        all_repos: set[str] = set()
        for month in product_months:
            all_repos.update(discover_repos(product_dir, month))
        all_repos = {r for r in all_repos if not _is_support_repo(product_dir, r, product_months)}

        for repo in sorted(all_repos):
            html = render_repo_page(repo, product, display_name, base_dir, product_months)
            out = output_dir / f"productivity_{product}_{repo}.html"
            out.write_text(html, encoding="utf-8")

        print(f"  Written: {len(all_repos)} repo pages for {display_name}")

    print("Done.")
