#!/usr/bin/env python3
"""Sync Slack conversation history into OpenClaw memory markdown files.

Workflow:
1) Fetch channel history (and thread replies) via Slack Web API.
2) Redact obvious secrets/PII.
3) Write staged markdown artifacts.
4) Optionally promote staged files into ~/.openclaw/memory/slack-history.

This script is intended for periodic launchd use and one-off backfills.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request

DEFAULT_CONFIG = Path("~/.openclaw/openclaw.json").expanduser()
DEFAULT_STAGE_ROOT = Path("/tmp/openclaw-slack-memory-staging")
DEFAULT_PROMOTE_DIR = Path("~/.openclaw/memory/slack-history").expanduser()
DEFAULT_STATE_FILE = Path("~/.openclaw/memory/slack-sync-state.json").expanduser()

TOKEN_PATTERNS = [
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^\s'\"]+"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    # Only redact long mixed-case/digit-like secrets; avoid nuking normal long context strings.
    re.compile(r"\b(?=[A-Za-z0-9_-]{40,}\b)(?=.*[a-z])(?=.*[A-Z])(?=.*\d)[A-Za-z0-9_-]+\b"),
]
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
ENV_SECRET_NAME_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)\b")


@dataclass
class SlackClient:
    token: str
    pause_seconds: float = 0.25

    def api(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        encoded_params: dict[str, Any] = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                encoded_params[key] = "true" if value else "false"
            else:
                encoded_params[key] = value
        query = parse.urlencode(encoded_params)
        url = f"https://slack.com/api/{method}?{query}"
        req = request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        max_attempts = 5
        body: str | None = None
        for attempt in range(max_attempts):
            try:
                with request.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                break
            except error.HTTPError as exc:
                if exc.code != 429 or attempt == max_attempts - 1:
                    raise
                retry_after = (exc.headers or {}).get("Retry-After")
                try:
                    wait_seconds = max(0.0, float(retry_after)) if retry_after else 0.0
                except (TypeError, ValueError):
                    wait_seconds = 0.0
                if wait_seconds <= 0:
                    wait_seconds = float(min(2 ** attempt, 30))
                time.sleep(wait_seconds)
        if body is None:
            raise RuntimeError(f"Slack API {method} returned no response body")
        data = json.loads(body)
        if not data.get("ok"):
            raise RuntimeError(f"Slack API {method} failed: {data.get('error', 'unknown_error')}")
        if self.pause_seconds > 0:
            time.sleep(self.pause_seconds)
        return data


def load_config_channel_ids(config_path: Path) -> list[str]:
    if not config_path.exists():
        return []
    data = json.loads(config_path.read_text(encoding="utf-8"))
    channels: dict[str, Any] | None = None
    for path in (("channels", "slack", "channels"), ("messaging", "slack", "channels")):
        current: Any = data
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if isinstance(current, dict):
            channels = current
            break
    if not isinstance(channels, dict):
        return []
    return [cid for cid in channels.keys() if isinstance(cid, str) and cid and cid != "*"]


def parse_channels(channels_arg: str | None, config_path: Path) -> list[str]:
    if channels_arg:
        return [c.strip() for c in channels_arg.split(",") if c.strip()]
    return load_config_channel_ids(config_path)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"channels": {}, "updated_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("channels", {})
            return data
    except json.JSONDecodeError:
        pass
    return {"channels": {}, "updated_at": None}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def redact_text(text: str) -> str:
    out = text
    for pattern in TOKEN_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    out = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", out)
    out = ENV_SECRET_NAME_PATTERN.sub("[REDACTED_KEYNAME]", out)
    return out


def to_iso(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception:
        return "1970-01-01T00:00:00+00:00"


def collect_history(
    client: SlackClient,
    channel: str,
    oldest: str | None,
    latest: str | None,
    max_pages: int,
    max_thread_roots: int,
) -> tuple[list[dict[str, Any]], str | None]:
    messages: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    newest_ts: str | None = None

    while pages < max_pages:
        params: dict[str, Any] = {
            "channel": channel,
            "limit": 200,
            "cursor": cursor,
            "oldest": oldest,
            "latest": latest,
            "inclusive": False,
        }
        data = client.api("conversations.history", params)
        batch = data.get("messages", [])
        if isinstance(batch, list):
            messages.extend(batch)
            for msg in batch:
                ts = str(msg.get("ts", ""))
                if ts and (newest_ts is None or float(ts) > float(newest_ts)):
                    newest_ts = ts
        cursor = data.get("response_metadata", {}).get("next_cursor")
        pages += 1
        if not cursor:
            break

    thread_roots = [m for m in messages if str(m.get("thread_ts", "")) == str(m.get("ts", "")) and int(m.get("reply_count", 0) or 0) > 0]
    for root in thread_roots[:max_thread_roots]:
        thread_ts = str(root.get("thread_ts"))
        if not thread_ts:
            continue
        replies_cursor: str | None = None
        reply_pages = 0
        while reply_pages < max_pages:
            replies_data = client.api(
                "conversations.replies",
                {
                    "channel": channel,
                    "ts": thread_ts,
                    "limit": 200,
                    "oldest": oldest,
                    "latest": latest,
                    "inclusive": False,
                    "cursor": replies_cursor,
                },
            )
            replies = replies_data.get("messages", [])
            for rep in replies:
                rep["_thread_of"] = thread_ts
                ts = str(rep.get("ts", ""))
                if ts and (newest_ts is None or float(ts) > float(newest_ts)):
                    newest_ts = ts
            messages.extend(replies)
            replies_cursor = replies_data.get("response_metadata", {}).get("next_cursor")
            reply_pages += 1
            if not replies_cursor:
                break

    dedup: dict[str, dict[str, Any]] = {}
    for msg in messages:
        ts = str(msg.get("ts", ""))
        if ts:
            dedup[ts] = msg
    deduped = [dedup[k] for k in sorted(dedup.keys(), key=float)]
    return deduped, newest_ts


def render_markdown(channel: str, messages: list[dict[str, Any]], fetched_at: str) -> str:
    lines = [
        f"# Slack Memory Sync: {channel}",
        "",
        f"- fetched_at_utc: {fetched_at}",
        f"- message_count: {len(messages)}",
        "",
        "## Messages",
        "",
    ]
    for msg in messages:
        ts = str(msg.get("ts", ""))
        user = str(msg.get("user", msg.get("bot_id", "unknown")))
        subtype = str(msg.get("subtype", ""))
        thread_of = str(msg.get("_thread_of", ""))
        text = redact_text(str(msg.get("text", ""))).strip()
        if not text:
            text = "[NO_TEXT]"

        lines.append(f"### {to_iso(ts)} | ts={ts}")
        lines.append(f"- user: {user}")
        if subtype:
            lines.append(f"- subtype: {subtype}")
        if thread_of and thread_of != ts:
            lines.append(f"- thread_of: {thread_of}")
        lines.append("")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def promote_files(stage_files: list[Path], dest_dir: Path) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for src in stage_files:
        dst = dest_dir / src.name
        shutil.copy2(src, dst)
        copied.append(dst)
    return copied


def resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    for key in ("OPENCLAW_SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_USER_TOKEN"):
        value = os.environ.get(key)
        if value:
            return value
    raise RuntimeError("Missing Slack token. Set OPENCLAW_SLACK_BOT_TOKEN, SLACK_BOT_TOKEN, SLACK_USER_TOKEN, or --token")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Slack history into OpenClaw memory markdown files")
    parser.add_argument("--channels", help="Comma-separated channel IDs; defaults to channels in config")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to openclaw.json")
    parser.add_argument("--token", help="Slack token (otherwise read from env)")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help="Incremental sync state file")
    parser.add_argument("--stage-dir", default=str(DEFAULT_STAGE_ROOT), help="Staging output directory")
    parser.add_argument("--promote-dir", default=str(DEFAULT_PROMOTE_DIR), help="Promotion directory under memory")
    parser.add_argument("--promote", action="store_true", help="Copy staged files into --promote-dir")
    parser.add_argument("--full-history", action="store_true", help="Ignore incremental state and backfill full accessible history")
    parser.add_argument("--max-pages", type=int, default=200, help="Max history pages per channel")
    parser.add_argument("--max-thread-roots", type=int, default=200, help="Max threaded roots to expand")
    parser.add_argument("--latest", help="Optional latest ts bound")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print summary without writing files")
    args = parser.parse_args()

    token = resolve_token(args.token)
    config_path = Path(args.config).expanduser()
    state_path = Path(args.state_file).expanduser()
    stage_dir = Path(args.stage_dir).expanduser() / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    promote_dir = Path(args.promote_dir).expanduser()

    channels = parse_channels(args.channels, config_path)
    if not channels:
        raise RuntimeError("No channels resolved. Provide --channels or configure channels.slack.channels (or messaging.slack.channels)")

    state = load_state(state_path)
    client = SlackClient(token=token)
    fetched_at = datetime.now(timezone.utc).isoformat()

    stage_files: list[Path] = []
    summary: dict[str, Any] = {"channels": {}, "fetched_at": fetched_at, "stage_dir": str(stage_dir)}

    for channel in channels:
        oldest = None if args.full_history else state.get("channels", {}).get(channel, {}).get("latest_ts")
        messages, newest_ts = collect_history(
            client=client,
            channel=channel,
            oldest=oldest,
            latest=args.latest,
            max_pages=args.max_pages,
            max_thread_roots=args.max_thread_roots,
        )

        summary["channels"][channel] = {
            "fetched": len(messages),
            "oldest_bound": oldest,
            "newest_ts": newest_ts,
        }

        if args.dry_run:
            continue

        stage_dir.mkdir(parents=True, exist_ok=True)
        filename = f"slack-history-{channel}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
        out_file = stage_dir / filename
        out_file.write_text(render_markdown(channel, messages, fetched_at), encoding="utf-8")
        stage_files.append(out_file)

        if newest_ts:
            state.setdefault("channels", {}).setdefault(channel, {})["latest_ts"] = newest_ts
            state["channels"][channel]["updated_at"] = fetched_at

    if not args.dry_run:
        manifest = stage_dir / "manifest.json"
        manifest.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        promoted: list[Path] = []
        if args.promote:
            promoted = promote_files(stage_files + [manifest], promote_dir)
            summary["promoted_files"] = [str(p) for p in promoted]
            save_state(state_path, state)
            summary["state_saved"] = True
        else:
            summary["state_saved"] = False

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
