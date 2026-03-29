"""Redaction pass for openclaw backup snapshots.

Called by scripts/backup-openclaw-full.sh after rsync mirrors ~/.openclaw.
Redacts secrets in text files in-place; removes high-risk binary key material.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


SENSITIVE_PATH_HINTS = [
    "/.ssh/",
    "/.aws/",
    "/.config/",
    "/.kube/",
    ".env",
    "id_rsa",
    "id_ed25519",
]

PATTERNS = [
    re.compile(r"(?im)^[\t ]*(?:export[\t ]+)?(?:[A-Za-z_][A-Za-z0-9_]*_?(?:KEY|KEYS?|TOKEN|SECRET|PASS|PASSWORD)|API[_-]?KEY|CLIENT_SECRET|CLIENTID|CLIENT_ID|CLIENT_SECRET)\s*[:=].+$"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key|bearer\s+token)\b[^\n]*"),
    re.compile(r"(?i)\"(?:botToken|appToken|token|apiKey|secret|password)\"\s*:\s*\"[^\"]+\""),
    re.compile(r"(?i)\b(sk-[A-Za-z0-9]{10,}|xox[baprs]-[0-9A-Za-z\-]{10,}|ghp_[A-Za-z0-9]{20,})\b"),
    re.compile(r"(?i)xai-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)https://hooks\.slack\.com/services/[A-Z0-9/]+"),
    re.compile(r"(?i)pypi-[A-Za-z0-9_\-]{60,}"),
    re.compile(r"(?i)https?://[^:\s]+:[^@\s]+@"),
]

HIGH_RISK_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der"}


def is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(4096)
    except Exception:
        return True


def path_is_sensitive(path: Path) -> bool:
    low = str(path).lower()
    if any(token in low for token in SENSITIVE_PATH_HINTS):
        return True
    if any(part.lower() in {"authorized_keys", "known_hosts", "config"} for part in path.parts):
        return True
    return False


def redact_snapshot(snapshot_dir: Path, src_dir_str: str, snapshot_ts: str) -> None:
    """Redact secrets in all files under snapshot_dir in-place.

    Symlinks are skipped — never followed — to prevent writing through
    symlinks to their targets outside the snapshot directory.
    """
    for p in snapshot_dir.rglob("*"):
        # Never follow symlinks: resolving through them could reach targets
        # outside the snapshot dir and corrupt the source.
        if p.is_symlink():
            continue
        if not p.is_file():
            continue

        # Remove high-risk binary key material entirely.
        if path_is_sensitive(p) and (is_binary(p) or p.suffix.lower() in HIGH_RISK_EXTENSIONS):
            p.unlink()
            continue

        # Skip binary files — no text redaction needed.
        if is_binary(p):
            continue

        # Redact secrets in text files in-place.
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            try:
                text = p.read_text(encoding="latin-1")
            except Exception:
                continue

        new = text
        for pattern in PATTERNS:
            new = pattern.sub("[REDACTED]", new)

        if new != text:
            p.write_text(new, encoding="utf-8")

    # Write audit manifest.
    (snapshot_dir / "REDACTION_MANIFEST.txt").write_text(
        f"Source: {src_dir_str}\nTimestamp: {snapshot_ts}\nStatus: rsync+redacted\n"
    )


def build_slack_payload(subject: str, body: str) -> str:
    """Build a valid JSON Slack webhook payload from subject + body text.

    Uses json.dumps to correctly escape newlines and special characters.
    """
    text = f"{subject}\n\n{body}"
    return json.dumps({"text": text})


if __name__ == "__main__":
    import os
    import sys

    snapshot_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(os.environ["SNAPSHOT_DIR"])
    src_dir_str = os.environ.get("SRC_DIR", os.path.expanduser("~/.openclaw"))
    snapshot_ts = os.environ.get("SNAPSHOT_TS", snapshot_dir.name)
    redact_snapshot(snapshot_dir, src_dir_str, snapshot_ts)
