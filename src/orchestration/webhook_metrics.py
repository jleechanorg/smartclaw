"""Lightweight observability for the webhook pipeline (no external dependencies).

This module provides:
- ``MetricCounters``   — in-process counters for every pipeline stage.
- ``MetricsCollector`` — thread-safe accumulator with a module-level singleton.
- ``SLOTargets``       — hardcoded SLO thresholds (documented below).
- ``check_slo_alerts`` — returns a list of alert strings when SLOs are breached.

Instrumentation pattern
-----------------------
The existing modules (webhook_ingress, webhook_queue, webhook_worker,
webhook_reconciler) are *not* modified by this module.  Callers that want
to emit metrics should import the collector and call ``inc()`` at the
appropriate call site:

Example — counting received webhooks in webhook_ingress.py::

    from orchestration.webhook_metrics import get_collector

    def do_POST(self) -> None:
        get_collector().inc("webhooks_received")
        ...
        if not validate_signature(...):
            get_collector().inc("webhooks_invalid_sig")
            self._respond(401)
            return
        inserted = self.store.store(record)
        if not inserted:
            get_collector().inc("webhooks_deduped")
        ...

Example — counting enqueued and dispatched events in webhook_worker.py::

    from orchestration.webhook_metrics import get_collector

    # After RemediationQueue.enqueue() returns True:
    get_collector().inc("events_enqueued")

    # After successful dispatch:
    get_collector().inc("events_dispatched")

    # After DispatchError exhausts retry budget:
    get_collector().inc("events_failed")

Example — counting reconciler resets in webhook_reconciler.py::

    from orchestration.webhook_metrics import get_collector

    # Per stuck IN_PROGRESS event reset:
    get_collector().inc("reconciler_stuck_reset")

    # Per stale PENDING event reset:
    get_collector().inc("reconciler_stale_reset")

    # Per event marked STALE by queue.mark_stale():
    get_collector().inc("events_stale")
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# MetricCounters — plain-data snapshot / accumulator
# ---------------------------------------------------------------------------


@dataclass
class MetricCounters:
    """In-process pipeline counters.

    All fields start at zero and are incremented by ``MetricsCollector.inc``.
    A snapshot of these counters is passed to ``check_slo_alerts``.
    """

    webhooks_received: int = 0
    """Total POST requests accepted by the ingress endpoint."""

    webhooks_invalid_sig: int = 0
    """Requests rejected due to HMAC signature mismatch."""

    webhooks_deduped: int = 0
    """Deliveries silently dropped because the delivery_id was already present."""

    events_enqueued: int = 0
    """Normalized events successfully written to the remediation queue."""

    events_dispatched: int = 0
    """Events dispatched successfully to Symphony (status → DONE)."""

    events_failed: int = 0
    """Events that exhausted their retry budget (status → FAILED)."""

    events_stale: int = 0
    """Events that expired before dispatch (status → STALE)."""

    reconciler_stuck_reset: int = 0
    """IN_PROGRESS events reset to PENDING by the reconciler."""

    reconciler_stale_reset: int = 0
    """Stale PENDING events whose attempt_count was reset by the reconciler."""


# ---------------------------------------------------------------------------
# MetricsCollector — thread-safe singleton
# ---------------------------------------------------------------------------


class MetricsCollector:
    """Thread-safe accumulator for ``MetricCounters`` fields.

    Use ``get_collector()`` to obtain the module-level singleton rather than
    constructing a new instance, unless you need an isolated instance for
    tests.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters = MetricCounters()

    def inc(self, counter: str, amount: int = 1) -> None:
        """Atomically increment the named counter by *amount*.

        Parameters
        ----------
        counter:
            One of the field names on ``MetricCounters``
            (e.g. ``"webhooks_received"``).
        amount:
            How much to add; defaults to 1.

        Raises
        ------
        AttributeError
            If *counter* is not a valid ``MetricCounters`` field.  Fail-fast
            so typos are caught early rather than silently lost.
        """
        with self._lock:
            current = getattr(self._counters, counter)
            setattr(self._counters, counter, current + amount)

    def snapshot(self) -> dict[str, int]:
        """Return a copy of all counter values as a plain ``dict``.

        The returned dict is safe to read without holding the lock.
        """
        with self._lock:
            return {
                f: getattr(self._counters, f)
                for f in self._counters.__dataclass_fields__
            }

    def reset(self) -> None:
        """Reset all counters to zero.

        Primarily intended for test isolation.
        """
        with self._lock:
            self._counters = MetricCounters()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_COLLECTOR: MetricsCollector = MetricsCollector()


def get_collector() -> MetricsCollector:
    """Return the module-level ``MetricsCollector`` singleton."""
    return _COLLECTOR


# ---------------------------------------------------------------------------
# SLOTargets — hardcoded thresholds
# ---------------------------------------------------------------------------


@dataclass
class SLOTargets:
    """Hardcoded SLO thresholds for the webhook pipeline.

    These values represent the minimum acceptable operational performance.
    Violations are surfaced by ``check_slo_alerts``.

    dispatch_success_rate_pct:
        Minimum percentage of enqueued events that must reach DONE status.
        Default: 95.0 % — allows up to 5 % transient failures before paging.

    p99_end_to_end_seconds:
        Maximum acceptable p99 latency from webhook receipt to Symphony
        dispatch completion.  Default: 300 s (5 min) — matches the
        reconciler's default ``stuck_in_progress_minutes`` threshold.

    missed_delivery_recovery_hours:
        Time within which a missed/stale delivery must be recovered by the
        reconciler.  Default: 2 h — matches ``ReconcilerConfig.stale_pending_hours``.
    """

    dispatch_success_rate_pct: float = 95.0
    p99_end_to_end_seconds: float = 300.0
    missed_delivery_recovery_hours: float = 2.0


# ---------------------------------------------------------------------------
# check_slo_alerts
# ---------------------------------------------------------------------------


def check_slo_alerts(
    metrics: MetricCounters,
    slo: SLOTargets | None = None,
) -> list[str]:
    """Return a list of alert strings for any breached SLO conditions.

    Parameters
    ----------
    metrics:
        A ``MetricCounters`` snapshot (e.g. from ``MetricsCollector.snapshot()``
        coerced into a dataclass, or built directly for unit tests).
    slo:
        Optional override of SLO thresholds.  Uses ``SLOTargets()`` defaults
        when omitted.

    Returns
    -------
    list[str]
        Empty when all SLOs are satisfied.  Each element is a human-readable
        alert string suitable for logging or Slack notification.
    """
    if slo is None:
        slo = SLOTargets()

    alerts: list[str] = []

    # --- Dispatch success rate -------------------------------------------
    if metrics.events_enqueued > 0:
        rate = (metrics.events_dispatched / metrics.events_enqueued) * 100.0
        if rate < slo.dispatch_success_rate_pct:
            alerts.append(
                f"ALERT: dispatch success rate {rate:.1f}% below SLO {slo.dispatch_success_rate_pct:.0f}%"
            )

    # --- Failed event count ----------------------------------------------
    if metrics.events_failed > 10:
        alerts.append(f"ALERT: {metrics.events_failed} events in FAILED state")

    # --- Stale event count -----------------------------------------------
    if metrics.events_stale > 20:
        alerts.append(f"ALERT: {metrics.events_stale} events marked STALE")

    return alerts
