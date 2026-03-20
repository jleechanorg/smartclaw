"""Helpers for provisioning a launchd-managed Symphony daemon."""

from __future__ import annotations

import shlex
from typing import Any, Iterable


def build_workflow(
    workspace_root: str,
    workflow_title: str,
    workflow_intro: str,
    task_lines: Iterable[str],
    requirements: Iterable[str],
) -> str:
    # Intentionally pin this workflow to `codex app-server` for Symphony compatibility.
    # This is an explicit override of broader default-lane guidance.
    tasks = "\n".join(task_lines)
    req_lines = "\n".join(f"{idx}. {line}" for idx, line in enumerate(requirements, start=1))

    return f"""---
tracker:
  kind: memory
  active_states:
    - Todo
    - In Progress
  terminal_states:
    - Done
polling:
  interval_ms: 1000
workspace:
  root: \"{workspace_root}\"
agent:
  max_concurrent_agents: 1
  max_turns: 4
codex:
  command: codex app-server
  read_timeout_ms: 120000
  approval_policy: never
  thread_sandbox: workspace-write
  turn_sandbox_policy:
    type: workspaceWrite
---
You are running an autonomous coding task.

Task type: {workflow_title}

{workflow_intro}

Assigned items:
{tasks}

Requirements:
{req_lines}

Do not ask for user input unless there is a hard blocker with concrete evidence.
"""


def build_runner_script(
    symphony_elixir_dir: str,
    workflow_path: str,
    node_name: str,
    cookie: str,
    port: int,
    mise_bin: str = "/opt/homebrew/bin/mise",
    runner_cwd: str | None = None,
) -> str:
    effective_runner_cwd = runner_cwd or symphony_elixir_dir
    symphony_bin = f"{symphony_elixir_dir.rstrip('/')}/bin/symphony"

    quoted_symphony_bin = shlex.quote(symphony_bin)
    quoted_workflow_path = shlex.quote(workflow_path)
    quoted_node_name = shlex.quote(node_name)
    quoted_cookie = shlex.quote(cookie)
    quoted_mise_bin = shlex.quote(mise_bin)
    quoted_runner_cwd = shlex.quote(effective_runner_cwd)

    return f"""#!/usr/bin/env bash
set -euo pipefail

cd {quoted_runner_cwd}
ERL_NODE_NAME={quoted_node_name}
ERL_COOKIE={quoted_cookie}
export ERL_AFLAGS="-sname ${{ERL_NODE_NAME}} -setcookie ${{ERL_COOKIE}}"
{quoted_mise_bin} exec -- epmd -daemon || true
exec {quoted_mise_bin} exec -- {quoted_symphony_bin} \\
  --i-understand-that-this-will-be-running-without-the-usual-guardrails \\
  --port {port} \\
  {quoted_workflow_path}
"""


def build_launch_agent(
    label: str,
    runner_path: str,
    stdout_path: str,
    stderr_path: str,
) -> dict[str, Any]:
    return {
        "Label": label,
        "ProgramArguments": ["/bin/bash", runner_path],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": stdout_path,
        "StandardErrorPath": stderr_path,
    }
