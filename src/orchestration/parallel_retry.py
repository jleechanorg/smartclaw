"""Parallel retry: speculative parallel CI fix attempts.

This module implements Phase 3.5 of the orchestration roadmap:
- When CI fails and retry budget has >= 2 attempts remaining, generate multiple
  different fix strategies
- Spawn parallel AO sessions (one per strategy) on separate worktrees
- First session to get CI green wins; kill the rest
- Trades compute for wall-clock time (15-30 min sequential -> 5-10 min parallel)

Integration points:
- escalation_router.py returns ParallelRetryAction when budget >= 2 and error parseable
- action_executor.py calls execute_parallel_retry for ParallelRetryAction
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from orchestration.session_registry import get_mapping
from orchestration.pattern_synthesizer import PatternSynthesizer, SynthesizedPattern
from orchestration.outcome_recorder import OutcomeRecorder

logger = logging.getLogger(__name__)

# Default timeouts and limits
DEFAULT_CI_CHECK_INTERVAL = 30  # seconds
DEFAULT_CI_TIMEOUT = 300  # 5 minutes max per session
DEFAULT_MAX_PARALLEL = 3


# ---------------------------------------------------------------------------
# Outcome-Based Strategy Loading
# ---------------------------------------------------------------------------


def load_winning_strategies(
    error_class: str,
    outcomes_path: str | None = None,
) -> list[FixStrategy]:
    """Load known-winning strategies from outcomes.jsonl for a given error class.

    Queries the outcome ledger for past successes with matching error_class,
    then returns strategies sorted by win rate.

    Args:
        error_class: The error class fingerprint to match
        outcomes_path: Optional path to outcomes.jsonl (defaults to ~/.openclaw/state/outcomes.jsonl)

    Returns:
        List of FixStrategy objects from past winners, sorted by win rate
    """
    if not error_class:
        return []

    try:
        recorder = OutcomeRecorder(outcomes_path=outcomes_path)
        outcomes = recorder.query_outcomes(error_class)

        if not outcomes:
            logger.debug(f"No outcomes found for error_class '{error_class}'")
            return []

        # Group by strategy and calculate win rates
        # Win rate = (times in winning_strategy) / (times in winning_strategy + times in losing_strategies)
        strategy_wins: dict[str, int] = {}
        strategy_total: dict[str, int] = {}

        for outcome in outcomes:
            # Winning strategy is always a success
            win_strategy = outcome.winning_strategy.approach_id
            strategy_total[win_strategy] = strategy_total.get(win_strategy, 0) + 1
            strategy_wins[win_strategy] = strategy_wins.get(win_strategy, 0) + 1
            
            # Losing strategies are failures
            for loser in outcome.losing_strategies:
                loser_id = loser.approach_id
                strategy_total[loser_id] = strategy_total.get(loser_id, 0) + 1
                # No win recorded for losers

        # Build strategies sorted by win rate
        strategies: list[FixStrategy] = []
        for strategy_id, total in sorted(
            strategy_total.items(),
            key=lambda x: strategy_wins.get(x[0], 0) / x[1] if x[1] > 0 else 0,
            reverse=True,
        ):
            wins = strategy_wins.get(strategy_id, 0)
            win_rate = wins / total if total > 0 else 0
            strategies.append(
                FixStrategy(
                    approach_id=strategy_id,
                    description=f"Outcome-ledger winner (win rate: {win_rate:.0%})",
                    prompt_injection=f"KNOWN WINNING APPROACH (outcome history, win rate {win_rate:.0%}): Apply the fix strategy '{strategy_id}' which has historically succeeded for error class '{error_class}'.",
                ),
            )

        logger.info(f"Loaded {len(strategies)} winning strategies for error_class '{error_class}'")
        return strategies

    except Exception as e:
        logger.warning(f"Failed to load winning strategies from outcomes: {e}")
        return []


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixStrategy:
    """A single fix strategy for a CI failure.

    Attributes:
        approach_id: Unique identifier for this approach (e.g., "approach-001")
        description: Human-readable description of the approach
        prompt_injection: Text to inject into the agent prompt to guide this approach
    """

    approach_id: str
    description: str
    prompt_injection: str

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FixStrategy):
            return NotImplemented
        return self.approach_id == other.approach_id


@dataclass
class ParallelRetryResult:
    """Result of a parallel retry execution.

    Attributes:
        winner: The winning FixStrategy, or None if all failed
        sessions_spawned: Number of sessions spawned
        sessions_killed: Number of sessions killed (losers)
    """

    winner: FixStrategy | None
    sessions_spawned: int = 0
    sessions_killed: int = 0


class ParallelRetryError(Exception):
    """Raised when parallel retry encounters a fatal error."""

    pass


# ---------------------------------------------------------------------------
# CI Error Parsing
# ---------------------------------------------------------------------------


# Patterns that indicate a parseable CI failure
# Must have specific error indicators, not just generic "failed"
PARSEABLE_PATTERNS = [
    r"FAILED\s+[a-zA-Z]",  # Test failure with file (e.g., "FAILED test_file.py")
    r"AssertionError",  # Assertion failure
    r"SyntaxError",  # Syntax error
    r"TypeError",  # Type error
    r"ReferenceError",  # JS reference error
    r"NameError",  # Name error
    r"ImportError",  # Import error
    r"compilation error",  # Compilation failure
    r"Traceback",  # Stack trace
    r"(?m)^[Ee][Rr][Rr][Oo][Rr]:",  # Error at start of line (case-insensitive, multiline)
    r":\d+:\s+error:",  # File:line: error pattern
]


def is_parseable_ci_failure(ci_failure: str) -> bool:
    """Check if a CI failure message contains parseable error information.

    Args:
        ci_failure: The CI failure message to check

    Returns:
        True if the failure contains parseable error patterns
    """
    if not ci_failure or not ci_failure.strip():
        return False

    # Check for any parseable pattern (case-sensitive for most patterns)
    for pattern in PARSEABLE_PATTERNS:
        if re.search(pattern, ci_failure):
            return True

    # Also check for test failure pattern with actual test name
    if re.search(r"FAILED\s+\S+\.\w+::", ci_failure):
        return True

    return False


def parse_ci_error(ci_failure: str) -> dict | None:
    """Parse a CI failure message to extract structured information.

    Args:
        ci_failure: The CI failure message to parse

    Returns:
        Dictionary with extracted information, or None if unparseable
    """
    if not is_parseable_ci_failure(ci_failure):
        return None

    # If we got here, it's parseable - return the parsed dict
    result: dict = {
        "raw": ci_failure,
        "error_type": None,
        "test_name": None,
        "file": None,
        "line": None,
        "message": None,
    }

    # Extract test name from patterns like "FAILED test_file.py::test_name"
    test_match = re.search(r"FAILED\s+(?:.*?::)?([\w\.]+)", ci_failure)
    if test_match:
        result["test_name"] = test_match.group(1)

    # Extract file path - match "in file.py:42", "in path/file.py:42", or "at /path/file.py:42"
    file_match = re.search(r"(?:in|at)\s+(?:.*/)?([^\s/]+\.(?:py|js|ts|go|rs)):\d+", ci_failure)
    if file_match:
        result["file"] = file_match.group(1)

    # Extract line number
    line_match = re.search(r":(\d+)\s*$", ci_failure)
    if line_match:
        result["line"] = int(line_match.group(1))

    # Extract error type - order matters, more specific first
    for error_type in [
        "ImportError",
        "NameError",
        "TypeError",
        "SyntaxError",
        "ReferenceError",
        "AttributeError",
        "ValueError",
        "AssertionError",
        "KeyError",
        "RuntimeError",
        "Error",  # Generic "Error" should be last
    ]:
        if error_type in ci_failure:
            result["error_type"] = error_type
            break

    # Extract error message (text after error type or "Error:")
    msg_match = re.search(r"(?:AssertionError|SyntaxError|TypeError|ReferenceError|Error):\s*(.+?)(?:\n|$)", ci_failure)
    if msg_match:
        result["message"] = msg_match.group(1).strip()

    return result


def _to_kebab_case(s: str) -> str:
    """Convert a string to kebab-case.

    Args:
        s: Input string (e.g., "ImportError", "myError")

    Returns:
        Kebab-case string (e.g., "import-error", "my-error")
    """
    # Insert hyphen before uppercase letters, then lowercase everything
    result = ""
    for i, char in enumerate(s):
        if char.isupper() and i > 0:
            result += "-"
        result += char
    return result.lower()


def derive_error_class(ci_failure: str) -> str | None:
    """Derive an error class fingerprint from a CI failure message.

    Creates a stable error class identifier that can be used to look up
    known-winning patterns.

    Args:
        ci_failure: The CI failure message

    Returns:
        Error class string (e.g., "ci-failed:import-error") or None if unparseable
    """
    parsed = parse_ci_error(ci_failure)
    if not parsed:
        return None

    error_type = parsed.get("error_type")
    test_name = parsed.get("test_name")
    file_path = parsed.get("file")

    # Build error class from available information
    if error_type:
        # Use kebab-case error type for consistency (e.g., "ImportError" -> "import-error")
        error_class = f"ci-failed:{_to_kebab_case(error_type)}"
    elif test_name:
        # Fallback to test name if no error type
        error_class = f"ci-failed:test:{test_name}"
    else:
        # Use file if available
        if file_path:
            file_base = Path(file_path).stem
            error_class = f"ci-failed:file:{file_base}"
        else:
            return None

    return error_class


# ---------------------------------------------------------------------------
# Strategy Generation
# ---------------------------------------------------------------------------


def _generate_llm_strategies(ci_failure: str, diff: str, max_strategies: int) -> list[FixStrategy]:
    """Generate fix strategies using LLM (placeholder for actual LLM call).

    In production, this would call the LLM with the error context to generate
    distinct, non-overlapping fix strategies. For now, returns deterministic
    fallback strategies.

    Args:
        ci_failure: The CI failure message
        diff: The diff/context for the change
        max_strategies: Maximum number of strategies to generate

    Returns:
        List of FixStrategy objects
    """
    # Parse the CI error for context
    parsed = parse_ci_error(ci_failure)

    # Generate strategies based on error type
    strategies: list[FixStrategy] = []

    # Strategy templates based on error patterns
    if parsed and parsed.get("error_type") == "AssertionError":
        strategies.extend([
            FixStrategy(
                approach_id="approach-001",
                description="Fix test expectation or assertion logic",
                prompt_injection="The CI failed with an AssertionError. Consider that the expected value in the test may be wrong, or the test assertion itself may be incorrect. Review the assertion logic and determine if it matches the intended behavior.",
            ),
            FixStrategy(
                approach_id="approach-002",
                description="Fix implementation to produce correct output",
                prompt_injection="The CI failed with an AssertionError. The implementation may not be producing the correct output. Trace through the code to find where the value diverges from expectations and fix the underlying implementation.",
            ),
            FixStrategy(
                approach_id="approach-003",
                description="Debug test and implementation state",
                prompt_injection="The CI failed with an AssertionError. Add debug output to examine the actual vs expected values at the assertion point. This will reveal whether the bug is in the implementation or in the test expectations.",
            ),
        ])

    elif parsed and parsed.get("error_type") in ("SyntaxError", "TypeError", "ReferenceError"):
        strategies.extend([
            FixStrategy(
                approach_id="approach-001",
                description=f"Correct code error at failure location",
                prompt_injection=f"The CI failed with {parsed.get('error_type')}. Review the error message and fix the issue at the specific location mentioned in the error trace.",
            ),
            FixStrategy(
                approach_id="approach-002",
                description="Analyze diff for regression source",
                prompt_injection=f"The CI failed with {parsed.get('error_type')}. The error may have been introduced by recent changes. Review the diff and identify which change introduced the problem.",
            ),
            FixStrategy(
                approach_id="approach-003",
                description="Trace value flow to find type mismatch",
                prompt_injection=f"The CI failed with {parsed.get('error_type')}. Trace the value flow through your code to understand where the type mismatch occurs and fix it at the source.",
            ),
        ])

    elif parsed and parsed.get("test_name"):
        strategies.extend([
            FixStrategy(
                approach_id="approach-001",
                description=f"Investigate test correctness",
                prompt_injection=f"The test '{parsed.get('test_name')}' is failing. Analyze whether the failure indicates a legitimate bug in the implementation or if the test expectations themselves are wrong.",
            ),
            FixStrategy(
                approach_id="approach-002",
                description=f"Debug implementation to find root cause",
                prompt_injection=f"The test '{parsed.get('test_name')}' is failing. Trace through the implementation to find the root cause - do not simply patch the test to make it pass.",
            ),
            FixStrategy(
                approach_id="approach-003",
                description=f"Add logging and reproduce failure locally",
                prompt_injection=f"The test '{parsed.get('test_name')}' is failing. Run the test locally with verbose output to capture state at failure, then fix the underlying issue.",
            ),
        ])

    else:
        # Generic failure - provide general strategies
        strategies.extend([
            FixStrategy(
                approach_id="approach-001",
                description="Analyze CI failure and fix root cause",
                prompt_injection="The CI failed. Analyze the error message to understand what went wrong, then fix the root cause in the implementation. Do not just patch tests.",
            ),
            FixStrategy(
                approach_id="approach-002",
                description="Review recent changes for CI failure",
                prompt_injection="The CI failed. Review the recent diff to identify which changes may have caused this regression and revert or fix them.",
            ),
            FixStrategy(
                approach_id="approach-003",
                description="Re-run failing CI component with debug output",
                prompt_injection="The CI failed. Run the failing component locally with debug output to get more information about the failure, then fix the issue.",
            ),
        ])

    # Limit to max_strategies
    return strategies[:max_strategies]


def generate_fix_strategies(
    ci_failure: str,
    diff: str,
    max_strategies: int = 3,
    known_patterns: list[SynthesizedPattern] | None = None,
    error_class: str | None = None,
) -> list[FixStrategy]:
    """Generate fix strategies for a CI failure.

    Uses known-winning patterns when available, otherwise generates new strategies
    via LLM (or deterministic fallback). Falls back to single enriched retry
    if the error is not parseable.

    Args:
        ci_failure: The CI failure message
        diff: The diff/context for the change
        max_strategies: Maximum number of strategies to generate (default 3)
        known_patterns: Pre-loaded patterns (if None, loads from patterns.json)
        error_class: Optional error class fingerprint (if None, derived from ci_failure)

    Returns:
        List of FixStrategy objects
    """
    # Check if CI failure is parseable
    if not is_parseable_ci_failure(ci_failure):
        # Fallback: single strategy for unparseable errors
        return [
            FixStrategy(
                approach_id="fallback-001",
                description="Retry with enriched prompt",
                prompt_injection=f"Previous attempt failed with CI error. Please analyze and fix. Error: {ci_failure[:200]}",
            ),
        ]

    # Derive error_class from ci_failure if not provided
    if error_class is None:
        error_class = derive_error_class(ci_failure)

    # First: Try to load winning strategies from outcome ledger (most recent data)
    outcome_strategies: list[FixStrategy] = []
    if error_class:
        outcome_strategies = load_winning_strategies(error_class)

    # Second: Try to use known-winning patterns from pattern synthesizer
    if error_class and known_patterns is None:
        # Load patterns from file
        synthesizer = PatternSynthesizer()
        known_patterns = synthesizer.load_patterns()

    # Check if we have a known pattern for this error class
    if known_patterns and error_class:
        for pattern in known_patterns:
            if pattern.error_class == error_class:
                # Use the known winning strategy as the first (preferred) approach
                logger.info(f"Using known-winning strategy '{pattern.winning_strategy}' for error class '{error_class}'")

                # Generate fallback strategies for diversity
                fallback_strategies = _generate_llm_strategies(ci_failure, diff, max_strategies - 1)

                # Create the known-winning strategy
                known_strategy = FixStrategy(
                    approach_id=pattern.winning_strategy,
                    description=f"Known-winning strategy (win rate: {pattern.win_rate:.0%})",
                    prompt_injection=f"KNOWN WINNING APPROACH (success rate {pattern.win_rate:.0%}): Apply the fix strategy '{pattern.winning_strategy}' which has historically succeeded for this error class.",
                )

                # Dedupe: exclude fallback strategies that have the same approach_id as known
                known_id = known_strategy.approach_id
                unique_fallbacks = [s for s in fallback_strategies if s.approach_id != known_id]

                return [known_strategy] + unique_fallbacks[:max_strategies - 1]

    # Generate strategies via LLM (or deterministic fallback)
    speculative_strategies = _generate_llm_strategies(ci_failure, diff, max_strategies)

    # Prepend outcome-ledger strategies if available
    if outcome_strategies:
        # Dedupe: exclude speculative strategies that have the same approach_id as outcome strategies
        outcome_ids = {s.approach_id for s in outcome_strategies}
        unique_speculative = [s for s in speculative_strategies if s.approach_id not in outcome_ids]
        
        # Combine: outcome strategies first (highest confidence), then speculative
        combined = outcome_strategies + unique_speculative
        return combined[:max_strategies]

    # Ensure we have unique approach_ids
    seen_ids: set[str] = set()
    unique_strategies: list[FixStrategy] = []

    for s in speculative_strategies:
        if s.approach_id not in seen_ids:
            seen_ids.add(s.approach_id)
            unique_strategies.append(s)

    return unique_strategies


# ---------------------------------------------------------------------------
# CI Status Checking
# ---------------------------------------------------------------------------


class AOCli(Protocol):
    """Protocol for AO CLI operations."""

    def spawn(self, project: str, issue: str, *, branch: str | None = None) -> str:
        """Spawn a new AO session."""
        ...

    def kill(self, session_id: str) -> None:
        """Kill an AO session."""
        ...

    def send(self, session_id: str, message: str) -> None:
        """Send a message to a session."""
        ...

    def list(self, project: str | None = None) -> list[dict]:
        """List active sessions."""
        ...


def check_ci_status(session_id: str, *, bead_id: str | None = None, repo: str | None = None, branch: str | None = None) -> dict:
    """Check the CI status for an AO session.

    Queries GitHub Actions for the session's branch status via the gh CLI.

    Args:
        session_id: The session ID to check
        bead_id: The bead ID that originated this session (used for registry lookup).
                 If omitted the registry lookup is skipped and git is used directly.
        repo: The repository in owner/repo format. If provided, skips extraction.
        branch: The branch name. If provided, skips registry/git lookup.

    Returns:
        Dict with 'status' ('green', 'red', 'pending') and 'session_id'
    """
    worktree_path: str | None = None

    # Use provided repo/branch if available, otherwise look up via bead_id
    if repo is None or branch is None:
        # Look up branch/worktree via bead_id (registry key), not session_id
        if bead_id is not None:
            mapping = get_mapping(bead_id=bead_id)
            if mapping:
                if branch is None:
                    branch = mapping.branch
                worktree_path = mapping.worktree_path

    # Determine repo if not provided: prefer git metadata over fragile path parsing
    if repo is None:
        repo = _extract_repo_from_git(worktree_path) or _extract_repo_from_worktree(worktree_path)
        if not repo:
            logger.debug(f"Could not extract repo from worktree_path {worktree_path}, returning pending")
            return {"status": "pending", "session_id": session_id}

    # Fall back to current git branch if not obtained from registry
    if not branch:
        try:
            r = subprocess.run(
                ["git", "-C", worktree_path or ".", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            branch = r.stdout.strip() or None
        except Exception:
            pass

    if not branch:
        logger.debug(f"Could not determine branch for session {session_id}, returning pending")
        return {"status": "pending", "session_id": session_id}

    # Query GH Actions for the most recent run on this branch
    try:
        result = subprocess.run(
            [
                "gh", "run", "list",
                "--branch", branch,
                "--repo", repo,
                "--limit", "1",
                "--json", "status,conclusion",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            logger.warning(f"gh run list failed: {result.stderr}")
            return {"status": "pending", "session_id": session_id}

        runs = json.loads(result.stdout)
        if not runs:
            # No runs yet
            return {"status": "pending", "session_id": session_id}

        latest = runs[0]
        status = latest.get("status", "")
        conclusion = latest.get("conclusion", "")

        # Map status:
        # - completed + success -> green
        # - completed + failure/other -> red
        # - in_progress or queued -> in_progress (CI actively running)
        # - any other status (waiting, etc.) -> pending
        if status == "completed":
            if conclusion == "success":
                return {"status": "green", "session_id": session_id}
            else:
                return {"status": "red", "session_id": session_id}
        elif status in ("in_progress", "queued"):
            return {"status": "in_progress", "session_id": session_id}
        else:
            return {"status": "pending", "session_id": session_id}

    except subprocess.TimeoutExpired:
        logger.warning(f"gh run list timed out for session {session_id}")
        return {"status": "pending", "session_id": session_id}
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse gh run list output: {e}")
        return {"status": "pending", "session_id": session_id}
    except Exception as e:
        logger.warning(f"Error checking CI status: {e}")
        return {"status": "pending", "session_id": session_id}


def _extract_repo_from_git(worktree_path: str | None) -> str | None:
    """Extract owner/repo by running `git remote get-url origin` in the worktree."""
    cwd = worktree_path or "."
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        url = r.stdout.strip()
        # https://github.com/owner/repo.git  or  git@github.com:owner/repo.git
        for pattern in [
            r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$",
        ]:
            m = re.search(pattern, url)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def _extract_repo_from_worktree(worktree_path: str | None) -> str | None:
    """Extract owner/repo from worktree path.

    Path format: /path/to/worktrees/owner-repo/branch
    Returns: owner/repo

    Args:
        worktree_path: The worktree path

    Returns:
        Owner/repo string or None if extraction fails
    """
    if not worktree_path:
        return None

    path = Path(worktree_path)
    parts = path.parts

    # Find 'worktrees' in the path
    try:
        worktrees_idx = parts.index("worktrees")
    except ValueError:
        return None

    # Expect /…/worktrees/<owner>-<repo>/… — need at least two segments after 'worktrees'
    if worktrees_idx + 2 >= len(parts):
        return None

    owner_repo_segment = parts[worktrees_idx + 1]
    # Segment should be owner-repo; split on LAST '-' to handle hyphens in owner (e.g., "my-org/repo")
    if "-" not in owner_repo_segment:
        return None
    owner, _, repo_name = owner_repo_segment.rpartition("-")
    return f"{owner}/{repo_name}"


# ---------------------------------------------------------------------------
# Parallel Retry Execution
# ---------------------------------------------------------------------------


def execute_parallel_retry(
    strategies: list[FixStrategy],
    project: str,
    issue: str,
    cli: AOCli,
    *,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    ci_check_interval: int = DEFAULT_CI_CHECK_INTERVAL,
    ci_timeout: int = DEFAULT_CI_TIMEOUT,
    error_class: str | None = None,
    session_id: str | None = None,
) -> ParallelRetryResult:
    """Execute parallel retry with multiple strategies.

    Spawns one AO session per strategy, waits for CI status, and returns
    the result. First session to achieve CI green wins; kills remaining.

    Args:
        strategies: List of FixStrategy objects to try
        project: Project identifier (e.g., 'owner/repo')
        issue: Issue/task description
        cli: AO CLI wrapper
        max_parallel: Maximum parallel sessions (default 3)
        ci_check_interval: Seconds between CI checks (default 30)
        ci_timeout: Max seconds to wait for CI (default 300)
        error_class: Optional error class fingerprint for outcome recording
        session_id: Optional session identifier for traceability

    Returns:
        ParallelRetryResult with winner, sessions_spawned, sessions_killed

    Raises:
        ParallelRetryError: If spawn fails for all strategies
    """
    # Preserve original session_id for outcome recording (avoid shadowing in loops)
    parent_session_id = session_id
    
    if not strategies:
        return ParallelRetryResult(winner=None, sessions_spawned=0, sessions_killed=0)

    # If only one strategy or max_parallel=1, do single retry
    if len(strategies) == 1 or max_parallel == 1:
        strategy = strategies[0]
        try:
            # Spawn session with strategy injection
            session_id = cli.spawn(project, f"{issue}\n\n{strategy.prompt_injection}")
            # Wait for CI (simplified - just return the strategy as winner)
            return ParallelRetryResult(winner=strategy, sessions_spawned=1, sessions_killed=0)
        except Exception as e:
            raise ParallelRetryError(f"Spawn failed: {e}") from e

    # Parallel execution
    spawned_sessions: list[tuple[str, FixStrategy, str]] = []
    sessions_killed = 0

    # Spawn all strategies
    for strategy in strategies[:max_parallel]:
        try:
            # Create unique branch for each strategy
            branch = f"fix/{strategy.approach_id}-{uuid.uuid4().hex[:8]}"
            spawned_session_id = cli.spawn(
                project,
                f"{issue}\n\n{strategy.prompt_injection}",
                branch=branch,
            )
            spawned_sessions.append((spawned_session_id, strategy, branch))
        except Exception as e:
            # Log but continue with other strategies
            continue

    if not spawned_sessions:
        raise ParallelRetryError("All spawn attempts failed")

    # Wait for CI status - first green wins; re-poll pending sessions each round
    winner: FixStrategy | None = None
    start_time = time.time()

    # Track sessions still pending (not yet green or red)
    pending_sessions = set(spawned_session_id for spawned_session_id, _, _ in spawned_sessions)

    while time.time() - start_time < ci_timeout and pending_sessions:
        for spawned_session_id, strategy, branch in spawned_sessions:
            # Skip sessions that are no longer pending
            if spawned_session_id not in pending_sessions:
                continue

            elapsed = time.time() - start_time
            if elapsed > ci_timeout:
                break

            # Check CI status
            try:
                ci_result = check_ci_status(spawned_session_id, repo=project, branch=branch)
                status = ci_result.get("status", "pending")
            except Exception:
                status = "pending"

            if status == "green":
                winner = strategy
                # Remove from pending
                pending_sessions.discard(spawned_session_id)
                # Kill remaining sessions
                for other_session, _, _ in spawned_sessions:
                    if other_session != spawned_session_id:
                        try:
                            cli.kill(other_session)
                            sessions_killed += 1
                        except Exception:
                            # Non-blocking - continue
                            pass
                break
            elif status == "red":
                # This strategy failed permanently, remove from pending
                pending_sessions.discard(spawned_session_id)
                continue
            # If pending, we'll check again in the next iteration

        # If we found a winner, exit outer loop
        if winner is not None:
            break

        # Sleep before next check cycle (only if we haven't timed out and have pending sessions)
        if time.time() - start_time < ci_timeout and pending_sessions:
            time.sleep(ci_check_interval)

    # If no winner found, return None
    if winner is None:
        # All strategies failed - record but don't kill (sessions already dead/completed)
        return ParallelRetryResult(
            winner=None,
            sessions_spawned=len(spawned_sessions),
            sessions_killed=sessions_killed,
        )

    # Determine losers for outcome recording
    losers = [s for _, s, _ in spawned_sessions if s != winner]

    # Record outcome if error_class provided
    if error_class:
        try:
            recorder = OutcomeRecorder()
            recorder.record_outcome(
                error_class=error_class,
                winner=winner,
                losers=losers,
                session_id=parent_session_id,
            )
            logger.info(f"Recorded outcome for error_class '{error_class}': winner={winner.approach_id}")
        except Exception as e:
            # Non-blocking - outcome recording should not fail the main operation
            logger.warning(f"Failed to record outcome: {e}")

    return ParallelRetryResult(
        winner=winner,
        sessions_spawned=len(spawned_sessions),
        sessions_killed=sessions_killed,
    )


# ---------------------------------------------------------------------------
# Integration with Escalation Router
# ---------------------------------------------------------------------------


# This function would be called by action_executor.py to execute ParallelRetryAction
def execute_from_action(
    strategies: list[FixStrategy],
    project: str,
    issue: str,
    cli: AOCli,
) -> ParallelRetryResult:
    """Execute parallel retry from escalation action.

    This is the entry point called by action_executor.py when handling
    a ParallelRetryAction from the escalation router.

    Args:
        strategies: Fix strategies to try
        project: Project identifier
        issue: Issue description
        cli: AO CLI wrapper

    Returns:
        ParallelRetryResult
    """
    return execute_parallel_retry(strategies, project, issue, cli)
