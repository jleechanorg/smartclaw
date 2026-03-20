"""dispatch_task — called by OpenClaw at agent spawn time.

Runs ai_orch, parses the session/worktree output, records git start_sha,
and writes BeadSessionMapping to the registry so the supervisor can track it.

Usage:
    python -m orchestration.dispatch_task \\
        --bead-id ORCH-xxx \\
        --task "implement feature X" \\
        --slack-trigger-ts 1772857900.668299 \\
        [--slack-trigger-channel C0A... ] \\
        [--agent-cli claude] \\
        [--registry-path .tracking/bead_session_registry.jsonl]
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import shlex
import shutil
import site
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from orchestration.openclaw_notifier import notify_openclaw, notify_slack_started
from orchestration.session_registry import BeadSessionMapping, upsert_mapping

logger = logging.getLogger(__name__)

# Legacy fallback project key for MCP mail registration.
LEGACY_DEFAULT_PROJECT_KEY = ""


def get_default_project_key() -> str:
    """Resolve default MCP project key from environment."""
    return (
        os.environ.get("SMARTCLAW_PROJECT_KEY", "").strip()
        or os.environ.get("OPENCLAW_PROJECT_KEY", "").strip()
        or LEGACY_DEFAULT_PROJECT_KEY
    )


def register_agent_mcp_mail(
    session_name: str,
    project: str,
    pr_number: str | int | None = None,
    agent_cli: str = "claude",
) -> bool:
    """Register agent with MCP mail for bidirectional messaging.

    Args:
        session_name: The AO session ID (e.g., 'ao-session-pr-237')
        project: Project name (e.g., 'jleechanclaw')
        pr_number: Optional PR number for display name
        agent_cli: Agent CLI program ('claude', 'codex', 'cursor')

    Returns:
        True if registration succeeded, False otherwise
    """
    try:
        import json
        import subprocess

        # Map agent_cli to program name
        program_map = {
            "claude": "claude-code",
            "codex": "codex-cli",
            "cursor": "cursor-agent",
        }
        program = program_map.get(agent_cli, agent_cli)

        # Build display name
        if pr_number:
            display_name = f"AO Agent: {project}/#{pr_number}"
        else:
            display_name = f"AO Agent: {project}"

        # Build task description
        task_desc = f"PR #{pr_number}" if pr_number else "General task"

        # Call MCP mail register_agent via mcporter
        cmd = [
            "mcporter", "call", "mcp-agent-mail.register_agent",
            "--project-key", get_default_project_key(),
            "--program", program,
            "--model", "default",
            "--name", session_name,
            "--task-description", task_desc,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            logger.info(f"Registered agent {session_name} with MCP mail")
            return True
        else:
            logger.warning(f"MCP mail registration failed: {result.stderr}")
            return False

    except Exception as e:
        logger.warning(f"MCP mail registration error (non-blocking): {e}")
        return False


def find_existing_worktree(branch: str, repo_root: str) -> str | None:
    """Return path of an existing worktree for branch, or None.

    Skips the primary worktree (repo_root itself) — dispatch must not run
    agents in the main checkout. Returns None on any git failure so callers
    can fall back gracefully.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
    except Exception:
        return None

    primary = os.path.realpath(repo_root)
    current_path: str | None = None
    current_branch: str | None = None

    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):].strip()
            current_branch = None
        elif line.startswith("branch refs/heads/"):
            current_branch = line[len("branch refs/heads/"):].strip()
        elif line == "":
            # End of a worktree block — check if it matches
            if (
                current_path
                and current_branch == branch
                and os.path.realpath(current_path) != primary
            ):
                return current_path
            current_path = None
            current_branch = None

    if (
        current_path
        and current_branch == branch
        and os.path.realpath(current_path) != primary
    ):
        return current_path

    return None


def _remote_branch_exists(branch: str, repo_root: str = ".") -> bool:
    """Return True if branch exists on origin."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
            cwd=repo_root,
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


def _worktree_add_path(branch: str, repo_root: str, worktree_base: str) -> str:
    """Checkout branch into a fresh directory under worktree_base. Returns path.

    If the branch is already checked out in the primary worktree, falls back to
    creating a detached HEAD worktree at origin/<branch>, then switches to the
    local tracking branch so the agent can commit and push normally.
    """
    safe = re.sub(r"[^a-zA-Z0-9._-]", "-", branch)
    dest = tempfile.mkdtemp(prefix=f"wt-{safe}-", dir=worktree_base)
    try:
        # Ensure origin/<branch> exists locally before the fallback path uses it.
        subprocess.run(
            ["git", "fetch", "--no-tags", "origin", branch],
            cwd=repo_root,
            check=True,
            timeout=30,
        )
        result = subprocess.run(
            ["git", "worktree", "add", dest, branch],
            cwd=repo_root,
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            return dest

        stderr = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else result.stderr
        if "already checked out" not in stderr:
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stderr)

        # Branch is checked out in another worktree — create from the fetched
        # remote ref, then attach the local tracking branch inside the detached
        # linked worktree so the agent can commit and push normally.
        subprocess.run(
            ["git", "worktree", "add", "--detach", dest, f"origin/{branch}"],
            cwd=repo_root,
            check=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "checkout", "-B", branch, "--track", f"origin/{branch}"],
            cwd=dest,
            check=True,
            timeout=15,
        )
        return dest
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def resolve_worktree_for_branch(
    branch: str,
    repo_root: str,
    worktree_base: str,
) -> tuple[str, bool]:
    """Return (worktree_path, is_new) for branch.

    - Existing worktree → (path, False)
    - Branch on remote but not in any worktree → checkout fresh, (path, True)
    - Branch missing everywhere → raises ValueError
    """
    existing = find_existing_worktree(branch, repo_root)
    if existing:
        return existing, False

    if not _remote_branch_exists(branch, repo_root):
        raise ValueError(f"branch {branch!r} not found in any worktree or on remote origin")

    path = _worktree_add_path(branch, repo_root, worktree_base)
    return path, True


def _parse_ai_orch_output(output: str, known_worktree: str = "") -> tuple[str, str, str]:
    """Extract (session_name, worktree_path, branch) from ai_orch run output.

    If known_worktree is provided (e.g., for cross-repo tasks we created ourselves),
    use that as the worktree path instead of parsing from output.
    """
    session = ""
    worktree = known_worktree
    branch = ""

    for line in output.splitlines():
        # 🧩 Worktree: /tmp/ai-orch-worktrees/ai-orch-12345 (branch: ai-orch-12345)
        m = re.search(r"Worktree:\s+(\S+)\s+\(branch:\s+(\S+)\)", line)
        if m:
            if not known_worktree:  # Only parse if we don't already know it
                worktree = m.group(1)
            branch = m.group(2)
        # 🚀 Async session: ai-claude-dfa2c0
        m2 = re.search(r"Async session:\s+(\S+)", line)
        if m2:
            session = m2.group(1)

    return session, worktree, branch


def _get_start_sha(worktree_path: str) -> str:
    """Get HEAD sha in the worktree — recorded as baseline for commit detection.

    Retries briefly because ai_orch may still be initializing the worktree git
    state when this is called immediately after spawn.
    """
    for _ in range(5):
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        time.sleep(1)
    return ""


def _get_current_branch(worktree_path: str) -> str:
    """Return current branch name for the given worktree, or empty on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", worktree_path, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        return ""
    return ""


_DEFAULT_WORKTREE_BASE = os.path.expanduser("~/.mctrl/worktrees")


# Cross-repo PR pattern - injected automatically for cross-repo tasks
_CROSS_REPO_CONTEXT = """
## CROSS-REPO PR PATTERN (IMPORTANT)
When the task involves making a PR to a DIFFERENT REPO than your current worktree:
- DO NOT clone the target repo into a subdirectory
- DO NOT work in the current jleechanclaw worktree
- Instead: create a NEW worktree FOR the target repo as a sibling directory
- Work directly in that repo's worktree
- Commit and push directly to that repo
- Example: for "mctrl_test" repo, create worktree at ../mctrl_test or /tmp/ai-orch-worktrees/mctrl_test
- Use: `gh pr create --repo owner/repo --base main --head <branch>` to PR cross-repo
"""


def _is_cross_repo_task(task: str) -> bool:
    """Detect if task is targeting a different repo.

    Uses high-confidence patterns and falls back to repo-name extraction
    (which includes a blocklist) to avoid false positives on generic
    English phrases like "fix the bug in this repo".
    """
    task_lower = task.lower()
    # High-confidence patterns only — avoid false positives
    if "github.com/" in task_lower or " pr against " in task_lower:
        return True
    # Fall back to repo-name extraction (includes blocklist filtering)
    return bool(_extract_repo_name_hint(task))


def _inject_cross_repo_context(task: str, repo_root: str) -> str:
    """Inject cross-repo PR context if task appears to target a different repo."""
    if not _is_cross_repo_task(task):
        return task

    # Check if we're already in the target repo (no cross-repo needed)
    repo_name = _extract_repo_name_hint(task)
    if repo_name:
        # Check if the current worktree repo matches
        current_repo = Path(repo_root).name
        if current_repo.lower() == repo_name.lower():
            return task

    # Inject cross-repo context
    return f"{task.rstrip()}\n{_CROSS_REPO_CONTEXT}"


def _resolve_target_repo(task: str, default_repo_root: str) -> tuple[str, str]:
    """Resolve the target repo for cross-repo tasks.

    Returns (repo_root, worktree_path) for the target repo.
    worktree_path is only set when a pre-existing worktree is intentionally reused.
    If no resolvable cross-repo target exists, returns ("", "").
    """
    if not _is_cross_repo_task(task):
        return "", ""

    repo_name = _extract_repo_name_hint(task)
    if not repo_name:
        return "", ""

    # Check if we're already in the target repo
    current_repo = Path(default_repo_root).name
    if current_repo.lower() == repo_name.lower():
        return default_repo_root, ""

    target_repo_root = _find_repo_by_name(repo_name, fallback_root=default_repo_root)
    if target_repo_root:
        return target_repo_root, ""

    return "", ""


def _get_repo_owner_and_name(repo_root: str) -> str:
    """Extract owner/repo from git remote URL. Returns empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return ""
        
        url = result.stdout.strip()
        # Parse SSH format: git@github.com:owner/repo.git
        if url.startswith("git@github.com:"):
            path = url[len("git@github.com:"):].removesuffix(".git")
            return path
        # Parse HTTPS format: https://github.com/owner/repo.git
        if "github.com/" in url:
            parts = url.split("github.com/", 1)
            if len(parts) == 2:
                return parts[1].removesuffix(".git")
        return ""
    except Exception:
        return ""


def _task_with_push_instruction(task: str, branch: str = "", repo_root: str = ".") -> str:
    """Append git commit/push instructions and cross-repo context to help the LLM."""
    # Inject cross-repo context when applicable
    task = _inject_cross_repo_context(task, repo_root)

    normalized = task.lower()
    # If task already mentions git push, don't append duplicate instructions.
    if "git push" in normalized:
        return task

    commit_text = "`git commit -m \"Your message\"`"
    push_text = f"`git push origin {branch or '<your-branch>'}`"
    pr_head = branch or "$(git rev-parse --abbrev-ref HEAD)"
    # Get the correct repo from git remote
    repo_full_name = _get_repo_owner_and_name(repo_root)
    repo_flag = f"--repo {repo_full_name} " if repo_full_name else ""
    pr_text = f"`gh pr create {repo_flag}--base main --head {pr_head} --fill`"
    return (
        f"{task.rstrip()}\n\n"
        f"IMPORTANT: Work in the worktree ROOT directory (not a subdirectory). "
        f"After completing your changes, run:\n"
        f"1. `git add .` to stage all changes\n"
        f"2. {commit_text} to commit them\n"
        f"3. {push_text} to push to remote.\n"
        f"4. {pr_text} to open a pull request to `main`.\n"
        f"Your work is only visible after it is pushed to origin and a PR URL exists."
    )


# Common English words that should never be treated as repo names.
_REPO_NAME_BLOCKLIST = frozenset({
    "a", "an", "the", "this", "that", "my", "our", "your", "its",
    "fix", "bug", "code", "main", "test", "tests", "new", "old",
    "repo", "repository", "project", "branch", "codebase",
    "same", "current", "local", "remote", "upstream",
})


def _extract_repo_name_hint(task: str) -> str:
    """Extract repo name from task text.

    Returns the first candidate that is not in the blocklist.
    """
    patterns = (
        r"\bagainst\s+`?([A-Za-z0-9._-]+)`?\s*$",
        r"\bagainst\s+`?([A-Za-z0-9._-]+)`?\s+repo",
        r"\bagainst\s+`?([A-Za-z0-9._-]+)`?\s+repository",
        r"\bin\s+`?([A-Za-z0-9._-]+)`?\s+repo\b",
        r"\bin\s+`?([A-Za-z0-9._-]+)`?\s+repository\b",
        r"github\.com/[^/\s]+/([A-Za-z0-9._-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, task, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = match.group(1).strip().strip(".")
        if candidate and candidate.lower() not in _REPO_NAME_BLOCKLIST:
            return candidate
    return ""


def _looks_like_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def _repo_hint_bases(*, fallback_root: str = ".") -> list[Path]:
    """Return base directories to search for repo-name hints."""
    bases: list[Path] = []

    hint_paths = os.environ.get("MCTRL_REPO_HINT_PATHS", "").strip()
    if hint_paths:
        for raw in hint_paths.split(":"):
            raw = raw.strip()
            if raw:
                bases.append(Path(os.path.expanduser(raw)))

    explicit = Path(os.path.realpath(os.path.expanduser(fallback_root or ".")))
    worktree_base = Path(os.environ.get("MCTRL_WORKTREE_BASE", _DEFAULT_WORKTREE_BASE))

    bases.extend([
        explicit,
        explicit.parent,
        worktree_base,
        worktree_base.parent,
        Path("/tmp"),
        Path("/tmp/ai-orch-worktrees"),
        Path.home() / "projects",
        Path.home() / "workspace",
        Path.home() / ".openclaw" / "workspace",
        Path.home() / ".mctrl" / "worktrees",
    ])

    deduped: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        resolved = os.path.realpath(str(base))
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(Path(resolved))

    return deduped


def _find_repo_by_name(name: str, *, fallback_root: str = ".") -> str | None:
    """Search for a local git repo matching name.

    Search order:
    1. Paths listed in MCTRL_REPO_HINT_PATHS (colon-separated).
    2. Common local clone locations.
    3. fallback_root itself.

    Returns the first matching path, or None if not found.
    Deduplicates candidates before checking.
    """
    for candidate in (base / name for base in _repo_hint_bases(fallback_root=fallback_root)):
        if _looks_like_git_repo(candidate):
            return str(candidate)

    return None


def _resolve_repo_root(repo_root: str, task: str) -> str:
    """Resolve dispatch repo root from explicit arg or task repo hints.

    If no target repo found, returns explicit path - the LLM will
    use memory to infer the target repo.
    """
    explicit = os.path.realpath(os.path.expanduser(repo_root or "."))
    if repo_root and repo_root != ".":
        return explicit

    repo_name = _extract_repo_name_hint(task)
    if not repo_name:
        # No explicit target - LLM will use memory to infer
        return explicit

    for candidate in (base / repo_name for base in _repo_hint_bases(fallback_root=explicit)):
        if _looks_like_git_repo(candidate):
            return str(candidate)

    return explicit


def _unique_session_name(bead_id: str, session_name: str) -> str:
    """Build a bead-traceable tmux session name for this dispatch run."""
    bead_token = re.sub(r"[^a-zA-Z0-9]+", "-", bead_id).strip("-").lower() or "bead"
    return f"{bead_token}-{int(time.time())}-{session_name}"


def _make_async_session_name(agent_cli: str, repo_root: str) -> str:
    cwd_hash = hashlib.md5(os.path.realpath(repo_root).encode()).hexdigest()[:6]
    return f"ai-{agent_cli}-{cwd_hash}"


def _create_new_worktree(repo_root: str, worktree_base: str) -> tuple[str, str]:
    """Create a fresh worktree + branch for direct CLI dispatch."""
    os.makedirs(worktree_base, exist_ok=True)
    branch = f"ai-orch-{int(time.time()) % 100000}"
    worktree_path = os.path.join(worktree_base, branch)
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch, worktree_path],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")
    return worktree_path, branch


def _build_cursor_shell_cmd(task: str, worktree_path: str) -> str:
    """Run Cursor in headless trusted mode so dispatch never blocks on trust UI."""
    model = os.environ.get("MCTRL_CURSOR_MODEL", "auto")
    return " ".join(
        [
            "cursor-agent",
            "--print",
            "--trust",
            "--approve-mcps",
            "--output-format",
            "text",
            "--model",
            shlex.quote(model),
            "--workspace",
            shlex.quote(worktree_path),
            "--yolo",
            shlex.quote(task),
        ]
    )


def _build_ai_orch_env() -> dict[str, str]:
    """Build a clean env so ai_orch does not import mctrl's local orchestration package."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    user_site = site.getusersitepackages()
    if user_site:
        env["PYTHONPATH"] = user_site

    # macOS/bash can reject `ulimit -v`; force-disable the ai_orch child vmem cap
    # unless the operator explicitly overrides it.
    env.setdefault("ORCHESTRATION_CHILD_PROCESS_VMEM_CAP_KB", "0")

    return env


def _run_ai_orch_with_fallback(cmd: list[str], *, cwd: str | None) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run ai_orch, retrying with orch when local wrappers are stale."""
    env = _build_ai_orch_env()
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    output = result.stdout + result.stderr
    if (
        result.returncode != 0
        and "ModuleNotFoundError: No module named 'orchestration.runner'" in output
        and shutil.which("orch")
    ):
        fallback_cmd = ["orch", *cmd[1:]]
        result = subprocess.run(
            fallback_cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        output = result.stdout + result.stderr
    return result, output


def _dispatch_cursor_direct(
    *,
    bead_id: str,
    task: str,
    branch: str,
    repo_root: str,
) -> tuple[str, str, str]:
    """Dispatch Cursor via tmux directly, bypassing ai_orch's interactive fallback."""
    worktree_base = os.environ.get("MCTRL_WORKTREE_BASE", _DEFAULT_WORKTREE_BASE)
    if branch:
        worktree_path, _ = resolve_worktree_for_branch(branch, repo_root, worktree_base)
        effective_branch = branch
    else:
        worktree_path, effective_branch = _create_new_worktree(repo_root, worktree_base)

    session_name = _make_async_session_name("cursor", repo_root)
    shell_cmd = _build_cursor_shell_cmd(
        _task_with_push_instruction(task, effective_branch, repo_root),
        worktree_path,
    )
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", worktree_path, shell_cmd],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cursor tmux dispatch failed: {result.stderr.strip()}")
    return session_name, worktree_path, effective_branch


def _rename_tmux_session(session_name: str, bead_id: str) -> str:
    """Rename the tmux session to a bead-traceable name when it is still alive."""
    target_name = _unique_session_name(bead_id, session_name)
    if target_name == session_name:
        return session_name

    result = subprocess.run(
        ["tmux", "rename-session", "-t", session_name, target_name],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        return target_name

    stderr = (result.stderr or "").strip().lower()
    if "can't find session" in stderr or "no such session" in stderr:
        return session_name
    logger.warning(
        "Could not rename tmux session %r to %r: %s",
        session_name,
        target_name,
        result.stderr.strip(),
    )
    return session_name


def dispatch(
    *,
    bead_id: str,
    task: str,
    slack_trigger_ts: str = "",
    slack_trigger_channel: str = "",
    agent_cli: str = "claude",
    registry_path: str,
    branch: str = "",
    repo_root: str = ".",
    ) -> BeadSessionMapping:
    """Spawn ai_orch session and register the mapping. Returns the created mapping.

    When branch is provided, resolve_worktree_for_branch is called first:
    - Existing linked worktree → ai_orch runs there (no --worktree flag)
    - Remote-only branch → checkout fresh worktree, ai_orch runs there
    - Branch missing everywhere → raises ValueError before spawning

    Without branch:
    - Cross-repo tasks (detected via repo hints or GitHub URLs) create a
      dedicated worktree inside the target repo. On failure, the worktree
      is cleaned up automatically.
    - Same-repo tasks use ai_orch's built-in --worktree flag.
    """
    if slack_trigger_ts and not slack_trigger_channel:
        raise ValueError("slack_trigger_channel is required when slack_trigger_ts is set")
    repo_root = _resolve_repo_root(repo_root, task)

    if agent_cli == "cursor":
        session_name, worktree_path, parsed_branch = _dispatch_cursor_direct(
            bead_id=bead_id,
            task=task,
            branch=branch,
            repo_root=repo_root,
        )
    else:
        # Track known worktree path for cross-repo tasks
        known_worktree_path = ""
        created_worktree_path = ""
        created_worktree_repo_root = ""

        if branch:
            worktree_base = os.environ.get("MCTRL_WORKTREE_BASE", _DEFAULT_WORKTREE_BASE)
            os.makedirs(worktree_base, exist_ok=True)
            cwd, _ = resolve_worktree_for_branch(branch, repo_root, worktree_base)
            agent_task = _task_with_push_instruction(task, branch, repo_root)
            cmd = ["ai_orch", "run", "--async", "--agent-cli", agent_cli, agent_task]
        else:
            # For cross-repo tasks, resolve the target repo first
            # NO FALLBACK - if target repo mentioned, must use it
            target_repo_root, existing_worktree = _resolve_target_repo(task, repo_root)

            if existing_worktree:
                # Use existing worktree for target repo
                cwd = existing_worktree
                known_worktree_path = existing_worktree
                agent_task = _task_with_push_instruction(task, "", cwd)
                cmd = ["ai_orch", "run", "--async", "--agent-cli", agent_cli, agent_task]
            else:
                # Target repo mentioned but not found locally - extract and create worktree
                repo_name = _extract_repo_name_hint(task)
                if not repo_name:
                    # No target repo in task - let LLM use memory to determine target
                    cwd = repo_root
                    agent_task = _task_with_push_instruction(task, "", repo_root)
                    cmd = ["ai_orch", "run", "--async", "--worktree", "--agent-cli", agent_cli, agent_task]
                else:
                    if not target_repo_root:
                        raise ValueError(
                            f"Target repo '{repo_name}' not found locally. "
                            "Please clone the target repo first."
                        )

                    # Target repo specified - create a dedicated worktree
                    worktree_base = os.environ.get("MCTRL_WORKTREE_BASE", _DEFAULT_WORKTREE_BASE)
                    os.makedirs(worktree_base, exist_ok=True)

                    if not _looks_like_git_repo(Path(target_repo_root)):
                        raise ValueError(
                            f"Target repo '{repo_name}' not found locally. "
                            f"Expected at: {target_repo_root}. "
                            f"Please clone the target repo first."
                        )

                    # Create worktree from the target repo - use unique path
                    unique_id = uuid.uuid4().hex[:6]
                    branch_name = f"ai-orch-{int(time.time()) % 100000}-{unique_id}"
                    target_worktree_path = os.path.join(worktree_base, f"{repo_name}-{unique_id}")
                    result = subprocess.run(
                        ["git", "worktree", "add", "-b", branch_name, target_worktree_path],
                        cwd=target_repo_root,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if result.returncode != 0:
                        raise RuntimeError(f"Failed to create worktree for {repo_name}: {result.stderr}")

                    cwd = target_worktree_path
                    known_worktree_path = target_worktree_path
                    created_worktree_path = target_worktree_path
                    created_worktree_repo_root = target_repo_root
                    agent_task = _task_with_push_instruction(task, "", cwd)
                    cmd = ["ai_orch", "run", "--async", "--agent-cli", agent_cli, agent_task]

        result, output = _run_ai_orch_with_fallback(cmd, cwd=cwd)

        if result.returncode != 0:
            if created_worktree_path:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", created_worktree_path],
                    cwd=created_worktree_repo_root or repo_root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            raise RuntimeError(f"ai_orch failed (exit {result.returncode}):\n{output}")

        session_name, worktree_path, parsed_branch = _parse_ai_orch_output(output, known_worktree_path)
        if not session_name or not worktree_path:
            raise RuntimeError(f"Could not parse ai_orch output:\n{output}")

    session_name = _rename_tmux_session(session_name, bead_id)

    effective_branch = branch or parsed_branch or _get_current_branch(worktree_path)
    start_sha = _get_start_sha(worktree_path)

    mapping = BeadSessionMapping.create(
        bead_id=bead_id,
        session_name=session_name,
        worktree_path=worktree_path,
        branch=effective_branch,
        agent_cli=agent_cli,
        status="in_progress",
        start_sha=start_sha,
        slack_trigger_ts=slack_trigger_ts,
        slack_trigger_channel=slack_trigger_channel,
    )
    upsert_mapping(mapping, registry_path=registry_path)

    # Register agent with MCP mail for bidirectional messaging (non-blocking)
    # Extract PR number from bead_id if available (e.g., ORCH-123 -> 123)
    pr_number = None
    if bead_id:
        import re
        match = re.search(r'(\d+)$', bead_id)
        if match:
            pr_number = match.group(1)
    project_name = repo_root.split("/")[-1] if repo_root else "unknown"
    register_agent_mcp_mail(
        session_name=session_name,
        project=project_name,
        pr_number=pr_number,
        agent_cli=agent_cli,
    )

    payload = {
        "event": "task_started",
        "bead_id": bead_id,
        "session": session_name,
        "worktree_path": worktree_path,
        "branch": effective_branch,
        "agent_cli": agent_cli,
        "slack_trigger_ts": slack_trigger_ts,
        "slack_trigger_channel": slack_trigger_channel,
    }
    notify_slack_started(payload)
    notify_openclaw(payload)
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser(description="Dispatch a task via ai_orch and register it.")
    parser.add_argument("--bead-id", required=True, help="Bead ID (e.g. ORCH-xxx)")
    parser.add_argument("--task", required=True, help="Task description passed to ai_orch")
    parser.add_argument("--slack-trigger-ts", default="", help="Slack ts of trigger message (for threading)")
    parser.add_argument("--slack-trigger-channel", default="", help="Slack channel ID of trigger message (for threading)")
    parser.add_argument("--agent-cli", default="claude", help="Agent CLI: claude, codex, gemini, minimax, cursor")
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repo root to dispatch from (defaults to current directory; may be auto-inferred from task text)",
    )
    parser.add_argument(
        "--registry-path",
        default=".tracking/bead_session_registry.jsonl",
        help="Path to bead session registry JSONL",
    )
    args = parser.parse_args()

    try:
        mapping = dispatch(
            bead_id=args.bead_id,
            task=args.task,
            slack_trigger_ts=args.slack_trigger_ts,
            slack_trigger_channel=args.slack_trigger_channel,
            agent_cli=args.agent_cli,
            repo_root=args.repo_root,
            registry_path=args.registry_path,
        )
        print(f"dispatched bead={mapping.bead_id} session={mapping.session_name} worktree={mapping.worktree_path}")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
