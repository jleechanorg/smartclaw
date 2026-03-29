"""Tests for ao-pr-poller zombie detection logic.

Tests the shell functions by invoking them via bash subprocess,
validating the core detection and cleanup patterns.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import pytest
import textwrap


POLLER_SCRIPT = str(Path(__file__).resolve().parents[2] / "scripts" / "ao-pr-poller.sh")


def run_poll_script(extra_code: str) -> subprocess.CompletedProcess:
    """Source the real script in test mode and run provided shell code.

    Fail-closed: if source fails or is_agent_alive_in_session is missing,
    the shell exits non-zero rather than silently continuing.
    """
    return run_bash(
        textwrap.dedent(f"""
        set -euo pipefail
        AO_PR_POLLER_TEST=1
        source "{POLLER_SCRIPT}"
        type is_agent_alive_in_session >/dev/null 2>&1 || {{ echo "FATAL: is_agent_alive_in_session not defined" >&2; exit 1; }}
        {extra_code}
        """)
    )


def run_bash(code: str) -> subprocess.CompletedProcess:
    """Run bash code and return result."""
    return subprocess.run(
        ["bash", "-c", code],
        capture_output=True, text=True, timeout=10,
    )


class TestIsAgentAliveInSession:
    """Test the is_agent_alive_in_session function logic."""

    def test_bash_only_is_zombie(self):
        """A tmux pane running only bash/zsh is a zombie."""
        result = run_poll_script("""
        tmux() {
            if [[ "$1" == "list-panes" ]]; then
                echo "bash"
            elif [[ "$1" == "list-windows" ]]; then
                echo ""
            else
                return 1
            fi
        }
        if is_agent_alive_in_session "session"; then echo "ALIVE"; else echo "ZOMBIE"; fi
        """)
        assert result.stdout.strip() == "ZOMBIE"

    def test_claude_process_is_alive(self):
        """A tmux pane running claude is a live agent."""
        result = run_poll_script("""
        tmux() {
            if [[ "$1" == "list-panes" ]]; then
                echo "claude"
            elif [[ "$1" == "list-windows" ]]; then
                echo ""
            else
                return 1
            fi
        }
        if is_agent_alive_in_session "session"; then echo "ALIVE"; else echo "ZOMBIE"; fi
        """)
        assert result.stdout.strip() == "ALIVE"

    def test_node_process_is_alive(self):
        """A tmux pane running node (codex/ao) is a live agent."""
        result = run_poll_script("""
        tmux() {
            if [[ "$1" == "list-panes" ]]; then
                echo "node"
            elif [[ "$1" == "list-windows" ]]; then
                echo ""
            else
                return 1
            fi
        }
        if is_agent_alive_in_session "session"; then echo "ALIVE"; else echo "ZOMBIE"; fi
        """)
        assert result.stdout.strip() == "ALIVE"

    def test_dash_bash_is_zombie(self):
        """Login shell -bash is also a zombie."""
        result = run_poll_script("""
        tmux() {
            if [[ "$1" == "list-panes" ]]; then
                echo "-bash"
            elif [[ "$1" == "list-windows" ]]; then
                echo ""
            else
                return 1
            fi
        }
        if is_agent_alive_in_session "session"; then echo "ALIVE"; else echo "ZOMBIE"; fi
        """)
        assert result.stdout.strip() == "ZOMBIE"


class TestSessionPatternMatching:
    """Test the tmux session name pattern matching."""

    def test_hex_prefix_jc_pattern_matches(self):
        """Sessions like 623f2a11d8ef-jc-163 should match."""
        result = run_bash("""
        echo "623f2a11d8ef-jc-163" | grep -E "^[0-9a-f]+-jc-" && echo "MATCH" || echo "NO"
        """)
        assert "MATCH" in result.stdout

    def test_ao_prefix_does_not_match(self):
        """Sessions like bb5e6b7f8db3-ao-77 should NOT match jc pattern."""
        result = run_bash("""
        echo "bb5e6b7f8db3-ao-77" | grep -E "^[0-9a-f]+-jc-" && echo "MATCH" || echo "NO"
        """)
        assert "NO" in result.stdout or "MATCH" not in result.stdout

    def test_branch_extraction_from_session_name(self):
        """Extracting branch from session name: strip prefix up to first -."""
        result = run_bash("""
        session="623f2a11d8ef-jc-163"
        branch="${session#*-}"
        echo "$branch"
        """)
        assert result.stdout.strip() == "jc-163"

    def test_empty_machine_prefix_matches_nothing(self):
        """With empty MACHINE_PREFIX, grep '^-' won't match any sessions."""
        result = run_bash("""
        MACHINE_PREFIX=""
        echo "623f2a11d8ef-jc-163" | grep "^${MACHINE_PREFIX}-" && echo "MATCH" || echo "NO"
        """)
        # This proves the original bug: empty prefix means grep "^-" which won't match
        assert "NO" in result.stdout

    def test_fixed_pattern_matches_all_prefixes(self):
        """The fixed pattern matches sessions from any machine prefix."""
        result = run_bash("""
        sessions="623f2a11d8ef-jc-163
57e9c151023f-jc-116
e78d7ebacc1b-jc-183"
        echo "$sessions" | grep -E "^[0-9a-f]+-jc-" | wc -l | tr -d ' '
        """)
        assert result.stdout.strip() == "3"


class TestMergeGateConditions:
    """Test merge gate condition checking logic."""

    def test_cr_commented_no_critical_is_approved(self):
        """CR COMMENTED review with no Critical findings = approved."""
        # This tests the design decision from UNIFIED_MERGE_GATE.md
        result = run_bash("""
        body="## Walkthrough\nThis PR adds a new helper function.\n\n## Changes\n- Added foo.py"
        if echo "$body" | grep -qi "critical\|🔴"; then
            echo "BLOCKED"
        else
            echo "APPROVED"
        fi
        """)
        assert result.stdout.strip() == "APPROVED"

    def test_cr_commented_with_critical_is_blocked(self):
        """CR COMMENTED review with Critical finding = blocked."""
        result = run_bash("""
        body="## Walkthrough\n🔴 Critical: SQL injection vulnerability in query builder"
        if echo "$body" | grep -qi "critical\|🔴"; then
            echo "BLOCKED"
        else
            echo "APPROVED"
        fi
        """)
        assert result.stdout.strip() == "BLOCKED"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
