from __future__ import annotations

from orchestration import mctrl_status


def test_print_status_includes_outbox_health_summary(monkeypatch, capsys) -> None:
    monkeypatch.setattr(mctrl_status, "_read_registry", lambda _path: [])
    monkeypatch.setattr(mctrl_status, "_live_tmux_sessions", lambda: {})
    monkeypatch.setattr(
        mctrl_status,
        "outbox_health_snapshot",
        lambda **_kwargs: {
            "pending_count": 3,
            "dead_letter_count": 1,
            "oldest_age_seconds": 120,
            "retry_histogram": {"0": 2, "2": 1, "10": 1},
        },
    )

    mctrl_status.print_status(active_only=True)
    output = capsys.readouterr().out

    assert "Outbox: 3 pending" in output
    assert "dead-letter 1" in output
    assert "r0:2" in output
    assert "r2:1" in output
    assert output.index("r2:1") < output.index("r10:1")
