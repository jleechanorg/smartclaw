"""Tests for orchestration.session_tail — live tmux session output viewer."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from orchestration.session_tail import (
    build_parser,
    capture_pane,
    check_session_exists,
    tail_session,
)


# ---------------------------------------------------------------------------
# check_session_exists()
# ---------------------------------------------------------------------------


class TestCheckSessionExists:
    @patch("orchestration.session_tail.subprocess.run")
    def test_existing_session(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert check_session_exists("my-agent") is True
        mock_run.assert_called_once_with(
            ["tmux", "has-session", "-t", "my-agent"],
            capture_output=True,
            timeout=5,
        )

    @patch("orchestration.session_tail.subprocess.run")
    def test_missing_session_returns_false(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        assert check_session_exists("ghost-session") is False

    @patch(
        "orchestration.session_tail.subprocess.run",
        side_effect=FileNotFoundError("tmux not found"),
    )
    def test_tmux_not_installed(self, mock_run):
        assert check_session_exists("session") is False

    @patch(
        "orchestration.session_tail.subprocess.run",
        side_effect=subprocess.TimeoutExpired("tmux", 5),
    )
    def test_timeout_returns_false(self, mock_run):
        assert check_session_exists("session") is False


# ---------------------------------------------------------------------------
# capture_pane()
# ---------------------------------------------------------------------------


class TestCapturePane:
    @patch("orchestration.session_tail.subprocess.run")
    def test_captures_output(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="line1\nline2\nline3\n")
        output = capture_pane("my-agent")
        assert output == "line1\nline2\nline3\n"
        mock_run.assert_called_once_with(
            ["tmux", "capture-pane", "-t", "my-agent", "-p", "-S", "-5000"],
            capture_output=True,
            text=True,
            timeout=10,
        )

    @patch("orchestration.session_tail.subprocess.run")
    def test_custom_history_lines(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="output\n")
        capture_pane("session", history_lines=100)
        args = mock_run.call_args[0][0]
        assert "-100" in args

    @patch("orchestration.session_tail.subprocess.run")
    def test_raises_on_tmux_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="no session named bad")
        with pytest.raises(subprocess.SubprocessError, match="bad-session"):
            capture_pane("bad-session")


# ---------------------------------------------------------------------------
# tail_session()
# ---------------------------------------------------------------------------


class TestTailSessionMissingSession:
    @patch("orchestration.session_tail.check_session_exists", return_value=False)
    def test_returns_error_code(self, mock_check):
        code = tail_session("ghost-session")
        assert code == 1

    @patch("orchestration.session_tail.check_session_exists", return_value=False)
    def test_prints_error_to_stderr(self, mock_check, capsys):
        tail_session("ghost-session")
        err = capsys.readouterr().err
        assert "ghost-session" in err
        assert "not found" in err

    @patch("orchestration.session_tail.check_session_exists", return_value=False)
    def test_stderr_mentions_list_sessions(self, mock_check, capsys):
        tail_session("ghost-session")
        err = capsys.readouterr().err
        assert "tmux list-sessions" in err


class TestTailSessionOneShot:
    def _setup_capture(self, mock_capture, n_lines: int = 100) -> str:
        lines = "\n".join(f"line{i}" for i in range(n_lines))
        mock_capture.return_value = lines + "\n"
        return lines

    @patch("orchestration.session_tail.capture_pane")
    @patch("orchestration.session_tail.check_session_exists", return_value=True)
    def test_returns_zero(self, _check, mock_capture):
        self._setup_capture(mock_capture)
        assert tail_session("agent", follow=False, lines=10) == 0

    @patch("orchestration.session_tail.capture_pane")
    @patch("orchestration.session_tail.check_session_exists", return_value=True)
    def test_prints_last_n_lines(self, _check, mock_capture, capsys):
        self._setup_capture(mock_capture, n_lines=100)
        tail_session("agent", follow=False, lines=10)
        out_lines = [l for l in capsys.readouterr().out.split("\n") if l]
        assert len(out_lines) == 10
        assert out_lines[-1] == "line99"

    @patch("orchestration.session_tail.capture_pane")
    @patch("orchestration.session_tail.check_session_exists", return_value=True)
    def test_prints_all_lines_when_fewer_than_limit(self, _check, mock_capture, capsys):
        mock_capture.return_value = "alpha\nbeta\n"
        tail_session("agent", follow=False, lines=50)
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "beta" in out

    @patch(
        "orchestration.session_tail.capture_pane",
        side_effect=subprocess.SubprocessError("pane gone"),
    )
    @patch("orchestration.session_tail.check_session_exists", return_value=True)
    def test_capture_error_returns_one(self, _check, _capture, capsys):
        code = tail_session("agent")
        assert code == 1
        assert "ERROR" in capsys.readouterr().err


class TestTailSessionFollow:
    @patch("orchestration.session_tail.time.sleep", side_effect=KeyboardInterrupt)
    @patch("orchestration.session_tail.capture_pane", return_value="l1\nl2\nl3\n")
    @patch("orchestration.session_tail.check_session_exists", return_value=True)
    def test_follow_honors_initial_lines_limit(
        self,
        _check,
        _capture,
        _sleep,
        capsys,
    ):
        code = tail_session("agent", follow=True, lines=2, poll_interval=0.01)
        assert code == 0
        out_lines = [line for line in capsys.readouterr().out.splitlines() if line]
        assert out_lines[:2] == ["l2", "l3"]
        assert "l1" not in out_lines

    @patch("orchestration.session_tail.time.sleep", side_effect=[None, KeyboardInterrupt])
    @patch(
        "orchestration.session_tail.capture_pane",
        side_effect=["a\n", "a\nb\n"],
    )
    @patch("orchestration.session_tail.check_session_exists", return_value=True)
    def test_follow_prints_newly_appended_line(
        self,
        _check,
        _capture,
        _sleep,
        capsys,
    ):
        code = tail_session("agent", follow=True, lines=50, poll_interval=0.01)
        assert code == 0
        out_lines = [line for line in capsys.readouterr().out.splitlines() if line]
        assert "a" in out_lines
        assert "b" in out_lines


# ---------------------------------------------------------------------------
# build_parser() — argument parsing
# ---------------------------------------------------------------------------


class TestBuildParser:
    def _parse(self, argv: list[str]):
        return build_parser().parse_args(argv)

    def test_logs_defaults(self):
        args = self._parse(["logs", "my-agent"])
        assert args.command == "logs"
        assert args.session == "my-agent"
        assert args.follow is False
        assert args.lines == 50
        assert args.interval == 1.0

    def test_logs_follow_long_flag(self):
        args = self._parse(["logs", "--follow", "my-agent"])
        assert args.follow is True

    def test_logs_follow_short_flag(self):
        args = self._parse(["logs", "-f", "my-agent"])
        assert args.follow is True

    def test_logs_custom_lines_long(self):
        args = self._parse(["logs", "--lines", "100", "my-agent"])
        assert args.lines == 100

    def test_logs_custom_lines_short(self):
        args = self._parse(["logs", "-n", "20", "my-agent"])
        assert args.lines == 20

    def test_logs_custom_interval(self):
        args = self._parse(["logs", "--interval", "2.5", "my-agent"])
        assert args.interval == 2.5

    def test_logs_rejects_non_positive_lines(self):
        with pytest.raises(SystemExit):
            self._parse(["logs", "--lines", "0", "my-agent"])

    def test_logs_rejects_non_positive_interval(self):
        with pytest.raises(SystemExit):
            self._parse(["logs", "--interval", "-1", "my-agent"])

    def test_tail_subcommand_defaults(self):
        args = self._parse(["tail", "my-agent"])
        assert args.command == "tail"
        assert args.session == "my-agent"
        assert args.lines == 50
        assert args.interval == 1.0

    def test_tail_custom_interval(self):
        args = self._parse(["tail", "--interval", "3.0", "my-agent"])
        assert args.interval == 3.0

    def test_tail_custom_lines(self):
        args = self._parse(["tail", "-n", "200", "my-agent"])
        assert args.lines == 200

    def test_tail_rejects_non_positive_lines(self):
        with pytest.raises(SystemExit):
            self._parse(["tail", "--lines", "-2", "my-agent"])

    def test_tail_rejects_non_positive_interval(self):
        with pytest.raises(SystemExit):
            self._parse(["tail", "--interval", "0", "my-agent"])

    def test_no_subcommand_parsed_as_none(self):
        args = build_parser().parse_args([])
        assert args.command is None
