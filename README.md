# EPI Data Pipeline

Engineering Productivity Index — DORA-aligned metrics collection, scoring, and reporting for board-level engineering visibility.

## Quick Start

```bash
make setup          # Create .venv, install all dependencies (one-time)
source .venv/bin/activate
make check          # Lint + typecheck + all tests (same as CI)
```

## Try it with the example dataset

The repo ships with a small **synthetic** dataset (products `alpha` / `beta` / `gamma`)
so you can render the dashboard without wiring up any real data source:

```bash
make setup && source .venv/bin/activate
generate-productivity-report --base-dir examples --output-dir reports
open reports/productivity.html
```

To run against your own data, copy `repos.yaml.example` → `repos.yaml`, populate
`products/`, and set the env vars from `.env.example` (see [Configuration](#configuration)).

## Productivity Dashboard

A standalone exec-facing dashboard designed for board meetings and monthly director reviews. It combines four per-engineer productivity trend charts with DORA-scored engineering metrics, a YoY deep-dive analysis, and AI tool spend tracking.

```bash
generate-productivity-report

# With options
generate-productivity-report --output-dir public/
generate-productivity-report --products app_a app_b

# Output — three page types
reports/productivity.html                          # Overview: all products, 13 months
reports/productivity_{product}.html                # Product: one line per repo
reports/productivity_{product}_{repo}.html         # Repo: full history + activity breakdown
```

**What it shows:**

| Metric | Definition |
|--------|-----------|
| Lines Changed per Engineer | (added + removed lines) / headcount |
| MRs per Engineer | MRs merged / headcount |
| Commits per Engineer | non-bot commits / headcount |
| Feature Commits per Engineer | feature-classified commits / headcount |

**Features:**
- 13-month rolling window — always shows true year-over-year comparison
- Overview:
  - Product filter checkboxes toggle chart datasets live; YoY badge per chart
  - Engineering metrics section — DORA-scored metrics (deployment frequency, lead time, MTTR, etc.) with band indicators and month-over-month delta per product
  - YoY deep-dive section — delta tables, trendlines, and residual analysis across the full archive history (not limited to 13 months)
  - AI spend stacked bar chart — LiteLLM, Cursor, and OpenAI cost by month from `ai-spend.yaml`; dual-axis active-users overlay
- Product page: sortable repo table (commits / contributors / MRs / lines); support repos collapsed
- Repo page: per-metric line charts + commit classification breakdown + top contributors bar chart + AI Readiness / AI Adoption sections
- **AI chat panel** on every page — with page-specific context (metric series, repo list, classification data) baked into the system prompt
- Archive data support — historical JSON files in `products/<product>/archive/<year>/` are included in YoY analysis automatically

## Entry Points

| Command | Module | Purpose |
|---------|--------|---------|
| `collect-git-metrics` | `epi.collectors.git_metrics` | Collects 9 git/GitLab metrics per repo → `products/<product>/YYYY-MM_<repo>.json` |
| `collect-repo-health` | `epi.collectors.repo_health` | Collects AI Readiness + AI Adoption metrics → `products/<product>/health-YYYY-MM_<repo>.json` |
| `score-epi` | `epi.scoring` | Scoring engine — reads metrics JSON + manual YAML, applies band interpolation |
| `generate-productivity-report` | `epi.reports.productivity` | Productivity dashboard — per-engineer trends, drilldowns, chat |
| `import-incidents-csv` | `epi.importers.incidents` | Converts incident CSV exports into manual YAML input files |
| `import-uptime` | `epi.importers.uptime` | Converts uptime monitoring exports into manual YAML health fields |

All commands are installed by `pip install -e .` (run via `make setup`).

## Data Flow

```
repos.yaml ──→ collect-git-metrics ──→ products/<product>/YYYY-MM_<repo>.json ──────┐
repos.yaml ──→ collect-repo-health ──→ products/<product>/health-YYYY-MM_<repo>.json ┤
import-incidents-csv / import-uptime ──→ products/<product>/manual-YYYY-MM.yaml ────┤
                                                                                      ▼
                                                                                score-epi
                                                                                      │
ai-spend.yaml (manually maintained) ─────────────────────────────────────────────── ┤
                                                                                      ▼
                                                                 generate-productivity-report
                                                                                      │
                                                                                      ▼
                                                                 reports/productivity.html
                                                                 reports/productivity_*.html
```

## Directory Structure

```
engineering-productivity-index/
├── repos.yaml                         # All products + repos config (single source of truth)
├── manual-input-template.yaml         # Template for non-automatable EPI metrics
├── manual-health-template.yaml        # Template for AI readiness/adoption overrides
├── SCORING_DESIGN.md                  # Weight and threshold rationale
├── epi/                               # Python package (pip install -e .)
│   ├── bot.py                         # is_bot(), BOT_EXACT, BOT_PATTERNS
│   ├── config.py                      # load_repos_config(), load_yaml_file()
│   ├── scoring.py                     # EPI scoring engine
│   ├── api/
│   │   ├── cursor.py                  # Cursor analytics API client
│   │   ├── litellm.py                 # LiteLLM spend API client
│   │   └── gitlab.py                  # GitLab REST helpers
│   ├── collectors/
│   │   ├── git_metrics.py             # collect-git-metrics logic
│   │   └── repo_health.py             # collect-repo-health logic
│   ├── importers/
│   │   ├── incidents.py               # import-incidents-csv logic
│   │   └── uptime.py                  # import-uptime logic
│   └── reports/
│       └── productivity.py            # generate-productivity-report logic
├── products/
│   └── <product>/
│       ├── YYYY-MM_<repo>.json        # Git metrics (auto — scheduled CI)
│       ├── health-YYYY-MM_<repo>.json # AI readiness/adoption (auto — scheduled CI)
│       └── manual-YYYY-MM.yaml        # Deployments, incidents, MTTR (manager-provided)
└── reports/                           # Git-ignored; regenerated from products/
    ├── productivity.html
    ├── productivity_<product>.html
    └── productivity_<product>_<repo>.html
```

## Configuration

### repos.yaml

Single source of truth for all products and their repositories. Copy
[`repos.yaml.example`](repos.yaml.example) to `repos.yaml` and fill in your own products,
hosts, and repos:

```yaml
products:
  app_a:
    display_name: "App A"
    gitlab_instance: https://githost.example.com
    token_env: GITLAB_TOKEN          # env var holding the API token
    repos:
      - name: service-one
        project_id: "123"
        path: /path/to/local/clone
        branch: main
        ticket_prefix: APPA
```

Tokens and instance URLs are read from environment variables — see
[`.env.example`](.env.example) for the full list. Never commit real tokens.

## Development

```bash
make setup        # Create .venv, install all deps including epi package (one-time)
make check        # lint + typecheck + test (run before every commit)
make test         # pytest only
make test-cov     # tests with coverage report
make format       # auto-fix formatting and import order
make typecheck    # mypy only
make clean        # remove caches and venv
```

**Python 3.11** target. Runtime dependencies: **PyYAML** and **Jinja2**. Dev tools (ruff, mypy, pytest) installed via `make setup`.

Run a single test file or test:
```bash
python3 -m pytest tests/test_scoring.py -v
python3 -m pytest tests/test_productivity_report.py::TestChatIntegration -v
```

## CI

- **GitHub Actions** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)): runs
  `make check` (ruff lint + mypy + pytest) on every push and pull request.
- **Metrics collection** is intended to run on a schedule in your own infrastructure
  (e.g. a cron job or CI runner) that calls `collect-git-metrics` + `collect-repo-health`
  across your products and commits the JSON output to `products/`. That pipeline is
  environment-specific and is not included here.
- **Chat panel API key**: the generated HTML contains the literal placeholder
  `__LLM_API_KEY__` (and `__LLM_API_ENDPOINT__` for the endpoint). If you deploy the
  reports and want the in-page chat to work, substitute these at deploy time with your
  own OpenAI-compatible LLM proxy endpoint and key — do not bake real secrets into the
  committed HTML.

## Metrics Reference

### Git Metrics (`collect-git-metrics`)

| Metric | What It Measures |
|--------|-----------------|
| Commits | Total + per contributor |
| MRs Merged | Via GitLab API |
| Unique Tickets | Jira refs in commit messages |
| Lines Changed | Added / removed |
| Focus Score | % commits changing < 50 lines |
| Commit Classification | feature / bugfix / test / tooling / maintenance / docs |
| Rework Rate | bugfix / (feature + bugfix) |
| Bus Factor | Min contributors covering 80% of commits (trailing 6mo) |
| Knowledge Distribution | % modules touched by >1 contributor (trailing 6mo) |

### AI Health Metrics (`collect-repo-health`)

**AI Readiness** — code hygiene signals that indicate a repo is AI-tool-friendly:

| Metric | What It Measures |
|--------|-----------------|
| Ticket Reference Rate | % commits linking to a Jira ticket |
| Feature-Test Coupling | % feature commits paired with a test commit |
| Test-to-Code Ratio | Test files / source files |
| Commit Message Quality | % messages with a meaningful subject line |
| README / CLAUDE.md / CI / Skills | File existence checks |

**AI Adoption** — signals of actual AI tool usage:

| Metric | What It Measures |
|--------|-----------------|
| AI-Assisted Commits | % commits with Claude/Cursor co-authorship markers |
| Cursor Tab / Composer | Line attribution from Cursor analytics API |
| Co-authored MRs | MRs with AI co-author labels in GitLab |

### EPI Scoring (`score-epi`)

Each metric scores 0–100 via band interpolation. No composite score — each metric is reported individually with its value, band, and month-over-month comparison. The 12 metrics cover velocity (deployment frequency, MRs per engineer, features shipped, commit intentionality), quality (change failure rate, post-release defect rate, rework rate), efficiency (lead time, cycle delivery accuracy), and health (MTTR, bus factor, knowledge distribution).

Band thresholds: **Elite** 90–100 · **Happy** 70–89 · **Acceptable** 50–69 · **Concerning** 0–49

See [SCORING_DESIGN.md](SCORING_DESIGN.md) for threshold rationale and scoring mechanics.
