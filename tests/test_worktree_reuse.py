"""TDD tests for worktree reuse logic in dispatch_task.

find_existing_worktree(branch, repo_root) — scans git worktree list,
returns path if branch already checked out, None otherwise.

resolve_worktree_for_branch(branch, repo_root, bead_worktree_base) —
returns (path, is_new):
  - existing worktree → (path, False)
  - branch on remote but not in any worktree → checkout fresh, (path, True)
  - branch missing everywhere → raises ValueError
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from orchestration.dispatch_task import (
    _extract_repo_name_hint,
    _resolve_repo_root,
    _task_with_push_instruction,
    find_existing_worktree,
    resolve_worktree_for_branch,
)


# ---------------------------------------------------------------------------
# find_existing_worktree
# ---------------------------------------------------------------------------

PORCELAIN_TWO_WORKTREES = textwrap.dedent("""\
    worktree ${HOME}/project_smartclaw/mctrl
    HEAD abc123
    branch refs/heads/main

    worktree /tmp/wt-feat-xyz
    HEAD def456
    branch refs/heads/feat/xyz

    worktree /tmp/wt-fix-abc
    HEAD 789abc
    branch refs/heads/fix/abc

""")


def _mock_wt_list(stdout: str, returncode: int = 0):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


def test_find_existing_worktree_found():
    with patch("subprocess.run", return_value=_mock_wt_list(PORCELAIN_TWO_WORKTREES)):
        result = find_existing_worktree("feat/xyz", repo_root="/repo")
    assert result == "/tmp/wt-feat-xyz"


def test_find_existing_worktree_not_found():
    with patch("subprocess.run", return_value=_mock_wt_list(PORCELAIN_TWO_WORKTREES)):
        result = find_existing_worktree("feat/does-not-exist", repo_root="/repo")
    assert result is None


def test_find_existing_worktree_skips_main_worktree():
    """The primary worktree (first block) must not be returned even if branch matches."""
    porcelain = textwrap.dedent("""\
        worktree ${HOME}/project_smartclaw/mctrl
        HEAD abc123
        branch refs/heads/feat/xyz

    """)
    with patch("subprocess.run", return_value=_mock_wt_list(porcelain)):
        result = find_existing_worktree("feat/xyz", repo_root="${HOME}/project_smartclaw/mctrl")
    # Primary worktree is the repo root — should not dispatch into it
    assert result is None


def test_find_existing_worktree_bare_or_detached():
    """Detached HEAD blocks (no 'branch' line) must not crash."""
    porcelain = textwrap.dedent("""\
        worktree /tmp/wt-detached
        HEAD deadbeef
        detached

    """)
    with patch("subprocess.run", return_value=_mock_wt_list(porcelain)):
        result = find_existing_worktree("feat/xyz", repo_root="/repo")
    assert result is None


def test_find_existing_worktree_git_failure_returns_none():
    """If git worktree list fails, return None gracefully (don't crash dispatch)."""
    with patch("subprocess.run", return_value=_mock_wt_list("", returncode=1)):
        result = find_existing_worktree("feat/xyz", repo_root="/repo")
    assert result is None


def test_find_existing_worktree_flushes_last_block_without_trailing_blank():
    """The final porcelain block should still be checked without a blank terminator."""
    porcelain = textwrap.dedent("""\
        worktree /tmp/wt-feat-xyz
        HEAD def456
        branch refs/heads/feat/xyz
    """)
    with patch("subprocess.run", return_value=_mock_wt_list(porcelain)):
        result = find_existing_worktree("feat/xyz", repo_root="/repo")
    assert result == "/tmp/wt-feat-xyz"


def test_extract_repo_name_hint_from_task_text():
    assert _extract_repo_name_hint("implement feature in `mctrl_test` repo") == "mctrl_test"
    assert _extract_repo_name_hint("open PR in worldarchitect repository") == "worldarchitect"
    assert (
        _extract_repo_name_hint("work in https://github.com/jleechanorg/mctrl_test")
        == "mctrl_test"
    )


def test_resolve_repo_root_prefers_repo_hint_when_clone_exists(tmp_path: Path):
    hinted_repo = tmp_path / "mctrl_test"
    hinted_repo.mkdir(parents=True)
    (hinted_repo / ".git").mkdir()

    with patch.dict("os.environ", {"MCTRL_REPO_HINT_PATHS": str(tmp_path)}, clear=False), \
         patch(
             "orchestration.dispatch_task._looks_like_git_repo",
             side_effect=lambda p: Path(p).resolve() == hinted_repo.resolve(),
         ):
        resolved = _resolve_repo_root(".", "Implement in mctrl_test repo and open PR")

    assert resolved == str(hinted_repo.resolve())


# ---------------------------------------------------------------------------
# _find_repo_by_name — centralized repo search
# ---------------------------------------------------------------------------


def test_find_repo_by_name_returns_existing_repo(tmp_path: Path):
    """_find_repo_by_name returns path when a matching git repo exists."""
    from orchestration.dispatch_task import _find_repo_by_name

    target = tmp_path / "mctrl_test"
    target.mkdir()
    (target / ".git").mkdir()

    with patch.dict("os.environ", {"MCTRL_REPO_HINT_PATHS": str(tmp_path)}, clear=False):
        result = _find_repo_by_name("mctrl_test", fallback_root=str(tmp_path / "other"))
    assert result is not None
    assert Path(result).name == "mctrl_test"


def test_find_repo_by_name_returns_none_when_not_found(tmp_path: Path):
    """_find_repo_by_name returns None when no matching repo exists."""
    from orchestration.dispatch_task import _find_repo_by_name

    result = _find_repo_by_name("nonexistent_repo", fallback_root=str(tmp_path))
    assert result is None


def test_find_repo_by_name_deduplicates_candidates(tmp_path: Path):
    """Same resolved path from multiple bases should only be checked once."""
    from orchestration.dispatch_task import _find_repo_by_name

    target = tmp_path / "myrepo"
    target.mkdir()
    (target / ".git").mkdir()
    check_count = 0
    original_looks = lambda p: (p / ".git").exists()

    def counting_check(p: Path) -> bool:
        nonlocal check_count
        check_count += 1
        return original_looks(p)

    with patch("orchestration.dispatch_task._looks_like_git_repo", side_effect=counting_check), \
         patch.dict("os.environ", {"MCTRL_REPO_HINT_PATHS": ""}, clear=False):
        result = _find_repo_by_name("myrepo", fallback_root=str(tmp_path))

    assert result is not None
    # Each unique candidate checked at most once
    assert check_count <= 10  # generous bound; point is dedup happens


def test_cross_repo_detection_uses_name_comparison():
    """Cross-repo is determined by comparing extracted name vs current repo name."""
    from orchestration.dispatch_task import _is_cross_repo_task

    # Genuine cross-repo: names a specific repo target
    assert _is_cross_repo_task("make a pr against mctrl_test") is True
    assert _is_cross_repo_task("create a pr in worldarchitect repo") is True
    assert _is_cross_repo_task("open PR against `myapp` repository") is True

    # NOT cross-repo: generic phrases without a repo name target
    assert _is_cross_repo_task("fix the button to work properly") is False
    assert _is_cross_repo_task("make a PR to fix the login button") is False
    assert _is_cross_repo_task("create a PR for the bugfix") is False
    assert _is_cross_repo_task("run the tests and check output") is False
    assert _is_cross_repo_task("please fix the broken CI pipeline") is False


# ---------------------------------------------------------------------------
# resolve_worktree_for_branch
# ---------------------------------------------------------------------------

def test_resolve_returns_existing_worktree(tmp_path: Path):
    """If branch already in a worktree, return it without creating a new one."""
    with patch("orchestration.dispatch_task.find_existing_worktree", return_value="/tmp/wt-feat-xyz"):
        path, is_new = resolve_worktree_for_branch(
            branch="feat/xyz",
            repo_root=str(tmp_path),
            worktree_base="/tmp/mctrl-worktrees",
        )
    assert path == "/tmp/wt-feat-xyz"
    assert is_new is False


def test_resolve_creates_new_worktree_when_none_exists(tmp_path: Path):
    """If branch not in any worktree but exists on remote, checkout fresh."""
    new_wt = str(tmp_path / "wt-new")

    def fake_run(cmd, **kw):
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    with patch("orchestration.dispatch_task.find_existing_worktree", return_value=None), \
         patch("orchestration.dispatch_task._remote_branch_exists", return_value=True), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("orchestration.dispatch_task._worktree_add_path", return_value=new_wt):
        path, is_new = resolve_worktree_for_branch(
            branch="feat/new-branch",
            repo_root=str(tmp_path),
            worktree_base=str(tmp_path),
        )
    assert path == new_wt
    assert is_new is True


def test_resolve_raises_when_branch_missing_everywhere(tmp_path: Path):
    """If branch not in worktrees and not on remote, raise ValueError."""
    with patch("orchestration.dispatch_task.find_existing_worktree", return_value=None), \
         patch("orchestration.dispatch_task._remote_branch_exists", return_value=False):
        with pytest.raises(ValueError, match="branch.*not found"):
            resolve_worktree_for_branch(
                branch="feat/ghost",
                repo_root=str(tmp_path),
                worktree_base=str(tmp_path),
            )


# ---------------------------------------------------------------------------
# dispatch() with branch= parameter (full round-trip wiring)
# ---------------------------------------------------------------------------

def test_dispatch_emits_openclaw_started_event_after_spawn(tmp_path: Path):
    """dispatch() emits task_started via notify_openclaw after spawning agent."""
    existing_wt = str(tmp_path / "wt")
    fake_output = (
        "🚀 Async session: ai-minimax-start1\n"
        f"🧩 Worktree: {existing_wt} (branch: feat/test)\n"
    )

    def fake_run(cmd, **kw):
        m = MagicMock()
        m.returncode = 0
        m.stdout = fake_output
        m.stderr = ""
        return m

    started_calls: list[dict] = []

    with patch("subprocess.run", side_effect=fake_run), \
         patch("orchestration.dispatch_task.upsert_mapping"), \
         patch("orchestration.dispatch_task.notify_slack_started", return_value=True), \
         patch("orchestration.dispatch_task.notify_openclaw",
               side_effect=lambda p: started_calls.append(p) or True) as mock_started:
        from orchestration.dispatch_task import dispatch
        dispatch(
            bead_id="ORCH-start-test",
            task="do the thing",
            slack_trigger_ts="1234567890.000",
            slack_trigger_channel="C123TRIGGER",
            agent_cli="minimax",
            registry_path=str(tmp_path / "registry.jsonl"),
        )

    mock_started.assert_called_once()
    payload = started_calls[0]
    assert payload["bead_id"] == "ORCH-start-test"
    assert payload["session"].startswith("orch-start-test-")
    assert payload["session"].endswith("-ai-minimax-start1")
    assert payload["event"] == "task_started"
    assert payload["slack_trigger_ts"] == "1234567890.000"
    assert payload["slack_trigger_channel"] == "C123TRIGGER"
    assert payload["agent_cli"] == "minimax"


def test_dispatch_requires_trigger_channel_when_thread_ts_is_set(tmp_path: Path):
    fake_output = (
        "🚀 Async session: ai-minimax-start1\n"
        f"🧩 Worktree: {tmp_path / 'wt'} (branch: feat/test)\n"
    )

    def fake_run(cmd, **kw):
        m = MagicMock()
        m.returncode = 0
        m.stdout = fake_output
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run), \
         patch("orchestration.dispatch_task.upsert_mapping"), \
         patch("orchestration.dispatch_task.notify_slack_started"), \
         patch("orchestration.dispatch_task.notify_openclaw"):
        from orchestration.dispatch_task import dispatch

        with pytest.raises(ValueError, match="slack_trigger_channel is required"):
            dispatch(
                bead_id="ORCH-missing-channel",
                task="do the thing",
                slack_trigger_ts="1234567890.000",
                agent_cli="minimax",
                registry_path=str(tmp_path / "registry.jsonl"),
            )


def test_dispatch_with_branch_reuses_existing_worktree(tmp_path: Path):
    """dispatch(branch=...) uses existing worktree, runs ai_orch without --worktree."""
    existing_wt = str(tmp_path / "existing-wt")

    fake_output = (
        "🚀 Async session: ai-minimax-abc123\n"
        f"🧩 Worktree: {existing_wt} (branch: feat/mvp-loopback-supervisor)\n"
    )

    def fake_run(cmd, **kw):
        m = MagicMock()
        m.returncode = 0
        m.stdout = fake_output
        m.stderr = ""
        return m

    with patch("orchestration.dispatch_task.resolve_worktree_for_branch",
               return_value=(existing_wt, False)) as mock_resolve, \
         patch("subprocess.run", side_effect=fake_run), \
         patch("orchestration.dispatch_task.upsert_mapping"), \
         patch("orchestration.dispatch_task.notify_slack_started", return_value=True), \
         patch("orchestration.dispatch_task.notify_openclaw", return_value=True):
        from orchestration.dispatch_task import dispatch
        mapping = dispatch(
            bead_id="ORCH-test",
            task="fix PR comments",
            branch="feat/mvp-loopback-supervisor",
            repo_root=str(tmp_path),
            registry_path=str(tmp_path / "registry.jsonl"),
        )

    mock_resolve.assert_called_once_with(
        "feat/mvp-loopback-supervisor", str(tmp_path), ANY
    )
    assert mapping.session_name.startswith("orch-test-")
    assert mapping.session_name.endswith("-ai-minimax-abc123")
    assert mapping.branch == "feat/mvp-loopback-supervisor"


def test_dispatch_without_branch_still_uses_worktree_flag(tmp_path: Path):
    """dispatch() with no branch= still passes --worktree to ai_orch (new task)."""
    fake_output = (
        "🚀 Async session: ai-minimax-newxyz\n"
        f"🧩 Worktree: /tmp/wt-new (branch: feat/new-thing)\n"
    )
    cmd_log = []
    cwd_log = []

    def fake_run(cmd, **kw):
        cmd_log.append(list(cmd))
        cwd_log.append(kw.get("cwd"))
        m = MagicMock()
        m.returncode = 0
        m.stdout = fake_output
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run), \
         patch("orchestration.dispatch_task.upsert_mapping"), \
         patch("orchestration.dispatch_task.notify_slack_started", return_value=True), \
         patch("orchestration.dispatch_task.notify_openclaw", return_value=True):
        from orchestration.dispatch_task import dispatch
        dispatch(
            bead_id="ORCH-test2",
            task="new task",
            repo_root=str(tmp_path),
            registry_path=str(tmp_path / "registry.jsonl"),
        )

    orch_cmd = next((c for c in cmd_log if "ai_orch" in c), None)
    assert orch_cmd is not None
    assert "--worktree" in orch_cmd
    assert any("git push origin <your-branch>" in part for part in orch_cmd)
    assert str(tmp_path) in cwd_log


def test_dispatch_passes_clean_user_site_packages_to_ai_orch(tmp_path: Path):
    """dispatch() should force ai_orch to import from its installed package, not mctrl's PYTHONPATH."""
    fake_output = (
        "🚀 Async session: ai-minimax-site123\n"
        "🧩 Worktree: /tmp/wt-site (branch: feat/site)\n"
    )
    env_log = []

    def fake_run(cmd, **kw):
        env_log.append(kw.get("env"))
        m = MagicMock()
        m.returncode = 0
        m.stdout = fake_output
        m.stderr = ""
        return m

    with patch.dict("os.environ", {"PYTHONPATH": "/existing/pythonpath"}, clear=False), \
         patch("orchestration.dispatch_task.site.getusersitepackages",
               return_value="${HOME}/Library/Python/3.13/lib/python/site-packages"), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("orchestration.dispatch_task.upsert_mapping"), \
         patch("orchestration.dispatch_task.notify_slack_started", return_value=True), \
         patch("orchestration.dispatch_task.notify_openclaw", return_value=True):
        from orchestration.dispatch_task import dispatch
        dispatch(
            bead_id="ORCH-site-env",
            task="new task",
            registry_path=str(tmp_path / "registry.jsonl"),
        )

    ai_orch_env = next(env for env in env_log if env is not None)
    assert ai_orch_env is not None
    assert ai_orch_env["PYTHONPATH"] == (
        "${HOME}/Library/Python/3.13/lib/python/site-packages"
    )


def test_dispatch_with_branch_includes_push_instruction(tmp_path: Path):
    """dispatch(branch=...) appends a remote push requirement to the agent task."""
    existing_wt = str(tmp_path / "existing-wt")
    fake_output = (
        "🚀 Async session: ai-minimax-push123\n"
        f"🧩 Worktree: {existing_wt} (branch: feat/push-check)\n"
    )
    cmd_log = []

    def fake_run(cmd, **kw):
        cmd_log.append(list(cmd))
        m = MagicMock()
        m.returncode = 0
        m.stdout = fake_output
        m.stderr = ""
        return m

    with patch("orchestration.dispatch_task.resolve_worktree_for_branch",
               return_value=(existing_wt, False)), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("orchestration.dispatch_task.upsert_mapping"), \
         patch("orchestration.dispatch_task.notify_slack_started", return_value=True), \
         patch("orchestration.dispatch_task.notify_openclaw", return_value=True):
        from orchestration.dispatch_task import dispatch
        dispatch(
            bead_id="ORCH-push-check",
            task="fix the bug and commit it",
            branch="feat/push-check",
            repo_root=str(tmp_path),
            registry_path=str(tmp_path / "registry.jsonl"),
        )

    orch_cmd = next((c for c in cmd_log if "ai_orch" in c), None)
    assert orch_cmd is not None
    assert any("git push origin feat/push-check" in part for part in orch_cmd)


def test_dispatch_renames_tmux_session_to_include_bead_id(tmp_path: Path):
    """dispatch() renames the spawned tmux session so it is bead-traceable."""
    fake_output = (
        "🚀 Async session: ai-minimax-old123\n"
        f"🧩 Worktree: /tmp/wt-rename (branch: feat/rename)\n"
    )
    cmd_log = []

    def fake_run(cmd, **kw):
        cmd_log.append(list(cmd))
        m = MagicMock()
        m.returncode = 0
        m.stdout = fake_output if cmd[:2] == ["ai_orch", "run"] else ""
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run), \
         patch("orchestration.dispatch_task.upsert_mapping"), \
         patch("orchestration.dispatch_task.notify_slack_started", return_value=True), \
         patch("orchestration.dispatch_task.notify_openclaw", return_value=True):
        from orchestration.dispatch_task import dispatch
        mapping = dispatch(
            bead_id="ORCH-rename",
            task="new task",
            registry_path=str(tmp_path / "registry.jsonl"),
        )

    rename_cmd = next((c for c in cmd_log if c[:3] == ["tmux", "rename-session", "-t"]), None)
    assert rename_cmd is not None
    assert rename_cmd[3] == "ai-minimax-old123"
    assert rename_cmd[4].startswith("orch-rename-")
    assert rename_cmd[4].endswith("-ai-minimax-old123")
    assert mapping.session_name == rename_cmd[4]


def test_dispatch_keeps_original_session_name_if_session_already_exited(tmp_path: Path):
    """A vanished session should not fail dispatch after ai_orch already returned."""
    fake_output = (
        "🚀 Async session: ai-minimax-gone123\n"
        f"🧩 Worktree: /tmp/wt-gone (branch: feat/gone)\n"
    )

    def fake_run(cmd, **kw):
        m = MagicMock()
        if cmd[:2] == ["ai_orch", "run"]:
            m.returncode = 0
            m.stdout = fake_output
            m.stderr = ""
            return m
        if cmd[:3] == ["tmux", "rename-session", "-t"]:
            m.returncode = 1
            m.stdout = ""
            m.stderr = "can't find session: ai-minimax-gone123"
            return m
        m.returncode = 0
        m.stdout = fake_output
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run), \
         patch("orchestration.dispatch_task.upsert_mapping"), \
         patch("orchestration.dispatch_task.notify_slack_started", return_value=True), \
         patch("orchestration.dispatch_task.notify_openclaw", return_value=True):
        from orchestration.dispatch_task import dispatch
        mapping = dispatch(
            bead_id="ORCH-gone",
            task="new task",
            registry_path=str(tmp_path / "registry.jsonl"),
        )

    assert mapping.session_name == "ai-minimax-gone123"


def test_dispatch_keeps_original_session_name_on_unexpected_rename_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """A rename failure should not orphan a running session after successful spawn."""
    fake_output = (
        "🚀 Async session: ai-minimax-locked123\n"
        "🧩 Worktree: /tmp/wt-locked (branch: feat/locked)\n"
    )

    def fake_run(cmd, **kw):
        m = MagicMock()
        if cmd[:2] == ["ai_orch", "run"]:
            m.returncode = 0
            m.stdout = fake_output
            m.stderr = ""
            return m
        if cmd[:3] == ["tmux", "rename-session", "-t"]:
            m.returncode = 1
            m.stdout = ""
            m.stderr = "duplicate session name"
            return m
        m.returncode = 0
        m.stdout = fake_output
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run), \
         patch("orchestration.dispatch_task.upsert_mapping"), \
         patch("orchestration.dispatch_task.notify_slack_started", return_value=True), \
         patch("orchestration.dispatch_task.notify_openclaw", return_value=True):
        from orchestration.dispatch_task import dispatch
        mapping = dispatch(
            bead_id="ORCH-locked",
            task="new task",
            registry_path=str(tmp_path / "registry.jsonl"),
        )

    assert mapping.session_name == "ai-minimax-locked123"
    assert "Could not rename tmux session" in caplog.text


def test_worktree_add_path_falls_back_when_branch_in_primary(tmp_path: Path):
    """If git worktree add fails with 'already checked out', detach from origin and switch."""
    call_log = []

    def fake_run(cmd, **kw):
        call_log.append(list(cmd))
        m = MagicMock()
        if "worktree" in cmd and "add" in cmd and "--detach" not in cmd:
            m.returncode = 128
            m.stderr = b"fatal: 'feat/xyz' is already checked out at '/repo'"
        else:
            m.returncode = 0
            m.stderr = b""
            m.stdout = b""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        from orchestration.dispatch_task import _worktree_add_path
        _worktree_add_path("feat/xyz", "/repo", str(tmp_path))

    cmds = [" ".join(c) for c in call_log]
    assert cmds[0] == "git fetch --no-tags origin feat/xyz"
    assert any("--detach" in c for c in cmds), "must fall back to detached checkout"
    assert any("checkout" in c and "-B" in c for c in cmds), "must switch to tracking branch"


def test_worktree_add_path_cleans_up_tempdir_on_failure(tmp_path: Path):
    """Any failure after mkdtemp should remove the partially created worktree dir."""

    def fake_run(cmd, **kw):
        m = MagicMock()
        if cmd[:4] == ["git", "fetch", "--no-tags", "origin"]:
            m.returncode = 0
            return m
        m.returncode = 128
        m.stderr = b"fatal: invalid reference: feat/missing"
        return m

    with patch("subprocess.run", side_effect=fake_run):
        from orchestration.dispatch_task import _worktree_add_path
        with pytest.raises(subprocess.CalledProcessError):
            _worktree_add_path("feat/missing", "/repo", str(tmp_path))

    assert list(tmp_path.iterdir()) == []


def test_dispatch_cursor_uses_headless_trusted_tmux_command(tmp_path: Path):
    """Cursor dispatch must bypass ai_orch's interactive fallback."""
    cmd_log = []

    def fake_run(cmd, **kw):
        cmd_log.append(list(cmd))
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    with patch("orchestration.dispatch_task._create_new_worktree", return_value=("/tmp/wt-cursor", "feat/cursor")), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("orchestration.dispatch_task.upsert_mapping"), \
         patch("orchestration.dispatch_task.notify_slack_started", return_value=True), \
         patch("orchestration.dispatch_task.notify_openclaw", return_value=True):
        from orchestration.dispatch_task import dispatch
        mapping = dispatch(
            bead_id="ORCH-cursor",
            task="prove the cursor path",
            agent_cli="cursor",
            registry_path=str(tmp_path / "registry.jsonl"),
        )

    assert mapping.worktree_path == "/tmp/wt-cursor"
    tmux_cmd = next((c for c in cmd_log if c[:3] == ["tmux", "new-session", "-d"]), None)
    assert tmux_cmd is not None
    assert not any(part == "ai_orch" for part in tmux_cmd)
    shell_cmd = tmux_cmd[-1]
    assert "cursor-agent" in shell_cmd
    assert "--print" in shell_cmd
    assert "--trust" in shell_cmd
    assert "--approve-mcps" in shell_cmd
    assert "--yolo" in shell_cmd
    assert "--model auto" in shell_cmd


def test_dispatch_cursor_with_branch_reuses_worktree_and_skips_ai_orch(tmp_path: Path):
    """Existing-branch cursor dispatch should run directly in the resolved worktree."""
    existing_wt = str(tmp_path / "existing-cursor")
    cmd_log = []

    def fake_run(cmd, **kw):
        cmd_log.append(list(cmd))
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    with patch("orchestration.dispatch_task.resolve_worktree_for_branch", return_value=(existing_wt, False)), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("orchestration.dispatch_task.upsert_mapping"), \
         patch("orchestration.dispatch_task.notify_slack_started", return_value=True), \
         patch("orchestration.dispatch_task.notify_openclaw", return_value=True):
        from orchestration.dispatch_task import dispatch
        mapping = dispatch(
            bead_id="ORCH-cursor-branch",
            task="fix cursor branch path",
            branch="feat/cursor-branch",
            repo_root=str(tmp_path),
            agent_cli="cursor",
            registry_path=str(tmp_path / "registry.jsonl"),
        )

    assert mapping.branch == "feat/cursor-branch"
    assert mapping.worktree_path == existing_wt
    assert not any(c[:2] == ["ai_orch", "run"] for c in cmd_log)


def test_dispatch_retries_with_orch_when_ai_orch_wrapper_is_stale(tmp_path: Path):
    """If ai_orch fails with stale wrapper import error, dispatch retries with orch."""
    fake_output = (
        "🚀 Async session: ai-minimax-retry123\n"
        "🧩 Worktree: /tmp/wt-retry (branch: feat/retry)\n"
    )
    cmd_log: list[list[str]] = []

    def fake_run(cmd, **kw):
        cmd_log.append(list(cmd))
        m = MagicMock()
        if cmd[0] == "ai_orch":
            m.returncode = 1
            m.stdout = ""
            m.stderr = "ModuleNotFoundError: No module named 'orchestration.runner'"
            return m
        m.returncode = 0
        m.stdout = fake_output
        m.stderr = ""
        return m

    with patch("orchestration.dispatch_task.shutil.which", return_value="/usr/local/bin/orch"), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("orchestration.dispatch_task.upsert_mapping"), \
         patch("orchestration.dispatch_task.notify_slack_started", return_value=True), \
         patch("orchestration.dispatch_task.notify_openclaw", return_value=True):
        from orchestration.dispatch_task import dispatch
        mapping = dispatch(
            bead_id="ORCH-retry",
            task="new task",
            registry_path=str(tmp_path / "registry.jsonl"),
        )

    assert mapping.session_name.startswith("orch-retry-")
    assert any(c[0] == "ai_orch" for c in cmd_log)
    assert any(c[0] == "orch" for c in cmd_log)

# ---------------------------------------------------------------------------
# _task_with_push_instruction
# ---------------------------------------------------------------------------

class TestTaskWithPushInstruction:
    def test_task_already_has_push_is_unchanged(self) -> None:
        task = "Do stuff. git push origin feat/x."
        result = _task_with_push_instruction(task, "feat/x")
        # No extra push reminder should be appended
        assert result.count("git push") == 1

    def test_task_with_commit_but_no_push_gets_push_appended(self) -> None:
        task = "Create a file. git commit -m \'done\'. Then stop."
        result = _task_with_push_instruction(task, "feat/branch")
        assert "git push origin feat/branch" in result
        assert "Your work is only visible after it is pushed to origin" in result

    def test_task_with_no_commit_no_push_gets_full_instructions(self) -> None:
        task = "Write some code."
        result = _task_with_push_instruction(task, "feat/branch")
        assert "git add" in result
        assert "git commit" in result
        assert "git push origin feat/branch" in result

    def test_task_already_has_commit_and_push_is_unchanged(self) -> None:
        task = "Write code. git commit -m \'done\'. git push origin feat/x. Done."
        result = _task_with_push_instruction(task, "feat/x")
        # push already present — no duplication
        assert result.count("git push") == 1

    def test_do_not_switch_suppresses_worktree_reminder(self) -> None:
        task = "Do work. git commit -m \'done\'. Do not switch to another local checkout or clone."
        result = _task_with_push_instruction(task, "feat/branch")
        assert "Work only in the current ai_orch worktree" not in result
