#!/usr/bin/env python3
"""Auto fact capture — fires at agent_end to extract and store session facts.

Bypasses the broken Node.js autoCapture (better-sqlite3 Node v24 mismatch).
Reads the last N messages from a session JSONL, calls Groq to extract 1-3
atomic facts, writes them to Qdrant via mem0_shared_client.add_memory().

Usage (called by openclaw hook):
    python3 scripts/auto_fact_capture.py --session-file /path/to/session.jsonl

Usage (manual test):
    python3 scripts/auto_fact_capture.py --session-file ~/.openclaw/agents/main/sessions/abc.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


MAX_MESSAGES = 20  # last N messages to read
MIN_CHARS = 200    # skip sessions with fewer chars of content (not worth extracting)
MAX_CHARS = 8000   # truncate to avoid Groq TPM limits


def _read_last_messages(jsonl_path: Path, n: int = MAX_MESSAGES) -> list[dict]:
    """Read last N user/assistant messages from session JSONL."""
    messages = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    d = json.loads(line.strip())
                    if d.get("type") == "message":
                        msg = d.get("message", {})
                        role = msg.get("role", "")
                        if role not in ("user", "assistant"):
                            continue
                        # Extract text content only
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            text = " ".join(
                                c.get("text", "") for c in content
                                if isinstance(c, dict) and c.get("type") == "text"
                            )
                        elif isinstance(content, str):
                            text = content
                        else:
                            continue
                        text = text.strip()
                        if text and len(text) > 10:
                            messages.append({"role": role, "text": text})
                except (json.JSONDecodeError, KeyError):
                    continue
    except (OSError, IOError) as e:
        print(f"[auto_fact_capture] cannot read {jsonl_path}: {e}", file=sys.stderr)
        return []
    return messages[-n:]


def _build_conversation_text(messages: list[dict], max_chars: int = MAX_CHARS) -> str:
    lines = []
    for m in messages:
        prefix = "User" if m["role"] == "user" else "Assistant"
        # Strip <relevant-memories> blocks — they're injected context, not real content
        text = m["text"]
        if "<relevant-memories>" in text:
            start = text.find("<relevant-memories>")
            end = text.find("</relevant-memories>")
            if end != -1:
                text = text[:start] + text[end + len("</relevant-memories>"):]
        text = text.strip()
        if text:
            lines.append(f"{prefix}: {text[:500]}")
    result = "\n".join(lines)
    return result[:max_chars]


def _extract_facts_groq(conversation: str, groq_api_key: str, groq_model: str) -> list[str]:
    """Call Groq to extract 1-3 atomic facts from the conversation."""
    from groq import Groq

    prompt = (
        "Extract 1-3 atomic facts from this conversation worth remembering in future sessions. "
        "Each fact should be a single sentence capturing a decision, configuration, or outcome. "
        "Skip small talk, errors, and transient debugging steps. "
        "Return ONLY the facts, one per line, no bullets or numbering.\n\n"
        f"CONVERSATION:\n{conversation}"
    )

    try:
        client = Groq(api_key=groq_api_key)
        response = client.chat.completions.create(
            model=groq_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.1,
        )
        text = response.choices[0].message.content.strip()
        facts = [line.strip() for line in text.splitlines() if line.strip() and len(line.strip()) > 20]
        return facts[:3]
    except Exception as e:
        print(f"[auto_fact_capture] Groq call failed: {e}", file=sys.stderr)
        return []


def capture(session_file: str, user_id: str = "jleechan", dry_run: bool = False) -> int:
    """Main entry point. Returns number of facts stored."""
    # Load Groq config from openclaw.json
    cfg_path = Path.home() / ".openclaw" / "openclaw.json"
    cfg = json.loads(cfg_path.read_text())
    llm_cfg = cfg["plugins"]["entries"]["openclaw-mem0"]["config"]["oss"]["llm"]["config"]
    groq_api_key = os.path.expandvars(llm_cfg.get("api_key", ""))
    groq_model = llm_cfg.get("model", "llama-3.3-70b-versatile")

    if not groq_api_key or groq_api_key.startswith("$"):
        # Try env
        groq_api_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_api_key:
        print("[auto_fact_capture] no GROQ_API_KEY available, skipping", file=sys.stderr)
        return 0

    jsonl_path = Path(session_file).expanduser()
    if not jsonl_path.exists():
        print(f"[auto_fact_capture] session file not found: {jsonl_path}", file=sys.stderr)
        return 0

    messages = _read_last_messages(jsonl_path)
    if not messages:
        return 0

    conversation = _build_conversation_text(messages)
    if len(conversation) < MIN_CHARS:
        print(f"[auto_fact_capture] session too short ({len(conversation)} chars), skipping")
        return 0

    print(f"[auto_fact_capture] extracting facts from {jsonl_path.name} ({len(messages)} msgs, {len(conversation)} chars)")

    facts = _extract_facts_groq(conversation, groq_api_key, groq_model)
    if not facts:
        print("[auto_fact_capture] no facts extracted")
        return 0

    print(f"[auto_fact_capture] extracted {len(facts)} facts:")
    for f in facts:
        print(f"  - {f}")

    if dry_run:
        print("[auto_fact_capture] dry-run mode, not storing")
        return len(facts)

    # Store via shared client (handles userId camelCase fix automatically)
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from scripts.mem0_shared_client import add_memory, MemoryWriteError

    stored = 0
    for fact in facts:
        try:
            add_memory(fact, user_id=user_id, infer=False)
            stored += 1
        except MemoryWriteError as e:
            print(f"[auto_fact_capture] write error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[auto_fact_capture] store failed: {e}", file=sys.stderr)

    print(f"[auto_fact_capture] stored {stored}/{len(facts)} facts")
    return stored


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract and store facts from a session JSONL")
    parser.add_argument("--session-file", required=True, help="Path to session .jsonl file")
    parser.add_argument("--session-key", default="", help="Session key (informational only)")
    parser.add_argument("--user-id", default="jleechan", help="User ID for Qdrant storage")
    parser.add_argument("--dry-run", action="store_true", help="Extract but don't store")
    args = parser.parse_args()

    count = capture(args.session_file, user_id=args.user_id, dry_run=args.dry_run)
    sys.exit(0 if count >= 0 else 1)


if __name__ == "__main__":
    main()
