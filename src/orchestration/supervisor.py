"""mctrl supervisor daemon.

Runs reconcile_registry_once on a fixed interval, watching bead/session
mappings and emitting task_finished / task_needs_human notifications when
agent sessions exit.

Usage:
    python -m orchestration.supervisor [--interval 30] [--once]

Environment (loaded from ~/.openclaw/set-slack-env.sh if not already set):
    SLACK_BOT_TOKEN or OPENCLAW_SLACK_BOT_TOKEN   — Slack bot token
    OPENCLAW_NOTIFY_AGENT                          — OpenClaw MCP agent name (optional)
    MCTRL_REGISTRY_PATHS                           — comma- or colon-separated extra registry paths
    MCTRL_ARCHIVE_AFTER_DAYS                       — days before archiving terminal entries (default: 7)
    MCTRL_OUTBOX_ALERT_THRESHOLD                   — pending count before alerting (default: 10)
    MCTRL_OUTBOX_AGE_ALERT_SECONDS                 — oldest-entry age before alerting (default: 3600)
    MCTRL_OUTBOX_ALERT_COOLDOWN_SECONDS            — min seconds between outbox alerts (default: 3600)
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("mctrl.supervisor")


def _parse_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


REGISTRY_PATH = os.environ.get(
    "MCTRL_REGISTRY_PATH", ".tracking/bead_session_registry.jsonl"
)
OUTBOX_PATH = os.environ.get(
    "MCTRL_OUTBOX_PATH", ".messages/outbox.jsonl"
)
DEAD_LETTER_PATH = os.environ.get(
    "MCTRL_DEAD_LETTER_PATH", ".messages/outbox_dead_letter.jsonl"
)
ARCHIVE_AFTER_DAYS: int = _parse_int_env("MCTRL_ARCHIVE_AFTER_DAYS", 7)
OUTBOX_ALERT_THRESHOLD: int = _parse_int_env("MCTRL_OUTBOX_ALERT_THRESHOLD", 10)
OUTBOX_AGE_ALERT_SECONDS: int = _parse_int_env("MCTRL_OUTBOX_AGE_ALERT_SECONDS", 3600)
OUTBOX_ALERT_COOLDOWN_SECONDS: int = _parse_int_env("MCTRL_OUTBOX_ALERT_COOLDOWN_SECONDS", 3600)

_running = True
_last_outbox_alert_at: float | None = None


def _handle_signal(sig: int, _frame: object) -> None:
    global _running
    logger.info("Signal %s received — shutting down after current tick", sig)
    _running = False


def _ensure_slack_token() -> None:
    """Load SLACK_BOT_TOKEN from set-slack-env.sh if not already set."""
    if os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("OPENCLAW_SLACK_BOT_TOKEN"):
        return
    script = os.path.expanduser("~/.openclaw/set-slack-env.sh")
    if not os.path.exists(script):
        logger.warning("No SLACK_BOT_TOKEN and %s not found — Slack notifications disabled", script)
        return
    try:
        result = subprocess.run(
            ["bash", "-c", f"source {script} && env"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.startswith("SLACK_BOT_TOKEN="):
                os.environ["SLACK_BOT_TOKEN"] = line.split("=", 1)[1]
                logger.info("Loaded SLACK_BOT_TOKEN from set-slack-env.sh")
            elif line.startswith("OPENCLAW_SLACK_BOT_TOKEN="):
                os.environ["OPENCLAW_SLACK_BOT_TOKEN"] = line.split("=", 1)[1]
                logger.info("Loaded OPENCLAW_SLACK_BOT_TOKEN from set-slack-env.sh")
    except Exception as exc:
        logger.warning("Could not load SLACK_BOT_TOKEN: %s", exc)


def maybe_alert_outbox_health(
    *,
    pending_count: int,
    dead_letter_count: int,
    oldest_age_seconds: int | None,
    notify_fn: Callable[[dict[str, Any]], bool],
    threshold: int = 10,
    age_threshold: int = 3600,
    cooldown_seconds: int = 3600,
    outbox_path: str = ".messages/outbox.jsonl",
    dead_letter_path: str = ".messages/outbox_dead_letter.jsonl",
) -> bool:
    """Fire an outbox health alert if thresholds are breached and cooldown has elapsed.

    Returns True if an alert was fired.
    """
    global _last_outbox_alert_at

    should_alert = (
        pending_count >= threshold
        or dead_letter_count > 0
        or (oldest_age_seconds is not None and oldest_age_seconds >= age_threshold)
    )
    if not should_alert:
        return False

    now = time.monotonic()
    if _last_outbox_alert_at is not None and (now - _last_outbox_alert_at) < cooldown_seconds:
        return False

    payload = {
        "event": "outbox_health_alert",
        "pending_count": pending_count,
        "dead_letter_count": dead_letter_count,
        "oldest_age_seconds": oldest_age_seconds,
        "outbox_path": outbox_path,
        "dead_letter_path": dead_letter_path,
    }
    fired = notify_fn(payload)
    if fired:
        _last_outbox_alert_at = now
    return fired


def _registry_paths_to_reconcile(
    *,
    registry_path: str,
    registry_paths_env: str,
    outbox_path: str,
) -> list[str]:
    """Return deduplicated list of registry paths to reconcile.

    Sources (in priority order):
    1. The explicit registry_path argument (always included, resolved to absolute).
    2. Paths listed in registry_paths_env (comma and/or colon separated; colon wins when both appear).
    3. Auto-discovered sibling registries: repos sharing the same .messages symlink target.
    """
    seen: set[str] = set()
    paths: list[str] = []

    def _add(p: str) -> None:
        resolved = str(Path(p).resolve())
        if resolved not in seen:
            seen.add(resolved)
            paths.append(resolved)

    _add(registry_path)

    # Explicit extra paths from env (supports comma or colon as separator)
    if registry_paths_env:
        # Prefer colon when present so colon-separated values still parse
        # correctly even if an individual path contains a comma.
        sep = ":" if ":" in registry_paths_env else ","
        for raw in registry_paths_env.split(sep):
            raw = raw.strip()
            if raw:
                _add(raw)

    # Auto-discover sibling repos that share the same .messages directory
    try:
        outbox_real = str(Path(outbox_path).resolve().parent)
        cwd = Path(".").resolve()
        parent = cwd.parent
        if parent.exists():
            for sibling in parent.iterdir():
                if not sibling.is_dir() or sibling == cwd:
                    continue
                messages_link = sibling / ".messages"
                if not messages_link.exists():
                    continue
                try:
                    sibling_messages_real = str(messages_link.resolve())
                except OSError:
                    continue
                if sibling_messages_real == outbox_real:
                    candidate = sibling / ".tracking" / "bead_session_registry.jsonl"
                    if candidate.exists():
                        _add(str(candidate))
    except Exception:
        pass

    return paths


# ORCH-7l5 fix: Path to mem0 extraction script
MEM0_EXTRACT_SCRIPT = Path.home() / ".openclaw" / "scripts" / "mem0_extract_facts.py"

# Rate limit for extraction triggers (don't spam - run at most once per interval)
_last_extraction_trigger: float = 0
EXTRACTION_INTERVAL_SECONDS = 300  # 5 min minimum between triggers


def _trigger_mem0_extraction_async() -> None:
    """Fire-and-forget mem0 extraction for recent sessions.

    ORCH-7l5 fix: Uses subprocess.Popen with no wait() to avoid blocking
    the supervisor loop if extraction hangs (qdrant timeout, LLM rate limit).

    Uses --since 10m to catch any sessions that ended since last run.
    The extraction script handles deduplication via extraction-state.json.
    """
    global _last_extraction_trigger

    if not MEM0_EXTRACT_SCRIPT.exists():
        logger.debug("mem0 extraction script not found at %s", MEM0_EXTRACT_SCRIPT)
        return

    # Rate limit: don't trigger more than once per interval
    now = time.monotonic()
    if now - _last_extraction_trigger < EXTRACTION_INTERVAL_SECONDS:
        return
    _last_extraction_trigger = now

    try:
        # Fire-and-forget: don't wait for completion
        # Use --since 10m to catch recent sessions (dedup handled by state file)
        subprocess.Popen(
            [
                sys.executable,  # Use current python (has mem0 in path)
                str(MEM0_EXTRACT_SCRIPT),
                "--since",
                "10",  # Scan last 10 minutes
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Detach from supervisor
        )
        logger.info("Triggered mem0 extraction for recent sessions")
    except Exception as exc:
        logger.warning("Failed to trigger mem0 extraction: %s", exc)


def run_once() -> list[dict]:
    """Reconcile all registries, archive terminal entries, check outbox health."""
    import orchestration.reconciliation as reconciliation_mod
    import orchestration.session_registry as session_registry_mod
    import orchestration.openclaw_notifier as notifier_mod

    registry_paths_env = os.environ.get("MCTRL_REGISTRY_PATHS", "")
    paths = _registry_paths_to_reconcile(
        registry_path=REGISTRY_PATH,
        registry_paths_env=registry_paths_env,
        outbox_path=OUTBOX_PATH,
    )

    all_emitted: list[dict] = []
    for reg_path in paths:
        try:
            emitted = reconciliation_mod.reconcile_registry_once(
                registry_path=reg_path,
                outbox_path=OUTBOX_PATH,
                dead_letter_path=DEAD_LETTER_PATH,
            )
            all_emitted.extend(emitted)
        except Exception as exc:
            logger.error("reconcile_registry_once failed for %s: %s", reg_path, exc, exc_info=True)
            continue

        try:
            session_registry_mod.archive_terminal_mappings(
                registry_path=reg_path,
                archive_after_days=ARCHIVE_AFTER_DAYS,
            )
        except Exception as exc:
            logger.warning("archive_terminal_mappings failed for %s: %s", reg_path, exc)

    # ORCH-7l5 fix: Trigger mem0 extraction asynchronously (fire-and-forget)
    _trigger_mem0_extraction_async()

    # Check outbox health and maybe alert
    try:
        snapshot = notifier_mod.outbox_health_snapshot(
            outbox_path=OUTBOX_PATH,
            dead_letter_path=DEAD_LETTER_PATH,
        )
        maybe_alert_outbox_health(
            pending_count=snapshot.get("pending_count", 0),
            dead_letter_count=snapshot.get("dead_letter_count", 0),
            oldest_age_seconds=snapshot.get("oldest_age_seconds"),
            notify_fn=notifier_mod.notify_slack_outbox_alert,
            threshold=OUTBOX_ALERT_THRESHOLD,
            age_threshold=OUTBOX_AGE_ALERT_SECONDS,
            cooldown_seconds=OUTBOX_ALERT_COOLDOWN_SECONDS,
            outbox_path=OUTBOX_PATH,
            dead_letter_path=DEAD_LETTER_PATH,
        )
    except Exception as exc:
        logger.warning("Outbox health check failed: %s", exc)

    return all_emitted


def main() -> None:
    global _running
    _running = True  # Reset in case main() is called again in the same process (e.g. tests)
    parser = argparse.ArgumentParser(description="mctrl supervisor loop")
    parser.add_argument("--interval", type=int, default=30, help="Poll interval in seconds (default: 30)")
    parser.add_argument("--once", action="store_true", help="Run once and exit (useful for cron)")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _ensure_slack_token()

    logger.info(
        "mctrl supervisor starting (interval=%ds, registry=%s, outbox=%s)",
        args.interval, REGISTRY_PATH, OUTBOX_PATH,
    )

    while _running:
        try:
            emitted = run_once()
            if emitted:
                logger.info("Emitted %d event(s): %s", len(emitted), [e["event"] for e in emitted])
        except Exception as exc:
            logger.error("run_once failed: %s", exc, exc_info=True)

        if args.once:
            break

        # Sleep in short chunks so SIGTERM is handled promptly
        deadline = time.monotonic() + args.interval
        while _running and time.monotonic() < deadline:
            time.sleep(1)

    logger.info("mctrl supervisor stopped")


if __name__ == "__main__":
    main()
