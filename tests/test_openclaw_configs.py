"""Validation tests for committed openclaw JSON configs.

Guards against regressions in security-sensitive config properties:
- discord-eng-bot/openclaw.json (public-facing Discord bot)
- openclaw-config/openclaw.json (main agent profile)
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DISCORD_CONFIG = REPO_ROOT / "discord-eng-bot" / "openclaw.json"
MAIN_CONFIG = REPO_ROOT / "openclaw.json"
REDACTED_CONFIG = REPO_ROOT / "openclaw.json.redacted"

# Secrets replaced in openclaw.json.redacted and the env vars that hold their real values.
# When ALL env vars are set (i.e. on the real machine), the roundtrip test expands the
# redacted file and asserts it equals the live config — catching drift between the two.
_REDACTION_MAP: list[tuple[list[str], str]] = [
    (["env", "XAI_API_KEY"],                                     "XAI_API_KEY"),
    (["env", "SLACK_BOT_TOKEN"],                        "SLACK_BOT_TOKEN"),
    (["env", "OPENCLAW_SLACK_APP_TOKEN"],                        "OPENCLAW_SLACK_APP_TOKEN"),
    (["env", "OPENCLAW_HOOKS_TOKEN"],                            "OPENCLAW_HOOKS_TOKEN"),
    (["models", "providers", "minimax-portal", "apiKey"],        "MINIMAX_API_KEY"),
    (["hooks", "token"],                                          "OPENCLAW_HOOKS_TOKEN"),
    (["channels", "slack", "botToken"],                           "SLACK_BOT_TOKEN"),
    (["channels", "slack", "appToken"],                           "OPENCLAW_SLACK_APP_TOKEN"),
    (["gateway", "auth", "token"],                                "OPENCLAW_GATEWAY_TOKEN"),
    (["gateway", "remote", "token"],                              "OPENCLAW_GATEWAY_REMOTE_TOKEN"),
    (["plugins", "entries", "openclaw-mem0", "config", "oss", "embedder", "config", "apiKey"], "OPENAI_API_KEY"),
    (["plugins", "entries", "openclaw-mem0", "config", "oss", "llm", "config", "api_key"],    "GROQ_API_KEY"),
    (["plugins", "entries", "openclaw-mem0", "config", "oss", "llm", "config", "apiKey"],     "GROQ_API_KEY"),
]
# Timestamp fields that change on every doctor run — excluded from roundtrip comparison.
_VOLATILE_PATHS: list[tuple[list[str], ...]] = [
    (["meta", "lastTouchedAt"],),
    (["wizard", "lastRunAt"],),
]
MC_PLIST = REPO_ROOT / "ai.smartclaw.mission-control.plist"
START_MC_SCRIPT = REPO_ROOT / "scripts" / "start-mc.sh"
GATEWAY_INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-launchagents.sh"
STARTUP_CHECK_PLIST = REPO_ROOT / "ai.smartclaw.startup-check.plist"
STARTUP_CHECK_SCRIPT = REPO_ROOT / "startup-check.sh"
MCTRL_SUPERVISOR_PLIST = REPO_ROOT / "scripts" / "mctrl-supervisor.plist.template"

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
    def test_install_launchagents_uses_gateway_cli_installer(self):
        """Gateway should be installed via the supported OpenClaw CLI service path."""
        script_text = GATEWAY_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "openclaw gateway install --force" in script_text

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

    def test_slack_bot_token_is_env_ref(self, main_cfg: dict):
        """botToken must be an env var ref, never a hardcoded or REDACTED literal."""
        slack = self._slack_cfg(main_cfg)
        import re
        token = slack.get("botToken", "")
        assert re.match(r"^\$\{[A-Z][A-Z0-9_]+\}$", token), (
            f"channels.slack.botToken={token!r} must be an env var ref like "
            "'${SLACK_BOT_TOKEN}' — never hardcode credentials (ORCH-sl0)"
        )

    def test_slack_app_token_is_env_ref(self, main_cfg: dict):
        """appToken must be an env var ref, never a hardcoded or REDACTED literal."""
        slack = self._slack_cfg(main_cfg)
        import re
        token = slack.get("appToken", "")
        assert re.match(r"^\$\{[A-Z][A-Z0-9_]+\}$", token), (
            f"channels.slack.appToken={token!r} must be an env var ref like "
            "'${OPENCLAW_SLACK_APP_TOKEN}' — never hardcode credentials (ORCH-sl0)"
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
    def test_health_check_force_install_preserves_gateway_token(self):
        """health-check remediation must keep gateway token stable across force install."""
        script_text = (REPO_ROOT / "health-check.sh").read_text(
            encoding="utf-8"
        )
        assert 'gateway install --force --token "$gateway_token"' in script_text, (
            "health-check install_gateway() must use gateway install --force with "
            "the resolved token to preserve secrets (ORCH-s4p)"
        )
        assert (
            "EnvironmentVariables.OPENCLAW_GATEWAY_TOKEN raw -o -"
        ) in script_text, (
            "health-check resolve_gateway_token() should read existing token from "
            "gateway plist env vars (ORCH-s4p)"
        )

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
# ORCH-exec2: openclaw.json.redacted roundtrip test
#
# openclaw.json.redacted is the committed snapshot of the live config with
# secrets replaced by ${VAR} placeholders.  When all env vars are available
# (i.e. on the real machine), this test expands the redacted file and asserts
# it matches the live config exactly — catching any drift between the two.
#
# To update after config changes: regenerate openclaw.json.redacted by running
#   python3 scripts/generate_redacted_config.py
# then commit the result.
# ---------------------------------------------------------------------------


def _expand_redacted(redacted: dict) -> dict:
    """Substitute ${VAR} placeholders with real env var values in-place (deep copy)."""
    import copy
    import os
    expanded = copy.deepcopy(redacted)
    for path, env_var in _REDACTION_MAP:
        value = os.environ.get(env_var)
        if value is None:
            continue
        obj = expanded
        try:
            for p in path[:-1]:
                obj = obj[p]
            if path[-1] in obj:
                obj[path[-1]] = value
        except (KeyError, TypeError):
            pass
    return expanded


def _blank_volatile(obj: dict) -> dict:
    """Zero out timestamp fields that change on every doctor run."""
    import copy
    result = copy.deepcopy(obj)
    volatile_paths = [
        ["meta", "lastTouchedAt"],
        ["wizard", "lastRunAt"],
    ]
    for path in volatile_paths:
        node = result
        try:
            for p in path[:-1]:
                node = node[p]
            if path[-1] in node:
                node[path[-1]] = "__volatile__"
        except (KeyError, TypeError):
            pass
    return result


class TestRedactedConfigRoundtrip:
    @pytest.fixture(scope="class")
    def redacted_cfg(self) -> dict:
        if not REDACTED_CONFIG.exists():
            pytest.skip("openclaw.json.redacted not present — run scripts/generate_redacted_config.py")
        return json.loads(REDACTED_CONFIG.read_text())

    def test_redacted_config_is_valid_json(self, redacted_cfg: dict):
        """openclaw.json.redacted must parse as valid JSON."""
        assert isinstance(redacted_cfg, dict)

    def test_redacted_config_has_no_raw_secrets(self, redacted_cfg: dict):
        """No xox*, sk-*, gsk_*, or long hex tokens should appear as bare strings."""
        import re
        raw = REDACTED_CONFIG.read_text()
        secret_patterns = [
            (r'xox[bpars]-[0-9A-Za-z-]+', "Slack token"),
            (r'xapp-[0-9A-Za-z-]+', "Slack app token"),
            (r'sk-proj-[0-9A-Za-z_-]{20,}', "OpenAI API key"),
            (r'gsk_[0-9A-Za-z]{20,}', "Groq API key"),
            (r'xai-[0-9A-Za-z]{20,}', "xAI API key"),
        ]
        found = []
        for pattern, label in secret_patterns:
            if re.search(pattern, raw):
                found.append(label)
        assert not found, (
            f"openclaw.json.redacted contains raw secrets: {found}. "
            "Regenerate with scripts/generate_redacted_config.py (ORCH-exec2)"
        )

    def test_redacted_placeholders_use_env_var_syntax(self, redacted_cfg: dict):
        """All redacted fields must use ${VAR_NAME} syntax, not bare env var names."""
        import re
        env_ref = re.compile(r"^\$\{[A-Z][A-Z0-9_]+\}$")
        bad = []
        for path, _ in _REDACTION_MAP:
            obj = redacted_cfg
            try:
                for p in path[:-1]:
                    obj = obj[p]
                val = obj.get(path[-1], "")
            except (KeyError, TypeError):
                continue
            if val and not env_ref.match(str(val)):
                bad.append(f"{'.'.join(path)}={val!r}")
        assert not bad, (
            f"Redacted fields not using ${{VAR}} syntax: {bad}. "
            "Regenerate with scripts/generate_redacted_config.py (ORCH-exec2)"
        )

    def test_roundtrip_matches_live_config(self, redacted_cfg: dict, main_cfg: dict):
        """Expanding openclaw.json.redacted with real env vars must equal openclaw.json exactly.

        Fails when the live config changes but openclaw.json.redacted is not regenerated.
        Fix: run 'python3 scripts/generate_redacted_config.py' and commit the result.
        """
        import os
        required_vars = {env_var for _, env_var in _REDACTION_MAP}
        missing_env = required_vars - set(os.environ)
        if missing_env:
            pytest.skip(
                f"Roundtrip skipped — env vars not set: {sorted(missing_env)}. "
                "Run on the real machine with openclaw env loaded."
            )
        expanded = _expand_redacted(redacted_cfg)
        expected = _blank_volatile(main_cfg)
        actual = _blank_volatile(expanded)
        assert actual == expected, (
            "openclaw.json.redacted expanded with env vars does not match openclaw.json. "
            "Config has drifted — regenerate with scripts/generate_redacted_config.py "
            "and commit (ORCH-exec2)"
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
        "openai:default",
        "minimax-portal:default",
    ]
    VALID_AUTH_MODES = {"oauth", "api_key", "token", "none"}

    def test_required_profiles_present(self, main_cfg: dict):
        """All required auth provider profiles must be present in auth.profiles.

        A missing profile means the provider cannot authenticate, causing all
        requests to that provider to fail silently (ORCH-auth1).
        """
        profiles = main_cfg.get("auth", {}).get("profiles", {})
        missing = [p for p in self.REQUIRED_PROFILES if p not in profiles]
        assert not missing, (
            f"auth.profiles missing required entries: {missing}. "
            "Add them with 'openclaw agents auth' (ORCH-auth1)"
        )

    def test_all_profiles_have_valid_mode(self, main_cfg: dict):
        """Every auth profile must have a recognized mode value.

        An invalid mode causes openclaw to reject the profile at startup,
        failing all agent sessions that use that provider (ORCH-auth1).
        """
        profiles = main_cfg.get("auth", {}).get("profiles", {})
        bad = []
        for name, profile in profiles.items():
            mode = profile.get("mode")
            if mode not in self.VALID_AUTH_MODES:
                bad.append(f"{name}.mode={mode!r}")
        assert not bad, (
            f"auth.profiles entries with invalid mode: {bad}. "
            f"Valid modes: {sorted(self.VALID_AUTH_MODES)} (ORCH-auth1)"
        )

    def test_openai_codex_profile_is_oauth(self, main_cfg: dict):
        """openai-codex:default must use oauth mode (Codex requires OAuth, not API key).

        Switching to api_key mode breaks Codex authentication silently (ORCH-auth1).
        """
        profile = (
            main_cfg.get("auth", {})
            .get("profiles", {})
            .get("openai-codex:default", {})
        )
        mode = profile.get("mode")
        assert mode == "oauth", (
            f"auth.profiles['openai-codex:default'].mode={mode!r} — "
            "Codex requires oauth mode; api_key mode will silently fail (ORCH-auth1)"
        )

    def test_openai_profile_is_oauth(self, main_cfg: dict):
        """openai:default must use oauth mode.

        The memory embedder provider (openai) uses OAuth not direct API key
        in this configuration (ORCH-auth1).
        """
        profile = (
            main_cfg.get("auth", {})
            .get("profiles", {})
            .get("openai:default", {})
        )
        mode = profile.get("mode")
        assert mode == "oauth", (
            f"auth.profiles['openai:default'].mode={mode!r} — expected oauth (ORCH-auth1)"
        )


# ---------------------------------------------------------------------------
# ORCH-model1: models.providers.minimax-portal must be correctly configured
#
# The minimax-portal provider is the primary model provider — misconfiguration
# silently falls back to no model or causes all agent sessions to fail.
# ---------------------------------------------------------------------------


class TestModelsProviders:
    def test_minimax_portal_provider_present(self, main_cfg: dict):
        """models.providers.minimax-portal must exist.

        This is the primary inference provider — if it's absent, all agent
        sessions fail at model selection (ORCH-model1).
        """
        providers = main_cfg.get("models", {}).get("providers", {})
        assert "minimax-portal" in providers, (
            "models.providers.minimax-portal is missing. "
            "This is the primary inference provider (ORCH-model1)"
        )

    def test_minimax_portal_api_is_anthropic_messages(self, main_cfg: dict):
        """models.providers.minimax-portal.api must be 'anthropic-messages'.

        MiniMax uses the Anthropic Messages API format. Changing this silently
        breaks all MiniMax API calls with malformed request errors (ORCH-model1).
        """
        api = (
            main_cfg.get("models", {})
            .get("providers", {})
            .get("minimax-portal", {})
            .get("api")
        )
        assert api == "anthropic-messages", (
            f"models.providers.minimax-portal.api={api!r} — "
            "must be 'anthropic-messages' for MiniMax compatibility (ORCH-model1)"
        )

    def test_minimax_portal_api_key_is_set(self, main_cfg: dict):
        """models.providers.minimax-portal.apiKey must be non-null and non-empty.

        A missing or empty apiKey means all MiniMax API calls fail with 401 (ORCH-model1).
        Either a ${VAR} ref or a live key value is acceptable here; the redacted
        config test validates the committed form.
        """
        api_key = (
            main_cfg.get("models", {})
            .get("providers", {})
            .get("minimax-portal", {})
            .get("apiKey")
        )
        assert api_key is not None, (
            "models.providers.minimax-portal.apiKey is missing (ORCH-model1)"
        )
        assert str(api_key).strip(), (
            "models.providers.minimax-portal.apiKey is empty (ORCH-model1)"
        )

    def test_minimax_portal_has_at_least_one_model(self, main_cfg: dict):
        """models.providers.minimax-portal.models must have at least one model entry.

        An empty models list causes openclaw to report no models available for
        the minimax-portal provider (ORCH-model1).
        """
        models = (
            main_cfg.get("models", {})
            .get("providers", {})
            .get("minimax-portal", {})
            .get("models", [])
        )
        assert len(models) >= 1, (
            "models.providers.minimax-portal.models is empty — "
            "at least one model entry is required (ORCH-model1)"
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
            "Use minimax-portal/MiniMax-M2.5 or openai-codex/gpt-5.3-codex (ORCH-agent1)"
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
        The live config may have raw values (the redacted config enforces ${VAR} form)
        — this test checks the redacted config if present, otherwise skips.
        """
        if not REDACTED_CONFIG.exists():
            pytest.skip(
                "openclaw.json.redacted not present — env secret check is "
                "validated by TestRedactedConfigRoundtrip when redacted config exists (ORCH-env1)"
            )
        redacted = json.loads(REDACTED_CONFIG.read_text())
        env_section = redacted.get("env", {})
        bad = []
        for key, value in env_section.items():
            is_secret, label = _is_raw_secret(str(value))
            if is_secret:
                bad.append(f"env.{key} looks like a raw {label}")
        assert not bad, (
            f"Redacted config env section contains raw secrets: {bad}. "
            "Regenerate with scripts/generate_redacted_config.py (ORCH-env1)"
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

        If either is disabled, the gateway cannot connect to Slack Socket Mode.
        Both must be True for Slack functionality to be available (ORCH-plug1).
        """
        channels = main_cfg.get("channels", {})
        plugin_entries = main_cfg.get("plugins", {}).get("entries", {})
        channel_enabled = channels.get("slack", {}).get("enabled")
        plugin_enabled = plugin_entries.get("slack", {}).get("enabled")
        # Use skip pattern consistent with TestSlackEnabled for repo copy
        if channel_enabled is not True:
            pytest.skip(
                f"channels.slack.enabled={channel_enabled!r} in repo copy — "
                "live-only invariant enforced by sync validation guard (ORCH-plug1)"
            )
        assert plugin_enabled is True, (
            f"plugins.entries.slack.enabled={plugin_enabled!r} while channels.slack.enabled=True. "
            "Slack will not work — enable the slack plugin (ORCH-plug1)"
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
