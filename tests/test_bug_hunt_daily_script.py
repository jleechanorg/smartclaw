from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]
BUG_HUNT_SCRIPT = REPO_ROOT / "scripts" / "bug-hunt-daily.sh"
THREAD_NUDGE_SCRIPT = REPO_ROOT / "scripts" / "thread-reply-nudge.sh"


def read(path: Path) -> str:
    return path.read_text()


def test_changed_scripts_are_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(BUG_HUNT_SCRIPT)], check=True)
    subprocess.run(["bash", "-n", str(THREAD_NUDGE_SCRIPT)], check=True)


def test_bug_hunt_uses_one_shot_openclaw_not_fire_and_forget_ao() -> None:
    text = read(BUG_HUNT_SCRIPT)

    assert "ao spawn" not in text
    assert "ao send" not in text
    assert "openclaw agent --agent" in text
    assert "wait \"$FIX_PID\"" in text


def test_bug_hunt_watchdog_targets_resolved_process_group() -> None:
    text = read(BUG_HUNT_SCRIPT)

    assert "set -m" in text
    assert "shopt -s monitor" not in text
    assert "terminate_process_tree" in text
    assert "run_openclaw_agent_help" in text
    assert "ps -o pgid= -p \"$pid\"" in text
    assert "kill -TERM \"-$pgid\"" in text


def test_bug_hunt_caches_openclaw_agent_preflight() -> None:
    text = read(BUG_HUNT_SCRIPT)

    assert 'OPENCLAW_AGENT_AVAILABLE=1' in text
    assert 'OPENCLAW_PREFLIGHT_ERR="${BUG_REPORTS_DIR}/bug-hunt-openclaw-preflight-${TIMESTAMP}.err"' in text
    assert 'configure_openclaw_agent "$OPENCLAW_PREFLIGHT_ERR"' in text
    assert 'configure_openclaw_agent "$ERR_FILE"' not in text
    assert 'configure_openclaw_agent "$FIX_LOG"' not in text


def test_bug_hunt_only_uses_short_message_flag_when_help_lists_it() -> None:
    text = read(BUG_HUNT_SCRIPT)

    assert "grep -q -- '--message'" in text
    assert "grep -Eq '(^|[[:space:],])-m([,[:space:]]|$)'" in text
    assert 'OPENCLAW_MESSAGE_FLAG="-m"' in text


def test_bug_hunt_missing_openclaw_writes_benign_artifact() -> None:
    text = read(BUG_HUNT_SCRIPT)

    assert "write_empty_findings" in text
    assert "printf '[]\\n' > \"$output_file\"" in text
    assert "write_empty_findings \"$OUTPUT_FILE\"" in text


def test_bug_hunt_empty_agent_pid_array_is_safe_for_bash_32() -> None:
    text = read(BUG_HUNT_SCRIPT)

    assert 'if [ "${#AGENT_PIDS[@]}" -eq 0 ]; then' in text

    subprocess.run(
        [
            "/bin/bash",
            "-c",
            'set -euo pipefail; AGENT_PIDS=(); '
            'if [ "${#AGENT_PIDS[@]}" -eq 0 ]; then :; '
            'else for PID in "${AGENT_PIDS[@]}"; do :; done; fi',
        ],
        check=True,
    )


def test_bug_hunt_dedupe_jq_expression_compiles() -> None:
    expression = 'unique_by("\\(.repo)\\(.pr)\\(.file)\\(.line)\\(.description)")'
    sample = '[{"repo":"r","pr":1,"file":"f","line":2,"description":"d"}]'

    subprocess.run(["jq", expression], input=sample, text=True, check=True, capture_output=True)
    assert expression in read(BUG_HUNT_SCRIPT)


def test_thread_nudge_verifies_agent_subcommand_and_uses_message_flag() -> None:
    text = read(THREAD_NUDGE_SCRIPT)

    assert "openclaw agent --help" in text
    assert "OPENCLAW_HELP_TIMEOUT_SECONDS" in text
    assert 'agent_message_flag="--message"' in text
    assert "grep -q -- '--message'" in text
    assert "openclaw agent --agent main \"$agent_message_flag\" \"$PROMPT\"" in text
    assert "openclaw agent --agent main -m" not in text
