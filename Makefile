.PHONY: setup test test-cov lint format typecheck check clean help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

setup: ## Create venv and install all dependencies
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"
	@echo "\n  Activate with: source .venv/bin/activate"

test: ## Run tests with pytest
	python3 -m pytest tests/ -v

test-cov: ## Run tests with coverage report
	python3 -m pytest tests/ -v --cov --cov-report=term-missing

lint: ## Check code style (ruff lint + ruff format check)
	ruff check .
	ruff format --check .

format: ## Auto-fix formatting and import order
	ruff check --fix .
	ruff format .

typecheck: ## Run mypy type checking
	mypy epi/ --no-error-summary

check: lint typecheck test ## Run all checks (lint + typecheck + test)

clean: ## Remove build artifacts and caches
	rm -rf .venv/ .pytest_cache/ .mypy_cache/ .ruff_cache/ .coverage htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
