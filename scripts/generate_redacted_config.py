#!/usr/bin/env python3
"""Generate openclaw.json.redacted from the live openclaw.json.

Replaces known secret values with ${ENV_VAR} placeholders so the file is safe
to commit. The output is used by test_openclaw_configs.py::TestRedactedConfigRoundtrip
to verify the committed snapshot stays in sync with the live config.

Usage:
    python3 scripts/generate_redacted_config.py
    python3 scripts/generate_redacted_config.py /path/to/live.json /path/to/redacted.json
    git add openclaw.json.redacted
    git commit -m "chore: sync openclaw.json.redacted"
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LIVE_CONFIG = REPO_ROOT / "openclaw.json"
DEFAULT_REDACTED_CONFIG = REPO_ROOT / "openclaw.json.redacted"

# (json_path, env_var_placeholder)
REDACTIONS: list[tuple[list[str], str]] = [
    (["env", "XAI_API_KEY"],                                     "${XAI_API_KEY}"),
    (["env", "SLACK_BOT_TOKEN"],                        "${SLACK_BOT_TOKEN}"),
    (["env", "OPENCLAW_SLACK_APP_TOKEN"],                        "${OPENCLAW_SLACK_APP_TOKEN}"),
    (["env", "OPENCLAW_HOOKS_TOKEN"],                            "${OPENCLAW_HOOKS_TOKEN}"),
    (["hooks", "token"],                                          "${OPENCLAW_HOOKS_TOKEN}"),
    (["channels", "slack", "botToken"],                           "${SLACK_BOT_TOKEN}"),
    (["channels", "slack", "appToken"],                           "${OPENCLAW_SLACK_APP_TOKEN}"),
    (["channels", "discord", "token"],                            "${DISCORD_BOT_TOKEN}"),
    (["gateway", "auth", "token"],                                "${OPENCLAW_GATEWAY_TOKEN}"),
    (["gateway", "remote", "token"],                              "${OPENCLAW_GATEWAY_REMOTE_TOKEN}"),
    (["plugins", "entries", "openclaw-mem0", "config", "oss", "embedder", "config", "apiKey"], "${OPENAI_API_KEY}"),
    (["plugins", "entries", "openclaw-mem0", "config", "oss", "llm", "config", "api_key"],    "${GROQ_API_KEY}"),
    (["plugins", "entries", "openclaw-mem0", "config", "oss", "llm", "config", "apiKey"],     "${GROQ_API_KEY}"),
]

# Timestamp fields that change on every doctor run — replaced with stable placeholder.
VOLATILE_FIELDS: list[tuple[list[str], str]] = [
    (["meta", "lastTouchedAt"],  "${OPENCLAW_LAST_TOUCHED_AT}"),
    (["wizard", "lastRunAt"],    "${OPENCLAW_WIZARD_LAST_RUN_AT}"),
]


def redact(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    for path, placeholder in REDACTIONS + VOLATILE_FIELDS:
        node = out
        try:
            for p in path[:-1]:
                node = node[p]
            if path[-1] in node:
                node[path[-1]] = placeholder
                print(f"  redacted: {'.'.join(str(p) for p in path)}")
        except (KeyError, TypeError) as exc:
            print(f"  skip (missing): {'.'.join(str(p) for p in path)} — {exc}")
    return out


def main() -> None:
    live_config = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT_LIVE_CONFIG
    redacted_config = Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 else DEFAULT_REDACTED_CONFIG

    if not live_config.exists():
        raise SystemExit(f"Live config not found: {live_config}")

    cfg = json.loads(live_config.read_text())
    result = redact(cfg)

    # Serialize, then replace machine-specific home paths with portable ${HOME}
    output = json.dumps(result, indent=2) + "\n"
    home = os.path.expanduser("~")
    if home and home != "~":
        output = output.replace(home, "${HOME}")

    redacted_config.parent.mkdir(parents=True, exist_ok=True)
    redacted_config.write_text(output)
    print(f"\nWrote {redacted_config} ({redacted_config.stat().st_size} bytes)")
    print("Next: git add openclaw.json.redacted && git commit -m 'chore: sync openclaw.json.redacted'")


if __name__ == "__main__":
    main()
