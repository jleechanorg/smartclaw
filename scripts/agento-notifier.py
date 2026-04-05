#!/usr/bin/env python3
"""
agento-notifier: Minimal HTTP server that receives AO webhook events
and posts them to Slack #ai-slack-test as the openclaw bot.

Run: python3 scripts/agento-notifier.py
Port: 18800
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("PYTHONUNBUFFERED", "1")

SLACK_CHANNEL = os.environ.get("OPENCLAW_SLACK_CHANNEL", "C0AKALZ4CKW")  # #ai-slack-test
PORT = 18800
WEBHOOK_SECRET = os.environ.get("OPENCLAW_AO_NOTIFY_TOKEN", "")
AO_BIN = os.environ.get("AO_BIN") or os.path.expanduser("~/bin/ao")
COOLDOWN_SECONDS = 60
COOLDOWN_DIR = "/tmp"


def get_cooldown_path(project_id: str) -> str:
    safe_id = project_id.replace("/", "_").replace("..", "_").replace("\\", "_")
    return f"{COOLDOWN_DIR}/ao-respawn-cooldown-{safe_id}"


def is_in_cooldown(project_id: str) -> bool:
    path = get_cooldown_path(project_id)
    if not os.path.exists(path):
        return False
    try:
        mtime = os.path.getmtime(path)
        return (time.time() - mtime) < COOLDOWN_SECONDS
    except OSError:
        return False


def set_cooldown(project_id: str) -> None:
    path = get_cooldown_path(project_id)
    try:
        with open(path, "w") as f:
            f.write(str(int(time.time())))
    except OSError as e:
        print(f"[agento-notifier] Warning: could not write cooldown file: {e}")


def ao_stop(project_id: str) -> None:
    """Stop AO session for project (non-blocking)."""
    try:
        subprocess.Popen([AO_BIN, "stop", project_id],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[agento-notifier] Triggered ao stop for {project_id}")
    except Exception as e:
        print(f"[agento-notifier] ao stop failed for {project_id}: {e}")


def ao_spawn(project_id: str, claim_pr: str | None = None) -> None:
    """Spawn AO session for project (non-blocking)."""
    if is_in_cooldown(project_id):
        print(f"[agento-notifier] Skipping spawn for {project_id} (in cooldown)")
        return
    try:
        args = [AO_BIN, "spawn", project_id]
        if claim_pr:
            args.extend(["--claim-pr", claim_pr])
        env = os.environ.copy()
        env["AO_CONFIG_PATH"] = os.path.expanduser("~/agent-orchestrator.yaml")
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=os.path.expanduser("~"), env=env)
        set_cooldown(project_id)
        print(f"[agento-notifier] Triggered ao spawn for {project_id}" +
              (f" with --claim-pr {claim_pr}" if claim_pr else ""))
    except Exception as e:
        print(f"[agento-notifier] ao spawn failed for {project_id}: {e}")


def post_to_slack(text: str) -> None:
    # Prefer bot token for service notifier; fall back to user token if not available
    token = (
        os.environ.get("OPENCLAW_SLACK_BOT_TOKEN")
        or os.environ.get("SLACK_BOT_TOKEN")
        or os.environ.get("SLACK_USER_TOKEN")
    )
    if not token:
        print(f"[agento-notifier] No Slack token in env, would have posted: {text}")
        return
    payload = json.dumps({"channel": SLACK_CHANNEL, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        if not body.get("ok"):
            print(f"[agento-notifier] Slack post failed: {body}")
        else:
            print(f"[agento-notifier] Posted to Slack: {text[:80]}")
    except Exception as exc:
        print(f"[agento-notifier] Slack request error: {exc}")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/ao-notify":
            self.send_response(404)
            self.end_headers()
            return

        # Optional webhook authentication
        if WEBHOOK_SECRET:
            auth_header = self.headers.get("Authorization", "")
            if auth_header != f"Bearer {WEBHOOK_SECRET}":
                self.send_response(401)
                self.end_headers()
                return

        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0:
                length = 0
        except ValueError:
            length = 0
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        if not isinstance(data, dict):
            self.send_response(400)
            self.end_headers()
            return

        self.send_response(200)
        self.end_headers()

        event = data.get("event")
        if not isinstance(event, dict):
            event = {}
        msg_type = data.get("type", "notification")

        if msg_type == "message":
            text = f":robot_face: *agento* | {data.get('message', '') or ''}"
        else:
            priority = event.get("priority") or "info"
            event_type = event.get("type") or "unknown"
            message = event.get("message") or ""
            session = event.get("sessionId") or ""
            project = event.get("projectId") or ""
            event_data = event.get("data")
            if isinstance(event_data, dict):
                pr_url = event_data.get("prUrl", "")
            else:
                pr_url = ""
            pr_part = f" | <{pr_url}|PR>" if pr_url else ""
            emoji = {"urgent": ":rotating_light:", "action": ":point_right:",
                     "warning": ":warning:", "info": ":information_source:"}.get(priority, ":bell:")
            text = f"{emoji} *agento* `{event_type}` [{project}/{session}]{pr_part}\n{message}"

        post_to_slack(text)

        # Recovery handlers - act on AO lifecycle events (non-blocking)
        event_type = event.get("type") or ""
        project_id = event.get("projectId") or ""
        event_data = event.get("data")
        if isinstance(event_data, dict):
            pr_number = event_data.get("prNumber") or event_data.get("pr")
        else:
            pr_number = None

        if event_type == "merge.completed" and project_id:
            ao_stop(project_id)
        elif event_type == "reaction.escalated" and project_id:
            # Only respawn after escalation (reaction exhausted retries), not on every stuck poll
            reaction_key = event_data.get("reactionKey") if isinstance(event_data, dict) else None
            if reaction_key == "agent-stuck":
                claim_pr = str(pr_number) if pr_number else None
                ao_spawn(project_id, claim_pr)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[agento-notifier] {fmt % args}")


if __name__ == "__main__":
    print(f"[agento-notifier] Listening on port {PORT}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
