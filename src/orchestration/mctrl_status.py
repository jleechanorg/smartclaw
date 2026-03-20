"""mctrl status — unified view of registry + live tmux sessions."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from orchestration.openclaw_notifier import outbox_health_snapshot

_REGISTRY = Path(__file__).parent.parent.parent / ".tracking" / "bead_session_registry.jsonl"
_STATUS_EMOJI = {
    "in_progress": "🔄",
    "finished": "✅",
    "needs_human": "⚠️ ",
}


def _live_tmux_sessions() -> dict[str, str]:
    """Return {session_name: created_str} for all tmux sessions."""
    try:
        r = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_created_string}"],
            capture_output=True, text=True, timeout=5,
        )
        sessions = {}
        for line in r.stdout.splitlines():
            parts = line.split("\t", 1)
            if parts:
                sessions[parts[0]] = parts[1] if len(parts) > 1 else ""
        return sessions
    except Exception:
        return {}


def _read_registry(path: Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
        # last entry per bead_id wins
        by_bead: dict[str, dict] = {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                by_bead[d["bead_id"]] = d
            except Exception:
                continue
        return list(by_bead.values())
    except FileNotFoundError:
        return []


def print_status(
    registry_path: str | None = None,
    active_only: bool = False,
    outbox_path: str | None = None,
    dead_letter_path: str | None = None,
) -> None:
    path = Path(registry_path) if registry_path else _REGISTRY
    entries = _read_registry(path)
    tmux = _live_tmux_sessions()

    tracked_sessions = {e["session_name"] for e in entries}
    untracked = {s: c for s, c in tmux.items() if s not in tracked_sessions}

    # Default (active): only live sessions + needs_human with live session
    if active_only:
        entries = [e for e in entries if e["session_name"] in tmux]

    print(f"\n{'BEAD':<26} {'STATUS':<14} {'SESSION':<28} {'LIVE':>4}  UPDATED")
    print("─" * 90)

    order = {"in_progress": 0, "needs_human": 1, "finished": 2}
    for e in sorted(entries, key=lambda x: (order.get(x["status"], 9), x.get("updated_at", ""))):
        status = e["status"]
        emoji = _STATUS_EMOJI.get(status, "  ")
        session = e["session_name"]
        live = "🟢" if session in tmux else "⚫"
        updated = e.get("updated_at", "")[:16].replace("T", " ")
        bead = e["bead_id"]
        print(f"{emoji} {bead:<24} {status:<14} {session:<28} {live}    {updated}")

    if untracked:
        print()
        print("UNTRACKED SESSIONS (not in mctrl registry)")
        print("─" * 90)
        for session, created in sorted(untracked.items()):
            print(f"   {'(no bead)':<24} {'unknown':<14} {session:<28} 🟢    {created[:20]}")

    all_entries = _read_registry(path)
    print()
    total = len(all_entries)
    in_prog = sum(1 for e in all_entries if e["status"] == "in_progress")
    needs = sum(1 for e in all_entries if e["status"] == "needs_human")
    done = sum(1 for e in all_entries if e["status"] == "finished")
    suffix = "  (--all to show finished/dead)" if active_only else ""
    print(f"Total: {total} beads  |  🔄 {in_prog} running  |  ⚠️  {needs} needs_human  |  ✅ {done} finished  |  ⚫ {len(untracked)} untracked{suffix}")
    outbox = outbox_health_snapshot(
        outbox_path=outbox_path,
        dead_letter_path=dead_letter_path,
    )
    histogram = ", ".join(
        f"r{retry}:{count}"
        for retry, count in sorted(
            outbox["retry_histogram"].items(),
            key=lambda item: int(item[0]),
        )
    ) or "none"
    oldest = outbox["oldest_age_seconds"]
    oldest_display = f"{oldest}s" if oldest is not None else "unknown"
    print(
        "Outbox: "
        f"{outbox['pending_count']} pending  |  "
        f"oldest {oldest_display}  |  "
        f"dead-letter {outbox['dead_letter_count']}  |  "
        f"retries [{histogram}]"
    )
    print()


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="mctrl status — bead/session tracker")
    p.add_argument("--registry-path", default=None)
    p.add_argument("--outbox-path", default=None)
    p.add_argument("--dead-letter-path", default=None)
    p.add_argument("--active", action="store_true", help="Show only live sessions and needs_human")
    p.add_argument("--all", dest="show_all", action="store_true", help="Show everything including dead/finished")
    args = p.parse_args()
    # Default: active-only view
    active_only = not args.show_all
    print_status(
        args.registry_path,
        active_only=active_only,
        outbox_path=args.outbox_path,
        dead_letter_path=args.dead_letter_path,
    )


if __name__ == "__main__":
    main()
