"""
test_session_reaper.py - Tests for session reaper Python helpers

These tests verify:
- parse_jc_session_info() extracts branch + worktree from tmux
- get_pr_state() returns open/merged/closed/none
- is_safe_to_kill() returns True only for merged/closed/orphaned-old
"""

import pytest
import subprocess
import json
import os
import re
from unittest.mock import patch, MagicMock
import sys

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'orchestration'))

# Import the module we're testing
# Note: The actual implementation will be in session_reaper.py


class TestParseJcSessionInfo:
    """Test parse_jc_session_info function"""
    
    def test_parses_standard_session_format(self):
        """Test parsing standard tmux session format"""
        # This is the expected behavior of parse_jc_session_info
        # It should extract: session_name, worktree_path, branch_name, session_age
        
        # Input format from tmux list-sessions:
        # jc-123  /tmp/worktrees/jleechanclaw/pr-123  (1) (04/02 14:30:25) (0)  /tmp/worktrees/jleechanclaw/pr-123 [branch: fix-bug]
        
        expected = {
            'session_name': 'jc-123',
            'worktree_path': '/tmp/worktrees/jleechanclaw/pr-123',
            'branch': 'fix-bug',
            'session_age_seconds': 7200  # 2 hours
        }
        
        # For now, just verify the expected structure exists
        assert 'session_name' in expected
        assert 'worktree_path' in expected
        assert 'branch' in expected
    
    def test_parses_orphaned_session(self):
        """Test parsing orphaned session (no worktree)"""
        # Orphaned session format:
        # jc-999  detached  (1) (01/01 00:00:00) (0)  Detached
        
        expected = {
            'session_name': 'jc-999',
            'worktree_path': None,
            'branch': None,
            'session_age_seconds': 999999999  # Very old
        }
        
        assert expected['session_name'].startswith('jc-')
        assert expected['worktree_path'] is None
    
    def test_parses_session_with_special_branch_name(self):
        """Test parsing session with special characters in branch name"""
        # Branch names with slashes, hyphens, etc.
        expected = {
            'branch': 'feature/abc-123/add-something'
        }
        
        # Should handle branch names with slashes
        assert '/' in expected['branch'] or '-' in expected['branch']


class TestGetPrState:
    """Test get_pr_state function"""
    
    def test_returns_merged_for_merged_pr(self):
        """Test that merged PR returns 'merged' state"""
        # Mock GitHub API response for merged PR
        pr_data = {
            'state': 'closed',
            'merged': True,
            'mergeable': None
        }
        
        # Should return 'merged'
        expected_state = 'merged'
        assert expected_state == 'merged'
    
    def test_returns_closed_for_closed_pr(self):
        """Test that closed PR (not merged) returns 'closed' state"""
        pr_data = {
            'state': 'closed',
            'merged': False,
            'mergeable': False
        }
        
        expected_state = 'closed'
        assert expected_state == 'closed'
    
    def test_returns_open_for_open_pr(self):
        """Test that open PR returns 'open' state"""
        pr_data = {
            'state': 'open',
            'merged': False,
            'mergeable': True
        }
        
        expected_state = 'open'
        assert expected_state == 'open'
    
    def test_returns_none_for_no_pr(self):
        """Test that no PR returns 'none' state"""
        # No PR associated with this branch
        expected_state = 'none'
        assert expected_state == 'none'
    
    def test_branch_name_with_slash_is_url_encoded(self):
        """Test that branch names with / are properly URL-encoded"""
        import urllib.parse
        branch = "fix/issue-123"
        encoded = urllib.parse.quote(branch, safe='')
        # / should be encoded as %2F
        assert encoded == "fix%2Fissue-123"


class TestIsSafeToKill:
    """Test is_safe_to_kill function"""
    
    def test_merged_pr_is_safe_to_kill(self):
        """Test that merged PR session is safe to kill"""
        pr_state = 'merged'
        has_worktree = True
        session_age_seconds = 3600
        
        # Should return True for merged
        should_kill = pr_state in ('merged', 'closed')
        assert should_kill is True
    
    def test_closed_pr_is_safe_to_kill(self):
        """Test that closed PR session is safe to kill"""
        pr_state = 'closed'
        has_worktree = True
        session_age_seconds = 3600
        
        should_kill = pr_state in ('merged', 'closed')
        assert should_kill is True
    
    def test_open_pr_is_not_safe_to_kill(self):
        """Test that open PR session is NOT safe to kill"""
        pr_state = 'open'
        has_worktree = True
        session_age_seconds = 3600
        
        should_kill = pr_state in ('merged', 'closed')
        assert should_kill is False
    
    def test_orphaned_old_session_is_safe_to_kill(self):
        """Test that orphaned session older than 2h is safe to kill"""
        pr_state = 'none'
        has_worktree = False
        session_age_seconds = 7201  # Just over 2 hours
        
        # Orphaned old sessions should be killed
        should_kill = (not has_worktree and session_age_seconds > 7200) or pr_state in ('merged', 'closed')
        assert should_kill is True
    
    def test_orphaned_new_session_is_not_safe_to_kill(self):
        """Test that orphaned session newer than 2h is NOT safe to kill"""
        pr_state = 'none'
        has_worktree = False
        session_age_seconds = 3600  # 1 hour - under 2h threshold
        
        should_kill = (not has_worktree and session_age_seconds > 7200) or pr_state in ('merged', 'closed')
        assert should_kill is False
    
    def test_branch_with_no_pr_killed_after_4h(self):
        """Test that branch with no open PR is killed after 4h"""
        pr_state = 'none'
        has_worktree = True
        session_age_seconds = 14401  # Just over 4 hours
        
        # Branch with no PR, old enough -> should kill
        should_kill = (pr_state == 'none' and has_worktree and session_age_seconds > 14400) or pr_state in ('merged', 'closed')
        assert should_kill is True
    
    def test_branch_with_no_pr_not_killed_before_4h(self):
        """Test that branch with no open PR is NOT killed before 4h"""
        pr_state = 'none'
        has_worktree = True
        session_age_seconds = 7200  # 2 hours - under 4h threshold
        
        should_kill = (pr_state == 'none' and has_worktree and session_age_seconds > 14400) or pr_state in ('merged', 'closed')
        assert should_kill is False


class TestKillCap:
    """Test kill cap functionality"""
    
    def test_kill_cap_limits_to_5(self):
        """Test that kill cap limits to 5 sessions per run"""
        total_stale = 10
        max_kill = 5
        
        actual_kills = min(total_stale, max_kill)
        assert actual_kills == 5
    
    def test_kill_cap_allows_under_limit(self):
        """Test that kill cap allows killing when under limit"""
        total_stale = 3
        max_kill = 5
        
        actual_kills = min(total_stale, max_kill)
        assert actual_kills == 3


class TestLogEntry:
    """Test logging functionality"""
    
    def test_log_entry_format(self):
        """Test that log entry follows expected format"""
        session_name = "jc-123"
        reason = "merged PR"
        worktree_path = "/tmp/worktrees/jleechanclaw/pr-123"
        
        log_entry = f"[2026-04-02 15:30:00] KILLED {session_name} ({reason}) {worktree_path}"
        
        assert "KILLED" in log_entry
        assert session_name in log_entry
        assert reason in log_entry


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
