#!/usr/bin/env python3
"""Webhook daemon: supervised ingress+worker process.

Starts:
- webhook_ingress HTTP server on port 19888
- webhook_worker polling loop

Supervised: restart on exit, logs to ~/.openclaw/logs/webhook_daemon.log
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestration.webhook_ingress import WebhookIngress, WebhookStore
from orchestration.webhook_worker import RemediationWorker
from orchestration.webhook_queue import RemediationQueue, normalize_event, QueueStatus
from orchestration.escalation_handler import (
    handle_escalation,
    load_escalation_policy,
    EscalationHandlerError,
)
from orchestration.action_executor import execute_action

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_PORT = 19888
DEFAULT_DB_PATH = str(Path.home() / ".openclaw" / "webhook_queue.db")
DEFAULT_LOG_DIR = Path.home() / ".openclaw" / "logs"

# Polling interval for worker
WORKER_POLL_INTERVAL = 5.0  # seconds


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(log_dir: Path) -> logging.Logger:
    """Configure logging to file and console."""
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "webhook_daemon.log"
    err_file = log_dir / "webhook_daemon.err.log"

    logger = logging.getLogger("webhook_daemon")
    logger.setLevel(logging.INFO)

    # File handler (all logs)
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh_format = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    fh.setFormatter(fh_format)

    # Error file handler
    eh = logging.FileHandler(err_file)
    eh.setLevel(logging.ERROR)
    eh.setFormatter(fh_format)

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fh_format)

    logger.addHandler(fh)
    logger.addHandler(eh)
    logger.addHandler(ch)

    return logger


# ---------------------------------------------------------------------------
# Daemon components
# ---------------------------------------------------------------------------


class WebhookDaemon:
    """Combined ingress + worker daemon.

    Starts the HTTP ingress server and a background thread that:
    1. Polls the ingress store for unprocessed webhooks
    2. Normalizes events and enqueues to remediation queue
    3. Processes events via RemediationWorker
    """

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        host: str = "0.0.0.0",
        db_path: str = DEFAULT_DB_PATH,
        log_dir: Path = DEFAULT_LOG_DIR,
    ) -> None:
        self._port = port
        self._host = host
        self._db_path = db_path
        self._log_dir = log_dir
        self._logger = _setup_logging(log_dir)

        # Components
        self._ingress: WebhookIngress | None = None
        self._store: WebhookStore | None = None
        self._queue: RemediationQueue | None = None
        self._worker: RemediationWorker | None = None

        # State
        self._running = False
        self._shutdown_event = threading.Event()

        # AO webhook handlers (for escalations)
        # Pass None to execute_action — it creates RealAOCli/RealNotifier internally
        self._ao_cli = None
        self._ao_notifier = None
        self._escalation_policy = load_escalation_policy()
        self._action_log_path = str(log_dir / "escalation_actions.jsonl")

    def _init_components(self) -> None:
        """Initialize all components."""
        self._logger.info(f"Initializing webhook daemon (port={self._port}, db={self._db_path})")

        # Ingress (HTTP server)
        self._store = WebhookStore(db_path=self._db_path)
        self._store.init_schema()
        self._ingress = WebhookIngress(
            host=self._host,
            port=self._port,
            db_path=self._db_path,
        )

        # Remediation queue
        self._queue = RemediationQueue(db_path=self._db_path)
        self._queue.init_schema()

        # Worker (for PR remediation)
        self._worker = RemediationWorker(queue=self._queue)

        self._logger.info("Components initialized")

    def _poll_and_process(self) -> None:
        """Poll ingress store, normalize events, process via worker."""
        if not self._store or not self._queue or not self._worker:
            return

        try:
            # Get unprocessed webhooks from ingress store
            records = self._store.get_unprocessed(limit=10)

            for record in records:
                try:
                    # Parse payload
                    import json
                    payload = json.loads(record.payload)

                    # Check if this is an AO webhook (tagged by ingress or by payload shape)
                    if record.event_type == "ao_escalation" or self._is_ao_webhook(payload):
                        self._handle_ao_webhook(record.delivery_id, payload)
                    else:
                        # Normalize and enqueue to remediation queue
                        normalized = normalize_event(
                            delivery_id=record.delivery_id,
                            event_type=record.event_type,
                            raw_payload=payload,
                        )
                        if normalized:
                            self._queue.enqueue(normalized)
                            self._logger.info(
                                f"Enqueued event {normalized.event_id} from {record.delivery_id}"
                            )

                    # Mark as processed
                    self._store.mark_processed(record.delivery_id)

                except json.JSONDecodeError as e:
                    self._logger.error(f"Failed to parse webhook {record.delivery_id}: {e}")
                    self._store.mark_processed(record.delivery_id)
                except Exception as e:
                    self._logger.error(f"Error processing webhook {record.delivery_id}: {e}")
                    # Mark as processed to prevent infinite retry loops
                    self._store.mark_processed(record.delivery_id)

            # Process pending remediation events via worker
            if self._queue:
                processed, failed = self._worker.run_once(limit=5)
                if processed > 0 or failed > 0:
                    self._logger.info(f"Worker processed={processed}, failed={failed}")

        except Exception as e:
            self._logger.error(f"Error in poll_and_process: {e}")

    def _is_ao_webhook(self, payload: dict) -> bool:
        """Check if payload is an AO (Agent Orchestration) webhook.

        Detects both:
        - AO native format: {"type": "notification", "event": {"type": ...}}
        - Legacy flat format: {"event_type": ..., "session_id": ...}
        - Ingress-tagged: event_type column == "ao_escalation" is checked upstream
        """
        # AO native format (notifier-webhook plugin)
        if isinstance(payload.get("event"), dict):
            return True
        # Legacy flat format (internal tests / old clients)
        return "ao_event_type" in payload or "event_type" in payload and "session_id" in payload

    def _handle_ao_webhook(self, delivery_id: str, payload: dict) -> None:
        """Handle AO webhook via escalation handler."""
        try:
            result = handle_escalation(
                raw_payload=payload,
                cli=self._ao_cli,
                notifier=self._ao_notifier,
                action_log_path=self._action_log_path,
                policy=self._escalation_policy,
            )
            self._logger.info(
                f"AO webhook {delivery_id} handled: action={result.action_type}, "
                f"success={result.success}"
            )
        except EscalationHandlerError as e:
            self._logger.error(f"Escalation handler error for {delivery_id}: {e}")
        except Exception as e:
            self._logger.error(f"Unexpected error handling AO webhook {delivery_id}: {e}")

    def _worker_loop(self) -> None:
        """Background thread: poll and process webhooks."""
        self._logger.info("Worker loop started")
        while self._running:
            self._poll_and_process()
            self._shutdown_event.wait(timeout=WORKER_POLL_INTERVAL)
        self._logger.info("Worker loop stopped")

    def _run_ingress(self) -> None:
        """Run the HTTP ingress server (blocking)."""
        self._logger.info(f"Starting ingress server on port {self._port}")
        try:
            self._ingress.serve_forever()
        except Exception as e:
            self._logger.error(f"Ingress server error: {e}")
            raise

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon (ingress + worker thread)."""
        if self._running:
            self._logger.warning("Daemon already running")
            return

        self._init_components()
        self._running = True
        self._shutdown_event.clear()

        # Start worker thread
        worker_thread = threading.Thread(target=self._worker_loop, name="webhook-worker")
        worker_thread.daemon = True
        worker_thread.start()

        self._logger.info("Webhook daemon started")

        # Run ingress (blocking) - but we need to handle signals
        try:
            self._run_ingress()
        except KeyboardInterrupt:
            self._logger.info("Received interrupt, shutting down...")
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the daemon gracefully."""
        if not self._running:
            return

        self._logger.info("Stopping webhook daemon...")
        self._running = False
        self._shutdown_event.set()

        # Gracefully stop the blocking serve_forever() call
        if self._ingress is not None:
            self._ingress.shutdown()

        self._logger.info("Webhook daemon stopped")

    def restart(self) -> None:
        """Restart the daemon."""
        self._logger.info("Restarting webhook daemon...")
        self.stop()
        time.sleep(1)
        self.start()


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_daemon_instance: WebhookDaemon | None = None


def _signal_handler(signum: int, frame) -> None:  # noqa: ANN001
    """Handle shutdown signals."""
    sig_name = signal.Signals(signum).name
    print(f"Received {sig_name}, shutting down...")
    if _daemon_instance:
        _daemon_instance.stop()
    sys.exit(0)


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Main entrypoint for the webhook daemon."""
    global _daemon_instance

    # Setup signal handlers
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Get configuration
    port = int(os.environ.get("WEBHOOK_PORT", DEFAULT_PORT))
    host = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
    db_path = os.environ.get("WEBHOOK_DB_PATH", DEFAULT_DB_PATH)
    log_dir = Path(os.environ.get("WEBHOOK_LOG_DIR", str(DEFAULT_LOG_DIR)))

    # Create and start daemon
    daemon = WebhookDaemon(
        port=port,
        host=host,
        db_path=db_path,
        log_dir=log_dir,
    )
    _daemon_instance = daemon

    try:
        daemon.start()
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
