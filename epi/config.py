from __future__ import annotations

import sys
from pathlib import Path

import yaml


def load_yaml_file(path: Path) -> dict:
    """Load a YAML file and return its contents, or an empty dict if missing."""
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def load_repos_config(base_dir: str | Path) -> dict:
    """Load the central repos.yaml and return the products mapping."""
    repos_file = Path(base_dir) / "repos.yaml"
    if not repos_file.exists():
        print(f"Error: repos.yaml not found at {repos_file}", file=sys.stderr)
        sys.exit(1)
    with open(repos_file) as f:
        config = yaml.safe_load(f) or {}
    return config.get("products", {})  # type: ignore[no-any-return]
