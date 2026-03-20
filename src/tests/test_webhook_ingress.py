"""Tests for webhook_ingress: validate_signature, WebhookStore dedup, and /ao-notify path."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestration.webhook_ingress import (
    WebhookIngress,
    WebhookRecord,
    WebhookStore,
    validate_signature,
)


# ---------------------------------------------------------------------------
# validate_signature tests
# ---------------------------------------------------------------------------


def _make_sig(payload: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_validate_signature_valid() -> None:
    payload = b'{"action":"opened"}'
    secret = "mysecret"
    sig = _make_sig(payload, secret)
    assert validate_signature(payload, sig, secret) is True


def test_validate_signature_wrong_secret() -> None:
    payload = b'{"action":"opened"}'
    sig = _make_sig(payload, "correct_secret")
    assert validate_signature(payload, sig, "wrong_secret") is False


def test_validate_signature_tampered_payload() -> None:
    secret = "mysecret"
    sig = _make_sig(b"original", secret)
    assert validate_signature(b"tampered", sig, secret) is False


def test_validate_signature_missing_prefix() -> None:
    payload = b"hello"
    secret = "s"
    raw_hex = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    # header without "sha256=" prefix should fail
    assert validate_signature(payload, raw_hex, secret) is False


def test_validate_signature_empty_payload() -> None:
    payload = b""
    secret = "empty"
    sig = _make_sig(payload, secret)
    assert validate_signature(payload, sig, secret) is True


def test_validate_signature_malformed_header() -> None:
    assert validate_signature(b"data", "notahex!!!", "secret") is False


# ---------------------------------------------------------------------------
# WebhookStore tests
# ---------------------------------------------------------------------------


def _make_record(delivery_id: str = "abc-123", event_type: str = "pull_request") -> WebhookRecord:
    return WebhookRecord(
        delivery_id=delivery_id,
        event_type=event_type,
        payload=json.dumps({"action": "opened"}),
        headers={"X-GitHub-Event": event_type},
        received_at=datetime.now(tz=timezone.utc),
    )


@pytest.fixture()
def store(tmp_path: Path) -> WebhookStore:
    db_path = str(tmp_path / "webhook_queue.db")
    s = WebhookStore(db_path=db_path)
    s.init_schema()
    return s


def test_store_returns_true_on_new_record(store: WebhookStore) -> None:
    record = _make_record("delivery-1")
    assert store.store(record) is True


def test_store_returns_false_on_duplicate(store: WebhookStore) -> None:
    record = _make_record("delivery-dup")
    assert store.store(record) is True
    assert store.store(record) is False


def test_store_dedup_different_delivery_ids(store: WebhookStore) -> None:
    assert store.store(_make_record("d-1")) is True
    assert store.store(_make_record("d-2")) is True


def test_get_unprocessed_empty(store: WebhookStore) -> None:
    assert store.get_unprocessed() == []


def test_get_unprocessed_returns_stored(store: WebhookStore) -> None:
    record = _make_record("d-unprocessed")
    store.store(record)
    results = store.get_unprocessed()
    assert len(results) == 1
    assert results[0].delivery_id == "d-unprocessed"
    assert results[0].processed is False


def test_mark_processed(store: WebhookStore) -> None:
    record = _make_record("d-mark")
    store.store(record)
    store.mark_processed("d-mark")
    results = store.get_unprocessed()
    assert results == []


def test_get_unprocessed_respects_limit(store: WebhookStore) -> None:
    for i in range(10):
        store.store(_make_record(f"d-{i}"))
    results = store.get_unprocessed(limit=3)
    assert len(results) == 3


def test_get_unprocessed_excludes_processed(store: WebhookStore) -> None:
    store.store(_make_record("d-processed"))
    store.store(_make_record("d-not-processed"))
    store.mark_processed("d-processed")
    results = store.get_unprocessed()
    assert len(results) == 1
    assert results[0].delivery_id == "d-not-processed"


def test_record_roundtrip_preserves_fields(store: WebhookStore) -> None:
    ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    record = WebhookRecord(
        delivery_id="d-roundtrip",
        event_type="check_suite",
        payload=json.dumps({"conclusion": "success"}),
        headers={"X-GitHub-Event": "check_suite", "X-GitHub-Delivery": "d-roundtrip"},
        received_at=ts,
    )
    store.store(record)
    results = store.get_unprocessed()
    assert len(results) == 1
    r = results[0]
    assert r.delivery_id == "d-roundtrip"
    assert r.event_type == "check_suite"
    assert json.loads(r.payload)["conclusion"] == "success"
    assert r.received_at == ts


# ---------------------------------------------------------------------------
# /ao-notify endpoint tests (live HTTP server)
# ---------------------------------------------------------------------------


@pytest.fixture()
def ingress_server(tmp_path: Path):
    """Start a WebhookIngress on a free port, yield (port, db_path), then stop."""
    import socket

    db_path = str(tmp_path / "test_ao_notify.db")
    # Find a free port
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = WebhookIngress(
        host="127.0.0.1",
        port=port,
        db_path=db_path,
        ao_bearer_token="test-ao-token",
    )
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    import time; time.sleep(0.1)  # let server start
    yield port, db_path
    server.shutdown()


def _ao_post(port: int, payload: dict, token: str | None, request_id: str = "ao-test-001") -> int:
    body = json.dumps(payload).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port)
    headers: dict[str, str] = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if request_id:
        headers["X-AO-Request-ID"] = request_id
    conn.request("POST", "/ao-notify", body, headers)
    resp = conn.getresponse()
    return resp.status


def test_ao_notify_valid_token_returns_204(ingress_server: tuple) -> None:
    port, _ = ingress_server
    status = _ao_post(port, {"ao_event_type": "reaction.escalated", "session_id": "jc-42"}, "test-ao-token")
    assert status == 204


def test_ao_notify_wrong_token_returns_401(ingress_server: tuple) -> None:
    port, _ = ingress_server
    status = _ao_post(port, {"ao_event_type": "session.stuck"}, "wrong-token")
    assert status == 401


def test_ao_notify_no_token_returns_401(ingress_server: tuple) -> None:
    port, _ = ingress_server
    status = _ao_post(port, {"ao_event_type": "session.stuck"}, token=None)
    assert status == 401


def test_ao_notify_stores_as_ao_escalation_event_type(ingress_server: tuple) -> None:
    port, db_path = ingress_server
    _ao_post(port, {"ao_event_type": "merge.ready", "pr_url": "https://github.com/test/repo/pull/1"}, "test-ao-token", "ao-merge-001")
    store = WebhookStore(db_path=db_path)
    store.init_schema()
    records = store.get_unprocessed()
    assert any(r.event_type == "ao_escalation" for r in records)
    ao_rec = next(r for r in records if r.event_type == "ao_escalation")
    assert ao_rec.delivery_id == "ao-merge-001"
    data = json.loads(ao_rec.payload)
    assert data["ao_event_type"] == "merge.ready"


def test_ao_notify_deduplicates_same_request_id(ingress_server: tuple) -> None:
    port, db_path = ingress_server
    payload = {"ao_event_type": "reaction.escalated"}
    _ao_post(port, payload, "test-ao-token", "ao-dup-001")
    _ao_post(port, payload, "test-ao-token", "ao-dup-001")  # duplicate
    store = WebhookStore(db_path=db_path)
    store.init_schema()
    records = [r for r in store.get_unprocessed() if r.delivery_id == "ao-dup-001"]
    assert len(records) == 1  # deduplicated


def test_github_webhook_path_still_works_alongside_ao_notify(ingress_server: tuple) -> None:
    """GitHub /webhook path must not be broken by adding /ao-notify."""
    port, _ = ingress_server
    # /webhook without HMAC returns 400 (missing delivery header) not 404
    body = json.dumps({"action": "opened"}).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request("POST", "/webhook", body, {"Content-Type": "application/json", "Content-Length": str(len(body))})
    resp = conn.getresponse()
    # Should get 400 (missing X-GitHub-Delivery), not 404
    assert resp.status == 400
