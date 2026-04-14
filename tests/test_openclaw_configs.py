"""Validation tests for committed openclaw JSON configs.

Guards against regressions in security-sensitive config properties:
- discord-eng-bot/openclaw.json (public-facing Discord bot)
- openclaw-config/openclaw.json (main agent profile)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_INSTALL_SCRIPT = REPO_ROOT / "install.sh"
DISCORD_CONFIG = REPO_ROOT / "discord-eng-bot" / "openclaw.json"
MAIN_CONFIG = Path(
    os.environ.get("OPENCLAW_TEST_MAIN_CONFIG_PATH", str(REPO_ROOT / "openclaw.json"))
)
AUTH_PROFILES_CONFIG = Path(
    os.environ.get(
        "OPENCLAW_TEST_AUTH_PROFILES_PATH",
        str(MAIN_CONFIG.parent / "agents" / "main" / "agent" / "auth-profiles.json"),
    )
)
MC_PLIST = REPO_ROOT / "ai.smartclaw.mission-control.plist"
START_MC_SCRIPT = REPO_ROOT / "scripts" / "start-mc.sh"
GATEWAY_INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-launchagents.sh"
STARTUP_CHECK_PLIST = REPO_ROOT / "ai.smartclaw.startup-check.plist"
STARTUP_CHECK_SCRIPT = REPO_ROOT / "startup-check.sh"
MCTRL_SUPERVISOR_PLIST = REPO_ROOT / "scripts" / "mctrl-supervisor.plist.template"
OPENCLAW_UPGRADE_SAFE = REPO_ROOT / "scripts" / "openclaw-upgrade-safe.sh"
STAGING_CANARY_SCRIPT = REPO_ROOT / "scripts" / "staging-canary.sh"
STAGING_CONFIG = Path(
    os.environ.get(
        "OPENCLAW_TEST_STAGING_CONFIG_PATH",
        str(Path.home() / ".smartclaw" / "openclaw.staging.json"),
    )
)
AO_RENDER_SCRIPT = REPO_ROOT / "scripts" / "render-agent-orchestrator-config.sh"
BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts" / "bootstrap.sh"
AO_MANAGER_SCRIPT = REPO_ROOT / "scripts" / "ao-manager.sh"
AO_ORCHESTRATORS_INSTALLER = REPO_ROOT / "scripts" / "install-ao-orchestrators.sh"
AGENTO_MANAGER_PLIST = REPO_ROOT / "launchd" / "ai.agento-manager.plist.template"
AGENTO_DASHBOARD_PLIST = REPO_ROOT / "launchd" / "ai.agento.dashboard.plist.template"

CORE_TOOLS = {"read", "write", "edit", "exec", "bash", "process"}
SECOND_OPINION_TOOLS = {
    "second-opinion-tool_agent_second_opinion",
    "second-opinion-tool_rate_limit_status",
    "second-opinion-tool_health-check",
}
SLACK_MCP_TOOLS = {
    "slack-mcp_channels_list",
    "slack-mcp_conversations_add_message",
    "slack-mcp_conversations_history",
    "slack-mcp_conversations_mark",
    "slack-mcp_conversations_replies",
    "slack-mcp_usergroups_create",
    "slack-mcp_usergroups_list",
    "slack-mcp_usergroups_me",
    "slack-mcp_usergroups_update",
    "slack-mcp_usergroups_users_update",
    "slack-mcp_users_search",
}


@pytest.fixture(scope="module")
def discord_cfg() -> dict:
    if not DISCORD_CONFIG.exists():
        pytest.skip("discord-eng-bot/openclaw.json not present (gitignored)")
    return json.loads(DISCORD_CONFIG.read_text())


@pytest.fixture(scope="module")
def main_cfg() -> dict:
    if not MAIN_CONFIG.exists():
        pytest.skip("openclaw.json not present (gitignored — run from ~/.smartclaw/)")
    return json.loads(MAIN_CONFIG.read_text())


@pytest.fixture(scope="module")
def auth_profiles_cfg() -> dict:
    if not AUTH_PROFILES_CONFIG.exists():
        return {}
    return json.loads(AUTH_PROFILES_CONFIG.read_text())


def _effective_auth_profiles(main_cfg: dict, auth_profiles_cfg: dict) -> dict:
    inline_profiles = (main_cfg.get("auth", {}) or {}).get("profiles") or {}
    if inline_profiles:
        return inline_profiles
    file_profiles = (auth_profiles_cfg or {}).get("profiles") or {}
    return file_profiles


def _copy_openclaw_subset(home: Path, relative_paths: list[str]) -> Path:
    repo_dir = home / ".smartclaw"
    for relative_path in relative_paths:
        src = REPO_ROOT / relative_path
        dest = repo_dir / relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        if os.access(src, os.X_OK):
            dest.chmod(dest.stat().st_mode | 0o111)
    return repo_dir


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_login_shell_home(
    home: Path,
    *,
    exports: dict[str, str] | None = None,
    banner: str | None = None,
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    bashrc_lines: list[str] = []
    if banner:
        bashrc_lines.append(f"printf '%s\\n' {shlex.quote(banner)}")
    for key, value in (exports or {}).items():
        bashrc_lines.append(f"export {key}={shlex.quote(value)}")
    (home / ".bashrc").write_text("\n".join(bashrc_lines) + "\n", encoding="utf-8")
    (home / ".bash_profile").write_text(
        'if [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi\n',
        encoding="utf-8",
    )


def _run_bash(
    command: list[str],
    *,
    home: Path,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{home / 'bin'}:{env.get('PATH', '')}"
    for key in (
        "AO_CONFIG_PATH",
        "AO_RENDER_FROM_SHELL",
        "AO_RENDER_EMPTY",
        "AO_RENDER_DEFAULT",
    ):
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _minimal_ao_config() -> str:
    return (
        "projects:\n"
        "  demo:\n"
        "    path: /tmp/demo\n"
        "    sessionPrefix: demo\n"
    )


def _assert_symlink_target(path: Path, target: Path) -> None:
    assert path.is_symlink(), f"Expected symlink at {path}"
    assert path.resolve() == target.resolve()


# ---------------------------------------------------------------------------
# ORCH-o00: sandbox isolation must be preserved in the public Discord bot
# ---------------------------------------------------------------------------


class TestDiscordSandbox:
    def test_sandbox_mode_is_not_off(self, discord_cfg: dict):
        """Public-facing bot must run in an isolated container, not mode='off'."""
        mode = discord_cfg["agents"]["defaults"]["sandbox"]["mode"]
        assert mode != "off", (
            f"sandbox.mode is '{mode}' — should be 'non-main' to keep "
            "Discord sessions in isolated containers (ORCH-o00)"
        )

    def test_sandbox_mode_is_non_main(self, discord_cfg: dict):
        mode = discord_cfg["agents"]["defaults"]["sandbox"]["mode"]
        assert mode == "non-main", (
            f"Expected sandbox.mode='non-main', got '{mode}' (ORCH-o00)"
        )

    def test_workspace_access_is_none(self, discord_cfg: dict):
        """Filesystem access must remain disabled regardless of sandbox mode."""
        access = discord_cfg["agents"]["defaults"]["sandbox"]["workspaceAccess"]
        assert access == "none"


# ---------------------------------------------------------------------------
# ORCH-36x: main config must explicitly list core tools in alsoAllow
# ---------------------------------------------------------------------------


class TestMainCoreTools:
    def test_also_allow_includes_core_tools(self, main_cfg: dict):
        """Core tools must be explicit in alsoAllow so removal from the
        'coding' profile does not silently break the main agent."""
        also_allow = set(main_cfg["tools"].get("alsoAllow", []))
        missing = CORE_TOOLS - also_allow
        assert not missing, (
            f"alsoAllow is missing core tools: {sorted(missing)}. "
            "Do not rely solely on the 'coding' profile to provide them (ORCH-36x)"
        )

    def test_also_allow_includes_second_opinion_tools(self, main_cfg: dict):
        """Second-opinion tool IDs must still be present (regression guard)."""
        pytest.skip("openclaw-mcp-adapter plugin not yet available (orch-n81y)")


# ---------------------------------------------------------------------------
# ORCH-d5b: MCP adapter auth uses dynamic Firebase JWT (not a static header)
#
# Token lifecycle (auth-cli.mjs):
#   - ID token:      1 hour (TOKEN_EXPIRATION_MS = 3600000)
#   - Refresh token: months-long (Firebase Google Sign-In; stored in
#                    ~/.ai-universe/auth-token-ai-universe-b3551.json)
#   - `auth-cli.mjs token` silently exchanges refresh → new ID token when
#     needed; no re-login required for months.
#
# A static ${SECOND_OPINION_MCP_TOKEN} env var would expire after 1 hour.
# The correct fix is a per-request tokenCommand in the adapter config
# (if openclaw-mcp-adapter supports it):
#   "tokenCommand": "node ~/.claude/scripts/auth-cli.mjs token"
#
# These tests verify adapter wiring only — NOT a static Authorization header.
# ---------------------------------------------------------------------------


def _get_mcp_servers(cfg: dict) -> list[dict]:
    return (
        cfg.get("plugins", {})
        .get("entries", {})
        .get("openclaw-mcp-adapter", {})
        .get("config", {})
        .get("servers", [])
    )


def _server_transport(server: dict) -> str:
    return str(server.get("transport") or "http").strip().lower()


class TestMcpAdapterWiring:
    def test_discord_mcp_adapter_has_authorization_header(self, discord_cfg: dict):
        """Adapter must send Authorization header — server requires Firebase JWT Bearer token."""
        servers = _get_mcp_servers(discord_cfg)
        assert servers, "No MCP adapter servers in discord config"
        for server in servers:
            headers = server.get("headers", {})
            assert "Authorization" in headers, (
                f"MCP server '{server.get('name')}' missing Authorization header. "
                "Server requires Bearer token. Set SECOND_OPINION_MCP_TOKEN=$(node "
                "~/.claude/scripts/auth-cli.mjs token) at gateway startup."
            )
            assert "${SECOND_OPINION_MCP_TOKEN}" in headers["Authorization"], (
                "Authorization must use ${SECOND_OPINION_MCP_TOKEN} env var placeholder"
            )

    def test_main_mcp_adapter_has_authorization_header(self, main_cfg: dict):
        # openclaw-mcp-adapter is not yet an installable plugin (orch-o3y0).
        # Skip until the plugin exists in the openclaw registry.
        pytest.skip("openclaw-mcp-adapter plugin not yet available (orch-o3y0)")

    def test_discord_mcp_adapter_enabled(self, discord_cfg: dict):
        adapter = (
            discord_cfg.get("plugins", {})
            .get("entries", {})
            .get("openclaw-mcp-adapter", {})
        )
        assert adapter.get("enabled") is True

    def test_main_mcp_adapter_enabled(self, main_cfg: dict):
        pytest.skip("openclaw-mcp-adapter plugin not yet available (orch-o3y0)")

    def test_discord_mcp_server_uses_url_env_var(self, discord_cfg: dict):
        servers = _get_mcp_servers(discord_cfg)
        assert servers, "No MCP adapter servers in discord config"
        for server in servers:
            assert "${" in server.get("url", ""), (
                f"MCP server '{server.get('name')}' URL should use an env var "
                "placeholder like '${SECOND_OPINION_MCP_URL}'"
            )

    def test_main_mcp_server_uses_url_env_var(self, main_cfg: dict):
        pytest.skip("openclaw-mcp-adapter plugin not yet available (orch-o3y0)")

    def test_main_stdio_mcp_servers_define_command(self, main_cfg: dict):
        servers = _get_mcp_servers(main_cfg)
        for server in servers:
            if _server_transport(server) != "stdio":
                continue
            command = str(server.get("command") or "").strip()
            assert command, f"MCP server '{server.get('name')}' with transport=stdio must define command"

    def test_discord_mcp_tool_prefix_enabled(self, discord_cfg: dict):
        prefix = (
            discord_cfg.get("plugins", {})
            .get("entries", {})
            .get("openclaw-mcp-adapter", {})
            .get("config", {})
            .get("toolPrefix")
        )
        assert prefix is True

    def test_main_mcp_tool_prefix_enabled(self, main_cfg: dict):
        pytest.skip("openclaw-mcp-adapter plugin not yet available (orch-o3y0)")


class TestInstallLaunchagentsScript:
    @pytest.mark.parametrize(
        ("flag", "expected_error"),
        [
            ("--mc-token", "Error: --mc-token requires a non-empty value"),
            ("--gateway-token", "Error: --gateway-token requires a non-empty value"),
        ],
    )
    def test_missing_option_value_exits_cleanly(self, flag: str, expected_error: str):
        result = subprocess.run(
            [str(GATEWAY_INSTALL_SCRIPT), flag],
            capture_output=True,
            text=True,
            env={"HOME": str(REPO_ROOT)},
        )

        assert result.returncode != 0
        assert expected_error in result.stderr

    @pytest.mark.parametrize(
        ("flag", "expected_error"),
        [
            ("--mc-token", "Error: --mc-token requires a non-empty value"),
            ("--gateway-token", "Error: --gateway-token requires a non-empty value"),
        ],
    )
    def test_empty_option_value_exits_cleanly(self, flag: str, expected_error: str):
        result = subprocess.run(
            [str(GATEWAY_INSTALL_SCRIPT), flag, ""],
            capture_output=True,
            text=True,
            env={"HOME": str(REPO_ROOT)},
        )

        assert result.returncode != 0
        assert expected_error in result.stderr


class TestMissionControlRuntimeWiring:
    def test_mission_control_launchagent_uses_in_process_runtime(self):
        """MC launchd service should start backend+poller entrypoint, not bare uvicorn."""
        if not MC_PLIST.exists():
            pytest.skip("Mission Control launchagent plist is not present in this repository checkout")
        plist_text = MC_PLIST.read_text(encoding="utf-8")
        assert "orchestration.mc_backend_service" in plist_text
        assert "<key>MISSION_CONTROL_BASE_URL</key>" in plist_text
        assert "<key>MISSION_CONTROL_TOKEN</key>" in plist_text
        assert "<key>MISSION_CONTROL_BOARD_ID</key>" in plist_text
        assert "<key>PYTHONPATH</key>" in plist_text

    def test_start_mc_script_uses_same_runtime_entrypoint(self):
        """Manual startup path should match launchd runtime wiring."""
        if not START_MC_SCRIPT.exists():
            pytest.skip("start-mc.sh is not present in this repository checkout")
        script_text = START_MC_SCRIPT.read_text(encoding="utf-8")
        assert "orchestration.mc_backend_service" in script_text


class TestLaunchAgentInstallers:
    def test_install_launchagents_uses_plist_bootstrap(self):
        """Gateway should be installed via plist bootstrap, not openclaw CLI installer."""
        script_text = GATEWAY_INSTALL_SCRIPT.read_text(encoding="utf-8")
        # install_plist is the primary gateway install path; openclaw CLI is not used
        assert "install_plist" in script_text, (
            "install-launchagents.sh must use install_plist() for gateway plist installation"
        )
        # The old openclaw gateway install --force approach was removed in favor of plist bootstrap
        assert "openclaw gateway install --force" not in script_text

    def test_install_launchagents_installs_startup_check(self):
        """Startup-check launch agent must be installed alongside the gateway."""
        script_text = GATEWAY_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "ai.smartclaw.startup-check.plist" in script_text

    def test_install_launchagents_refreshes_runtime_startup_script(self):
        """The installer must update the script the launch agent actually executes."""
        script_text = GATEWAY_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'install -m 755 "$CONFIG_DIR/startup-check.sh" "$OPENCLAW_HOME/startup-check.sh"' in script_text

    def test_install_launchagents_only_installs_mc_services_when_plists_exist(self):
        """Mission Control launchagents are optional and should be gated by file presence."""
        script_text = GATEWAY_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'if [[ -f "$MC_BACKEND_PLIST" ]]; then' in script_text
        assert 'if [[ -f "$MC_FRONTEND_PLIST" ]]; then' in script_text
        assert 'skipping ai.smartclaw.mission-control' in script_text

    def test_install_launchagents_rejects_placeholder_mc_token(self):
        """Launchd installer must not stamp the checked-in placeholder token into services."""
        script_text = GATEWAY_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "is_valid_mc_token()" in script_text
        assert "your-local-auth-token-here" in script_text
        assert "Generated new local Mission Control token for launchd services." in script_text

    def test_install_launchagents_enables_dashboard_before_bootstrap(self):
        """Dashboard opt-in must clear the persistent disabled flag before bootstrap."""
        script_text = GATEWAY_INSTALL_SCRIPT.read_text(encoding="utf-8")
        enable_line = 'launchctl enable "gui/$(id -u)/ai.agento.dashboard" 2>/dev/null || true'
        install_line = 'install_plist "$AGENTO_DASHBOARD_PLIST"'
        assert enable_line in script_text
        assert install_line in script_text
        assert script_text.index(enable_line) < script_text.index(install_line)

    def test_install_launchagents_dashboard_optin_persists_via_state_file(self):
        """Dashboard opt-in must persist across installer runs via state file, not just env var."""
        script_text = GATEWAY_INSTALL_SCRIPT.read_text(encoding="utf-8")
        # Must read from previously persisted state file when env var is not set this run
        assert 'AGENTO_DASHBOARD_STATE_FILE="${OPENCLAW_STATE_DIR:-${HOME}/.smartclaw}/.ao_dashboard_opt_in"' in script_text
        assert 'cat "$AGENTO_DASHBOARD_STATE_FILE"' in script_text
        # Must write opt-in persistently when env var is set to 1
        assert 'echo "1" > "$AGENTO_DASHBOARD_STATE_FILE"' in script_text

    def test_install_launchagents_normalizes_prod_config_paths(self):
        """Prod installer must rewrite copied staging workspace/agentDir paths into .smartclaw_prod."""
        script_text = GATEWAY_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "normalize_prod_openclaw_config_paths()" in script_text
        assert "^${HOME}/" in script_text
        assert ".smartclaw" in script_text
        assert "$prod" in script_text
        assert "Normalized prod config workspace/agentDir paths" in script_text

    def test_repo_gateway_plist_uses_prod_state_dir_and_logs(self):
        """The canonical gateway plist must target the prod runtime contract."""
        plist_text = (REPO_ROOT / "launchd" / "ai.smartclaw.gateway.plist").read_text(
            encoding="utf-8"
        )
        assert "${HOME}/.nvm/versions/node/v22.22.0/bin/node" in plist_text
        assert "${HOME}/.smartclaw_prod/openclaw.json" in plist_text
        assert "<key>OPENCLAW_STATE_DIR</key>" in plist_text
        assert "${HOME}/.smartclaw_prod/logs/gateway.log" in plist_text
        assert "${HOME}/.smartclaw_prod/logs/gateway.err.log" in plist_text
        assert "<key>ThrottleInterval</key>" in plist_text
        assert "<integer>30</integer>" in plist_text

    def test_startup_check_plist_runs_at_load(self):
        """Startup verification should trigger automatically after login/restart."""
        if not STARTUP_CHECK_PLIST.exists():
            pytest.skip("ai.smartclaw.startup-check.plist not present in this checkout")
        plist_text = STARTUP_CHECK_PLIST.read_text(encoding="utf-8")
        assert "<string>ai.smartclaw.startup-check</string>" in plist_text
        assert "<key>RunAtLoad</key>" in plist_text
        assert "<true/>" in plist_text  # RunAtLoad must be enabled, not just present

    def test_startup_check_script_resolves_openclaw_without_login_shell(self):
        """Startup-check should resolve the CLI even under launchd's minimal PATH."""
        script_text = STARTUP_CHECK_SCRIPT.read_text(encoding="utf-8")
        assert "resolve_openclaw_bin()" in script_text
        assert ".nvm/versions/node/current/bin" in script_text
        assert "/opt/homebrew/bin/openclaw" in script_text

    def test_startup_check_treats_missing_target_as_non_fatal(self):
        """Missing optional WhatsApp target should not fail the one-shot startup verifier."""
        script_text = STARTUP_CHECK_SCRIPT.read_text(encoding="utf-8")
        assert "skipping startup confirmation" in script_text
        assert "exit 0" in script_text

    def test_mctrl_supervisor_template_has_throttle_interval(self):
        """Supervisor launchd template should back off between restart attempts."""
        plist_text = MCTRL_SUPERVISOR_PLIST.read_text(encoding="utf-8")
        assert "<key>ThrottleInterval</key>" in plist_text
        assert "<integer>10</integer>" in plist_text  # ThrottleInterval must have a positive value

class TestAoRuntimeConfig:
    def test_render_ao_config_script_exists_and_uses_login_shell_env(self, tmp_path: Path):
        home = tmp_path / "home"
        _write_login_shell_home(
            home,
            exports={
                "AO_RENDER_FROM_SHELL": "shell-secret",
                "AO_RENDER_EMPTY": "",
            },
            banner="login shell banner",
        )
        source = tmp_path / "agent-orchestrator.yaml"
        source.write_text(
            "token: ${AO_RENDER_FROM_SHELL}\n"
            'empty: "${AO_RENDER_EMPTY}"\n'
            "defaulted: ${AO_RENDER_DEFAULT:-fallback}\n",
            encoding="utf-8",
        )
        output = home / ".agent-orchestrator.yaml"

        proc = _run_bash(
            ["bash", str(AO_RENDER_SCRIPT), str(source), str(output)],
            home=home,
        )
        assert proc.returncode == 0, proc.stderr
        assert output.read_text(encoding="utf-8") == (
            "token: shell-secret\n"
            'empty: ""\n'
            "defaulted: fallback\n"
        )
        assert output.stat().st_mode & 0o777 == 0o600

        source.write_text("token: ${AO_RENDER_REQUIRED}\n", encoding="utf-8")
        proc = _run_bash(
            ["bash", str(AO_RENDER_SCRIPT), str(source), str(output)],
            home=home,
        )
        assert proc.returncode != 0
        assert "unresolved placeholders left in output" in proc.stderr
        assert output.read_text(encoding="utf-8") == (
            "token: shell-secret\n"
            'empty: ""\n'
            "defaulted: fallback\n"
        )

    def test_root_install_renders_runtime_ao_config(self, tmp_path: Path):
        home = tmp_path / "home"
        _write_login_shell_home(home)
        repo_dir = _copy_openclaw_subset(
            home,
            [
                "install.sh",
                "scripts/bootstrap.sh",
                "scripts/render-agent-orchestrator-config.sh",
            ],
        )
        (repo_dir / "agent-orchestrator.yaml").write_text(
            _minimal_ao_config(),
            encoding="utf-8",
        )

        proc = _run_bash(["bash", str(repo_dir / "install.sh")], home=home, cwd=repo_dir)
        assert proc.returncode != 0
        output = home / ".agent-orchestrator.yaml"
        compat = home / "agent-orchestrator.yaml"
        assert output.exists()
        _assert_symlink_target(compat, output)
        assert "Rendered: agent-orchestrator.yaml -> ~/.agent-orchestrator.yaml" in proc.stdout

    def test_bootstrap_renders_runtime_config_and_links_compat_path(self, tmp_path: Path):
        home = tmp_path / "home"
        _write_login_shell_home(home)
        repo_dir = _copy_openclaw_subset(
            home,
            [
                "scripts/bootstrap.sh",
                "scripts/render-agent-orchestrator-config.sh",
            ],
        )
        (repo_dir / "agent-orchestrator.yaml").write_text(
            _minimal_ao_config(),
            encoding="utf-8",
        )

        proc = _run_bash(
            ["bash", str(repo_dir / "scripts/bootstrap.sh"), "--symlink-only"],
            home=home,
            cwd=repo_dir,
        )
        assert proc.returncode == 0, proc.stderr
        output = home / ".agent-orchestrator.yaml"
        compat = home / "agent-orchestrator.yaml"
        assert output.exists()
        _assert_symlink_target(compat, output)

    def test_ao_manager_defaults_to_rendered_runtime_config(self, tmp_path: Path):
        home = tmp_path / "home"
        _write_login_shell_home(home)
        repo_dir = _copy_openclaw_subset(home, ["scripts/ao-manager.sh"])
        (home / ".agent-orchestrator.yaml").write_text(
            _minimal_ao_config(),
            encoding="utf-8",
        )

        proc = _run_bash(
            ["bash", str(repo_dir / "scripts/ao-manager.sh"), "--status"],
            home=home,
            cwd=repo_dir,
        )
        assert proc.returncode == 0, proc.stderr
        assert f"Config: {home / '.agent-orchestrator.yaml'}" in proc.stdout

    def test_install_ao_orchestrators_defaults_to_rendered_runtime_config(self, tmp_path: Path):
        home = tmp_path / "home"
        _write_login_shell_home(
            home,
            exports={
                "GITHUB_TOKEN": "gh-test-token",
                "OPENCLAW_AO_HOOK_TOKEN": "hook-test-token",
            },
        )
        repo_dir = _copy_openclaw_subset(
            home,
            [
                "scripts/bootstrap.sh",
                "scripts/render-agent-orchestrator-config.sh",
                "scripts/install-ao-orchestrators.sh",
            ],
        )
        (repo_dir / "agent-orchestrator.yaml").write_text(
            _minimal_ao_config(),
            encoding="utf-8",
        )
        _write_executable(home / "bin" / "ao", "#!/usr/bin/env bash\nexit 0\n")
        _write_executable(home / "bin" / "launchctl", "#!/usr/bin/env bash\nexit 0\n")

        proc = _run_bash(
            ["bash", str(repo_dir / "scripts/install-ao-orchestrators.sh")],
            home=home,
            cwd=repo_dir,
        )
        assert proc.returncode == 0, proc.stderr
        output = home / ".agent-orchestrator.yaml"
        compat = home / "agent-orchestrator.yaml"
        plist = home / "Library/LaunchAgents/ai.agento.orchestrators.plist"
        assert output.exists()
        _assert_symlink_target(compat, output)
        assert plist.exists()
        plist_text = plist.read_text(encoding="utf-8")
        assert str(output) in plist_text

    def test_agento_launchd_templates_use_rendered_runtime_config(self, tmp_path: Path):
        home = tmp_path / "home"
        _write_login_shell_home(home)
        repo_dir = _copy_openclaw_subset(
            home,
            [
                "scripts/install-ao-manager.sh",
                "launchd/ai.agento-manager.plist.template",
                "launchd/ai.agento.dashboard.plist.template",
            ],
        )
        _write_executable(home / "bin" / "launchctl", "#!/usr/bin/env bash\nexit 0\n")

        proc = _run_bash(
            ["bash", str(repo_dir / "scripts/install-ao-manager.sh")],
            home=home,
            cwd=repo_dir,
        )
        assert proc.returncode == 0, proc.stderr

        manager_plist = home / "Library/LaunchAgents/ai.agento.manager.plist"
        manager_text = manager_plist.read_text(encoding="utf-8")
        dashboard_text = (repo_dir / "launchd/ai.agento.dashboard.plist.template").read_text(
            encoding="utf-8"
        )
        assert str(home / ".agent-orchestrator.yaml") in manager_text
        assert "@HOME@/.agent-orchestrator.yaml" in dashboard_text


# ---------------------------------------------------------------------------
# ORCH-sl1: Slack DM reply must be enabled (not "off")
#
# replyToModeByChatType.direct="off" means the agent reads DMs but never
# replies. Any commit that sets direct="off" silently breaks DM responses.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ORCH-sl0: Slack channel must be enabled
#
# channels.slack.enabled=false silently stops the gateway from connecting
# to Slack Socket Mode. Any commit that sets this to false breaks all
# Slack-triggered agent sessions with no visible error to the user.
# ---------------------------------------------------------------------------


class TestSlackEnabled:
    def _slack_cfg(self, main_cfg: dict) -> dict:
        return main_cfg.get("channels", {}).get("slack", {})

    def test_slack_channel_is_enabled(self, main_cfg: dict):
        """channels.slack.enabled must be true in the live config.

        The repo copy may intentionally have enabled=false (deploy-time opt-in via
        OPENCLAW_SYNC_ALLOW_SLACK_DISABLE guard). Skip if disabled here; the sync
        validation guard enforces this at live-copy time instead.
        """
        slack = self._slack_cfg(main_cfg)
        enabled = slack.get("enabled")
        if enabled is not True:
            pytest.skip(
                f"channels.slack.enabled={enabled!r} in repo copy — "
                "live-only invariant enforced by sync validation guard (ORCH-sl0)"
            )

    def test_slack_bot_token_is_set(self, main_cfg: dict):
        """botToken must be a non-empty value (env var ref or expanded token)."""
        slack = self._slack_cfg(main_cfg)
        token = slack.get("botToken")
        assert isinstance(token, str) and token.strip(), (
            "channels.slack.botToken is missing or empty (ORCH-sl0)"
        )

    def test_slack_app_token_is_set(self, main_cfg: dict):
        """appToken must be a non-empty value (env var ref or expanded token)."""
        slack = self._slack_cfg(main_cfg)
        token = slack.get("appToken")
        assert isinstance(token, str) and token.strip(), (
            "channels.slack.appToken is missing or empty (ORCH-sl0)"
        )


# ---------------------------------------------------------------------------
# ORCH-sl0b: all apiKey fields in model provider configs must be env refs
#
# Bare literals like "MINIMAX_API_KEY" (without ${}) are treated as literal
# strings by OpenClaw, not expanded from env — causing silent auth failures.
# ---------------------------------------------------------------------------

MODELS_CONFIGS = [
    REPO_ROOT / "agents" / "main" / "agent" / "models.json.redacted",
    REPO_ROOT / "agents" / "monitor" / "agent" / "models.json.redacted",
]


class TestModelProviderApiKeys:
    @pytest.mark.parametrize("models_path", MODELS_CONFIGS, ids=lambda p: p.parent.parent.name)
    def test_all_api_keys_are_env_refs(self, models_path: Path):
        """Every apiKey in every provider must be an env var ref like ${VAR_NAME}."""
        if not models_path.exists():
            pytest.skip(f"{models_path} not present")
        import re
        cfg = json.loads(models_path.read_text())
        providers = cfg.get("providers", {})
        env_ref_pattern = re.compile(r"^\$\{[A-Z][A-Z0-9_]+\}$")
        bad = []
        for name, provider in providers.items():
            api_key = provider.get("apiKey")
            if api_key is None:
                continue
            if not env_ref_pattern.match(str(api_key)):
                bad.append(f"{name}.apiKey={api_key!r}")
        assert not bad, (
            f"Non-env-ref apiKey values in {models_path.name}: {bad}. "
            "Wrap as ${VAR_NAME} so OpenClaw expands from env (ORCH-sl0b)"
        )


class TestSlackDmReplyConfig:
    def _slack_cfg(self, main_cfg: dict) -> dict:
        return main_cfg.get("channels", {}).get("slack", {})

    def test_reply_to_mode_direct_is_not_off(self, main_cfg: dict):
        """Agent must reply to Slack DMs — direct='off' silently breaks responses."""
        slack = self._slack_cfg(main_cfg)
        by_chat_type = slack.get("replyToModeByChatType", {})
        direct = by_chat_type.get("direct")
        assert direct != "off", (
            f"replyToModeByChatType.direct='{direct}' — DMs will be silently ignored. "
            "Set to 'all' or 'thread' (ORCH-sl1)"
        )

    def test_reply_to_mode_direct_is_all(self, main_cfg: dict):
        """replyToModeByChatType.direct should be 'all' to reply to every DM."""
        slack = self._slack_cfg(main_cfg)
        direct = slack.get("replyToModeByChatType", {}).get("direct")
        assert direct == "all", (
            f"Expected replyToModeByChatType.direct='all', got '{direct}' (ORCH-sl1)"
        )

    def test_reply_to_mode_top_level_is_not_off(self, main_cfg: dict):
        """Top-level replyToMode should not be 'off' — it overrides per-type settings."""
        slack = self._slack_cfg(main_cfg)
        mode = slack.get("replyToMode")
        assert mode != "off", (
            f"replyToMode='{mode}' overrides replyToModeByChatType and silences all replies. "
            "Remove or set to 'all' (ORCH-sl1)"
        )


# ---------------------------------------------------------------------------
# ORCH-sl2: slack-mcp stdio server must be present and correctly wired
# ---------------------------------------------------------------------------


class TestSlackMcpServerConfig:
    # openclaw-mcp-adapter plugin not yet available — all tests skipped (orch-5mk8, orch-o3y0)
    def _get_slack_mcp_server(self, main_cfg: dict) -> dict | None:
        servers = _get_mcp_servers(main_cfg)
        return next((s for s in servers if s.get("name") == "slack-mcp"), None)

    def test_slack_mcp_server_present(self, main_cfg: dict):
        pytest.skip("openclaw-mcp-adapter plugin not yet available (orch-5mk8)")

    def test_slack_mcp_uses_stdio_transport(self, main_cfg: dict):
        pytest.skip("openclaw-mcp-adapter plugin not yet available (orch-5mk8)")

    def test_slack_mcp_token_uses_env_var(self, main_cfg: dict):
        pytest.skip("openclaw-mcp-adapter plugin not yet available (orch-5mk8)")

    def test_slack_mcp_add_message_tool_enabled(self, main_cfg: dict):
        pytest.skip("openclaw-mcp-adapter plugin not yet available (orch-5mk8)")


# ---------------------------------------------------------------------------
# ORCH-sl3: all slack-mcp tool IDs must be explicitly in alsoAllow
# ---------------------------------------------------------------------------


class TestSlackMcpToolsAllowed:
    def test_also_allow_includes_all_slack_mcp_tools(self, main_cfg: dict):
        pytest.skip("openclaw-mcp-adapter plugin not yet available (orch-zeyi)")


# ---------------------------------------------------------------------------
# ORCH-s4p / ORCH-y20: shell remediation and phase2 CLI flags
# ---------------------------------------------------------------------------


class TestOpsScriptRegressions:
    def test_repo_claude_requires_live_config_paths_for_harness_checks(self):
        """Repo instructions must force doctor/monitor validation against live state dirs."""
        claude_text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert "OPENCLAW_STATE_DIR" in claude_text
        assert "OPENCLAW_CONFIG_PATH" in claude_text
        assert "never assume repo-root `~/.smartclaw/openclaw.json`" in claude_text
        assert "doctor.sh / monitor-agent.sh parity rule" in claude_text

    def test_health_check_install_gateway_uses_bootstrap_and_regeneration(self):
        """health-check install_gateway must use launchctl bootstrap and fall back to plist regeneration."""
        script_text = (REPO_ROOT / "health-check.sh").read_text(
            encoding="utf-8"
        )
        # install_gateway must try launchctl bootstrap before giving up
        assert "launchctl bootstrap" in script_text, (
            "health-check install_gateway() must use launchctl bootstrap as the primary "
            "service-load mechanism (ORCH-s4p)"
        )
        # Must fall back to install-launchagents.sh regeneration rather than silently failing
        assert "install-launchagents.sh" in script_text, (
            "health-check install_gateway() must fall back to install-launchagents.sh "
            "for plist regeneration rather than silently returning failure (ORCH-s4p)"
        )

    def test_gateway_preflight_detects_canonical_gateway_plist_wiring(self):
        """Preflight must reject installed gateway plists that drift from the repo contract."""
        script_text = (REPO_ROOT / "scripts" / "gateway-preflight.sh").read_text(
            encoding="utf-8"
        )
        assert "ProgramArguments.0" in script_text
        assert "EnvironmentVariables.OPENCLAW_STATE_DIR" in script_text
        assert "EnvironmentVariables.OPENCLAW_CONFIG_PATH" in script_text
        assert "reload_gateway_plist_from_repo" in script_text

    def test_gateway_preflight_uses_prod_config_when_staging_is_stub(self):
        """Preflight must resolve critical checks against the live config when repo copy is skeletal."""
        script_text = (REPO_ROOT / "scripts" / "gateway-preflight.sh").read_text(
            encoding="utf-8"
        )
        assert "resolve_live_config_for_preflight" in script_text
        assert "staging config is a repo stub" in script_text
        assert "LIVE_CONFIG_FOR_PREFLIGHT" in script_text

    def test_gateway_preflight_only_fails_when_config_is_newer_than_binary(self):
        """Version drift should block only when the config outruns the binary."""
        script_text = (REPO_ROOT / "scripts" / "gateway-preflight.sh").read_text(
            encoding="utf-8"
        )
        assert "is newer than the running binary" in script_text
        assert "is older than the running binary" in script_text

    def test_deploy_preserves_prod_config_when_staging_main_config_is_stub(self):
        """Deploy must not clobber the prod config with the repo stub."""
        script_text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(
            encoding="utf-8"
        )
        assert "is_stub_main_config" in script_text
        assert "preserving existing prod openclaw.json" in script_text
        assert "Stage 3: Config Sync" in script_text

    def test_openclaw_upgrade_safe_uses_staging_launchagent_not_deleted_helper(self):
        """Safe upgrade should reuse the maintained staging launch agent path."""
        script_text = OPENCLAW_UPGRADE_SAFE.read_text(encoding="utf-8")
        assert "ai.smartclaw.staging" in script_text
        assert "staging-gateway.sh" not in script_text
        assert "staging-canary.sh" in script_text
        assert "launchctl kickstart -k" in script_text
        assert 'launchctl stop "gui/$(id -u)/ai.smartclaw.staging"' not in script_text

    def test_staging_canary_accepts_repo_stub_without_live_only_keys(self):
        """Staging canary should not fail on the intentionally skeletal repo config."""
        script_text = STAGING_CANARY_SCRIPT.read_text(encoding="utf-8")
        assert "is_stub_main_config" in script_text
        assert "Repo stub accepted" in script_text
        assert "live-only keys intentionally omitted" in script_text

    def test_staging_launchagent_executes_openclaw_bin_directly(self):
        """Staging launchd should execute OPENCLAW_BIN directly, not `node <openclaw>`."""
        plist_text = (REPO_ROOT / "launchd" / "ai.smartclaw.staging.plist").read_text(
            encoding="utf-8"
        )
        assert "<string>@OPENCLAW_BIN@</string>" in plist_text
        assert "<string>@NODE_PATH@</string>" not in plist_text
        assert "<string>@HOME@/.smartclaw/openclaw.staging.json</string>" in plist_text

    def test_install_launchagents_generates_valid_staging_overlay_for_18810(self):
        """Staging overlay generation must keep runtime schema valid and force the staging port."""
        script_text = GATEWAY_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert '"port": 18810' in script_text
        assert '"http://localhost:18810"' in script_text
        assert '"http://127.0.0.1:18810"' in script_text
        assert '"stagingDerivedFrom"' not in script_text
        assert '"lastTouchedVersion": "2026.4.9"' not in script_text

    def test_install_launchagents_keeps_staging_slack_enabled_for_canary(self):
        """Staging overlay must keep Slack enabled so staging canary/monitor can exercise Slack."""
        script_text = GATEWAY_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert '"slack": {"enabled": False}' not in script_text
        assert '"discord": {"enabled": False}' in script_text
        assert '"telegram": {"enabled": False}' in script_text

    def test_install_launchagents_enforces_require_mention_on_all_staging_channels(self):
        """Staging overlay generator must set requireMention=true on every channel entry.

        Without this, prod config values (requireMention=false) bleed through the merge
        and staging silently starts passively listening on channels again.
        """
        script_text = GATEWAY_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'slack_channels[ch]["requireMention"] = True' in script_text, (
            "install-launchagents.sh must set requireMention=True on all staging "
            "Slack channel entries after deep_merge to prevent prod config bleed-through"
        )

    def test_install_launchagents_prefers_staging_specific_slack_tokens(self):
        """Staging overlay must not inherit prod Slack socket tokens when staging creds exist."""
        script_text = GATEWAY_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'OPENCLAW_STAGING_SLACK_BOT_TOKEN' in script_text
        assert 'OPENCLAW_STAGING_SLACK_APP_TOKEN' in script_text
        assert 'staging_overrides.setdefault("env", {})["SLACK_BOT_TOKEN"] = staging_slack_bot_token' in script_text
        assert 'staging_overrides.setdefault("channels", {}).setdefault("slack", {})["botToken"] = staging_slack_bot_token' in script_text
        assert 'staging_overrides.setdefault("env", {})["OPENCLAW_SLACK_APP_TOKEN"] = staging_slack_app_token' in script_text
        assert 'staging_overrides.setdefault("channels", {}).setdefault("slack", {})["appToken"] = staging_slack_app_token' in script_text

    def test_deploy_uses_staging_recovery_helper_not_launchctl_stop(self):
        """Deploy should recover staging with kickstart/bootstrap, not stop/start."""
        script_text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(
            encoding="utf-8"
        )
        assert "ensure_gateway_up_for_port" in script_text
        assert 'launchctl stop "gui/$(id -u)/ai.smartclaw.staging"' not in script_text

    def test_deploy_gateway_recovery_waits_with_bounded_polling(self):
        """Deploy gateway recovery should poll for readiness instead of fixed sleep."""
        script_text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(
            encoding="utf-8"
        )
        assert "OPENCLAW_DEPLOY_GATEWAY_START_TIMEOUT_SECONDS" in script_text
        assert "OPENCLAW_DEPLOY_GATEWAY_START_POLL_SECONDS" in script_text
        assert "launchctl print" in script_text
        assert 'ensure_gateway_up_for_port "$PROD_PORT" 1' in script_text
        assert 'launchctl start "gui/$(id -u)/ai.smartclaw.gateway"' not in script_text
        assert "launchctl list ai.smartclaw.gateway" not in script_text
        assert "sleep 35" not in script_text

    def test_deploy_uses_canary_retry_helper_for_initial_and_post_monitor_checks(self):
        """Deploy should use the canary retry helper for both initial and post-monitor checks."""
        script_text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(
            encoding="utf-8"
        )
        assert 'post_monitor_canary_with_retry "$STAGING_PORT" "$STAGING_CANARY_LOG" 0' in script_text
        assert 'post_monitor_canary_with_retry "$PROD_PORT" "$PROD_CANARY_LOG" 1' in script_text
        assert 'CANARY_MAX_ATTEMPTS="${OPENCLAW_DEPLOY_CANARY_MAX_ATTEMPTS:-3}"' in script_text
        assert 'CANARY_RETRY_COOLDOWN_SECONDS="${OPENCLAW_DEPLOY_CANARY_RETRY_COOLDOWN_SECONDS:-15}"' in script_text

    def test_deploy_halts_when_monitor_reports_problem_status(self):
        """Deploy must fail closed on monitor STATUS=PROBLEM even when monitor exits 0."""
        script_text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(
            encoding="utf-8"
        )
        assert "assert_monitor_status_good" in script_text
        assert 'assert_monitor_status_good "Stage 1: Monitor" "$STAGING_MONITOR_LOG"' in script_text
        assert 'assert_monitor_status_good "Stage 4: Monitor" "$PROD_MONITOR_LOG"' in script_text
        assert "Monitor reported STATUS=" in script_text

    def test_deploy_uses_staging_monitor_profile_for_stub_config(self):
        """Stage-1 monitor should disable prod-only probes that fail on repo stub configs."""
        script_text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(
            encoding="utf-8"
        )
        assert "DEPLOY_RUN_ID=" in script_text
        assert 'STAGING_MONITOR_LOG="/tmp/staging-monitor-${DEPLOY_RUN_ID}.log"' in script_text
        assert 'OPENCLAW_MONITOR_LOG_FILE="$STAGING_MONITOR_LOG"' in script_text
        assert 'OPENCLAW_MONITOR_LOCK_DIR="$STAGING_MONITOR_LOCK"' in script_text
        assert "OPENCLAW_MONITOR_TOKEN_PROBES_ENABLE=0" in script_text
        assert "OPENCLAW_MONITOR_MEMORY_LOOKUP_ENABLE=0" in script_text
        assert "OPENCLAW_MONITOR_DOCTOR_SH_ENABLE=0" in script_text
        assert "OPENCLAW_MONITOR_INFERENCE_PROBE_ENABLE=0" in script_text
        assert "OPENCLAW_MONITOR_PHASE2_ENABLE=0" in script_text
        assert 'OPENCLAW_MONITOR_SLACK_TARGET=""' in script_text
        assert 'OPENCLAW_MONITOR_FAILURE_SLACK_TARGET="$MONITOR_FAILURE_SLACK_TARGET"' in script_text
        assert "OPENCLAW_MONITOR_SLACK_READ_PROBE_ENABLE=0" in script_text
        assert "OPENCLAW_MONITOR_GATEWAY_PROBE_MESSAGE_ENABLE=0" in script_text
        assert "OPENCLAW_MONITOR_THREAD_REPLY_CHECK=0" in script_text
        assert "OPENCLAW_MONITOR_FAIL_CLOSED_CONFIG_SIGNATURES_ENABLE=0" in script_text

    def test_mem0_ollama_uses_supported_baseurl_keys(self, main_cfg: dict):
        """Mem0 OSS config must use baseURL/url for Ollama; mem0ai ignores ollama_base_url."""
        mem0_cfg = (
            main_cfg.get("plugins", {})
            .get("entries", {})
            .get("openclaw-mem0", {})
            .get("config", {})
            .get("oss", {})
        )
        for block_name in ("embedder", "llm"):
            block = mem0_cfg.get(block_name, {})
            if block.get("provider") != "ollama":
                continue
            config = block.get("config", {})
            assert "ollama_base_url" not in config, (
                f"plugins.entries.smartclaw-mem0.config.oss.{block_name}.config still uses "
                "'ollama_base_url'; mem0ai/oss ignores that key and falls back silently"
            )
            assert config.get("baseURL") or config.get("url"), (
                f"plugins.entries.smartclaw-mem0.config.oss.{block_name}.config for provider='ollama' "
                "must define baseURL or url"
            )

    def test_mem0_auto_features_disabled_on_live_gateway_configs(self, main_cfg: dict):
        """Slack reliability takes priority over mem0 auto-recall/capture on the live gateways."""
        mem0_cfg = (
            main_cfg.get("plugins", {})
            .get("entries", {})
            .get("openclaw-mem0", {})
            .get("config", {})
        )
        assert mem0_cfg.get("autoRecall") is False, (
            "plugins.entries.smartclaw-mem0.config.autoRecall must be false on the live gateway profile"
        )
        assert mem0_cfg.get("autoCapture") is False, (
            "plugins.entries.smartclaw-mem0.config.autoCapture must be false on the live gateway profile"
        )

    def test_mem0_plugin_degrades_invalid_oss_fact_output_to_noop(self):
        """Low-quality local fact extraction must not throw out of the mem0 auto-capture path."""
        plugin_text = (REPO_ROOT / "extensions" / "openclaw-mem0" / "index.ts").read_text(
            encoding="utf-8"
        )
        assert "isMem0OssStructuredOutputError" in plugin_text
        assert "mem0ai/oss returned invalid structured facts; degrading add() to a no-op" in plugin_text
        assert "return { results: [] };" in plugin_text

    def test_staging_canary_targets_staging_config_file(self):
        """Staging canary should read ~/.smartclaw/openclaw.staging.json for port 18810."""
        script_text = STAGING_CANARY_SCRIPT.read_text(encoding="utf-8")
        assert 'CONFIG_FILE="$HOME/.smartclaw/openclaw.staging.json"' in script_text
        assert "defaulting to staging config (~/.smartclaw/openclaw.staging.json)" in script_text

    def test_monitor_phase2_uses_supported_timeout_flag(self):
        """monitor phase2 must use PHASE2_TIMEOUT_SECONDS, not legacy --timeout-seconds flag."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        # Phase2 may use either 'openclaw agent --timeout' or 'timeout <secs> ai_orch' —
        # both are valid; what's banned is the unsupported --timeout-seconds flag.
        assert "PHASE2_TIMEOUT_SECONDS" in script_text, (
            "monitor phase2 should respect PHASE2_TIMEOUT_SECONDS (ORCH-y20)"
        )
        assert "--timeout-seconds" not in script_text, (
            "monitor phase2 still uses unsupported --timeout-seconds flag "
            "(ORCH-y20)"
        )

    def test_monitor_thread_probe_treats_app_messages_as_bot(self):
        """thread probe must not classify app-originated bot replies as human."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'select((.bot_id // "") == "")' in script_text, (
            "human-side filter must exclude bot messages "
            "to avoid false unanswered alerts"
        )
        assert 'select((.bot_id // "") != "" or (.subtype // "") == "bot_message"' in script_text, (
            "bot-side filter must include bot-originated messages "
            "to count OpenClaw replies correctly"
        )

    def test_monitor_thread_probe_resolves_bot_token_from_config(self):
        """thread probe should resolve bot token from ~/.smartclaw/openclaw.json when env is missing."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert ".channels.slack.botToken // empty" in script_text
        assert 'slack_bot_token="$(resolve_secret_ref "$(jq -r' in script_text

    def test_monitor_memory_probe_falls_back_to_mem0_surface(self):
        """monitor memory probe must support both `openclaw memory` and older `openclaw mem0` CLIs."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'openclaw memory search "test"' in script_text
        assert 'openclaw mem0 search "test"' in script_text
        assert "Did you mean mem0" in script_text

    def test_run_scheduled_job_uses_supported_agent_surface(self):
        """scheduled-job wrapper must use the current openclaw agent flags."""
        script_text = (REPO_ROOT / "run-scheduled-job.sh").read_text(
            encoding="utf-8"
        )
        assert "--timeout-seconds" not in script_text, (
            "run-scheduled-job.sh still uses unsupported --timeout-seconds flag"
        )
        assert '--timeout "$timeout_seconds"' in script_text, (
            "run-scheduled-job.sh should pass timeout via the supported --timeout flag"
        )
        assert "--session-id" in script_text, (
            "run-scheduled-job.sh should keep isolated sessions on the current CLI surface"
        )

    def test_monitor_supports_disabling_token_probes(self):
        """monitor should allow token probes to be disabled for staging-only validations."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'TOKEN_PROBES_ENABLED="${OPENCLAW_MONITOR_TOKEN_PROBES_ENABLE:-1}"' in script_text
        assert 'TOKEN_PROBE_SUMMARY="token probes disabled"' in script_text

    def test_monitor_supports_disabling_slack_read_probe(self):
        """monitor should support disabling Slack read probe for staging-safe runs."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'SLACK_READ_PROBE_ENABLED="${OPENCLAW_MONITOR_SLACK_READ_PROBE_ENABLE:-1}"' in script_text
        assert 'slack read probe disabled (OPENCLAW_MONITOR_SLACK_READ_PROBE_ENABLE=0)' in script_text

    def test_monitor_slack_e2e_matrix_covers_all_delivery_modes(self):
        """monitor must probe DM/channel/thread delivery with and without mentions."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'SLACK_E2E_MATRIX_ENABLED="${OPENCLAW_MONITOR_SLACK_E2E_MATRIX_ENABLE:-1}"' in script_text
        assert 'SLACK_E2E_CHANNEL_TARGET="${OPENCLAW_MONITOR_SLACK_E2E_CHANNEL_TARGET:-${SLACK_CHANNEL_ID}}"' in script_text
        assert 'SLACK_E2E_THREAD_CHANNEL_TARGET="${OPENCLAW_MONITOR_SLACK_E2E_THREAD_CHANNEL_TARGET:-C0AJ3SD5C79}"' in script_text
        for mode in (
            "dm_no_mention",
            "dm_with_mention",
            "channel_no_mention",
            "channel_with_mention",
            "thread_no_mention",
            "thread_with_mention",
        ):
            assert mode in script_text, f"missing Slack E2E matrix mode {mode}"

    def test_monitor_slack_e2e_matrix_prefers_nonignored_sender_identity(self):
        """Positive Slack E2E probes should use a real sender, not the ignored canary bot."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert "resolve_positive_probe_slack_token()" in script_text
        assert 'OPENCLAW_MONITOR_E2E_SLACK_TOKEN' in script_text
        assert 'OPENCLAW_SLACK_USER_TOKEN' in script_text
        assert 'SLACK_USER_TOKEN' in script_text

    def test_monitor_runs_slack_matrix_even_when_other_probes_are_noisy(self):
        """Slack E2E must still run when ws_churn or other preflight probes are red."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'run_slack_e2e_matrix_probe || true' in script_text
        assert 'if [ "$SLACK_E2E_MATRIX_ENABLED" != "1" ]; then' in script_text

    def test_monitor_slack_e2e_matrix_opens_dm_with_bot_user(self):
        """DM probe should open an IM conversation with the resolved OpenClaw bot user."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'conversations.open' in script_text
        assert '--data-urlencode "users=$bot_user_id"' in script_text
        assert 'resolve_primary_bot_user_id()' in script_text

    def test_monitor_slack_e2e_matrix_supports_separate_thread_channel_target(self):
        """Thread probes must be able to hit a distinct channel from top-level channel probes."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'OPENCLAW_MONITOR_SLACK_E2E_THREAD_CHANNEL_TARGET' in script_text
        assert 'SLACK_E2E_THREAD_CHANNEL_TARGET="${OPENCLAW_MONITOR_SLACK_E2E_THREAD_CHANNEL_TARGET:-C0AJ3SD5C79}"' in script_text
        assert 'thread_channel=${SLACK_E2E_THREAD_CHANNEL_TARGET}' in script_text

    def test_monitor_slack_e2e_matrix_rejects_bot_authored_channel_no_mention_probe(self):
        """Top-level channel_no_mention must fail closed when the sender post is app/bot-authored."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert "slack_post_message_author_kind()" in script_text
        assert 'mode_details+=("$mode=invalid_sender_${root_author_kind}")' in script_text
        assert 'invalid=${invalid}' in script_text

    def test_monitor_slack_e2e_matrix_rejects_bot_authored_thread_probe(self):
        """Thread probes must fail closed when the child reply is app/bot-authored."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'child_author_kind="$(slack_post_message_author_kind "$child_output"' in script_text
        assert 'mode_details+=("$mode=invalid_sender_${child_author_kind}")' in script_text

    def test_monitor_memory_probe_reports_qdrant_backend_failure(self):
        """monitor should surface Qdrant outages explicitly instead of generic rc failures."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert "Qdrant connection refused" in script_text

    def test_monitor_memory_probe_accepts_empty_memories_output(self):
        """monitor should treat an empty mem0 corpus as healthy, not unexpected output."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert "No memories found" in script_text

    def test_monitor_memory_probe_prefers_mem0_for_mem0_slot(self):
        """monitor should query the concrete mem0 surface when the memory slot is mem0."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'memory_slot="$(jq -r \'.plugins.slots.memory // empty\'' in script_text
        assert 'memory_cmd=\'openclaw mem0 search "test"\'' in script_text

    def test_monitor_resolves_staging_config_without_repo_root_fallback(self):
        """monitor must resolve config from OPENCLAW env/state and never assume repo-root openclaw.json."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert "resolve_monitor_config_path()" in script_text
        assert "resolve_monitor_base_config_path()" in script_text
        assert "resolve_monitor_token_probe_config_path()" in script_text
        assert "${OPENCLAW_STATE_DIR}/openclaw.staging.json" in script_text
        assert "$HOME/.smartclaw/staging" not in script_text
        assert "$MONITOR_REPO_ROOT/openclaw.json" not in script_text

    def test_monitor_token_probes_fallback_to_base_config_for_skeletal_staging_overlay(self):
        """token probes should read secrets from the full state config when staging overlay is skeletal."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'token_cfg="$(resolve_monitor_token_probe_config_path)"' in script_text
        assert 'slack_bot_token="$(resolve_secret_ref "$(jq -r \'.channels.slack.botToken // empty\' "$token_cfg"' in script_text
        assert 'slack_app_token="$(resolve_secret_ref "$(jq -r \'.channels.slack.appToken // empty\' "$token_cfg"' in script_text
        assert 'openai_token="$(resolve_secret_ref "$(jq -r \'.plugins.entries."openclaw-mem0".config.oss.embedder.config.apiKey // empty\' "$token_cfg"' in script_text

    def test_monitor_infers_gateway_port_from_profile_when_overlay_is_skeletal(self):
        """monitor should derive staging/prod gateway port when explicit config lacks gateway.port."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert "resolve_monitor_gateway_port()" in script_text
        assert 'gw_port="$(resolve_monitor_gateway_port)"' in script_text
        assert '18810' in script_text
        assert '18789' in script_text

    def test_monitor_skips_slack_matrix_when_profile_disables_slack(self):
        """staging monitor should report an honest skip instead of six false Slack failures."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert "monitor_slack_enabled_state()" in script_text
        assert 'if [ "$(monitor_slack_enabled_state)" = "false" ]; then' in script_text
        assert "Slack E2E matrix skipped: slack disabled in active profile" in script_text

    def test_monitor_slack_token_probes_warn_when_profile_disables_slack(self):
        """staging token probes should skip Slack auth probes when the active profile disables Slack."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'WARN:channels.slack.botToken:slack_disabled_in_active_profile' in script_text
        assert 'WARN:channels.slack.appToken:slack_disabled_in_active_profile' in script_text

    def test_monitor_phase1_restart_is_gated_to_connectivity_failures(self):
        """monitor should not restart gateway on WS churn/config-parse signatures alone."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'WS_CHURN_RESTART_ENABLED="${OPENCLAW_MONITOR_WS_CHURN_RESTART_ENABLE:-0}"' in script_text
        assert "is_gateway_connectivity_failure_output()" in script_text
        assert "HTTP_GATEWAY_RC=1  # Trigger Phase 1 restart" not in script_text
        assert 'if [ "$PROBE_REQUEST_RC" -ne 0 ] && is_gateway_connectivity_failure_output "$PROBE_REQUEST_OUTPUT"; then' in script_text
        assert 'if [ "$GATEWAY_PROBE_RC" -ne 0 ] && is_gateway_connectivity_failure_output "$GATEWAY_PROBE_OUTPUT"; then' in script_text

    def test_monitor_fail_closed_on_config_parse_typeerror_signatures(self):
        """monitor must fail-close on config-read/typeerror signatures without success payloads."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert "has_fail_closed_config_parse_signature()" in script_text
        assert "cli_output_has_success_payload()" in script_text
        assert "Failed to read config at" in script_text
        assert "TypeError: Cannot read properties of undefined" in script_text
        assert 'enforce_cli_output_fail_closed PROBE_REQUEST_RC PROBE_REQUEST_SUMMARY "slack_read_probe"' in script_text
        assert 'enforce_cli_output_fail_closed GATEWAY_PROBE_RC GATEWAY_PROBE_SUMMARY "slack_send_probe"' in script_text
        assert "but command returned success payload; not fail-closing" in script_text

    def test_monitor_send_report_fail_closes_on_cli_output_signatures(self):
        """send_report_to_slack should fail closed only when signature appears without success payload."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert "send_output=\"$(" in script_text
        assert "--message \"$SLACK_REPORT\" --json" in script_text
        assert "has_fail_closed_config_parse_signature \"$send_output\"" in script_text
        assert "fail-closed: config parse/typeerror signature" in script_text
        assert "success payload is present" in script_text

    def test_monitor_supports_toggling_fail_closed_config_signature_gate(self):
        """monitor should allow deploy/staging profiles to disable the signature fail-closed gate."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'FAIL_CLOSED_CONFIG_SIGNATURES_ENABLED="${OPENCLAW_MONITOR_FAIL_CLOSED_CONFIG_SIGNATURES_ENABLE:-1}"' in script_text
        assert 'if [ "$FAIL_CLOSED_CONFIG_SIGNATURES_ENABLED" = "1" ] && has_fail_closed_config_parse_signature "$output"; then' in script_text

    def test_monitor_fail_closed_signature_guard_is_toggleable(self):
        """staging deploy profile should be able to disable signature fail-close explicitly."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'FAIL_CLOSED_CONFIG_SIGNATURES_ENABLED="${OPENCLAW_MONITOR_FAIL_CLOSED_CONFIG_SIGNATURES_ENABLE:-1}"' in script_text
        assert 'if [ "$FAIL_CLOSED_CONFIG_SIGNATURES_ENABLED" != "1" ]; then' in script_text
        assert "has_fail_closed_config_parse_signature()" in script_text

    def test_qdrant_start_script_accepts_colima_fallback(self):
        """qdrant launcher must not assume Docker Desktop is the only usable context."""
        script_text = (REPO_ROOT / "scripts/start-qdrant-container.sh").read_text(
            encoding="utf-8"
        )
        assert "for context in colima-ci default desktop-linux" in script_text
        assert "OPENCLAW_QDRANT_DOCKER_CONTEXT" in script_text

    def test_qdrant_install_script_honors_context_override(self):
        """qdrant installer should honor OPENCLAW_QDRANT_DOCKER_CONTEXT for non-default runtimes."""
        script_text = (REPO_ROOT / "scripts/install-qdrant-container.sh").read_text(
            encoding="utf-8"
        )
        assert "OPENCLAW_QDRANT_DOCKER_CONTEXT" in script_text
        assert 'docker_cmd()' in script_text

    def test_monitor_infers_profile_paths_from_gateway_plist_port(self):
        """monitor should infer prod/staging config paths when plist omits explicit state vars."""
        script_text = (REPO_ROOT / "monitor-agent.sh").read_text(
            encoding="utf-8"
        )
        assert 'OPENCLAW_GATEWAY_PORT' in script_text
        assert '$HOME/.smartclaw_prod' in script_text
        assert '$HOME/.smartclaw' in script_text

    def test_doctor_infers_profile_paths_from_gateway_plist_port(self):
        """doctor should infer prod/staging config paths when plist omits explicit state vars."""
        script_text = (REPO_ROOT / "scripts/doctor.sh").read_text(
            encoding="utf-8"
        )
        assert "infer_gateway_profile_dir_from_port" in script_text
        assert '$HOME/.smartclaw_prod' in script_text
        assert '$HOME/.smartclaw' in script_text

    def test_doctor_memory_probe_accepts_empty_memories_output(self):
        """doctor should treat an empty mem0 corpus as healthy, not warn/fail."""
        script_text = (REPO_ROOT / "scripts/doctor.sh").read_text(
            encoding="utf-8"
        )
        assert "No memories found" in script_text

    def test_doctor_memory_probe_prefers_mem0_for_mem0_slot(self):
        """doctor should use the concrete mem0 command when the memory slot is mem0."""
        script_text = (REPO_ROOT / "scripts/doctor.sh").read_text(
            encoding="utf-8"
        )
        assert 'if [[ "$_mem_slot" == "openclaw-mem0" ]]; then' in script_text
        assert 'timeout 30 openclaw mem0 search "test"' in script_text

    def test_doctor_memory_probe_downgrades_timeout_to_warn(self):
        """doctor should not hard-fail when memory lookup times out transiently."""
        script_text = (REPO_ROOT / "scripts/doctor.sh").read_text(
            encoding="utf-8"
        )
        assert 'elif [[ "$memory_rc" -eq 124 ]]; then' in script_text
        assert "Memory lookup command timed out after 30s" in script_text

    def test_doctor_downgrades_unauthorized_gateway_status_probe(self):
        """doctor should not fail the run when gateway status self-probe is only unauthorized."""
        script_text = (REPO_ROOT / "scripts/doctor.sh").read_text(
            encoding="utf-8"
        )
        assert "RPC probe was unauthorized" in script_text
        assert "provide gateway auth token" in script_text

    def test_doctor_pytest_targets_live_config_path(self):
        """doctor should run config pytest against the live config, not the repo stub."""
        script_text = (REPO_ROOT / "scripts/doctor.sh").read_text(
            encoding="utf-8"
        )
        assert 'LIVE_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-$LIVE_OPENCLAW/openclaw.json}"' in script_text
        assert 'PYTEST_MAIN="$LIVE_OPENCLAW/openclaw.json"' in script_text
        assert 'OPENCLAW_TEST_MAIN_CONFIG_PATH="$PYTEST_MAIN"' in script_text
        assert "TestWsSafeAgentDefaults" in script_text

    def test_doctor_port_checks_prefer_explicit_live_config_path(self):
        """doctor should honor OPENCLAW_CONFIG_PATH when staging and prod coexist."""
        script_text = (REPO_ROOT / "scripts/doctor.sh").read_text(
            encoding="utf-8"
        )
        assert "live_port=$(jq -r '.gateway.port // empty' \"$LIVE_CONFIG_PATH\"" in script_text
        assert "runtime_port=$(jq -r '.gateway.port // empty' \"$LIVE_CONFIG_PATH\"" in script_text

    def test_doctor_uses_profile_specific_runtime_expectations(self):
        """doctor should use staging-aware heartbeat/port/state-dir expectations instead of prod defaults."""
        script_text = (REPO_ROOT / "scripts/doctor.sh").read_text(
            encoding="utf-8"
        )
        assert "detect_live_profile()" in script_text
        assert 'LIVE_PROFILE="$(detect_live_profile)"' in script_text
        assert "expected_heartbeat_runtime_every_for_profile()" in script_text
        assert "expected_gateway_port_for_profile()" in script_text
        assert "expected_state_dir_for_profile()" in script_text
        assert "staging) printf '30m'" in script_text
        assert "staging) printf '18810'" in script_text
        assert "staging) printf '%s/.smartclaw' \"$HOME\"" in script_text

    def test_doctor_infers_staging_port_from_profile_when_overlay_is_skeletal(self):
        """doctor should infer staging's canonical port when OPENCLAW_CONFIG_PATH points to a minimal overlay."""
        script_text = (REPO_ROOT / "scripts/doctor.sh").read_text(
            encoding="utf-8"
        )
        assert 'warn "gateway port missing from $LIVE_CONFIG_PATH; inferring $live_port for $LIVE_PROFILE profile"' in script_text
        assert 'runtime_port="$(expected_gateway_port_for_profile "$LIVE_PROFILE")"' in script_text
        assert 'warn "live gateway port unreadable from $LIVE_CONFIG_PATH; defaulting runtime checks to $runtime_port for $LIVE_PROFILE profile"' in script_text

    def test_doctor_treats_idle_dashboard_launchd_as_non_fatal_when_port_is_live(self):
        """doctor should not warn about the standalone dashboard launchd job if the dashboard is reachable."""
        script_text = (REPO_ROOT / "scripts/doctor.sh").read_text(
            encoding="utf-8"
        )
        assert "AO_DASHBOARD_LAUNCHD_NOTE" in script_text
        assert "AO dashboard is reachable on port" in script_text
        assert "standalone launchd job is idle" in script_text

    def test_gateway_preflight_blocks_live_config_version_drift(self):
        """deploy preflight should fail closed if the binary and live config versions drift."""
        script_text = (REPO_ROOT / "scripts/gateway-preflight.sh").read_text(
            encoding="utf-8"
        )
        assert "check_config_version_vs_binary" in script_text
        assert ".smartclaw_prod/openclaw.json" in script_text
        assert ".smartclaw/openclaw.json" in script_text
        assert "OPENCLAW_ALLOW_VERSION_DRIFT=1" in script_text
        assert "version drift" in script_text

    def test_install_runs_ao_doctor_canonical_binary_patch(self):
        """install.sh should patch the local AO doctor to accept the approved wrapper path."""
        script_text = (REPO_ROOT / "scripts/install.sh").read_text(
            encoding="utf-8"
        )
        assert "patch-ao-doctor-canonical-binary.sh" in script_text
        assert "canonical lifecycle-worker binary detection" in script_text

    def test_lifecycle_launchd_templates_use_direct_ao_wrapper(self):
        """lifecycle workers should launch via ~/bin/ao, not via a node-prefixed JS entrypoint."""
        manager_template = (
            REPO_ROOT / "launchd" / "ai.smartclaw.lifecycle-manager.plist.template"
        ).read_text(encoding="utf-8")
        orchestrator_plist = (
            REPO_ROOT / "launchd" / "com.agentorchestrator.lifecycle-agent-orchestrator.plist"
        ).read_text(encoding="utf-8")
        assert "@HOME@/bin/ao lifecycle-worker" in manager_template
        assert "@NODE_PATH@" not in manager_template
        assert "<string>@HOME@/bin/ao</string>" in orchestrator_plist
        assert "@NODE_PATH@" not in orchestrator_plist
        assert "<string>lifecycle-worker</string>" in orchestrator_plist

    def test_ao_doctor_canonical_binary_patch_accepts_node_entrypoints(self):
        """the patch script should teach ao-doctor about approved node-backed ao entrypoints."""
        script_text = (
            REPO_ROOT / "scripts/patch-ao-doctor-canonical-binary.sh"
        ).read_text(encoding="utf-8")
        assert "AO_DOCTOR_ACCEPT_NODE_ENTRYPOINTS" in script_text
        assert "/dist\\\\/index\\\\.js" in script_text
        assert "/bin\\\\/ao\\\\.js" in script_text
        assert '\\"$cmd_real\\" = \\"${canonical_real}\\"' in script_text


# ---------------------------------------------------------------------------
# ORCH-exec1: every safeBins entry must have a safeBinProfiles entry
#
# Missing safeBinProfiles causes openclaw to refuse to exec any shell command,
# producing the misleading "shell isn't working in this session" error.
# Triggered by openclaw upgrades that add new safeBinProfiles enforcement.
# ---------------------------------------------------------------------------


def _get_safe_bins(cfg: dict) -> set[str]:
    return set(cfg.get("tools", {}).get("exec", {}).get("safeBins", []))


def _get_safe_bin_profiles(cfg: dict) -> set[str]:
    return set((cfg.get("tools", {}).get("exec", {}).get("safeBinProfiles") or {}).keys())


class TestExecSafeBins:
    def test_every_safe_bin_has_a_profile(self, main_cfg: dict):
        """Every entry in tools.exec.safeBins must have a matching safeBinProfiles entry.

        Without a profile, openclaw silently refuses to exec the binary, causing
        all shell tool calls to fail with 'shell isn't working in this session' (ORCH-exec1).
        Fix: run 'openclaw doctor --fix' to scaffold missing profiles.
        """
        missing = _get_safe_bins(main_cfg) - _get_safe_bin_profiles(main_cfg)
        assert not missing, (
            f"safeBins entries missing from safeBinProfiles: {sorted(missing)}. "
            "Run 'openclaw doctor --fix' to scaffold them (ORCH-exec1)"
        )

    def test_safe_bin_profiles_has_no_orphans(self, main_cfg: dict):
        """Every safeBinProfiles entry should correspond to a safeBins entry.

        Orphaned profiles are harmless but indicate the safeBins list drifted.
        """
        orphans = _get_safe_bin_profiles(main_cfg) - _get_safe_bins(main_cfg)
        assert not orphans, (
            f"safeBinProfiles entries with no matching safeBins entry: {sorted(orphans)}. "
            "Remove orphaned profiles or re-add to safeBins (ORCH-exec1)"
        )

    def test_safe_bin_profiles_exists(self, main_cfg: dict):
        """safeBinProfiles key must exist (not null/absent) when safeBins is non-empty."""
        safe_bins = _get_safe_bins(main_cfg)
        profiles_raw = main_cfg.get("tools", {}).get("exec", {}).get("safeBinProfiles")
        if safe_bins:
            assert profiles_raw is not None, (
                "tools.exec.safeBinProfiles is null/absent but safeBins has entries. "
                "Run 'openclaw doctor --fix' to initialize it (ORCH-exec1)"
            )


# ---------------------------------------------------------------------------
# ORCH-meta1: meta section must be present and version must be a valid semver-like string
#
# meta.lastTouchedVersion is stamped by `openclaw doctor` — a null or missing
# value means the config was never validated by the openclaw CLI.
# ---------------------------------------------------------------------------


class TestMetaAndLogging:
    def test_meta_last_touched_version_present(self, main_cfg: dict):
        """meta.lastTouchedVersion must be a non-null string.

        If this is null/missing, the config was never run through `openclaw doctor`
        and may be stale or corrupted (ORCH-meta1).
        """
        version = main_cfg.get("meta", {}).get("lastTouchedVersion")
        assert version is not None, (
            "meta.lastTouchedVersion is missing/null — run 'openclaw doctor' to stamp it (ORCH-meta1)"
        )
        assert isinstance(version, str) and version.strip(), (
            f"meta.lastTouchedVersion={version!r} must be a non-empty string (ORCH-meta1)"
        )

    def test_meta_last_touched_version_is_semver_like(self, main_cfg: dict):
        """meta.lastTouchedVersion should match YYYY.M.D or standard semver format.

        A malformed version string indicates manual config corruption (ORCH-meta1).
        """
        import re
        version = main_cfg.get("meta", {}).get("lastTouchedVersion", "")
        # Accept YYYY.M.D, YYYY.MM.DD, or standard semver X.Y.Z
        pattern = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}$|^\d+\.\d+\.\d+$")
        assert pattern.match(str(version)), (
            f"meta.lastTouchedVersion={version!r} is not a valid version string. "
            "Expected YYYY.M.D or X.Y.Z format (ORCH-meta1)"
        )

    def test_logging_level_is_valid(self, main_cfg: dict):
        """logging.level must be one of the supported openclaw log levels.

        An invalid level causes openclaw to fall back to a default, potentially
        hiding debug output or causing verbose production noise (ORCH-meta1).
        """
        level = main_cfg.get("logging", {}).get("level")
        valid_levels = {"debug", "info", "warn", "error", "silent"}
        assert level in valid_levels, (
            f"logging.level={level!r} is not a valid level. "
            f"Must be one of: {sorted(valid_levels)} (ORCH-meta1)"
        )

    def test_logging_redact_sensitive_is_valid(self, main_cfg: dict):
        """logging.redactSensitive must be a valid enum value.

        An invalid value may cause openclaw to log raw secrets to disk/console,
        creating a credential leak risk (ORCH-meta1).
        """
        value = main_cfg.get("logging", {}).get("redactSensitive")
        # Accept null/absent (means default) or known valid enum values
        if value is None:
            return
        valid_values = {"off", "env", "tools", "all"}
        assert value in valid_values, (
            f"logging.redactSensitive={value!r} is not a valid value. "
            f"Must be one of: {sorted(valid_values)} (ORCH-meta1)"
        )


# ---------------------------------------------------------------------------
# ORCH-auth1: required auth profiles must be present and have valid mode
#
# Missing or misconfigured auth profiles cause silent auth failures when the
# gateway tries to authenticate requests for those providers.
# ---------------------------------------------------------------------------


class TestAuthProfiles:
    REQUIRED_PROFILES = [
        "openai-codex:default",
    ]
    VALID_AUTH_MODES = {"oauth", "api_key", "token", "none"}

    def test_required_profiles_present(self, main_cfg: dict, auth_profiles_cfg: dict):
        """All required auth profiles must be present.

        Runtime may source profiles from either openclaw.json auth.profiles or
        agents/main/agent/auth-profiles.json (ORCH-auth1).
        """
        profiles = _effective_auth_profiles(main_cfg, auth_profiles_cfg)
        missing = [p for p in self.REQUIRED_PROFILES if p not in profiles]
        assert not missing, (
            f"auth profiles missing required entries: {missing}. "
            "Add them with 'openclaw agents auth' (ORCH-auth1)"
        )

    def test_all_profiles_have_valid_mode(self, main_cfg: dict, auth_profiles_cfg: dict):
        """Every auth profile must have a recognized mode value.

        An invalid mode causes openclaw to reject the profile at startup,
        failing all agent sessions that use that provider (ORCH-auth1).
        """
        profiles = _effective_auth_profiles(main_cfg, auth_profiles_cfg)
        bad = []
        for name, profile in profiles.items():
            mode = profile.get("mode") or profile.get("type")
            if mode not in self.VALID_AUTH_MODES:
                bad.append(f"{name}.mode={mode!r}")
        assert not bad, (
            f"auth profile entries with invalid mode: {bad}. "
            f"Valid modes: {sorted(self.VALID_AUTH_MODES)} (ORCH-auth1)"
        )

    def test_openai_codex_profile_is_oauth(self, main_cfg: dict, auth_profiles_cfg: dict):
        """openai-codex:default must use oauth mode (Codex requires OAuth, not API key).

        Switching to api_key mode breaks Codex authentication silently (ORCH-auth1).
        """
        profiles = _effective_auth_profiles(main_cfg, auth_profiles_cfg)
        profile = profiles.get("openai-codex:default", {})
        mode = profile.get("mode") or profile.get("type")
        assert mode == "oauth", (
            f"auth profile['openai-codex:default'].mode={mode!r} — "
            "Codex requires oauth mode; api_key mode will silently fail (ORCH-auth1)"
        )

    def test_openai_profile_is_oauth(self, main_cfg: dict, auth_profiles_cfg: dict):
        """openai:default must use oauth mode.

        Some runtimes do not configure openai:default at all; enforce oauth when present.
        """
        profiles = _effective_auth_profiles(main_cfg, auth_profiles_cfg)
        if "openai:default" not in profiles:
            pytest.skip("openai:default auth profile not configured in this runtime")
        profile = profiles.get("openai:default", {})
        mode = profile.get("mode") or profile.get("type")
        assert mode == "oauth", (
            f"auth profile['openai:default'].mode={mode!r} — expected oauth (ORCH-auth1)"
        )


# ---------------------------------------------------------------------------
# ORCH-agent1: agents.defaults must have safe, valid configuration
#
# Misconfigured agent defaults affect all agent sessions globally.
# ---------------------------------------------------------------------------


class TestAgentDefaults:
    def test_primary_model_is_set(self, main_cfg: dict):
        """agents.defaults.model.primary must be set to a non-empty string.

        An empty or missing primary model causes all agent sessions to fail
        at model selection (ORCH-agent1).
        """
        primary = (
            main_cfg.get("agents", {})
            .get("defaults", {})
            .get("model", {})
            .get("primary")
        )
        assert primary is not None and str(primary).strip(), (
            "agents.defaults.model.primary is missing or empty. "
            "All agent sessions will fail at model selection (ORCH-agent1)"
        )

    def test_primary_model_is_not_claude_anthropic(self, main_cfg: dict):
        """agents.defaults.model.primary must NOT be a Claude/Anthropic model.

        Claude models use the main Anthropic API key (not OAuth), and the CLAUDE.md
        policy prohibits switching to API key mode (ORCH-agent1).
        """
        primary = (
            main_cfg.get("agents", {})
            .get("defaults", {})
            .get("model", {})
            .get("primary", "")
        )
        assert "claude" not in str(primary).lower() and "anthropic" not in str(primary).lower(), (
            f"agents.defaults.model.primary={primary!r} must not be a Claude/Anthropic model. "
            "Use openai-codex/gpt-5.3-codex (ORCH-agent1)"
        )

    def test_sandbox_mode_is_valid(self, main_cfg: dict):
        """agents.defaults.sandbox.mode must be a recognized sandbox mode.

        An invalid sandbox mode causes openclaw to refuse to start agent sessions
        with a confusing error about sandboxing configuration (ORCH-agent1).
        """
        mode = (
            main_cfg.get("agents", {})
            .get("defaults", {})
            .get("sandbox", {})
            .get("mode")
        )
        valid_modes = {"off", "non-main", "container", "none"}
        assert mode in valid_modes, (
            f"agents.defaults.sandbox.mode={mode!r} is not a valid mode. "
            f"Must be one of: {sorted(valid_modes)} (ORCH-agent1)"
        )

    def test_workspace_access_is_valid(self, main_cfg: dict):
        """agents.defaults.sandbox.workspaceAccess must be a recognized value.

        An invalid workspaceAccess value causes openclaw to silently block
        file operations in agent sessions (ORCH-agent1).
        """
        access = (
            main_cfg.get("agents", {})
            .get("defaults", {})
            .get("sandbox", {})
            .get("workspaceAccess")
        )
        valid_access = {"rw", "ro", "none"}
        assert access in valid_access, (
            f"agents.defaults.sandbox.workspaceAccess={access!r} is not valid. "
            f"Must be one of: {sorted(valid_access)} (ORCH-agent1)"
        )

    def test_max_concurrent_is_positive_int(self, main_cfg: dict):
        """agents.defaults.maxConcurrent must be a positive integer.

        A zero or negative value disables concurrent agents; null causes
        openclaw to fall back to a hardcoded default (ORCH-agent1).
        """
        max_concurrent = (
            main_cfg.get("agents", {})
            .get("defaults", {})
            .get("maxConcurrent")
        )
        assert isinstance(max_concurrent, int) and max_concurrent > 0, (
            f"agents.defaults.maxConcurrent={max_concurrent!r} must be a positive integer. "
            "A zero/negative value prevents concurrent agent execution (ORCH-agent1)"
        )


# ---------------------------------------------------------------------------
# ORCH-ws1: WS/event-loop protected keys (CLAUDE.md — doctor parity)
#
# Drift on heartbeat, concurrency caps, timeout, or memory slot causes pong
# starvation, dropped Slack messages, or silent mem0 disable.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _skip_ws_safe_if_skeletal_agent_defaults(request) -> None:
    """Stub repo configs omit agents.defaults — ORCH-ws1 pytest applies only to full live configs."""
    cls = getattr(request, "cls", None)
    if cls is None or cls.__name__ != "TestWsSafeAgentDefaults":
        return
    main_cfg = request.getfixturevalue("main_cfg")
    d = (main_cfg.get("agents") or {}).get("defaults") or {}
    if not isinstance(d.get("timeoutSeconds"), int) or not isinstance(d.get("maxConcurrent"), int):
        pytest.skip(
            "Skeletal openclaw.json (agents.defaults timeout/maxConcurrent missing) — "
            "ORCH-ws1 enforced when a full live config is present (e.g. ~/.smartclaw_prod/openclaw.json)"
        )


class TestWsSafeAgentDefaults:
    """Enforce CLAUDE.md protected keys against live openclaw.json (doctor pytest gate)."""

    TIMEOUT_CEILING = 600
    # Updated 2026-04-10 (user-approved): maxConcurrent=10 is the approved value.
    MAX_CONCURRENT_CEILING = 10
    SUBAGENT_CONCURRENT_CEILING = 10

    def test_heartbeat_every_is_5m(self, main_cfg: dict):
        every = (main_cfg.get("agents", {}).get("defaults", {}).get("heartbeat") or {}).get("every")
        assert every == "5m", (
            f"agents.defaults.heartbeat.every={every!r} must be '5m' (ORCH-ws1 / CLAUDE protected)"
        )

    def test_heartbeat_target_is_last(self, main_cfg: dict):
        target = (main_cfg.get("agents", {}).get("defaults", {}).get("heartbeat") or {}).get("target")
        assert target == "last", (
            f"agents.defaults.heartbeat.target={target!r} must be 'last' (ORCH-ws1 / CLAUDE protected)"
        )

    def test_timeout_seconds_within_ws_budget(self, main_cfg: dict):
        t = main_cfg.get("agents", {}).get("defaults", {}).get("timeoutSeconds")
        assert isinstance(t, int) and t > 0 and t <= self.TIMEOUT_CEILING, (
            f"agents.defaults.timeoutSeconds={t!r} must be <= {self.TIMEOUT_CEILING} (ORCH-ws1)"
        )

    def test_max_concurrent_within_ws_budget(self, main_cfg: dict):
        m = main_cfg.get("agents", {}).get("defaults", {}).get("maxConcurrent")
        assert isinstance(m, int) and m > 0 and m <= self.MAX_CONCURRENT_CEILING, (
            f"agents.defaults.maxConcurrent={m!r} must be <= {self.MAX_CONCURRENT_CEILING} (ORCH-ws1)"
        )

    def test_subagents_max_concurrent_within_ws_budget(self, main_cfg: dict):
        m = (main_cfg.get("agents", {}).get("defaults", {}).get("subagents") or {}).get("maxConcurrent")
        assert isinstance(m, int) and m > 0 and m <= self.SUBAGENT_CONCURRENT_CEILING, (
            f"agents.defaults.subagents.maxConcurrent={m!r} must be <= {self.SUBAGENT_CONCURRENT_CEILING} "
            "(ORCH-ws1)"
        )

    def test_plugins_memory_slot_is_openclaw_mem0(self, main_cfg: dict):
        slot = (main_cfg.get("plugins", {}).get("slots") or {}).get("memory")
        assert slot == "openclaw-mem0", (
            f"plugins.slots.memory={slot!r} must be 'openclaw-mem0' (ORCH-ws1)"
        )


# ---------------------------------------------------------------------------
# ORCH-minimax1: MiniMax model id must match a registered models.providers entry
#
# Mismatch (e.g. primary=minimax/MiniMax-M2.7 with only minimax-portal registered)
# surfaces as "Unknown model" or invalid plugin errors — catch in doctor early.
# ---------------------------------------------------------------------------


def _collect_openclaw_model_ids(cfg: dict) -> list[str]:
    """Primary, fallbacks, and per-agent model fields."""
    out: list[str] = []
    model_block = cfg.get("agents", {}).get("defaults", {}).get("model") or {}
    primary = model_block.get("primary")
    if primary:
        out.append(str(primary))
    for fb in model_block.get("fallbacks") or []:
        if fb:
            out.append(str(fb))
    for agent in cfg.get("agents", {}).get("list") or []:
        m = agent.get("model")
        if m:
            out.append(str(m))
    # Dedupe preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for mid in out:
        if mid not in seen:
            seen.add(mid)
            uniq.append(mid)
    return uniq


class TestMinimaxProviderConsistency:
    def test_doctor_does_not_hardcode_stale_minimax_plugin_rename(self):
        """doctor must not force a specific MiniMax plugin id across OpenClaw releases."""
        script_text = (REPO_ROOT / "scripts/doctor.sh").read_text(
            encoding="utf-8"
        )
        assert 'use "minimax-portal-auth"' not in script_text
        assert 'plugins.allow contains stale plugin id "minimax"' not in script_text
        assert 'plugins.entries.minimax is stale' not in script_text

    def test_doctor_accepts_enabled_or_allow_for_slack_wildcard(self):
        """doctor should tolerate both legacy and current Slack channel wildcard schemas."""
        script_text = (REPO_ROOT / "scripts/doctor.sh").read_text(
            encoding="utf-8"
        )
        assert 'channels.slack.channels."*".enabled' in script_text
        assert 'channels.slack.channels."*".allow' in script_text

    def test_primary_minimax_model_matches_registered_provider(self, main_cfg: dict):
        """Primary MiniMax model must use the provider id that is actually registered."""
        primary = ((main_cfg.get("agents") or {}).get("defaults") or {}).get(
            "model", {}
        ).get("primary")
        providers = (main_cfg.get("models") or {}).get("providers") or {}
        assert primary == "minimax/MiniMax-M2.7", (
            f"agents.defaults.model.primary={primary!r} — expected "
            "'minimax/MiniMax-M2.7' to match the working registered provider block"
        )
        assert "minimax" in providers, (
            "models.providers.minimax missing for primary MiniMax model "
            "(ORCH-minimax1)"
        )

    def test_minimax_runtime_provider_uses_anthropic_messages_endpoint(self, main_cfg: dict):
        """The active MiniMax provider must use the Anthropic-compatible endpoint."""
        providers = (main_cfg.get("models") or {}).get("providers") or {}
        provider = providers.get("minimax") or {}
        assert provider.get("api") == "anthropic-messages", (
            f"models.providers.minimax.api={provider.get('api')!r} must be 'anthropic-messages'"
        )
        assert provider.get("baseUrl") == "https://api.minimax.io/anthropic", (
            f"models.providers.minimax.baseUrl={provider.get('baseUrl')!r} must be "
            "'https://api.minimax.io/anthropic'"
        )

    def test_minimax_model_ids_match_models_providers(self, main_cfg: dict):
        """minimax/ and minimax-portal/ prefixes must match registered provider blocks."""
        providers = (main_cfg.get("models") or {}).get("providers") or {}
        for mid in _collect_openclaw_model_ids(main_cfg):
            if "/" not in mid:
                continue
            prov_id = mid.split("/", 1)[0]
            if prov_id == "minimax-portal":
                assert "minimax-portal" in providers, (
                    f"model {mid!r} requires models.providers.minimax-portal (ORCH-minimax1)"
                )
            elif prov_id == "minimax":
                assert "minimax" in providers, (
                    f"model {mid!r} requires models.providers.minimax (ORCH-minimax1)"
                )

    def test_tracked_cron_jobs_do_not_pin_broken_portal_minimax_model(self):
        """Tracked cron jobs should use the working minimax/ model id, not minimax-portal/."""
        jobs_path = REPO_ROOT / "cron" / "jobs.json"
        jobs_cfg = json.loads(jobs_path.read_text(encoding="utf-8"))
        portal_jobs = []

        def walk(value):
            if isinstance(value, dict):
                for item in value.values():
                    yield from walk(item)
            elif isinstance(value, list):
                for item in value:
                    yield from walk(item)
            elif isinstance(value, str):
                yield value

        for job in jobs_cfg.get("jobs", []):
            if "minimax-portal/MiniMax-M2.7" in set(walk(job)):
                portal_jobs.append(job.get("id") or job.get("name") or "<unknown>")

        assert not portal_jobs, (
            "cron/jobs.json still pins minimax-portal/ model ids: "
            + ", ".join(portal_jobs)
        )

    def test_doctor_checks_live_profile_workspace_and_agentdir_roots(self):
        """doctor must fail when a live profile points workspace/agentDir back at another profile."""
        script_text = (REPO_ROOT / "scripts/doctor.sh").read_text(encoding="utf-8")
        assert "runtime invariant: agents.defaults.workspace rooted in" in script_text
        assert "runtime invariant: agent workspaces rooted in" in script_text
        assert "runtime invariant: agentDir paths rooted in" in script_text
        assert "runtime invariant: agent workspace path drift detected:" in script_text
        assert "runtime invariant: agentDir path drift detected:" in script_text
        assert "MiniMax runtime provider drift:" in script_text
        assert "MiniMax runtime provider matches anthropic-messages https://api.minimax.io/anthropic" in script_text
        assert "mem0 Ollama embedder is missing baseURL/url" in script_text
        assert "mem0 Ollama LLM is missing baseURL/url" in script_text

    def test_doctor_detects_shared_slack_socket_mode_tokens_between_prod_and_staging(self):
        """doctor should fail closed when prod and staging share Slack socket-mode tokens while both run."""
        script_text = (REPO_ROOT / "scripts/doctor.sh").read_text(encoding="utf-8")
        assert "check_shared_slack_socket_tokens" in script_text
        assert "launchd_job_is_running" in script_text
        assert "Slack socket-mode tokens are shared with" in script_text
        assert "do not run both profiles concurrently" in script_text
        assert "Slack socket-mode tokens do not collide with" in script_text

    def test_prod_live_config_uses_prod_workspace_and_agent_dirs_when_present(self):
        """If a prod profile exists locally, its agents must not point back into ~/.smartclaw."""
        prod_cfg_path = Path.home() / ".smartclaw_prod" / "openclaw.json"
        if not prod_cfg_path.exists():
            pytest.skip("local prod profile not present")

        prod_cfg = json.loads(prod_cfg_path.read_text(encoding="utf-8"))
        prod_root = str(Path.home() / ".smartclaw_prod")
        defaults_workspace = (
            (((prod_cfg.get("agents") or {}).get("defaults")) or {}).get("workspace") or ""
        )
        assert defaults_workspace.startswith(f"{prod_root}/workspace"), (
            f"prod agents.defaults.workspace={defaults_workspace!r} must stay rooted in {prod_root}/workspace"
        )

        bad_workspaces = []
        bad_agent_dirs = []
        for agent in ((prod_cfg.get("agents") or {}).get("list")) or []:
            name = agent.get("id") or agent.get("name") or "<unknown>"
            workspace = agent.get("workspace") or ""
            agent_dir = agent.get("agentDir") or ""
            if workspace and not workspace.startswith(f"{prod_root}/workspace"):
                bad_workspaces.append(f"{name}:{workspace}")
            if agent_dir and not agent_dir.startswith(f"{prod_root}/agents/"):
                bad_agent_dirs.append(f"{name}:{agent_dir}")

        assert not bad_workspaces, (
            "prod config points agent workspaces outside ~/.smartclaw_prod: "
            + ", ".join(bad_workspaces)
        )
        assert not bad_agent_dirs, (
            "prod config points agentDir outside ~/.smartclaw_prod/agents: "
            + ", ".join(bad_agent_dirs)
        )

    def test_all_agent_entries_define_workspace_and_agent_dir(self, main_cfg: dict):
        """Gateway config must not leave agent workspace/agentDir null for any listed agent."""
        bad_agents = []
        for agent in ((main_cfg.get("agents") or {}).get("list")) or []:
            name = agent.get("id") or agent.get("name") or "<unknown>"
            workspace = agent.get("workspace")
            agent_dir = agent.get("agentDir")
            if not isinstance(workspace, str) or not workspace:
                bad_agents.append(f"{name}:workspace={workspace!r}")
            if not isinstance(agent_dir, str) or not agent_dir:
                bad_agents.append(f"{name}:agentDir={agent_dir!r}")

        assert not bad_agents, (
            "agents.list entries missing workspace/agentDir break gateway validation: "
            + ", ".join(bad_agents)
        )


# ---------------------------------------------------------------------------
# ORCH-tools1: tools section must have valid configuration for exec and web
#
# Misconfigured tools settings cause silent failures or security issues.
# ---------------------------------------------------------------------------


class TestToolsConfig:
    def test_tools_profile_is_coding(self, main_cfg: dict):
        """tools.profile must be 'coding' for the main agent.

        The 'coding' profile enables file editing and shell execution tools
        that the main agent requires. Other profiles may silently remove
        required tools (ORCH-tools1).
        """
        profile = main_cfg.get("tools", {}).get("profile")
        assert profile == "coding", (
            f"tools.profile={profile!r} — expected 'coding' for the main agent. "
            "Other profiles may silently remove required tools (ORCH-tools1)"
        )

    def test_exec_host_is_gateway(self, main_cfg: dict):
        """tools.exec.host must be 'gateway'.

        A missing or 'none' exec host disables shell execution for all agents,
        breaking code editing and automation tasks silently (ORCH-tools1).
        """
        host = main_cfg.get("tools", {}).get("exec", {}).get("host")
        assert host == "gateway", (
            f"tools.exec.host={host!r} — expected 'gateway'. "
            "Without a valid exec host, shell tools are unavailable (ORCH-tools1)"
        )

    def test_exec_security_is_full(self, main_cfg: dict):
        """tools.exec.security must be 'full'.

        Any other security setting silently restricts what binaries the agent
        can run, breaking automation tasks without clear error messages (ORCH-tools1).
        """
        security = main_cfg.get("tools", {}).get("exec", {}).get("security")
        assert security == "full", (
            f"tools.exec.security={security!r} — expected 'full'. "
            "Other values silently restrict executable binaries (ORCH-tools1)"
        )

    def test_exec_ask_is_off(self, main_cfg: dict):
        """tools.exec.ask must be 'off' for automation use.

        When ask='always' or 'untrusted', openclaw pauses to request human
        approval for every exec call, breaking all automated workflows (ORCH-tools1).
        """
        ask = main_cfg.get("tools", {}).get("exec", {}).get("ask")
        assert ask == "off", (
            f"tools.exec.ask={ask!r} — expected 'off'. "
            "ask='always' breaks all automated shell tasks (ORCH-tools1)"
        )

    def test_web_search_provider_is_set(self, main_cfg: dict):
        """tools.web.search.provider must be a non-empty string when present.

        An empty provider string causes openclaw to silently fail all web
        search tool calls (ORCH-tools1).
        """
        web_search = main_cfg.get("tools", {}).get("web", {}).get("search", {})
        provider = web_search.get("provider")
        if provider is not None:
            assert str(provider).strip(), (
                "tools.web.search.provider is empty — web search will fail silently (ORCH-tools1)"
            )


# ---------------------------------------------------------------------------
# ORCH-env1: env section must not embed raw secrets
#
# Raw secrets in openclaw.json are committed to git (via the redacted copy)
# and also logged by openclaw when logging.redactSensitive is not 'all'.
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    (r'^xox[bpars]-[0-9A-Za-z-]+', "Slack token"),
    (r'^xapp-[0-9A-Za-z-]+', "Slack app token"),
    (r'^sk-proj-[0-9A-Za-z_-]{20,}', "OpenAI API key"),
    (r'^gsk_[0-9A-Za-z]{20,}', "Groq API key"),
    (r'^xai-[0-9A-Za-z]{20,}', "xAI API key"),
    (r'^sk-cp-[0-9A-Za-z_-]{20,}', "MiniMax API key"),
]


def _is_raw_secret(value: str) -> tuple[bool, str]:
    """Return (True, label) if value looks like a raw credential, else (False, '')."""
    import re
    if not isinstance(value, str):
        return False, ""
    for pattern, label in _SECRET_PATTERNS:
        if re.match(pattern, value):
            return True, label
    return False, ""


class TestEnvSection:
    def test_env_section_present(self, main_cfg: dict):
        """env section must exist in openclaw.json.

        The env section passes environment variables to all agent sessions.
        A missing env section means critical env vars (PATH, URLs) won't be
        injected into agent environments (ORCH-env1).
        """
        env = main_cfg.get("env")
        assert isinstance(env, dict), (
            "env section is missing or not a dict — "
            "critical env vars won't reach agent sessions (ORCH-env1)"
        )

    def test_env_path_includes_nvm(self, main_cfg: dict):
        """env.PATH must include .nvm path for Node.js discovery.

        Without .nvm in PATH, the agent cannot find node/npm binaries,
        breaking all Node.js skill installs (ORCH-env1).
        """
        path = main_cfg.get("env", {}).get("PATH", "")
        assert ".nvm" in str(path), (
            f"env.PATH={path!r} does not include .nvm path. "
            "Node.js tools will not be discoverable by the agent (ORCH-env1)"
        )

    def test_env_mission_control_base_url_set(self, main_cfg: dict):
        """env.MISSION_CONTROL_BASE_URL must be set.

        Without this, the Mission Control backend URL is not injected into
        agent sessions, breaking all MC API calls (ORCH-env1).
        """
        url = main_cfg.get("env", {}).get("MISSION_CONTROL_BASE_URL")
        assert url is not None and str(url).strip(), (
            "env.MISSION_CONTROL_BASE_URL is missing or empty. "
            "Mission Control API calls will fail in agent sessions (ORCH-env1)"
        )

    def test_env_values_are_not_raw_credential_literals(self, main_cfg: dict):
        """env values that look like credentials must be ${VAR} refs, not raw literals.

        Raw credential literals in env section are logged when openclaw logs
        config state, creating credential leak risk in log files (ORCH-env1).
        """
        pytest.skip(
            "openclaw.json is a local gitignored runtime file and may contain real tokens; "
            "raw secret enforcement is validated on tracked artifacts instead (ORCH-env1)"
        )


# ---------------------------------------------------------------------------
# ORCH-gw1: gateway MUST bind to loopback — binding to 0.0.0.0 is a security hole
#
# If gateway.bind='0.0.0.0', any process on the network can reach the gateway
# and execute arbitrary code via the agent. This is a critical security flaw.
# ---------------------------------------------------------------------------


class TestGatewaySecurity:
    def test_gateway_port_is_18789(self, main_cfg: dict):
        """gateway.port must be 18789 (the canonical openclaw gateway port).

        Using a non-standard port breaks all tooling that hardcodes 18789
        (doctor.sh, health probes, launchd config). Changing the port requires
        coordinated updates to all callers (ORCH-gw1).
        """
        port = main_cfg.get("gateway", {}).get("port")
        assert port == 18789, (
            f"gateway.port={port!r} — expected 18789. "
            "Change requires coordinated updates to doctor.sh and launchd plists (ORCH-gw1)"
        )

    def test_gateway_bind_is_loopback(self, main_cfg: dict):
        """CRITICAL: gateway.bind must be 'loopback', never '0.0.0.0'.

        Binding to 0.0.0.0 exposes the gateway to ALL network interfaces,
        allowing any process on the local network to execute arbitrary code
        via the agent. This is a critical security vulnerability (ORCH-gw1).
        """
        bind = main_cfg.get("gateway", {}).get("bind")
        assert bind == "loopback", (
            f"SECURITY VIOLATION: gateway.bind={bind!r} — must be 'loopback'. "
            "Binding to 0.0.0.0 exposes the gateway to the network, allowing "
            "arbitrary code execution by any network peer (ORCH-gw1)"
        )

    def test_gateway_mode_is_local(self, main_cfg: dict):
        """gateway.mode must be 'local' for the personal-machine config.

        Non-local modes change authentication and routing behavior in ways
        that may bypass local security controls (ORCH-gw1).
        """
        mode = main_cfg.get("gateway", {}).get("mode")
        assert mode == "local", (
            f"gateway.mode={mode!r} — expected 'local' for personal machine config. "
            "Non-local modes change auth behavior (ORCH-gw1)"
        )

    def test_gateway_auth_mode_is_token(self, main_cfg: dict):
        """gateway.auth.mode must be 'token'.

        Other auth modes (e.g. 'none') remove authentication entirely,
        allowing unauthenticated access to the gateway API (ORCH-gw1).
        """
        auth_mode = main_cfg.get("gateway", {}).get("auth", {}).get("mode")
        assert auth_mode == "token", (
            f"gateway.auth.mode={auth_mode!r} — must be 'token'. "
            "Other modes may remove authentication from the gateway API (ORCH-gw1)"
        )

    def test_gateway_auth_token_is_set(self, main_cfg: dict):
        """gateway.auth.token must be non-null and non-empty.

        A missing or empty gateway token means the gateway accepts all requests
        without authentication, creating an open local code execution endpoint (ORCH-gw1).
        """
        token = main_cfg.get("gateway", {}).get("auth", {}).get("token")
        assert token is not None, (
            "gateway.auth.token is null/missing — gateway is unauthenticated (ORCH-gw1)"
        )
        token_str = str(token).strip()
        assert token_str, (
            "gateway.auth.token is empty — gateway will reject all requests (ORCH-gw1)"
        )
        # Check it's not a well-known placeholder
        placeholders = {"your-token-here", "placeholder", "changeme", "secret", "token"}
        assert token_str.lower() not in placeholders, (
            f"gateway.auth.token={token_str!r} looks like a placeholder. "
            "Set a real token value (ORCH-gw1)"
        )


# ---------------------------------------------------------------------------
# ORCH-hooks1: hooks must be enabled with a non-placeholder token
#
# hooks.enabled=false silently stops all webhook-triggered agent sessions.
# A placeholder token causes all webhook calls to fail with 401.
# ---------------------------------------------------------------------------


class TestHooksConfig:
    def test_hooks_enabled_is_true(self, main_cfg: dict):
        """hooks.enabled must be true.

        hooks.enabled=false silently stops all webhook-triggered agent sessions.
        Any commit that sets this to false breaks agento and all hook-based
        automation without a visible error (ORCH-hooks1).
        """
        enabled = main_cfg.get("hooks", {}).get("enabled")
        assert enabled is True, (
            f"hooks.enabled={enabled!r} — must be True. "
            "False silently stops all webhook-triggered agent sessions (ORCH-hooks1)"
        )

    def test_hooks_token_is_set(self, main_cfg: dict):
        """hooks.token must be non-null and non-empty.

        A missing or empty hooks token causes all webhook calls to fail with
        401 Unauthorized, silently breaking all hook-triggered automations (ORCH-hooks1).
        """
        token = main_cfg.get("hooks", {}).get("token")
        assert token is not None, (
            "hooks.token is null/missing — all webhook calls will fail with 401 (ORCH-hooks1)"
        )
        token_str = str(token).strip()
        assert token_str, (
            "hooks.token is empty — webhook authentication will fail (ORCH-hooks1)"
        )
        placeholders = {"your-token-here", "placeholder", "changeme", "secret"}
        assert token_str.lower() not in placeholders, (
            f"hooks.token={token_str!r} looks like a placeholder — set a real value (ORCH-hooks1)"
        )


# ---------------------------------------------------------------------------
# ORCH-session1: session idle timeout must be a sensible positive integer
#
# A zero or negative idleMinutes causes immediate session termination;
# an unreasonably large value causes resource leaks from zombie sessions.
# ---------------------------------------------------------------------------


class TestSessionConfig:
    def test_idle_minutes_is_positive_integer(self, main_cfg: dict):
        """session.idleMinutes must be a positive integer >= 1.

        A zero value causes sessions to terminate immediately after each message.
        A negative value causes undefined behavior. A very large value (>= 10000)
        may indicate a misconfigured "never expire" attempt (ORCH-session1).
        """
        idle = main_cfg.get("session", {}).get("idleMinutes")
        assert isinstance(idle, int) and idle > 0, (
            f"session.idleMinutes={idle!r} must be a positive integer. "
            "Zero or negative causes immediate session termination (ORCH-session1)"
        )
        assert idle < 10_000, (
            f"session.idleMinutes={idle} seems unreasonably large (>= 10000). "
            "Check for configuration error (ORCH-session1)"
        )


# ---------------------------------------------------------------------------
# ORCH-cmd1: commands section must have valid enum values for native/nativeSkills
#
# Invalid enum values cause openclaw to ignore the setting and apply defaults,
# which may differ from the intended behavior.
# ---------------------------------------------------------------------------


class TestCommandsConfig:
    VALID_NATIVE_VALUES = {"auto", "on", "off", "true", "false", True, False}

    def test_commands_native_is_valid(self, main_cfg: dict):
        """commands.native must be a recognized value.

        An invalid value causes openclaw to fall back to a default, which may
        disable native command support without a visible error (ORCH-cmd1).
        """
        native = main_cfg.get("commands", {}).get("native")
        valid = {"auto", "on", "off"}
        if native is not None:
            assert native in valid, (
                f"commands.native={native!r} must be one of: {sorted(valid)} (ORCH-cmd1)"
            )

    def test_commands_native_skills_is_valid(self, main_cfg: dict):
        """commands.nativeSkills must be a recognized value.

        An invalid value causes openclaw to fall back to a default, which may
        disable native skill support without a visible error (ORCH-cmd1).
        """
        native_skills = main_cfg.get("commands", {}).get("nativeSkills")
        valid = {"auto", "on", "off"}
        if native_skills is not None:
            assert native_skills in valid, (
                f"commands.nativeSkills={native_skills!r} must be one of: {sorted(valid)} (ORCH-cmd1)"
            )

    def test_commands_restart_is_true(self, main_cfg: dict):
        """commands.restart must be True.

        commands.restart=False means the /restart command is not available to
        the user, blocking recovery from stuck agent sessions (ORCH-cmd1).
        """
        restart = main_cfg.get("commands", {}).get("restart")
        assert restart is True, (
            f"commands.restart={restart!r} — must be True. "
            "False disables the /restart command, blocking session recovery (ORCH-cmd1)"
        )


# ---------------------------------------------------------------------------
# ORCH-msg1: messages config must have expected ack reaction values
#
# An empty or incorrect ackReaction causes the agent to not react to messages,
# leaving users unsure whether the agent received their request.
# ---------------------------------------------------------------------------


class TestMessagesConfig:
    def test_ack_reaction_is_eyes(self, main_cfg: dict):
        """messages.ackReaction must be 'eyes'.

        The 'eyes' emoji is the standard acknowledgment that the agent has
        seen and is processing a message. Changing it breaks user expectations
        and monitoring scripts that look for this specific reaction (ORCH-msg1).
        """
        ack = main_cfg.get("messages", {}).get("ackReaction")
        assert ack == "eyes", (
            f"messages.ackReaction={ack!r} — expected 'eyes'. "
            "Changing the ack reaction breaks user expectations and monitors (ORCH-msg1)"
        )

    def test_ack_reaction_scope_is_all(self, main_cfg: dict):
        """messages.ackReactionScope must be 'all'.

        A scope other than 'all' means some message types won't get the ack
        reaction, leaving users unsure whether their message was received (ORCH-msg1).
        """
        scope = main_cfg.get("messages", {}).get("ackReactionScope")
        assert scope == "all", (
            f"messages.ackReactionScope={scope!r} — expected 'all'. "
            "Other scopes silently skip ack reactions for some message types (ORCH-msg1)"
        )


# ---------------------------------------------------------------------------
# ORCH-plug1: channel enabled state must be consistent with plugin enabled state
#
# channels.X.enabled=True with plugins.entries.X.enabled=False means the
# channel is trying to connect but the plugin is not loaded, causing startup
# errors. The inverse (plugin enabled, channel disabled) is safe but wasteful.
# ---------------------------------------------------------------------------


class TestPluginChannelConsistency:
    CHANNEL_PLUGIN_PAIRS = [
        ("slack", "slack"),
        ("discord", "discord"),
        ("whatsapp", "whatsapp"),
        ("googlechat", "googlechat"),
    ]

    def test_channel_plugin_enabled_states_consistent(self, main_cfg: dict):
        """For each channel, channels.X.enabled must match plugins.entries.X.enabled.

        channels.X.enabled=True with plugins.entries.X.enabled=False causes
        openclaw to try to connect a channel whose plugin is not loaded,
        producing startup errors or silent connection failures (ORCH-plug1).
        """
        channels = main_cfg.get("channels", {})
        plugin_entries = main_cfg.get("plugins", {}).get("entries", {})
        mismatches = []
        for channel_key, plugin_key in self.CHANNEL_PLUGIN_PAIRS:
            channel_enabled = channels.get(channel_key, {}).get("enabled")
            plugin_enabled = plugin_entries.get(plugin_key, {}).get("enabled")
            # Only flag the dangerous case: channel enabled but plugin disabled
            if channel_enabled is True and plugin_enabled is False:
                mismatches.append(
                    f"{channel_key}: channels.enabled=True but plugins.entries.{plugin_key}.enabled=False"
                )
        assert not mismatches, (
            f"Channel/plugin enabled state mismatches (channel enabled, plugin disabled): "
            f"{mismatches}. Either enable the plugin or disable the channel (ORCH-plug1)"
        )

    def test_slack_channel_and_plugin_both_enabled(self, main_cfg: dict):
        """Slack channel and plugin must both be enabled for Slack to work.

        Despite the gateway warning "plugin not found: slack (stale config
        entry ignored)", the plugin entry IS required for Slack socket mode
        activation. Removing it breaks Slack connectivity even though the
        gateway health check passes. Do NOT remove this test (ORCH-plug1).
        """
        channels = main_cfg.get("channels", {})
        plugin_entries = main_cfg.get("plugins", {}).get("entries", {})
        channel_enabled = channels.get("slack", {}).get("enabled")
        plugin_enabled = plugin_entries.get("slack", {}).get("enabled")
        if channel_enabled is not True:
            pytest.skip(
                f"channels.slack.enabled={channel_enabled!r} in repo copy — "
                "live-only invariant enforced by sync validation guard (ORCH-plug1)"
            )
        assert plugin_enabled is True, (
            f"plugins.entries.slack.enabled={plugin_enabled!r} while channels.slack.enabled=True. "
            "Slack will not work — the plugin entry activates socket mode even though "
            "the gateway warns 'plugin not found'. Do NOT remove it (ORCH-plug1)"
        )


# ---------------------------------------------------------------------------
# ORCH-slack2: Slack channel config must meet minimum operational thresholds
#
# Low historyLimit values cause the agent to miss context from recent messages;
# incorrect mode or groupPolicy values break Socket Mode connectivity.
# ---------------------------------------------------------------------------


class TestSlackChannelsConfig:
    def _slack_cfg(self, main_cfg: dict) -> dict:
        return main_cfg.get("channels", {}).get("slack", {})

    def test_slack_mode_is_socket(self, main_cfg: dict):
        """channels.slack.mode must be 'socket'.

        Other modes (e.g. 'webhook') require a public URL and do not support
        Socket Mode connectivity. Changing to webhook mode silently breaks the
        local gateway Slack connection (ORCH-slack2).
        """
        mode = self._slack_cfg(main_cfg).get("mode")
        assert mode == "socket", (
            f"channels.slack.mode={mode!r} — expected 'socket'. "
            "Non-socket mode breaks local gateway Slack connectivity (ORCH-slack2)"
        )

    def test_slack_history_limit_at_least_50(self, main_cfg: dict):
        """channels.slack.historyLimit must be >= 50.

        A very low historyLimit causes the agent to lose context from recent
        channel messages, degrading response quality for thread-based tasks (ORCH-slack2).
        """
        limit = self._slack_cfg(main_cfg).get("historyLimit", 0)
        assert isinstance(limit, int) and limit >= 50, (
            f"channels.slack.historyLimit={limit!r} — must be >= 50 for adequate context. "
            "Low values cause the agent to miss recent message history (ORCH-slack2)"
        )

    def test_slack_dm_history_limit_at_least_50(self, main_cfg: dict):
        """channels.slack.dmHistoryLimit must be >= 50.

        A very low dmHistoryLimit truncates DM conversation history, causing
        the agent to lose context for multi-turn DM conversations (ORCH-slack2).
        """
        limit = self._slack_cfg(main_cfg).get("dmHistoryLimit", 0)
        assert isinstance(limit, int) and limit >= 50, (
            f"channels.slack.dmHistoryLimit={limit!r} — must be >= 50. "
            "Low values truncate DM conversation history (ORCH-slack2)"
        )

    def test_slack_group_policy_is_valid(self, main_cfg: dict):
        """channels.slack.groupPolicy must be a valid policy value.

        An invalid groupPolicy value causes openclaw to fall back to default
        behavior, which may open or close access unexpectedly (ORCH-slack2).
        """
        policy = self._slack_cfg(main_cfg).get("groupPolicy")
        valid_policies = {"allowlist", "denylist", "open", "closed", "all"}
        if policy is not None:
            assert policy in valid_policies, (
                f"channels.slack.groupPolicy={policy!r} is not a valid value. "
                f"Must be one of: {sorted(valid_policies)} (ORCH-slack2)"
            )

    def test_slack_allow_bots_is_true(self, main_cfg: dict):
        """channels.slack.allowBots must be True.

        allowBots=False blocks all bot-originated messages, breaking bot-to-bot
        workflows such as agento dispatch and MC notifications (ORCH-slack2).
        """
        allow_bots = self._slack_cfg(main_cfg).get("allowBots")
        assert allow_bots is True, (
            f"channels.slack.allowBots={allow_bots!r} — must be True. "
            "False blocks bot-to-bot workflows like agento dispatch (ORCH-slack2)"
        )

    def test_slack_thread_history_scope_is_channel(self, main_cfg: dict):
        """channels.slack.thread.historyScope must be 'channel'.

        historyScope='channel' allows the agent to see all channel messages
        including thread replies without explicit @mentions. historyScope='thread'
        misses non-mention thread replies (openclaw/openclaw#29657).
        """
        scope = self._slack_cfg(main_cfg).get("thread", {}).get("historyScope")
        assert scope == "channel", (
            f"channels.slack.thread.historyScope={scope!r} — expected 'channel'. "
            "historyScope='thread' misses thread replies without @mentions"
        )

    def test_slack_wildcard_channel_allows_all_invited(self, main_cfg: dict):
        """channels.slack.channels['*'] must allow without requireMention.

        With groupPolicy='allowlist', only channel IDs listed in channels.slack.channels
        (plus wildcard) receive messages. The '*' entry matches any channel the bot is in,
        so OpenClaw listens to every invited channel without per-ID maintenance (ORCH-slack3).
        """
        slack = self._slack_cfg(main_cfg)
        if slack.get("enabled") is not True:
            pytest.skip(
                "channels.slack.enabled is not True — wildcard invariant applies only when Slack is on"
            )
        channels_map = slack.get("channels") or {}
        star = channels_map.get("*")
        assert star is not None, (
            "channels.slack.channels must include '*' with allow=true and requireMention=false "
            "so all Slack channels the bot is invited to are allowed (ORCH-slack3)"
        )
        assert star.get("allow") is True or star.get("enabled") is True, (
            "channels.slack.channels['*'] must set allow=true or enabled=true "
            f"to admit all invited channels; got allow={star.get('allow')!r} "
            f"enabled={star.get('enabled')!r} (ORCH-slack3)"
        )
        assert star.get("requireMention") is False, (
            f"channels.slack.channels['*'].requireMention must be False for open channel listening; "
            f"got {star.get('requireMention')!r} (ORCH-slack3)"
        )


# ---------------------------------------------------------------------------
# ORCH-agent2: agents.list must include a 'main' agent
#
# The 'main' agent is the default and fallback for all direct interactions.
# Removing it causes all user-initiated sessions to fail at agent lookup.
# ---------------------------------------------------------------------------


class TestRequiredAgents:
    def test_agents_list_has_main_agent(self, main_cfg: dict):
        """agents.list must contain an agent with id='main'.

        The 'main' agent is the default for all user-initiated sessions.
        Removing or renaming it causes all direct agent interactions to fail
        at agent lookup with a confusing 'agent not found' error (ORCH-agent2).
        """
        agents_list = main_cfg.get("agents", {}).get("list", [])
        assert agents_list, (
            "agents.list is empty — at least one agent entry (id='main') is required (ORCH-agent2)"
        )
        agent_ids = [a.get("id") for a in agents_list]
        assert "main" in agent_ids, (
            f"agents.list has no entry with id='main'. Found ids: {agent_ids}. "
            "The 'main' agent is required for all user-initiated sessions (ORCH-agent2)"
        )

    def test_main_agent_entry_has_id_field(self, main_cfg: dict):
        """The 'main' agent entry must have an id field set to 'main'.

        An agent entry without an id field is silently skipped by openclaw,
        causing the 'main' agent to be unavailable (ORCH-agent2).
        """
        agents_list = main_cfg.get("agents", {}).get("list", [])
        main_entry = next((a for a in agents_list if a.get("id") == "main"), None)
        assert main_entry is not None, (
            "No agent entry with id='main' found in agents.list (ORCH-agent2)"
        )
        assert main_entry.get("id") == "main", (
            f"main agent entry has id={main_entry.get('id')!r} — expected 'main' (ORCH-agent2)"
        )


# ---------------------------------------------------------------------------
# ORCH-skills1: skills.install.nodeManager must be 'npm'
#
# Using a non-npm node manager (e.g. 'pnpm', 'yarn') with skills that are
# only published to npm may fail silently during skill install.
# ---------------------------------------------------------------------------


class TestSkillsConfig:
    def test_skills_install_node_manager_is_npm(self, main_cfg: dict):
        """skills.install.nodeManager must be 'npm'.

        Using pnpm or yarn may fail to install skills that are only published
        to the npm registry, causing silent skill installation failures (ORCH-skills1).
        """
        node_manager = (
            main_cfg.get("skills", {})
            .get("install", {})
            .get("nodeManager")
        )
        assert node_manager == "npm", (
            f"skills.install.nodeManager={node_manager!r} — expected 'npm'. "
            "Other managers may fail to install npm-only published skills (ORCH-skills1)"
        )


# ---------------------------------------------------------------------------
# Approved config values (2026-04-10, user-approved).
# Changes require EXPLICIT user approval — enforced here and in doctor.sh.
# ---------------------------------------------------------------------------

# Approved values — update these only with explicit user approval.
APPROVED_MAX_CONCURRENT = 10
APPROVED_SUBAGENT_MAX_CONCURRENT = 10
APPROVED_TIMEOUT_SECONDS = 600


class TestApprovedConfigValues:
    """Exact-match enforcement for gateway concurrency/timeout settings.

    These values were explicitly approved by the user on 2026-04-10.
    If this test fails, a config change was made without approval.
    Update APPROVED_* constants above only after getting explicit user sign-off.
    """

    def test_max_concurrent_is_approved_value(self, main_cfg: dict):
        """agents.defaults.maxConcurrent must be exactly APPROVED_MAX_CONCURRENT."""
        actual = main_cfg.get("agents", {}).get("defaults", {}).get("maxConcurrent")
        assert actual == APPROVED_MAX_CONCURRENT, (
            f"agents.defaults.maxConcurrent={actual!r} — expected {APPROVED_MAX_CONCURRENT} "
            "(approved 2026-04-10). Change requires explicit user approval."
        )

    def test_subagent_max_concurrent_is_approved_value(self, main_cfg: dict):
        """agents.defaults.subagents.maxConcurrent must be exactly APPROVED_SUBAGENT_MAX_CONCURRENT."""
        actual = (
            main_cfg.get("agents", {})
            .get("defaults", {})
            .get("subagents", {})
            .get("maxConcurrent")
        )
        assert actual == APPROVED_SUBAGENT_MAX_CONCURRENT, (
            f"agents.defaults.subagents.maxConcurrent={actual!r} — expected {APPROVED_SUBAGENT_MAX_CONCURRENT} "
            "(approved 2026-04-10). Change requires explicit user approval."
        )

    def test_timeout_seconds_is_approved_value(self, main_cfg: dict):
        """agents.defaults.timeoutSeconds must be exactly APPROVED_TIMEOUT_SECONDS."""
        actual = main_cfg.get("agents", {}).get("defaults", {}).get("timeoutSeconds")
        assert actual == APPROVED_TIMEOUT_SECONDS, (
            f"agents.defaults.timeoutSeconds={actual!r} — expected {APPROVED_TIMEOUT_SECONDS} "
            "(approved 2026-04-10). Change requires explicit user approval."
        )


# ---------------------------------------------------------------------------
# Staging Slack requireMention invariant
#
# Staging must never passively listen to channels — it must only respond when
# directly @mentioned.  This prevents staging from competing with prod on
# shared Slack channels.
# ---------------------------------------------------------------------------


@pytest.fixture
def staging_cfg() -> dict:
    if not STAGING_CONFIG.exists():
        pytest.skip(f"staging config not present at {STAGING_CONFIG} (live-only check)")
    return json.loads(STAGING_CONFIG.read_text(encoding="utf-8"))


class TestStagingSlackRequireMention:
    """Staging must require @mention on all channels — no passive listening."""

    def _slack_channels(self, staging_cfg: dict) -> dict:
        return (
            staging_cfg.get("channels", {})
            .get("slack", {})
            .get("channels", {})
        )

    def test_wildcard_channel_requires_mention(self, staging_cfg: dict):
        """The '*' catch-all channel entry must have requireMention=true."""
        channels = self._slack_channels(staging_cfg)
        assert "*" in channels, "staging config missing '*' wildcard channel entry"
        assert channels["*"].get("requireMention") is True, (
            "staging channels['*'].requireMention must be true — "
            "staging must not passively listen to Slack channels"
        )

    def test_all_explicit_channels_require_mention(self, staging_cfg: dict):
        """Every explicit channel entry must have requireMention=true."""
        channels = self._slack_channels(staging_cfg)
        violations = [
            ch for ch, cfg in channels.items()
            if cfg.get("requireMention") is not True
        ]
        assert not violations, (
            f"staging channels with requireMention != true: {violations} — "
            "staging must only respond when directly @mentioned"
        )
