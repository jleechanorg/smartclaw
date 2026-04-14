#!/usr/bin/env python3
"""Install and start a launchd-managed Symphony daemon for smartclaw."""

from __future__ import annotations

import json
import os
import plistlib
import secrets
import socket
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestration.symphony_daemon import (
    build_launch_agent,
    build_runner_script,
)
import yaml


def run(cmd: list[str], check: bool = True) -> None:
    subprocess.run(cmd, check=check)


def port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def pick_port(preferred_port: int, attempts: int = 32) -> int:
    for candidate in range(preferred_port, preferred_port + attempts):
        if not port_is_listening(candidate):
            return candidate
    raise RuntimeError(f"no available Symphony daemon port in [{preferred_port}, {preferred_port + attempts - 1}]")


def _extract_workspace_root_from_workflow(workflow_path: Path) -> str | None:
    text = workflow_path.read_text(encoding="utf-8")
    
    # Extract only YAML frontmatter (between first two --- delimiters)
    parts = text.split('---', 2)
    if len(parts) < 3:
        return None
    
    frontmatter = parts[1].strip()
    if not frontmatter:
        return None
    
    try:
        parsed = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Failed to parse workflow YAML at {workflow_path}: {exc}") from exc

    if not isinstance(parsed, dict):
        return None

    workspace = parsed.get("workspace")
    if not isinstance(workspace, dict):
        return None

    root = workspace.get("root")
    if root is None:
        return None
    root_text = str(root).strip()
    return root_text or None


def _resolve_workspace_root(workflow_path: Path, fallback_root: Path) -> Path:
    workspace_root_env = os.environ.get("SYMPHONY_WORKSPACE_ROOT")
    if workspace_root_env:
        return Path(workspace_root_env).expanduser().resolve()

    workflow_root = _extract_workspace_root_from_workflow(workflow_path)
    if workflow_root:
        workflow_root_path = Path(workflow_root).expanduser()
        if not workflow_root_path.is_absolute():
            return (fallback_root.parent / workflow_root_path).resolve()
        return workflow_root_path.resolve()

    return fallback_root.resolve()


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    uid = os.getuid()

    label = os.environ.get("SYMPHONY_DAEMON_LABEL", "ai.symphony.daemon")
    node_name = os.environ.get("SYMPHONY_DAEMON_NODE", "symphonyd")
    requested_port = int(os.environ.get("SYMPHONY_DAEMON_PORT", "19191"))
    mise_bin = os.environ.get("MISE_BIN", "/opt/homebrew/bin/mise")

    symphony_elixir_dir = Path(
        os.environ.get("SYMPHONY_ELIXIR_DIR", "~/projects_reference/symphony/elixir")
    ).expanduser().resolve()
    workflow_path = Path(
        os.environ.get(
            "SYMPHONY_WORKFLOW_PATH",
            str(repo_root / "symphony" / "WORKFLOW.md"),
        )
    ).expanduser().resolve()

    runtime_root = Path(
        os.environ.get(
            "SYMPHONY_DAEMON_RUNTIME",
            str(Path.home() / "Library" / "Application Support" / "smartclaw" / "symphony_daemon"),
        )
    ).expanduser().resolve()

    if not workflow_path.is_file():
        raise RuntimeError(f"Workflow file does not exist: {workflow_path}")

    workspace_root = _resolve_workspace_root(workflow_path, runtime_root / "workspaces")
    runner_path = runtime_root / "run_symphony_daemon.sh"
    stdout_path = runtime_root / "stdout.log"
    stderr_path = runtime_root / "stderr.log"
    metadata_path = runtime_root / "daemon_metadata.json"

    launch_agents_dir = Path.home() / "Library/LaunchAgents"
    plist_path = launch_agents_dir / f"{label}.plist"

    runtime_root.mkdir(parents=True, exist_ok=True)
    workspace_root.mkdir(parents=True, exist_ok=True)
    launch_agents_dir.mkdir(parents=True, exist_ok=True)
    runtime_root.chmod(0o700)
    workspace_root.chmod(0o700)

    # Stop any existing daemon first so port selection can reuse the preferred port on reinstall.
    run(["launchctl", "bootout", f"gui/{uid}", str(plist_path)], check=False)
    port = pick_port(requested_port)

    existing_cookie: str | None = None
    if metadata_path.exists():
        try:
            existing_cookie = json.loads(metadata_path.read_text(encoding="utf-8")).get("cookie")
        except (json.JSONDecodeError, OSError):
            existing_cookie = None

    cookie = os.environ.get("SYMPHONY_DAEMON_COOKIE") or existing_cookie or secrets.token_hex(16)

    runner_path.write_text(
        build_runner_script(
            str(symphony_elixir_dir),
            str(workflow_path),
            node_name,
            cookie,
            port,
            mise_bin=mise_bin,
            runner_cwd=str(runtime_root),
        ),
        encoding="utf-8",
    )
    runner_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    plist_data = build_launch_agent(
        label=label,
        runner_path=str(runner_path),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )
    with plist_path.open("wb") as f:
        plistlib.dump(plist_data, f)

    metadata = {
        "label": label,
        "node_name": node_name,
        "cookie": cookie,
        "port": port,
        "mise_bin": mise_bin,
        "workflow_path": str(workflow_path),
        "workspace_root": str(workspace_root),
        "runner_path": str(runner_path),
        "plist_path": str(plist_path),
        "symphony_elixir_dir": str(symphony_elixir_dir),
        "runtime_root": str(runtime_root),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    metadata_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    domain_target = f"gui/{uid}/{label}"

    run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)])
    run(["launchctl", "kickstart", "-k", domain_target])

    print(f"label={label}")
    if port != requested_port:
        print(f"requested_port={requested_port}")
        print(f"selected_port={port}")
    print(f"plist={plist_path}")
    print(f"workflow={workflow_path}")
    print(f"workspace_root={workspace_root}")
    print(f"metadata={metadata_path}")


if __name__ == "__main__":
    main()
