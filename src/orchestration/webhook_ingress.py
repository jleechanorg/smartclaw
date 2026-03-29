"""Webhook ingress service: HTTP adapter, HMAC validation, and SQLite queue.

Responsibilities:
- Validate X-Hub-Signature-256 on incoming GitHub App webhook deliveries.
- Store raw payload + headers + receive timestamp to a SQLite queue DB.
- Deduplicate by X-GitHub-Delivery header (idempotent re-delivery).

Note: event normalisation is handled by 6.2 (not this module).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


# ---------------------------------------------------------------------------
# Constants / enums
# ---------------------------------------------------------------------------


class GitHubEventType(StrEnum):
    """Known GitHub webhook event types handled by the ingress layer."""

    PULL_REQUEST = "pull_request"
    PULL_REQUEST_REVIEW = "pull_request_review"
    PULL_REQUEST_REVIEW_COMMENT = "pull_request_review_comment"
    ISSUE_COMMENT = "issue_comment"
    CHECK_SUITE = "check_suite"
    PUSH = "push"
    PING = "ping"


_DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/webhook_queue.db")

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id  TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    payload      TEXT NOT NULL,
    headers      TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    processed    INTEGER NOT NULL DEFAULT 0
);
"""

_INSERT_SQL = """
INSERT OR IGNORE INTO webhook_deliveries
    (delivery_id, event_type, payload, headers, received_at, processed)
VALUES (?, ?, ?, ?, ?, 0)
"""

_SELECT_UNPROCESSED_SQL = """
SELECT delivery_id, event_type, payload, headers, received_at, processed
FROM webhook_deliveries
WHERE processed = 0
ORDER BY received_at ASC
LIMIT ?
"""

_MARK_PROCESSED_SQL = """
UPDATE webhook_deliveries SET processed = 1 WHERE delivery_id = ?
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class WebhookRecord:
    """Represents a single GitHub webhook delivery in the queue."""

    delivery_id: str
    event_type: str
    payload: str  # raw JSON string
    headers: dict[str, str]
    received_at: datetime
    processed: bool = False


# ---------------------------------------------------------------------------
# Pure validation helper
# ---------------------------------------------------------------------------


def validate_signature(
    payload_bytes: bytes,
    signature_header: str,
    secret: str,
) -> bool:
    """Return True iff X-Hub-Signature-256 header matches HMAC-SHA256 of payload.

    Args:
        payload_bytes: Raw request body bytes.
        signature_header: Value of the X-Hub-Signature-256 header (e.g. "sha256=abc...").
        secret: The shared webhook secret.

    Returns:
        True if signature is valid, False otherwise.
    """
    if not signature_header.startswith("sha256="):
        return False
    expected_hex = signature_header[len("sha256="):]
    # Reject obviously malformed hex early (avoid timing leak on non-hex input)
    if not expected_hex:
        return False
    try:
        expected_bytes = bytes.fromhex(expected_hex)
    except ValueError:
        return False
    actual_bytes = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).digest()
    return hmac.compare_digest(actual_bytes, expected_bytes)


# ---------------------------------------------------------------------------
# SQLite-backed store
# ---------------------------------------------------------------------------


class WebhookStore:
    """Manages a SQLite queue of received webhook deliveries."""

    def __init__(self, db_path: str = _DEFAULT_DB_PATH) -> None:
        self._db_path = db_path

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def init_schema(self) -> None:
        """Create the deliveries table if it does not already exist."""
        with closing(self._connect()) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.commit()

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def store(self, record: WebhookRecord) -> bool:
        """Persist a WebhookRecord.

        Returns:
            True  — record inserted (new delivery).
            False — delivery_id already present (idempotent re-delivery).
        """
        received_iso = record.received_at.astimezone(timezone.utc).isoformat()
        headers_json = json.dumps(record.headers)
        with closing(self._connect()) as conn:
            cur = conn.execute(
                _INSERT_SQL,
                (
                    record.delivery_id,
                    record.event_type,
                    record.payload,
                    headers_json,
                    received_iso,
                ),
            )
            conn.commit()
            return cur.rowcount > 0

    def mark_processed(self, delivery_id: str) -> None:
        """Mark a delivery as processed so it is excluded from future polling."""
        with closing(self._connect()) as conn:
            conn.execute(_MARK_PROCESSED_SQL, (delivery_id,))
            conn.commit()

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_unprocessed(self, limit: int = 100) -> list[WebhookRecord]:
        """Return up to *limit* unprocessed records, oldest first."""
        with closing(self._connect()) as conn:
            rows = conn.execute(_SELECT_UNPROCESSED_SQL, (limit,)).fetchall()
        return [self._row_to_record(row) for row in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> WebhookRecord:
        delivery_id, event_type, payload, headers_json, received_iso, processed_int = row
        headers: dict[str, str] = json.loads(headers_json)
        received_at = datetime.fromisoformat(received_iso)
        return WebhookRecord(
            delivery_id=delivery_id,
            event_type=event_type,
            payload=payload,
            headers=headers,
            received_at=received_at,
            processed=bool(processed_int),
        )


# ---------------------------------------------------------------------------
# HTTP ingress handler
# ---------------------------------------------------------------------------


class _WebhookHandler(BaseHTTPRequestHandler):
    """BaseHTTPRequestHandler that validates and queues GitHub webhook deliveries."""

    # Injected by WebhookIngress.serve_forever()
    store: WebhookStore
    webhook_secret: str | None
    ao_bearer_token: str | None  # Bearer token for AO /ao-notify endpoint

    def do_GET(self) -> None:  # noqa: N802
        """Handle health check requests."""
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        # Route /ao-notify to AO escalation handler (Bearer token auth)
        if self.path == "/ao-notify":
            self._handle_ao_notify()
            return
        # Route /webhook to the same handler as root
        if self.path not in ("/", "/webhook"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length) if content_length else b""
        except (ValueError, OSError):
            self._respond(400)
            return

        secret = self.webhook_secret
        if secret:
            sig_header = self.headers.get("X-Hub-Signature-256", "")
            if not validate_signature(raw_body, sig_header, secret):
                self._respond(401)
                return

        delivery_id = self.headers.get("X-GitHub-Delivery", "")
        event_type = self.headers.get("X-GitHub-Event", "unknown")

        if not delivery_id:
            self._respond(400)
            return

        try:
            payload_str = raw_body.decode("utf-8")
            json.loads(payload_str)  # validate it is parseable JSON
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._respond(400)
            return

        headers_snapshot = {
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": event_type,
            "X-Hub-Signature-256": self.headers.get("X-Hub-Signature-256", ""),
            "Content-Type": self.headers.get("Content-Type", ""),
        }

        record = WebhookRecord(
            delivery_id=delivery_id,
            event_type=event_type,
            payload=payload_str,
            headers=headers_snapshot,
            received_at=datetime.now(tz=timezone.utc),
        )
        self.store.store(record)
        self._respond(204)

    def _handle_ao_notify(self) -> None:
        """Handle AO escalation webhooks at /ao-notify with Bearer token auth."""
        token = self.ao_bearer_token
        if token:
            auth_header = self.headers.get("Authorization", "")
            if auth_header != f"Bearer {token}":
                self._respond(401)
                return
        else:
            # No token configured - require Authorization header to prevent open access
            # Log warning for operators to configure OPENCLAW_AO_NOTIFY_TOKEN
            auth_header = self.headers.get("Authorization", "")
            if not auth_header:
                # TODO: Add proper logging here if logger is available
                # For now, return 401 to prevent unauthenticated access
                self._respond(401)
                return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length) if content_length else b""
        except (ValueError, OSError):
            self._respond(400)
            return
        try:
            payload_str = raw_body.decode("utf-8")
            json.loads(payload_str)  # validate parseable
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._respond(400)
            return
        # Use AO request ID if provided, otherwise generate one from content hash
        delivery_id = self.headers.get("X-AO-Request-ID") or f"ao-{hashlib.sha256(raw_body).hexdigest()[:16]}"
        headers_snapshot = {
            "X-AO-Request-ID": delivery_id,
            "X-AO-Source": self.headers.get("X-AO-Source", "ao-notifier"),
            "Content-Type": self.headers.get("Content-Type", ""),
        }
        record = WebhookRecord(
            delivery_id=delivery_id,
            event_type="ao_escalation",
            payload=payload_str,
            headers=headers_snapshot,
            received_at=datetime.now(tz=timezone.utc),
        )
        self.store.store(record)
        self._respond(204)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ANN001
        # Suppress default BaseHTTPRequestHandler stdout logging.
        pass

    def _respond(self, status: int) -> None:
        self.send_response(status)
        self.end_headers()


class WebhookIngress:
    """Minimal HTTP server that ingests GitHub webhook deliveries into a local queue."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9100,
        db_path: str = _DEFAULT_DB_PATH,
        webhook_secret: str | None = None,
        ao_bearer_token: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._store = WebhookStore(db_path=db_path)
        self._store.init_schema()
        self._secret = webhook_secret or os.environ.get("GITHUB_WEBHOOK_SECRET")
        self._ao_bearer_token = ao_bearer_token or os.environ.get("OPENCLAW_AO_NOTIFY_TOKEN")
        self._server: HTTPServer | None = None

    def serve_forever(self) -> None:
        """Start the HTTP server and block until interrupted."""
        store = self._store
        secret = self._secret
        ao_token = self._ao_bearer_token

        class _Handler(_WebhookHandler):
            pass

        _Handler.store = store  # type: ignore[attr-defined]
        _Handler.webhook_secret = secret  # type: ignore[attr-defined]
        _Handler.ao_bearer_token = ao_token  # type: ignore[attr-defined]

        self._server = HTTPServer((self._host, self._port), _Handler)
        self._server.serve_forever()

    def shutdown(self) -> None:
        """Stop the HTTP server if running. Safe to call when not started."""
        if self._server is not None:
            self._server.shutdown()
            self._server = None
