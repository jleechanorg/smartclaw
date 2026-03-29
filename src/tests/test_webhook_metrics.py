"""Tests for webhook_metrics: MetricsCollector and check_slo_alerts."""
from __future__ import annotations

import threading

from orchestration.webhook_metrics import (
    MetricCounters,
    MetricsCollector,
    SLOTargets,
    check_slo_alerts,
    get_collector,
)


# ---------------------------------------------------------------------------
# MetricsCollector thread-safety
# ---------------------------------------------------------------------------


def test_collector_inc_thread_safety() -> None:
    """10 threads × 100 increments must all land: total == 1000."""
    collector = MetricsCollector()
    n_threads = 10
    increments_each = 100

    def worker() -> None:
        for _ in range(increments_each):
            collector.inc("webhooks_received")

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = collector.snapshot()
    assert snap["webhooks_received"] == n_threads * increments_each


def test_collector_inc_multiple_counters() -> None:
    """inc() updates the named counter correctly."""
    collector = MetricsCollector()
    collector.inc("events_enqueued", 5)
    collector.inc("events_dispatched", 3)
    snap = collector.snapshot()
    assert snap["events_enqueued"] == 5
    assert snap["events_dispatched"] == 3


def test_collector_snapshot_is_copy() -> None:
    """Mutating the snapshot dict does not affect the collector's state."""
    collector = MetricsCollector()
    collector.inc("webhooks_received")
    snap = collector.snapshot()
    snap["webhooks_received"] = 999
    assert collector.snapshot()["webhooks_received"] == 1


def test_collector_reset() -> None:
    """reset() zeros all counters."""
    collector = MetricsCollector()
    collector.inc("webhooks_received", 10)
    collector.inc("events_failed", 3)
    collector.reset()
    snap = collector.snapshot()
    assert all(v == 0 for v in snap.values())


def test_get_collector_returns_singleton() -> None:
    """get_collector() always returns the same instance."""
    c1 = get_collector()
    c2 = get_collector()
    assert c1 is c2


# ---------------------------------------------------------------------------
# check_slo_alerts — alert conditions
# ---------------------------------------------------------------------------


def _make_counters(**kwargs: int) -> MetricCounters:
    """Return MetricCounters with all fields defaulting to 0."""
    defaults = {
        "webhooks_received": 0,
        "webhooks_invalid_sig": 0,
        "webhooks_deduped": 0,
        "events_enqueued": 0,
        "events_dispatched": 0,
        "events_failed": 0,
        "events_stale": 0,
        "reconciler_stuck_reset": 0,
        "reconciler_stale_reset": 0,
    }
    defaults.update(kwargs)
    return MetricCounters(**defaults)


def test_check_slo_alerts_healthy_returns_empty() -> None:
    """No alerts for healthy metrics."""
    counters = _make_counters(
        events_enqueued=100,
        events_dispatched=99,
        events_failed=1,
        events_stale=5,
    )
    alerts = check_slo_alerts(counters)
    assert alerts == []


def test_check_slo_alerts_low_dispatch_success_rate() -> None:
    """Alert fires when dispatch success rate < 95%."""
    # 80 dispatched out of 100 enqueued = 80%
    counters = _make_counters(events_enqueued=100, events_dispatched=80)
    alerts = check_slo_alerts(counters)
    assert any("dispatch success rate" in a and "below SLO 95%" in a for a in alerts)


def test_check_slo_alerts_dispatch_success_rate_exact_boundary() -> None:
    """Alert does NOT fire when rate is exactly 95%."""
    counters = _make_counters(events_enqueued=100, events_dispatched=95)
    alerts = check_slo_alerts(counters)
    assert not any("dispatch success rate" in a for a in alerts)


def test_check_slo_alerts_failed_events_threshold() -> None:
    """Alert fires when failed event count > 10."""
    counters = _make_counters(events_failed=11)
    alerts = check_slo_alerts(counters)
    assert any("events in FAILED state" in a for a in alerts)


def test_check_slo_alerts_failed_events_at_boundary() -> None:
    """Alert does NOT fire when failed count == 10."""
    counters = _make_counters(events_failed=10)
    alerts = check_slo_alerts(counters)
    assert not any("FAILED state" in a for a in alerts)


def test_check_slo_alerts_stale_events_threshold() -> None:
    """Alert fires when stale event count > 20."""
    counters = _make_counters(events_stale=21)
    alerts = check_slo_alerts(counters)
    assert any("events marked STALE" in a for a in alerts)


def test_check_slo_alerts_stale_events_at_boundary() -> None:
    """Alert does NOT fire when stale count == 20."""
    counters = _make_counters(events_stale=20)
    alerts = check_slo_alerts(counters)
    assert not any("STALE" in a for a in alerts)


def test_check_slo_alerts_multiple_conditions() -> None:
    """Multiple alert conditions can fire simultaneously."""
    counters = _make_counters(
        events_enqueued=100,
        events_dispatched=50,
        events_failed=15,
        events_stale=25,
    )
    alerts = check_slo_alerts(counters)
    assert len(alerts) == 3


def test_check_slo_alerts_no_enqueued_no_rate_alert() -> None:
    """When events_enqueued == 0, no dispatch rate alert (avoid divide-by-zero)."""
    counters = _make_counters(events_enqueued=0, events_dispatched=0)
    alerts = check_slo_alerts(counters)
    assert not any("dispatch success rate" in a for a in alerts)


# ---------------------------------------------------------------------------
# SLOTargets defaults
# ---------------------------------------------------------------------------


def test_slo_targets_defaults() -> None:
    """SLOTargets has the documented default values."""
    slo = SLOTargets()
    assert slo.dispatch_success_rate_pct == 95.0
    assert slo.p99_end_to_end_seconds == 300.0
    assert slo.missed_delivery_recovery_hours == 2.0
