# Contributing to EPI Data

## License & Contributor Agreement

This project is licensed under the [MIT License](LICENSE), copyright Michael
Metternich. By submitting a contribution (pull/merge request, patch, or other
content) to this project, you agree that:

1. Your contribution is licensed to the project under the same MIT License, and
2. You grant Michael Metternich, as maintainer, a perpetual, worldwide,
   non-exclusive license to use, modify, relicense, and sublicense your
   contribution as part of the project — including in versions distributed
   under different license terms in the future.

This keeps the project's licensing simple and consistent for everyone who
depends on it, while ensuring contributors always retain the rights to their
own original work outside this project. If your employer requires a signed
CLA before you can contribute, reach out before opening a merge request.

## Getting Started

```bash
git clone <repo-url> && cd epi-data
make setup          # Creates .venv, installs all dependencies
source .venv/bin/activate
make check          # Verify everything works: lint + typecheck + test
```

## Development Loop

```bash
make format         # Auto-fix formatting and import order
make check          # Lint + typecheck + test (same as CI)
git add <files>
git commit
```

## Branch Naming

`<your-name>/<short-description>` — e.g., `michael/add-jira-metric`

No gitflow, no prefixes. Keep it simple.

## Code Style

All enforced by CI — you don't need to memorise rules:

- **120-char line limit** (ruff)
- **Double quotes** (ruff format)
- **Sorted imports** (ruff isort)
- **Type checking** (mypy, gentle config — no annotation requirements, but function bodies are checked)
- Run `make format` to auto-fix most issues

## Merge Request Expectations

- CI must be green (lint + typecheck + test)
- Add tests for new logic
- Keep changes focused — one concern per MR
- No new runtime dependencies without discussion (PyYAML is the only one by design)

## Adding a New Metric

The most common contribution. Follow these steps:

### 1. Collection

In `collect-git-metrics.py` (or `collect-repo-health.py` for health metrics):
- Add the metric collection logic
- Include it in the output JSON structure
- If it needs manual input, add it to `manual-input-template.yaml`

### 2. Scoring Bands

In `score-epi.py`:
- Add the metric to `METRIC_BANDS` with direction (`higher` or `lower` is better) and band thresholds
- Add it to the appropriate component in `COMPONENTS` with a weight
- The scoring engine handles interpolation automatically

### 3. Display

In `generate-html-report.py`:
- The metric will appear automatically in the component breakdown
- For custom display (suffix, formatting), update the format helpers

### 4. Tests

- Add band scoring tests in `tests/test_scoring.py`
- Add collection tests if applicable in `tests/test_collect_health.py`
- Integration tests in `tests/test_integration.py` use fixture data in `tests/fixtures/`

### 5. Verify

```bash
make check          # All 132+ tests should pass
```

## Pre-commit Hooks (Optional)

If you want auto-formatting on every commit:

```bash
pre-commit install
```

This runs ruff check + format before each commit. CI remains the enforcement point — hooks are opt-in.

## Commands Reference

| Command | What It Does |
|---------|-------------|
| `make setup` | Create venv, install all deps |
| `make test` | Run pytest |
| `make test-cov` | Run tests with coverage |
| `make lint` | Check formatting + linting |
| `make format` | Auto-fix formatting |
| `make typecheck` | Run mypy |
| `make check` | All checks (lint + typecheck + test) |
| `make clean` | Remove caches and venv |
