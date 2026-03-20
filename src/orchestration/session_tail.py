"""Session tail — view live output of async tmux agent sessions.

Provides two subcommands:

  logs <session>             One-shot: capture current pane output and exit.
  logs <session> --follow    Follow mode: stream new output until Ctrl-C.
  tail <session>             Alias for ``logs --follow``.

Usage::

    python -m orchestration.session_tail logs my-agent
    python -m orchestration.session_tail logs --follow my-agent
    python -m orchestration.session_tail tail my-agent
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def check_session_exists(session_name: str) -> bool:
    """Return True if the named tmux session is running."""
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def capture_pane(session_name: str, history_lines: int = 5000) -> str:
    """Capture current pane content from a tmux session.

    Args:
        session_name: tmux session name.
        history_lines: Number of scroll-back lines to include.

    Returns:
        Captured output as a string.

    Raises:
        subprocess.SubprocessError: If tmux capture-pane fails.
    """
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", session_name, "-p", "-S", f"-{history_lines}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise subprocess.SubprocessError(
            f"tmux capture-pane failed for '{session_name}': {result.stderr.strip()}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# Tail logic
# ---------------------------------------------------------------------------


def tail_session(
    session_name: str,
    follow: bool = False,
    lines: int = 50,
    poll_interval: float = 1.0,
) -> int:
    """Show output from a tmux agent session.

    Args:
        session_name: tmux session name.
        follow: If True, stream output continuously until session ends or Ctrl-C.
        lines: Number of lines to show in one-shot mode.
        poll_interval: Seconds between polls in follow mode.

    Returns:
        0 on success, 1 on error.
    """
    if not check_session_exists(session_name):
        print(
            f"ERROR: session '{session_name}' not found",
            file=sys.stderr,
        )
        print(
            "Run 'tmux list-sessions' to see active sessions.",
            file=sys.stderr,
        )
        return 1

    try:
        content = capture_pane(session_name)
    except (subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not follow:
        # One-shot: print last N lines and exit.
        all_lines = content.rstrip("\n").split("\n")
        output = all_lines[-lines:] if len(all_lines) > lines else all_lines
        print("\n".join(output))
        return 0

    # Follow mode: print only the requested initial tail, then stream appended lines.
    prev_content = content
    prev_lines = content.rstrip("\n").split("\n") if content.rstrip("\n") else []
    initial = prev_lines[-lines:] if len(prev_lines) > lines else prev_lines
    if initial:
        print("\n".join(initial), flush=True)

    try:
        while True:
            time.sleep(poll_interval)

            try:
                new_content = capture_pane(session_name)
            except (subprocess.SubprocessError, subprocess.TimeoutExpired):
                if not check_session_exists(session_name):
                    print(f"\n[session '{session_name}' ended]", flush=True)
                    return 0
                continue

            if new_content != prev_content:
                new_lines = (
                    new_content.rstrip("\n").split("\n")
                    if new_content.rstrip("\n")
                    else []
                )
                if len(new_lines) > len(prev_lines):
                    # Append lines added at the end.
                    added = new_lines[len(prev_lines) :]
                    if added:
                        print("\n".join(added), flush=True)
                else:
                    # Pane was cleared or rotated — reprint in full.
                    print(new_content, end="", flush=True)
                prev_content = new_content
                prev_lines = new_lines

    except KeyboardInterrupt:
        print("\n[interrupted]", flush=True)
        return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    """Argparse type for positive integer values."""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("--lines must be > 0")
    return parsed


def _positive_float(value: str) -> float:
    """Argparse type for positive float values."""
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("--interval must be > 0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser.

    Exposed as a public function so tests can parse arguments directly.
    """
    parser = argparse.ArgumentParser(
        prog="python -m orchestration.session_tail",
        description="View live output of async tmux agent sessions",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # 'logs' subcommand
    logs_parser = subparsers.add_parser(
        "logs",
        help="Show captured output from a tmux session (default: last 50 lines)",
    )
    logs_parser.add_argument("session", help="tmux session name")
    logs_parser.add_argument(
        "--follow",
        "-f",
        action="store_true",
        help="Stream output continuously until session ends or Ctrl-C",
    )
    logs_parser.add_argument(
        "--lines",
        "-n",
        type=_positive_int,
        default=50,
        help="Number of lines to show in one-shot mode (default: 50)",
    )
    logs_parser.add_argument(
        "--interval",
        type=_positive_float,
        default=1.0,
        help="Poll interval in seconds for follow mode (default: 1.0)",
    )

    # 'tail' subcommand — alias for logs --follow
    tail_parser = subparsers.add_parser(
        "tail",
        help="Follow output from a tmux session (alias for 'logs --follow')",
    )
    tail_parser.add_argument("session", help="tmux session name")
    tail_parser.add_argument(
        "--lines",
        "-n",
        type=_positive_int,
        default=50,
        help="Number of initial lines to show (default: 50)",
    )
    tail_parser.add_argument(
        "--interval",
        type=_positive_float,
        default=1.0,
        help="Poll interval in seconds (default: 1.0)",
    )

    return parser


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "tail":
        code = tail_session(
            session_name=args.session,
            follow=True,
            lines=args.lines,
            poll_interval=args.interval,
        )
    else:  # logs
        code = tail_session(
            session_name=args.session,
            follow=args.follow,
            lines=args.lines,
            poll_interval=args.interval,
        )

    sys.exit(code)
