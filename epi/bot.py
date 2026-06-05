from __future__ import annotations

import re

# Exact-match service/bot account names to exclude from contributor counts.
# Customise this set for your organisation's CI / automation accounts.
BOT_EXACT = {
    "jenkins",
    "devops",
    "ci.cd",
    "ghost",
    "prodops",
    "hammertime",
    "root",
    "ops",
    "gitlab.jira",
    "--global",
}
BOT_PATTERNS = [
    re.compile(r"^project_\d+_bot_"),
    re.compile(r"^group_\d+_bot_"),
    re.compile(r"\d+\+dependabot\[bot\]"),
    re.compile(r"^pulsar\.service$"),
]


def is_bot(name: str) -> bool:
    """Return True if *name* looks like a bot / service account."""
    if name.lower() in BOT_EXACT:
        return True
    return any(p.search(name) for p in BOT_PATTERNS)
