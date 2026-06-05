# CLAUDE.md

Guidance for Claude Code (and other AI coding tools) working in this repository.

## What this is

The **Engineering Productivity Index (EPI)** — a Python pipeline that collects
DORA-aligned git/GitLab metrics per repository, scores them, and renders a static
HTML productivity dashboard. See [README.md](README.md) for the full overview.

## Layout

- `epi/` — the installable Python package (`pip install -e .`)
  - `collectors/` — `collect-git-metrics`, `collect-repo-health`
  - `importers/` — CSV/uptime → manual YAML
  - `reports/productivity.py` — the dashboard generator
  - `scoring.py` — band-interpolation scoring engine
  - `api/` — GitLab / Cursor / LiteLLM clients
  - `config.py`, `bot.py` — config loading and bot/service-account detection
- `tests/` — pytest suite; `tests/fixtures/` holds synthetic data (alpha/beta/gamma)
- `examples/` — a runnable synthetic dataset (`--base-dir examples`)
- `*.example` files — copy to the unsuffixed name and fill in your own values

## Conventions

- **Python 3.11**, runtime deps limited to **PyYAML** + **Jinja2**.
- Run `make check` (ruff lint + format check, mypy, pytest) before every commit —
  this is exactly what CI runs.
- No secrets in the repo. Tokens and instance URLs come from environment variables
  (`.env.example`) and `repos.yaml` (`repos.yaml.example`). The dashboard HTML uses
  the `__LLM_API_KEY__` / `__LLM_API_ENDPOINT__` placeholders, substituted at deploy.
- Products, repos, and hosts are **configuration**, not code — keep them out of `epi/`.

## Common commands

```bash
make setup        # venv + install (one-time)
make check        # lint + typecheck + test
make format       # auto-fix formatting and imports
generate-productivity-report --base-dir examples --output-dir reports
```
