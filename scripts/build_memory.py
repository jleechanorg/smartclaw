#!/usr/bin/env python3
"""Build OpenClaw memory from git history, PRs, and beads.

Three stages run in sequence:
  1. collect  — gather raw commits, PRs, beads from configured repos
  2. synthesize — call ai_orch (claude haiku) to produce structured summaries per week
  3. write    — write weekly .md files to ~/.smartclaw/memory/ and update SOUL.md/MEMORY.md

Run all stages (default):
  python scripts/build_memory.py --days 14

Run individual stage (for debugging):
  python scripts/build_memory.py --days 14 --stage collect
  python scripts/build_memory.py --days 14 --stage synthesize  # reads collect output
  python scripts/build_memory.py --days 14 --stage write       # reads synthesize output
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_REPOS: dict[str, str] = {
    "worldarchitect.ai": "~/projects/worldarchitect.ai",
    "smartclaw": "~/project_smartclaw/smartclaw",
    "worldai_claw": "~/project_worldaiclaw/worldai_claw",
}
MEMORY_DIR = Path("~/.smartclaw/memory").expanduser()
SOUL_PATH = Path("~/.smartclaw/SOUL.md").expanduser()
MEMORY_MD_PATH = Path("~/.smartclaw/MEMORY.md").expanduser()  # root mirrors workspace; write to root (canonical)

COLLECT_OUTPUT = Path("/tmp/build_memory_collect.json")
SYNTHESIZE_OUTPUT = Path("/tmp/build_memory_synthesize.json")

# Note: SOUL.md may have suffixes like "(auto-updated weekly)", so we use startswith for flexible matching
LEARNED_PATTERNS_ANCHOR = "## Learned Patterns (auto-updated weekly)"
PROJECT_STATUS_ANCHOR = "## Project Status"


# ── Helpers ───────────────────────────────────────────────────────────────────

def run(cmd: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        check=True, text=True, capture_output=True,
    ).stdout.strip()


def week_key(d: dt.date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def date_range(key: str) -> tuple[dt.date, dt.date]:
    year, w = key.split("-W")
    start = dt.date.fromisocalendar(int(year), int(w), 1)
    return start, start + dt.timedelta(days=6)


# ── Stage 1: Collect ──────────────────────────────────────────────────────────

def collect_commits(repo_path: Path, repo_name: str, since: str) -> list[dict]:
    try:
        raw = run([
            "git", "log", "--no-merges",
            f"--since={since}",
            "--date=iso-strict",
            "--pretty=%ad%x1f%s",
        ], cwd=repo_path)
    except subprocess.CalledProcessError:
        return []

    out = []
    for line in filter(None, raw.splitlines()):
        parts = line.split("\x1f", 1)
        if len(parts) != 2:
            continue
        date_str, subject = parts
        try:
            d = dt.datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        out.append({"repo": repo_name, "date": d.isoformat(), "subject": subject.strip()})
    return out


def collect_prs(repo_name: str, since: str) -> list[dict]:
    try:
        raw = run([
            "gh", "pr", "list",
            "--repo", f"jleechanorg/{repo_name}",
            "--state", "merged",
            "--limit", "100",
            "--json", "number,title,mergedAt,body",
        ])
        items = json.loads(raw)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return []

    cutoff = dt.datetime.fromisoformat(since).date()
    out = []
    for item in items:
        raw_date = (item.get("mergedAt") or "").replace("Z", "+00:00")
        try:
            d = dt.datetime.fromisoformat(raw_date).date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        body = (item.get("body") or "").strip()
        out.append({
            "repo": repo_name,
            "date": d.isoformat(),
            "number": item.get("number"),
            "title": item.get("title", ""),
            "body": body[:400] if body else "",
        })
    return out


def collect_beads(days: int) -> list[dict]:
    beads_path = Path("~/.beads/issues.jsonl").expanduser()
    if not beads_path.exists():
        return []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    out = []
    for line in beads_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = item.get("updated_at") or item.get("created_at") or ""
        try:
            # Handle both naive and timezone-aware ISO strings
            ts_str = str(ts).replace("Z", "+00:00")
            when = dt.datetime.fromisoformat(ts_str)
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if when < cutoff:
            continue
        title = (item.get("title") or item.get("summary") or "").strip()
        if title:
            out.append({
                "id": item.get("id", ""),
                "title": title,
                "status": item.get("status", ""),
                "updated_at": when.date().isoformat(),
            })
    return sorted(out, key=lambda x: x["updated_at"], reverse=True)[:100]


def stage_collect(args: argparse.Namespace) -> dict:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)).isoformat()
    repos = {}
    for entry in (args.repo or [f"{n}:{p}" for n, p in DEFAULT_REPOS.items()]):
        if ":" in entry:
            name, path_str = entry.split(":", 1)
        else:
            name = Path(entry).name
            path_str = entry
        repos[name] = Path(path_str).expanduser()

    all_commits: list[dict] = []
    all_prs: list[dict] = []
    for name, path in repos.items():
        if not path.exists():
            print(f"  skip missing repo: {path}", file=sys.stderr)
            continue
        commits = collect_commits(path, name, since)
        prs = collect_prs(name, since)
        print(f"  {name}: {len(commits)} commits, {len(prs)} PRs")
        all_commits.extend(commits)
        all_prs.extend(prs)

    beads = collect_beads(args.days)
    print(f"  beads: {len(beads)} updated")

    return {"since": since, "days": args.days, "commits": all_commits, "prs": all_prs, "beads": beads}


# ── Stage 2: Synthesize ───────────────────────────────────────────────────────

WEEKLY_PROMPT = """You are synthesizing a developer's week of work into a structured memory entry.

Given the raw commits, merged PRs, and bead (task) updates for {week} below, produce a structured summary.

OUTPUT FORMAT — respond with ONLY this markdown (no preamble):

## {week}

### What shipped
- <bullet per significant PR/feature, max 5>

### Key decisions
- <bullet per architectural or process decision inferred from PR descriptions, max 3>

### Patterns observed
- <bullet per behavioral/engineering pattern worth remembering, max 3>

### Project status
<one sentence per active project with current focus>

RAW DATA:
{data}
"""

SOUL_PROMPT = """You are updating an AI agent's "Learned Patterns" section in SOUL.md.

Given this week's activity across all projects, extract durable behavioral patterns and preferences.
These will be used by an AI agent (OpenClaw) to make decisions on behalf of the developer.

Focus on:
- How the developer prefers to handle specific situations (CI failures, PR reviews, test failures)
- Communication preferences
- Technical decisions that reflect values (real tests > mocks, concise > verbose, etc.)
- What NOT to do (anti-patterns observed)

OUTPUT FORMAT — respond with ONLY bullet points (no headers, no preamble):
- <pattern>
- <pattern>
...

ACTIVITY SUMMARY:
{summary}
"""

MEMORY_STATUS_PROMPT = """Given this week's activity, write a concise project status update for each active project.

Format as:
### <project-name>
- Current focus: <one line>
- Recent decisions: <bullet, max 2>
- Active work: <bullet, max 2>

ACTIVITY:
{summary}
"""


def call_ai_orch(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    """Call ai_orch with a prompt, return stdout."""
    try:
        result = subprocess.run(
            ["ai_orch", "run", "--agent-cli", "claude", "--model", model, prompt],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            print(f"  ai_orch error: {result.stderr[:200]}", file=sys.stderr)
            return ""
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        print("  ai_orch timed out", file=sys.stderr)
        return ""


def group_by_week(data: dict) -> dict[str, dict]:
    grouped: dict[str, dict] = defaultdict(lambda: {"commits": [], "prs": [], "beads": []})
    for c in data["commits"]:
        try:
            d = dt.date.fromisoformat(c["date"])
        except ValueError:
            continue
        grouped[week_key(d)]["commits"].append(c)
    for p in data["prs"]:
        try:
            d = dt.date.fromisoformat(p["date"])
        except ValueError:
            continue
        grouped[week_key(d)]["prs"].append(p)
    # Beads span the whole window — attach to most recent week
    if grouped and data["beads"]:
        latest_week = max(grouped.keys())
        grouped[latest_week]["beads"] = data["beads"]
    return dict(grouped)


def stage_synthesize(data: dict, dry_run: bool = False) -> dict:
    grouped = group_by_week(data)
    results: dict[str, dict] = {}

    for week in sorted(grouped.keys()):
        wdata = grouped[week]
        start, end = date_range(week)
        print(f"  synthesizing {week} ({start} – {end}): {len(wdata['commits'])} commits, {len(wdata['prs'])} PRs")

        # Sample data to keep prompt manageable (<4KB)
        # Take representative commits per repo, prioritize PRs (higher signal)
        by_repo: dict[str, list] = defaultdict(list)
        for c in wdata["commits"]:
            by_repo[c["repo"]].append(c)
        sampled_commits = []
        per_repo = max(5, 30 // max(len(by_repo), 1))
        for repo_commits in by_repo.values():
            sampled_commits.extend(repo_commits[:per_repo])
        sampled_prs = wdata["prs"][:25]

        data_lines = []
        for p in sampled_prs:
            data_lines.append(f"PR #{p['number']} [{p['repo']}] {p['date']}: {p['title']}")
            if p.get("body"):
                data_lines.append(f"  notes: {p['body'][:150]}")
        for c in sampled_commits:
            data_lines.append(f"COMMIT [{c['repo']}] {c['date']}: {c['subject']}")
        for b in wdata.get("beads", []):
            data_lines.append(f"BEAD {b['id']} ({b['status']}): {b['title']}")

        data_str = "\n".join(data_lines) if data_lines else "(no activity)"
        prompt = WEEKLY_PROMPT.format(week=week, data=data_str)

        if dry_run:
            print(f"  [dry-run] would call ai_orch for {week}")
            summary = f"## {week}\n\n(dry-run placeholder)"
        else:
            summary = call_ai_orch(prompt)
            if not summary:
                summary = f"## {week}\n\n(synthesis failed — raw: {len(data_lines)} events)"

        results[week] = {"summary": summary, "raw": wdata}

    # Synthesize cross-week patterns for SOUL.md and MEMORY.md
    all_summaries = "\n\n".join(r["summary"] for r in results.values())

    if dry_run:
        soul_patterns = "(dry-run)"
        memory_status = "(dry-run)"
    else:
        print("  synthesizing SOUL.md learned patterns...")
        soul_patterns = call_ai_orch(SOUL_PROMPT.format(summary=all_summaries[:3000]))
        print("  synthesizing MEMORY.md project status...")
        memory_status = call_ai_orch(MEMORY_STATUS_PROMPT.format(summary=all_summaries[:3000]))

    return {"weeks": results, "soul_patterns": soul_patterns, "memory_status": memory_status}


# ── Stage 3: Write ────────────────────────────────────────────────────────────

def upsert_section(text: str, anchor: str, new_content: str) -> str:
    """Replace or append a section identified by anchor header.

    Uses startswith matching to handle anchors with suffixes like
    '## Learned Patterns (auto-updated weekly)'.
    """
    lines = text.split('\n')
    found_idx = -1
    found_line = ""

    # Find line starting with anchor (handles suffixes like "(auto-updated weekly)")
    for i, line in enumerate(lines):
        if line.strip().startswith(anchor):
            found_idx = i
            found_line = line
            break

    if found_idx < 0:
        return text.rstrip() + f"\n\n{anchor}\n\n{new_content}\n"

    # Reconstruct: keep the original header line (with its suffix), replace content
    result_lines = lines[:found_idx]
    result_lines.append(found_line)  # Keep original header like "## Learned Patterns (auto-updated weekly)"
    result_lines.append("")  # Empty line after header
    result_lines.extend(new_content.split('\n'))

    # Add content from after the original section until next ## header
    in_section = True
    for line in lines[found_idx + 1:]:
        if line.strip().startswith('## '):
            in_section = False
        if not in_section:
            result_lines.append(line)

    return '\n'.join(result_lines) + '\n'


def write_weekly_files(weeks: dict[str, dict], memory_dir: Path, dry_run: bool) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    for week, data in weeks.items():
        path = memory_dir / f"{week}.md"
        content = data["summary"]
        if dry_run:
            print(f"  [dry-run] would write {path}")
            print(f"    preview: {content[:120].replace(chr(10), ' ')}")
        else:
            path.write_text(content + "\n", encoding="utf-8")
            print(f"  wrote {path}")


def update_soul_md(soul_path: Path, patterns: str, dry_run: bool) -> None:
    if not patterns or patterns.startswith("("):
        print(f"  skip SOUL.md update (no patterns)")
        return
    dated = f"_Updated {dt.date.today().isoformat()}_\n\n{patterns}"
    if dry_run:
        print(f"  [dry-run] would update {soul_path} Learned Patterns section")
        print(f"    preview: {patterns[:120].replace(chr(10), ' ')}")
        return
    # Resolve symlinks to use a canonical path (for existence checks and logging)
    soul_path = soul_path.resolve()
    if soul_path.exists():
        text = soul_path.read_text(encoding="utf-8")
    else:
        text = ""
    updated = upsert_section(text, LEARNED_PATTERNS_ANCHOR, dated)
    soul_path.write_text(updated, encoding="utf-8")
    print(f"  updated {soul_path} (Learned Patterns)")


def update_memory_md(memory_path: Path, status: str, dry_run: bool) -> None:
    if not status or status.startswith("("):
        print(f"  skip MEMORY.md update (no status)")
        return
    dated = f"_Updated {dt.date.today().isoformat()}_\n\n{status}"
    if dry_run:
        print(f"  [dry-run] would update {memory_path} Project Status section")
        print(f"    preview: {status[:120].replace(chr(10), ' ')}")
        return
    # Resolve symlinks to use a canonical path (for existence checks and logging)
    memory_path = memory_path.resolve()
    if memory_path.exists():
        text = memory_path.read_text(encoding="utf-8")
    else:
        text = ""
    updated = upsert_section(text, PROJECT_STATUS_ANCHOR, dated)
    memory_path.write_text(updated, encoding="utf-8")
    print(f"  updated {memory_path} (Project Status)")


def stage_write(synthesized: dict, args: argparse.Namespace) -> None:
    write_weekly_files(synthesized["weeks"], MEMORY_DIR, args.dry_run)
    update_soul_md(SOUL_PATH, synthesized["soul_patterns"], args.dry_run)
    update_memory_md(MEMORY_MD_PATH, synthesized["memory_status"], args.dry_run)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=14, help="Lookback window in days (default: 14)")
    p.add_argument("--repo", action="append", help="name:path pairs; repeatable. Defaults to 3 main repos.")
    p.add_argument("--stage", choices=["collect", "synthesize", "write", "all"], default="all",
                   help="Run only this stage (default: all)")
    p.add_argument("--dry-run", action="store_true", help="Show what would be written without writing")
    p.add_argument("--collect-out", default=str(COLLECT_OUTPUT), help="Path for collect stage JSON output")
    p.add_argument("--synthesize-out", default=str(SYNTHESIZE_OUTPUT), help="Path for synthesize stage JSON output")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.stage in ("collect", "all"):
        print("Stage 1: collect")
        collected = stage_collect(args)
        Path(args.collect_out).write_text(json.dumps(collected, indent=2), encoding="utf-8")
        print(f"  written to {args.collect_out}")
        if args.stage == "collect":
            return 0

    if args.stage in ("synthesize", "all"):
        print("Stage 2: synthesize")
        if args.stage == "synthesize":
            collected = json.loads(Path(args.collect_out).read_text(encoding="utf-8"))
        synthesized = stage_synthesize(collected, dry_run=args.dry_run)
        Path(args.synthesize_out).write_text(json.dumps(synthesized, indent=2), encoding="utf-8")
        print(f"  written to {args.synthesize_out}")
        if args.stage == "synthesize":
            return 0

    if args.stage in ("write", "all"):
        print("Stage 3: write")
        if args.stage == "write":
            synthesized = json.loads(Path(args.synthesize_out).read_text(encoding="utf-8"))
        stage_write(synthesized, args)

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
