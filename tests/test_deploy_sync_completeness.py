"""Verify hermes_sync_config() covers the files that MUST be synced to prod.

This test catches regressions where a file that should be synced to prod
(e.g. a policy/config file tracked in git) is inadvertently removed from
the deploy sync list.

Run: python -m pytest tests/test_deploy_sync_completeness.py -v
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

HERMES_REPO = Path(os.environ.get("HERMES_REPO", str(Path.home() / ".hermes")))
DEPLOY_SCRIPT = HERMES_REPO / "scripts" / "deploy.sh"


def _parse_policy_loop_files(script_content: str) -> list[str]:
    """Extract files listed in the hermes_sync_config() policy files loop.

    The loop looks like:
      for policy_file in SOUL.md AGENTS.md TOOLS.md HEARTBEAT.md prefill.json agent-orchestrator.yaml; do
    Returns the list: ['SOUL.md', 'AGENTS.md', 'TOOLS.md', 'HEARTBEAT.md', 'prefill.json', 'agent-orchestrator.yaml']
    """
    match = re.search(
        r"# Policy files.*?\n\s+for policy_file in (.+?)\s*;?\s*do",
        script_content,
        re.DOTALL,
    )
    if not match:
        pytest.fail("Policy files loop not found in deploy.sh")
    return [f for f in match.group(1).split() if f]


def _parse_sync_dirs(script_content: str) -> list[str]:
    """Extract directories synced via rsync --delete in hermes_sync_config().

    Handles multi-line rsync commands with --exclude flags between
    the rsync invocation and the source/dest paths.
    """
    match = re.search(
        r"^hermes_sync_config\(\)\s*\{(.+?)\n^}\s*$",
        script_content,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        pytest.fail("hermes_sync_config() function not found in deploy.sh")
    func_body = match.group(1)
    synced_dirs: list[str] = []
    # Normalize whitespace: collapse backslash-continued lines into single lines
    normalized = re.sub(r'\\\n\s+', ' ', func_body)
    for m in re.finditer(
        r'rsync\s+[^\n]*"\$HERMES_STAGING_HOME/([^"/]+)/"\s+"\$HERMES_PROD_HOME/\1/"',
        normalized,
    ):
        synced_dirs.append(m.group(1))
    return synced_dirs


# ─── Curated must-sync list ────────────────────────────────────────────────────
# These are files/dirs that hermes_sync_config() MUST sync to prod.
# If any of these are missing from the sync list, prod will run stale code/config.

MUST_SYNC_FILES: list[str] = [
    "SOUL.md",
    "AGENTS.md",
    "TOOLS.md",
    "HEARTBEAT.md",
    "prefill.json",
    "agent-orchestrator.yaml",
]

MUST_SYNC_DIRS: list[str] = [
    "skills",
]


def test_policy_files_loop_contains_must_sync():
    """Every must-sync policy file must appear in the policy files loop."""
    script_content = DEPLOY_SCRIPT.read_text()
    policy_files = _parse_policy_loop_files(script_content)

    missing = [f for f in MUST_SYNC_FILES if f not in policy_files]
    assert not missing, (
        f"Policy files missing from hermes_sync_config() loop: {missing}\n"
        "These files are tracked in git and MUST be synced to prod."
    )


def test_sync_dirs_contain_must_sync():
    """Every must-sync directory must be synced via rsync --delete."""
    script_content = DEPLOY_SCRIPT.read_text()
    synced_dirs = _parse_sync_dirs(script_content)

    missing = [d for d in MUST_SYNC_DIRS if d not in synced_dirs]
    assert not missing, (
        f"Directories missing from hermes_sync_config() rsync: {missing}\n"
        "These dirs are tracked in git and MUST be synced to prod."
    )


def test_config_yaml_is_synced():
    """config.yaml must be synced (with prod-native override patching)."""
    script_content = DEPLOY_SCRIPT.read_text()
    # config.yaml is synced via explicit cp, not the loop
    assert "config.yaml" in script_content and "cp" in script_content, (
        "config.yaml must be synced via hermes_sync_config()"
    )
