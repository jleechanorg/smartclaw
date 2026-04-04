from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from orchestration.symphony_daemon import (
    build_launch_agent,
    build_runner_script,
    build_workflow,
)
from orchestration.symphony_plugins import list_plugins, load_plugin


REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_DAEMON_PATH = REPO_ROOT / "scripts" / "setup-symphony-daemon.py"


def _load_setup_daemon_module():
    spec = importlib.util.spec_from_file_location("setup_symphony_daemon", SETUP_DAEMON_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_workflow_uses_memory_tracker_and_operator_defaults() -> None:
    txt = build_workflow(
        workspace_root="/tmp/symphony/workspaces",
        workflow_title="General coding tasks",
        workflow_intro="Handle assigned coding tasks.",
        task_lines=["- TASK-1 Implement feature"],
        requirements=["Run tests", "Report outcomes"],
    )

    assert "kind: memory" in txt
    assert 'root: "/tmp/symphony/workspaces"' in txt
    assert "command: codex app-server" in txt
    assert "approval_policy: never" in txt
    assert "Task type: General coding tasks" in txt
    assert "1. Run tests" in txt


def test_build_runner_uses_default_operator_cli_path() -> None:
    txt = build_runner_script(
        symphony_elixir_dir="/Users/me/projects_reference/symphony/elixir",
        workflow_path="/tmp/symphony/WORKFLOW.md",
        node_name="symphonyd",
        cookie="cookie123",
        port=19191,
    )

    assert "/opt/homebrew/bin/mise exec -- /Users/me/projects_reference/symphony/elixir/bin/symphony" in txt
    assert "--i-understand-that-this-will-be-running-without-the-usual-guardrails" in txt
    assert "--port 19191" in txt
    assert "/tmp/symphony/WORKFLOW.md" in txt


def test_build_runner_shell_quotes_values_to_prevent_expansion() -> None:
    txt = build_runner_script(
        symphony_elixir_dir="/tmp/symphony dir",
        workflow_path="/tmp/workflow with spaces.md",
        node_name="node$name",
        cookie="cookie`whoami`",
        port=19191,
        mise_bin="/opt/homebrew/bin/mise",
    )

    assert "ERL_NODE_NAME='node$name'" in txt
    assert "ERL_COOKIE='cookie`whoami`'" in txt
    assert "cd '/tmp/symphony dir'" in txt
    assert "  '/tmp/workflow with spaces.md'" in txt
    assert "exec /opt/homebrew/bin/mise exec -- '/tmp/symphony dir/bin/symphony' \\" in txt


def test_build_runner_supports_runtime_cwd_override() -> None:
    txt = build_runner_script(
        symphony_elixir_dir="/opt/symphony/elixir",
        workflow_path="/tmp/workflow.md",
        node_name="symphonyd",
        cookie="cookie123",
        port=19191,
        runner_cwd="/tmp/jleechan-runtime",
    )

    assert "cd /tmp/jleechan-runtime" in txt
    assert "exec /opt/homebrew/bin/mise exec -- /opt/symphony/elixir/bin/symphony \\" in txt


def test_build_launch_agent_wires_runner_and_logs() -> None:
    plist = build_launch_agent(
        label="ai.symphony.daemon",
        runner_path="/tmp/symphony/run.sh",
        stdout_path="/tmp/symphony/stdout.log",
        stderr_path="/tmp/symphony/stderr.log",
    )
    assert plist["Label"] == "ai.symphony.daemon"
    assert plist["ProgramArguments"] == ["/bin/bash", "/tmp/symphony/run.sh"]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True


def test_plugin_registry_has_general_and_benchmark_plugins() -> None:
    names = list_plugins()
    assert "generic_tasks" in names
    assert "leetcode_hard" in names
    assert "swe_bench_verified" in names


def test_generic_tasks_plugin_parses_json(tmp_path: Path) -> None:
    fixture = REPO_ROOT / "tests" / "fixtures" / "generic_tasks_fixture.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    temp_fixture = tmp_path / "generic_tasks_fixture.json"
    temp_fixture.write_text(json.dumps(payload), encoding="utf-8")
    plugin = load_plugin("generic_tasks")
    issues = plugin.load_issues(str(temp_fixture))

    assert len(issues) == 2
    assert issues[0].identifier == "GEN-1"
    assert "general" in issues[0].labels


def test_swebench_plugin_parses_fixture(tmp_path: Path) -> None:
    fixture = REPO_ROOT / "tests" / "fixtures" / "swe_bench_verified_fixture.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    temp_fixture = tmp_path / "swe_bench_verified_fixture.json"
    temp_fixture.write_text(json.dumps(payload), encoding="utf-8")
    plugin = load_plugin("swe_bench_verified")
    issues = plugin.load_issues(str(temp_fixture))

    assert len(issues) == 1
    assert issues[0].identifier.startswith("SWE-")


def test_generic_tasks_plugin_raises_clear_error_for_invalid_payload(tmp_path: Path) -> None:
    bad_payload_path = tmp_path / "bad_generic.json"
    bad_payload_path.write_text(json.dumps({"tasks": [{"id": 1}]}), encoding="utf-8")
    plugin = load_plugin("generic_tasks")

    with pytest.raises(ValueError, match="generic_tasks: record\\[0\\].*title"):
        plugin.load_issues(str(bad_payload_path))


def test_swebench_plugin_raises_clear_error_for_invalid_payload(tmp_path: Path) -> None:
    bad_payload_path = tmp_path / "bad_swebench.json"
    bad_payload_path.write_text(json.dumps({"instances": [{}]}), encoding="utf-8")
    plugin = load_plugin("swe_bench_verified")

    with pytest.raises(ValueError, match="swe_bench_verified: record\\[0\\]"):
        plugin.load_issues(str(bad_payload_path))


def test_sym_skill_exists_and_mentions_keyword() -> None:
    skill_path = REPO_ROOT / "skills" / "sym" / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")

    assert "name: sym" in text
    assert "contains the word **sym**" in text
    assert "scripts/sym-dispatch.sh" in text


def test_sym_dispatch_uses_dynamic_task_ids_for_freeform_tasks() -> None:
    dispatch_script = (REPO_ROOT / "scripts" / "sym-dispatch.sh").read_text(encoding="utf-8")
    assert "uuid.uuid4().hex[:12]" in dispatch_script
    assert '"id": "1"' not in dispatch_script


def test_enqueue_script_preserves_explicit_false_assignment_flag() -> None:
    enqueue_script = (REPO_ROOT / "scripts" / "enqueue-symphony-memory-issues.exs").read_text(
        encoding="utf-8"
    )
    assert 'Map.fetch(item, "assigned_to_worker")' in enqueue_script
    assert "assigned_to_worker: assigned_to_worker" in enqueue_script


def test_enqueue_script_resolves_mise_from_env_or_metadata() -> None:
    enqueue_script = (REPO_ROOT / "scripts" / "enqueue-symphony-tasks.sh").read_text(encoding="utf-8")
    assert 'MISE_BIN="${MISE_BIN:-$(jq -r \'.mise_bin // empty\' "$METADATA")}"' in enqueue_script
    assert '"$MISE_BIN" exec -- mix run' in enqueue_script
    assert "Application Support/jleechanclaw/symphony_daemon" in enqueue_script


def test_setup_daemon_defaults_to_private_runtime_and_no_test_fixture_seed() -> None:
    setup_script = (REPO_ROOT / "scripts" / "setup-symphony-daemon.py").read_text(encoding="utf-8")
    assert "Application Support" in setup_script
    assert "tests\" / \"fixtures\" / \"generic_tasks_fixture.json" not in setup_script
    assert "generic_tasks.json" not in setup_script


def test_sym_dispatch_and_install_use_private_runtime_default() -> None:
    dispatch_script = (REPO_ROOT / "scripts" / "sym-dispatch.sh").read_text(encoding="utf-8")
    install_script = (REPO_ROOT / "scripts" / "install-symphony-daemon.sh").read_text(
        encoding="utf-8"
    )

    assert "Application Support/jleechanclaw/symphony_daemon" in dispatch_script
    assert "Application Support/jleechanclaw/symphony_daemon" in install_script
    assert "/tmp/jleechanclaw/symphony_daemon" not in dispatch_script
    assert "/tmp/jleechanclaw/symphony_daemon" not in install_script


def test_setup_daemon_avoids_static_cookie_literal() -> None:
    setup_script = (REPO_ROOT / "scripts" / "setup-symphony-daemon.py").read_text(encoding="utf-8")
    assert "jleechanclaw_symphony_cookie" not in setup_script
    assert "secrets.token_hex(16)" in setup_script


def test_setup_daemon_picks_port_after_bootout() -> None:
    setup_script = (REPO_ROOT / "scripts" / "setup-symphony-daemon.py").read_text(encoding="utf-8")
    assert setup_script.index("run([\"launchctl\", \"bootout\"") < setup_script.index("port = pick_port(requested_port)")


def test_plugin_helper_scripts_bootstrap_via_sym_dispatch() -> None:
    leetcode_helper = (REPO_ROOT / "scripts" / "sym-send-5-leetcode-hard.sh").read_text(encoding="utf-8")
    swebench_helper = (
        REPO_ROOT / "scripts" / "sym-send-5-swebench-verified.sh"
    ).read_text(encoding="utf-8")

    assert "--plugin leetcode_hard" in leetcode_helper
    assert "--plugin swe_bench_verified" in swebench_helper


def test_setup_daemon_uses_repo_owned_workflow_file() -> None:
    setup_script = (REPO_ROOT / "scripts" / "setup-symphony-daemon.py").read_text(encoding="utf-8")
    assert '"symphony" / "WORKFLOW.md"' in setup_script
    assert 'workflow_path = runtime_root / "WORKFLOW.md"' not in setup_script
    assert "workflow_path.write_text(" not in setup_script


def test_setup_daemon_bootstrap_is_thin_and_avoids_task_materialization() -> None:
    setup_script = (REPO_ROOT / "scripts" / "setup-symphony-daemon.py").read_text(encoding="utf-8")
    assert "load_plugin" not in setup_script
    assert "issues_json_path" not in setup_script
    assert '"task_plugin"' not in setup_script
    assert '"issues_json"' not in setup_script


def test_enqueue_script_requires_explicit_plugin_input_no_metadata_fallback() -> None:
    enqueue_script = (REPO_ROOT / "scripts" / "enqueue-symphony-tasks.sh").read_text(encoding="utf-8")
    assert 'PLUGIN_INPUT="${SYMPHONY_TASK_PLUGIN_INPUT:-}"' in enqueue_script
    assert "task_plugin_input" not in enqueue_script
    assert "SYMPHONY_TASK_PLUGIN_INPUT is required" in enqueue_script


def test_enqueue_script_gates_memory_tracker_to_benchmark_only_by_default() -> None:
    enqueue_script = (REPO_ROOT / "scripts" / "enqueue-symphony-tasks.sh").read_text(encoding="utf-8")
    assert 'SYMPHONY_MEMORY_QUEUE_MODE="${SYMPHONY_MEMORY_QUEUE_MODE:-benchmark-only}"' in enqueue_script
    assert 'if [[ "$SYMPHONY_MEMORY_QUEUE_MODE" == "benchmark-only" ]]; then' in enqueue_script
    assert '[[ "$PLUGIN_NAME" == "leetcode_hard" || "$PLUGIN_NAME" == "swe_bench_verified" ]]' in enqueue_script
    assert 'elif [[ "$SYMPHONY_MEMORY_QUEUE_MODE" == "always" ]]; then' in enqueue_script
    assert 'elif [[ "$SYMPHONY_MEMORY_QUEUE_MODE" == "never" ]]; then' in enqueue_script
    assert "Invalid SYMPHONY_MEMORY_QUEUE_MODE" in enqueue_script
    assert "Skipping memory_tracker_issues RPC enqueue" in enqueue_script


def test_docs_capture_retained_extensions_non_goals_and_rollback_plan() -> None:
    doc_text = (REPO_ROOT / "docs" / "symphony-runtime-dedupe.md").read_text(encoding="utf-8")
    assert "## Retained Local Extensions" in doc_text
    assert "## Explicit Non-Goals" in doc_text
    assert "## Rollback Plan" in doc_text


def test_sym_dispatch_freeform_defaults_to_live_enqueue_mode() -> None:
    dispatch_script = (REPO_ROOT / "scripts" / "sym-dispatch.sh").read_text(encoding="utf-8")
    assert 'SYMPHONY_MEMORY_QUEUE_MODE="${SYMPHONY_MEMORY_QUEUE_MODE:-always}"' in dispatch_script


def test_setup_daemon_records_workspace_root_from_workflow_contract(tmp_path: Path) -> None:
    module = _load_setup_daemon_module()
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("---\nworkspace:\n  root: workspaces\n---\n", encoding="utf-8")
    fallback_root = tmp_path / "runtime" / "workspaces"

    resolved = module._resolve_workspace_root(workflow, fallback_root)

    assert resolved == (fallback_root.parent / "workspaces").resolve()


def test_setup_daemon_runner_executes_with_runtime_cwd() -> None:
    setup_script = (REPO_ROOT / "scripts" / "setup-symphony-daemon.py").read_text(encoding="utf-8")
    assert "runner_cwd=str(runtime_root)" in setup_script


def test_extract_workspace_root_accepts_multiple_yaml_scalar_styles(tmp_path: Path) -> None:
    module = _load_setup_daemon_module()
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("---\nworkspace:\n  root: '/tmp/from-single-quote'\n---\n", encoding="utf-8")

    assert module._extract_workspace_root_from_workflow(workflow) == "/tmp/from-single-quote"


def test_extract_workspace_root_fails_fast_for_invalid_yaml(tmp_path: Path) -> None:
    module = _load_setup_daemon_module()
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("---\nworkspace:\n  root: [\n---\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Failed to parse workflow YAML"):
        module._extract_workspace_root_from_workflow(workflow)


def test_setup_daemon_missing_workflow_raises_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_setup_daemon_module()
    missing_workflow = tmp_path / "missing" / "WORKFLOW.md"
    runtime_root = tmp_path / "runtime"

    monkeypatch.setenv("SYMPHONY_WORKFLOW_PATH", str(missing_workflow))
    monkeypatch.setenv("SYMPHONY_DAEMON_RUNTIME", str(runtime_root))

    with pytest.raises(RuntimeError, match="Workflow file does not exist"):
        module.main()
