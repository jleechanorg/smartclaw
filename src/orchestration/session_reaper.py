#!/usr/bin/env python3
"""
session_reaper.py - Python helpers for session reaper functionality

Provides functions for:
- parse_jc_session_info: Extract branch + worktree from tmux session
- get_pr_state: Query GitHub for PR state (open/merged/closed/none)
- is_safe_to_kill: Determine if a session should be killed
"""

import os
import re
import subprocess
import json
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple


# Configuration
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
if not GITHUB_TOKEN:
    try:
        GITHUB_TOKEN = subprocess.check_output(
            ['gh', 'auth', 'token'], text=True
        ).strip()
    except subprocess.CalledProcessError:
        pass

ORPHANED_AGE_THRESHOLD = 7200  # 2 hours in seconds
NO_PR_AGE_THRESHOLD = 14400   # 4 hours in seconds
MAX_KILLS_PER_RUN = 5


def get_default_repo() -> str:
    """
    Resolve the default GitHub repo from the environment.

    Preference order:
    - OPENCLAW_SESSION_REAPER_REPO
    - AO_SESSION_REAPER_REPO
    - GITHUB_REPOSITORY
    """
    for env_var in (
        'OPENCLAW_SESSION_REAPER_REPO',
        'AO_SESSION_REAPER_REPO',
        'GITHUB_REPOSITORY',
    ):
        value = os.environ.get(env_var, '').strip()
        if value:
            return value
    return ''


def parse_tmux_session_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Parse a tmux list-sessions line to extract session info.
    
    Expected format:
    jc-123  /tmp/worktrees/smartclaw/pr-123  (1) (04/02 14:30:25) (0)  /tmp/worktrees/smartclaw/pr-123 [branch: fix-bug]
    
    Returns dict with:
    - session_name: str
    - worktree_path: str or None
    - branch: str or None
    - session_age_seconds: int
    """
    if not line.strip():
        return None
    
    # Check if it's a jc-* session
    parts = line.split()
    if not parts or not parts[0].startswith('jc-'):
        return None
    
    session_name = parts[0]
    
    # Check for "detached" (orphaned)
    if 'detached' in line.lower() or '(detached)' in line.lower():
        # Try to parse creation time from tmux
        age_seconds = get_session_age_from_tmux(session_name)
        return {
            'session_name': session_name,
            'worktree_path': None,
            'branch': None,
            'session_age_seconds': age_seconds
        }
    
    # Try to extract worktree path - usually first path after session name
    worktree_path = None
    branch = None
    
    # Look for path patterns
    for part in parts:
        if part.startswith('/') and 'worktree' in part:
            worktree_path = part
            break
    
    # Look for branch: branch-name pattern
    branch_match = re.search(r'\[branch:\s*([^\]]+)\]', line)
    if branch_match:
        branch = branch_match.group(1).strip()
    
    # Get session age
    age_seconds = get_session_age_from_tmux(session_name)
    
    return {
        'session_name': session_name,
        'worktree_path': worktree_path,
        'branch': branch,
        'session_age_seconds': age_seconds
    }


def get_session_age_from_tmux(session_name: str) -> int:
    """
    Get session age in seconds by querying tmux.
    """
    try:
        # Get session creation time
        result = subprocess.run(
            ['tmux', 'display-message', '-t', session_name, '-F', '#{session_created}'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            created_at = int(result.stdout.strip())
            current_time = int(time.time())
            return current_time - created_at
    except (subprocess.TimeoutExpired, ValueError, subprocess.CalledProcessError):
        pass
    
    return 0  # Default to 0 if we can't determine


def list_jc_sessions() -> List[Dict[str, Any]]:
    """
    List all jc-* tmux sessions with their info.
    """
    try:
        result = subprocess.run(
            ['tmux', 'list-sessions', '-F', '#{session_name} #{session_path} #{session_created}'],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            return []
        
        sessions = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            
            parts = line.split(None, 2)
            if len(parts) < 1:
                continue
            
            session_name = parts[0]
            if not session_name.startswith('jc-'):
                continue
            
            session_path = parts[1] if len(parts) > 1 else ''
            created_at = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            
            current_time = int(time.time())
            age_seconds = current_time - created_at if created_at > 0 else 0
            
            # Check if worktree exists
            worktree_path = None
            if session_path and os.path.isdir(session_path):
                worktree_path = session_path
            
            sessions.append({
                'session_name': session_name,
                'worktree_path': worktree_path,
                'branch': None,  # Would need additional parsing
                'session_age_seconds': age_seconds
            })
        
        return sessions
        
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        print(f"Error listing tmux sessions: {e}")
        return []


def get_pr_state_from_branch(repo: str, branch: str) -> str:
    """
    Query GitHub to find PR state for a given branch.
    
    Returns: 'open', 'merged', 'closed', or 'none'
    """
    if not GITHUB_TOKEN:
        return 'none'
    
    if not repo or not branch:
        return 'none'
    
    try:
        # Search for PR by branch head
        import urllib.request
        import urllib.error
        
        # Extract owner from repo (format: owner/repo_name)
        owner = repo.split('/')[0] if '/' in repo else ''
        if not owner:
            return 'none'
        
        # GitHub API requires head parameter in format "owner:branch"
        head_param = f"{owner}:{branch}"
        url = f"https://api.github.com/repos/{repo}/pulls?head={urllib.parse.quote(head_param, safe='')}&state=all"
        
        req = urllib.request.Request(url)
        req.add_header('Authorization', f'Bearer {GITHUB_TOKEN}')
        req.add_header('Accept', 'application/vnd.github.v3+json')
        
        response = urllib.request.urlopen(req, timeout=10)
        data = json.loads(response.read().decode())
        
        if not data:
            return 'none'
        
        pr = data[0]  # First matching PR
        state = pr.get('state', 'unknown')
        merged = pr.get('merged', False)
        
        if state == 'closed' and merged:
            return 'merged'
        elif state == 'closed':
            return 'closed'
        elif state == 'open':
            return 'open'
        
        return 'none'
        
    except Exception as e:
        print(f"Error getting PR state for {branch}: {e}")
        return 'none'


def is_safe_to_kill(
    session_info: Dict[str, Any],
    repo: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Determine if a session is safe to kill.
    
    Returns: (should_kill: bool, reason: str)
    """
    repo = (repo or get_default_repo()).strip()
    session_name = session_info['session_name']
    worktree_path = session_info['worktree_path']
    branch = session_info.get('branch')
    age_seconds = session_info.get('session_age_seconds', 0)
    
    # Case 1: No worktree (orphaned)
    if not worktree_path:
        if age_seconds > ORPHANED_AGE_THRESHOLD:
            return True, f"orphaned session ({age_seconds}s old)"
        else:
            return False, f"orphaned but recent ({age_seconds}s old)"
    
    # Case 2: Branch with no PR - kill after 4h
    if not branch:
        if age_seconds > NO_PR_AGE_THRESHOLD:
            return True, f"branch with no PR ({age_seconds}s old)"
        else:
            return False, f"branch with no PR but recent ({age_seconds}s old)"
    
    # Case 3: Check PR state
    pr_state = get_pr_state_from_branch(repo, branch)
    
    if pr_state in ('merged', 'closed'):
        return True, f"PR {pr_state}"
    elif pr_state == 'open':
        return False, f"PR {pr_state}"
    else:
        # No PR found for this branch
        if age_seconds > NO_PR_AGE_THRESHOLD:
            return True, f"no associated PR ({age_seconds}s old)"
        else:
            return False, f"no associated PR but recent ({age_seconds}s old)"


def kill_session(session_name: str) -> bool:
    """
    Kill a tmux session.
    """
    try:
        subprocess.run(
            ['tmux', 'kill-session', '-t', session_name],
            capture_output=True,
            timeout=10
        )
        return True
    except subprocess.TimeoutExpired:
        return False


def remove_worktree(worktree_path: str) -> bool:
    """
    Remove a git worktree.
    """
    if not worktree_path or not os.path.isdir(worktree_path):
        return False
    
    try:
        # Get the parent repo to remove worktree properly
        result = subprocess.run(
            ['git', 'worktree', 'remove', worktree_path],
            capture_output=True,
            timeout=30
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        print(f"Error removing worktree {worktree_path}: {e}")
        return False


def reap_sessions(
    repo: Optional[str] = None,
    max_kills: int = MAX_KILLS_PER_RUN,
    log_file: Optional[str] = None,
) -> List[str]:
    """
    Main function to reap stale sessions.
    
    Returns list of killed session names.
    """
    repo = (repo or get_default_repo()).strip()
    killed_sessions = []
    
    sessions = list_jc_sessions()
    
    for session_info in sessions:
        if len(killed_sessions) >= max_kills:
            break
        
        should_kill, reason = is_safe_to_kill(session_info, repo)
        
        if should_kill:
            session_name = session_info['session_name']
            worktree_path = session_info['worktree_path']
            
            # Log the kill
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_entry = f"[{timestamp}] KILLED {session_name} ({reason})"
            if worktree_path:
                log_entry += f" {worktree_path}"
            
            if log_file:
                with open(log_file, 'a') as f:
                    f.write(log_entry + '\n')
            else:
                print(log_entry)
            
            # Kill the session
            if kill_session(session_name):
                killed_sessions.append(session_name)
                
                # Optionally remove worktree
                if worktree_path:
                    remove_worktree(worktree_path)
    
    return killed_sessions


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Reap stale tmux sessions')
    parser.add_argument(
        '--repo',
        default=get_default_repo(),
        help=(
            'GitHub repo (defaults from OPENCLAW_SESSION_REAPER_REPO, '
            'AO_SESSION_REAPER_REPO, or GITHUB_REPOSITORY)'
        ),
    )
    parser.add_argument('--max-kills', type=int, default=MAX_KILLS_PER_RUN, help='Max sessions to kill')
    parser.add_argument('--log-file', help='Log file path')
    
    args = parser.parse_args()
    
    killed = reap_sessions(args.repo, args.max_kills, args.log_file)
    print(f"Killed {len(killed)} sessions: {killed}")
