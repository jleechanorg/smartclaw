"""Tests for orchestration.lifecycle_reactions — state machine + reaction engine."""

from __future__ import annotations

import pytest
from time import monotonic
from unittest.mock import patch, MagicMock, AsyncMock

from orchestration.lifecycle_reactions import (
    SessionStatus,
    ReactionConfig,
    ReactionTracker,
    LifecycleManager,
    LifecyclePoller,
    determine_status,
    status_to_event_type,
    event_to_reaction_key,
    infer_priority,
)


# ---------------------------------------------------------------------------
# SessionStatus enum
# ---------------------------------------------------------------------------


class TestSessionStatus:
    def test_working(self):
        assert SessionStatus.WORKING == "working"

    def test_pr_open(self):
        assert SessionStatus.PR_OPEN == "pr_open"

    def test_ci_failed(self):
        assert SessionStatus.CI_FAILED == "ci_failed"

    def test_review_pending(self):
        assert SessionStatus.REVIEW_PENDING == "review_pending"

    def test_changes_requested(self):
        assert SessionStatus.CHANGES_REQUESTED == "changes_requested"

    def test_approved(self):
        assert SessionStatus.APPROVED == "approved"

    def test_mergeable(self):
        assert SessionStatus.MERGEABLE == "mergeable"

    def test_merged(self):
        assert SessionStatus.MERGED == "merged"

    def test_stuck(self):
        assert SessionStatus.STUCK == "stuck"

    def test_needs_input(self):
        assert SessionStatus.NEEDS_INPUT == "needs_input"

    def test_errored(self):
        assert SessionStatus.ERRORED == "errored"

    def test_killed(self):
        assert SessionStatus.KILLED == "killed"


# ---------------------------------------------------------------------------
# status_to_event_type()
# ---------------------------------------------------------------------------


class TestStatusToEventType:
    def test_working(self):
        assert status_to_event_type(None, SessionStatus.WORKING) == "session.working"

    def test_pr_created(self):
        assert status_to_event_type(SessionStatus.WORKING, SessionStatus.PR_OPEN) == "pr.created"

    def test_ci_failing(self):
        assert status_to_event_type(SessionStatus.PR_OPEN, SessionStatus.CI_FAILED) == "ci.failing"

    def test_review_approved(self):
        assert status_to_event_type(SessionStatus.REVIEW_PENDING, SessionStatus.APPROVED) == "review.approved"

    def test_merge_ready(self):
        assert status_to_event_type(SessionStatus.APPROVED, SessionStatus.MERGEABLE) == "merge.ready"

    def test_merge_completed(self):
        assert status_to_event_type(SessionStatus.MERGEABLE, SessionStatus.MERGED) == "merge.completed"

    def test_stuck(self):
        assert status_to_event_type(None, SessionStatus.STUCK) == "session.stuck"


# ---------------------------------------------------------------------------
# event_to_reaction_key()
# ---------------------------------------------------------------------------


class TestEventToReactionKey:
    def test_ci_failing(self):
        assert event_to_reaction_key("ci.failing") == "ci-failed"

    def test_changes_requested(self):
        assert event_to_reaction_key("review.changes_requested") == "changes-requested"

    def test_merge_ready(self):
        assert event_to_reaction_key("merge.ready") == "approved-and-green"

    def test_agent_stuck(self):
        assert event_to_reaction_key("session.stuck") == "agent-stuck"

    def test_unknown(self):
        assert event_to_reaction_key("unknown.event") is None


# ---------------------------------------------------------------------------
# infer_priority()
# ---------------------------------------------------------------------------


class TestInferPriority:
    def test_stuck_is_urgent(self):
        assert infer_priority("session.stuck") == "urgent"

    def test_needs_input_is_urgent(self):
        assert infer_priority("session.needs_input") == "urgent"

    def test_approved_is_action(self):
        assert infer_priority("review.approved") == "action"

    def test_failing_is_warning(self):
        assert infer_priority("ci.failing") == "warning"

    def test_working_is_info(self):
        assert infer_priority("session.working") == "info"

    def test_killed_is_urgent(self):
        assert infer_priority("session.killed") == "urgent"

    def test_exited_is_urgent(self):
        assert infer_priority("session.exited") == "urgent"


# ---------------------------------------------------------------------------
# ReactionConfig
# ---------------------------------------------------------------------------


class TestReactionConfig:
    def test_defaults(self):
        rc = ReactionConfig(action="notify")
        assert rc.action == "notify"
        assert rc.retries is None
        assert rc.escalate_after is None

    def test_with_retries(self):
        rc = ReactionConfig(action="send-to-agent", retries=3, escalate_after="10m")
        assert rc.retries == 3
        assert rc.escalate_after == "10m"


# ---------------------------------------------------------------------------
# ReactionTracker
# ---------------------------------------------------------------------------


class TestReactionTracker:
    def test_initial_attempts(self):
        tracker = ReactionTracker()
        assert tracker.attempts == 0

    def test_increment(self):
        tracker = ReactionTracker()
        tracker.attempts += 1
        assert tracker.attempts == 1


# ---------------------------------------------------------------------------
# LifecycleManager
# ---------------------------------------------------------------------------


class TestLifecycleManager:
    def test_creation(self):
        lm = LifecycleManager(reactions={})
        assert lm is not None

    def test_get_states_initially_empty(self):
        lm = LifecycleManager(reactions={})
        assert lm.get_states() == {}

    def test_record_transition(self):
        lm = LifecycleManager(reactions={})
        lm.record_state("session-1", SessionStatus.WORKING)
        assert lm.get_states()["session-1"] == SessionStatus.WORKING

    def test_state_transition_detected(self):
        lm = LifecycleManager(reactions={})
        lm.record_state("session-1", SessionStatus.WORKING)
        old, new = lm.check_transition("session-1", SessionStatus.PR_OPEN)
        assert old == SessionStatus.WORKING
        assert new == SessionStatus.PR_OPEN

    def test_no_transition_same_state(self):
        lm = LifecycleManager(reactions={})
        lm.record_state("session-1", SessionStatus.WORKING)
        result = lm.check_transition("session-1", SessionStatus.WORKING)
        assert result is None

    def test_first_observation_returns_none_old(self):
        """First observation of a session should have None as old_status."""
        lm = LifecycleManager(reactions={})
        result = lm.check_transition("new-session", SessionStatus.WORKING)
        assert result is not None
        old, new = result
        assert old is None
        assert new == SessionStatus.WORKING

    def test_reaction_tracking(self):
        reactions = {
            "ci-failed": ReactionConfig(action="send-to-agent", retries=3,
                                         message="Fix CI"),
        }
        lm = LifecycleManager(reactions=reactions)
        result = lm.execute_reaction("session-1", "ci-failed")
        assert result["success"] is True
        assert result["action"] == "send-to-agent"

    def test_escalation_after_max_retries(self):
        reactions = {
            "ci-failed": ReactionConfig(action="send-to-agent", retries=2,
                                         message="Fix CI"),
        }
        lm = LifecycleManager(reactions=reactions)
        lm.execute_reaction("session-1", "ci-failed")  # attempt 1
        lm.execute_reaction("session-1", "ci-failed")  # attempt 2
        result = lm.execute_reaction("session-1", "ci-failed")  # attempt 3 → escalate
        assert result["escalated"] is True

    def test_escalation_after_elapsed_duration(self):
        reactions = {
            "changes-requested": ReactionConfig(
                action="send-to-agent",
                escalate_after="10m",
                message="Address requested changes",
            ),
        }
        lm = LifecycleManager(reactions=reactions)

        first = lm.execute_reaction("session-1", "changes-requested")
        assert first["escalated"] is False

        tracker = lm._trackers["session-1:changes-requested"]
        tracker.first_triggered = monotonic() - (11 * 60)

        second = lm.execute_reaction("session-1", "changes-requested")
        assert second["escalated"] is True
        assert second["action"] == "escalated"

    def test_invalid_escalation_duration_is_ignored(self):
        reactions = {
            "changes-requested": ReactionConfig(
                action="send-to-agent",
                escalate_after="soon-ish",
            ),
        }
        lm = LifecycleManager(reactions=reactions)
        result = lm.execute_reaction("session-1", "changes-requested")
        assert result["escalated"] is False
        assert result["action"] == "send-to-agent"

    def test_unknown_reaction_key(self):
        lm = LifecycleManager(reactions={})
        result = lm.execute_reaction("session-1", "nonexistent")
        assert result["success"] is False

    def test_manual_replay_resets_retry_count(self):
        reactions = {
            "ci-failed": ReactionConfig(action="send-to-agent", retries=1, message="Fix CI"),
        }
        lm = LifecycleManager(reactions=reactions)
        lm.execute_reaction("session-1", "ci-failed")  # attempt 1
        lm.execute_reaction("session-1", "ci-failed")  # attempt 2 -> escalated
        escalated = lm.execute_reaction("session-1", "ci-failed")
        assert escalated["escalated"] is True

        replay = lm.manual_replay("session-1", "ci-failed")
        assert replay["escalated"] is False
        assert replay["action"] == "send-to-agent"

    def test_manual_replay_resets_elapsed_escalation_window(self):
        reactions = {
            "changes-requested": ReactionConfig(
                action="send-to-agent",
                escalate_after="10m",
            ),
        }
        lm = LifecycleManager(reactions=reactions)
        lm.execute_reaction("session-1", "changes-requested")

        tracker = lm._trackers["session-1:changes-requested"]
        tracker.first_triggered = monotonic() - (11 * 60)

        replay = lm.manual_replay("session-1", "changes-requested")
        assert replay["escalated"] is False
        assert replay["action"] == "send-to-agent"

    def test_manual_replay_unknown_key(self):
        lm = LifecycleManager(reactions={})
        result = lm.manual_replay("session-1", "nonexistent")
        assert result["success"] is False
        assert result["error"] == "Unknown reaction key"

    def test_clear_tracker_on_state_change(self):
        reactions = {
            "ci-failed": ReactionConfig(action="send-to-agent", retries=2,
                                         message="Fix CI"),
        }
        lm = LifecycleManager(reactions=reactions)
        lm.record_state("s1", SessionStatus.CI_FAILED)
        lm.execute_reaction("s1", "ci-failed")  # attempt 1
        lm.execute_reaction("s1", "ci-failed")  # attempt 2

        # State changes — trackers should reset
        lm.record_state("s1", SessionStatus.WORKING)
        lm.record_state("s1", SessionStatus.CI_FAILED)
        result = lm.execute_reaction("s1", "ci-failed")  # should be attempt 1 again
        assert result["escalated"] is False


# ---------------------------------------------------------------------------
# determine_status()
# ---------------------------------------------------------------------------


class TestDetermineStatus:
    """Tests for determine_status() — infers session status from SCM state."""

    def test_no_pr_returns_working(self):
        session = {"id": "s1", "status": "working", "pr": None, "branch": "feat-x"}
        result = determine_status(session, scm_state=None)
        assert result == SessionStatus.WORKING

    def test_pr_merged(self):
        session = {"id": "s1", "status": "pr_open", "pr": {"number": 1}, "branch": "feat-x"}
        scm = {"pr_state": "merged"}
        result = determine_status(session, scm_state=scm)
        assert result == SessionStatus.MERGED

    def test_pr_closed(self):
        session = {"id": "s1", "status": "pr_open", "pr": {"number": 1}, "branch": "feat-x"}
        scm = {"pr_state": "closed"}
        result = determine_status(session, scm_state=scm)
        assert result == SessionStatus.KILLED

    def test_ci_failed(self):
        session = {"id": "s1", "status": "pr_open", "pr": {"number": 1}, "branch": "feat-x"}
        scm = {"pr_state": "open", "ci_status": "failing"}
        result = determine_status(session, scm_state=scm)
        assert result == SessionStatus.CI_FAILED

    def test_changes_requested(self):
        session = {"id": "s1", "status": "pr_open", "pr": {"number": 1}, "branch": "feat-x"}
        scm = {"pr_state": "open", "ci_status": "passing", "review_decision": "changes_requested"}
        result = determine_status(session, scm_state=scm)
        assert result == SessionStatus.CHANGES_REQUESTED

    def test_approved_and_mergeable(self):
        session = {"id": "s1", "status": "pr_open", "pr": {"number": 1}, "branch": "feat-x"}
        scm = {"pr_state": "open", "ci_status": "passing",
               "review_decision": "approved", "mergeable": True}
        result = determine_status(session, scm_state=scm)
        assert result == SessionStatus.MERGEABLE

    def test_approved_not_mergeable(self):
        session = {"id": "s1", "status": "pr_open", "pr": {"number": 1}, "branch": "feat-x"}
        scm = {"pr_state": "open", "ci_status": "passing",
               "review_decision": "approved", "mergeable": False}
        result = determine_status(session, scm_state=scm)
        assert result == SessionStatus.APPROVED

    def test_review_pending(self):
        session = {"id": "s1", "status": "pr_open", "pr": {"number": 1}, "branch": "feat-x"}
        scm = {"pr_state": "open", "ci_status": "passing", "review_decision": "pending"}
        result = determine_status(session, scm_state=scm)
        assert result == SessionStatus.REVIEW_PENDING

    def test_pr_open_no_review(self):
        session = {"id": "s1", "status": "pr_open", "pr": {"number": 1}, "branch": "feat-x"}
        scm = {"pr_state": "open", "ci_status": "passing", "review_decision": "none"}
        result = determine_status(session, scm_state=scm)
        assert result == SessionStatus.PR_OPEN


# ---------------------------------------------------------------------------
# LifecyclePoller
# ---------------------------------------------------------------------------


class TestLifecycleIntegration:
    """Integration test: determine_status → check_transition → execute_reaction."""

    def test_full_lifecycle_flow(self):
        """Simulate: working → PR open → CI fail → retry → CI pass → approved → merged."""
        reactions = {
            "ci-failed": ReactionConfig(action="send-to-agent", retries=2, message="Fix CI"),
            "approved-and-green": ReactionConfig(action="notify", message="Ready to merge"),
        }
        lm = LifecycleManager(reactions=reactions)
        session = {"id": "s1", "status": "working", "pr": None, "branch": "feat-x"}

        # 1. Agent starts working — no PR yet
        status = determine_status(session, scm_state=None)
        assert status == SessionStatus.WORKING
        lm.record_state("s1", status)

        # 2. Agent opens a PR — CI starts
        session["pr"] = {"number": 42}
        scm = {"pr_state": "open", "ci_status": "pending", "review_decision": "none"}
        status = determine_status(session, scm_state=scm)
        assert status == SessionStatus.PR_OPEN
        transition = lm.check_transition("s1", status)
        assert transition == (SessionStatus.WORKING, SessionStatus.PR_OPEN)
        lm.record_state("s1", status)

        # 3. CI fails
        scm["ci_status"] = "failing"
        status = determine_status(session, scm_state=scm)
        assert status == SessionStatus.CI_FAILED
        lm.record_state("s1", status)
        event = status_to_event_type(SessionStatus.PR_OPEN, status)
        assert event == "ci.failing"
        rk = event_to_reaction_key(event)
        assert rk == "ci-failed"
        result = lm.execute_reaction("s1", rk)
        assert result["action"] == "send-to-agent"
        assert result["escalated"] is False

        # 4. Agent fixes, CI passes, review approved + mergeable
        scm["ci_status"] = "passing"
        scm["review_decision"] = "approved"
        scm["mergeable"] = True
        status = determine_status(session, scm_state=scm)
        assert status == SessionStatus.MERGEABLE
        lm.record_state("s1", status)
        event = status_to_event_type(SessionStatus.CI_FAILED, status)
        assert event == "merge.ready"
        rk = event_to_reaction_key(event)
        assert rk == "approved-and-green"
        result = lm.execute_reaction("s1", rk)
        assert result["action"] == "notify"

        # 5. PR merged
        scm["pr_state"] = "merged"
        status = determine_status(session, scm_state=scm)
        assert status == SessionStatus.MERGED
        lm.record_state("s1", status)

    def test_escalation_after_repeated_ci_failures(self):
        """After max retries, escalation fires instead of send-to-agent."""
        reactions = {
            "ci-failed": ReactionConfig(action="send-to-agent", retries=2, message="Fix CI"),
        }
        lm = LifecycleManager(reactions=reactions)
        lm.record_state("s1", SessionStatus.CI_FAILED)

        # Retry 1 and 2 — normal action
        r1 = lm.execute_reaction("s1", "ci-failed")
        assert r1["action"] == "send-to-agent"
        r2 = lm.execute_reaction("s1", "ci-failed")
        assert r2["action"] == "send-to-agent"

        # Retry 3 — escalated
        r3 = lm.execute_reaction("s1", "ci-failed")
        assert r3["escalated"] is True
        assert r3["action"] == "escalated"


class TestLifecyclePoller:
    """Tests for the polling loop wrapper."""

    def test_creation(self):
        lm = LifecycleManager(reactions={})
        poller = LifecyclePoller(lifecycle_manager=lm, interval_seconds=60)
        assert poller.interval_seconds == 60
        assert poller.is_running is False

    def test_start_stop(self):
        lm = LifecycleManager(reactions={})
        poller = LifecyclePoller(lifecycle_manager=lm, interval_seconds=60)
        poller.start()
        assert poller.is_running is True
        poller.stop()
        assert poller.is_running is False

    def test_double_start_idempotent(self):
        lm = LifecycleManager(reactions={})
        poller = LifecyclePoller(lifecycle_manager=lm, interval_seconds=60)
        poller.start()
        poller.start()  # Should not error
        assert poller.is_running is True
        poller.stop()

    def test_stop_when_not_running(self):
        lm = LifecycleManager(reactions={})
        poller = LifecyclePoller(lifecycle_manager=lm, interval_seconds=60)
        poller.stop()  # Should not error
        assert poller.is_running is False

    def test_poll_fn_exception_logged(self, caplog):
        """ORCH-1g8: poll_fn exceptions are logged, not silently swallowed."""
        def failing_fn(_lm):
            raise RuntimeError("boom")

        lm = LifecycleManager(reactions={})
        poller = LifecyclePoller(
            lifecycle_manager=lm,
            interval_seconds=0,
            poll_fn=failing_fn,
        )
        import logging
        with caplog.at_level(logging.WARNING, logger="orchestration.lifecycle_reactions"):
            poller.start()
            import time; time.sleep(0.1)
            poller.stop()
        assert any("poll_fn raised" in r.message for r in caplog.records)
