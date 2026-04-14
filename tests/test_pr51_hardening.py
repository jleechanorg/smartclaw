from __future__ import annotations

import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
OPENCLAW_CONFIG = REPO_ROOT / "openclaw.json"
LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
BEADS_BACKUP_DIR = REPO_ROOT / ".beads" / "backup"

SENSITIVE_PATTERNS = [
    re.compile(r"xoxb-[A-Za-z0-9-]+"),
    re.compile(r"xai-[A-Za-z0-9_-]+"),
    re.compile(r"C0[A-Z0-9]{8,}"),
    re.compile(r"/Users/jleechan"),
    re.compile(r"[A-Za-z0-9._%+-]+@users\.noreply\.github\.com"),
]


def test_mem0_history_db_path_uses_home_placeholder() -> None:
    if not OPENCLAW_CONFIG.exists():
        import pytest
        pytest.skip("openclaw.json not present (gitignored — run from ~/.smartclaw/)")
    cfg = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "historyDbPath" and isinstance(v, str):
                    found.append(v)
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(cfg)
    # openclaw does not expand ${HOME} at runtime; accept the absolute path
    acceptable = {
        "${HOME}/.smartclaw/mem0-history.db",
        str(Path.home() / ".smartclaw" / "mem0-history.db"),
    }
    assert any(v in acceptable for v in found), (
        f"historyDbPath values {found!r} must be one of {acceptable}"
    )


def test_no_runtime_db_or_progress_artifacts_tracked() -> None:
    unexpected = [
        REPO_ROOT / "memory.db",
        REPO_ROOT / "vector_store.db",
        REPO_ROOT / "ralph" / "metrics.json",
        REPO_ROOT / "ralph" / "progress.txt",
    ]
    import subprocess
    tracked = []
    for artifact in unexpected:
        rel = str(artifact.relative_to(REPO_ROOT))
        proc = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=REPO_ROOT, capture_output=True, text=True)
        if proc.returncode == 0:
            tracked.append(rel)
    assert not tracked, f"Runtime artifacts should not be tracked: {tracked}"


def test_no_literal_tokens_in_backup_configs() -> None:
    # Scan committed config artefacts only: launchd templates checked in under
    # launchd/. The live openclaw.json and
    # ~/Library/LaunchAgents/*.plist are gitignored runtime files that must
    # contain real tokens to work — scanning them here would always fail.
    LAUNCHD_TEMPLATES_DIR = REPO_ROOT / "launchd"
    files: list[Path] = []
    if LAUNCHD_TEMPLATES_DIR.is_dir():
        files.extend(sorted(LAUNCHD_TEMPLATES_DIR.glob("*.plist")))

    bad_hits: list[str] = []
    for path in files:
        display = str(path.relative_to(REPO_ROOT))
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pat in SENSITIVE_PATTERNS[:2]:
            if pat.search(text):
                bad_hits.append(f"{display} matches {pat.pattern}")

    assert not bad_hits, "Found literal secrets in committed config files: " + "; ".join(bad_hits)


def test_beads_backup_is_redacted() -> None:
    files = [
        BEADS_BACKUP_DIR / "events.jsonl",
        BEADS_BACKUP_DIR / "issues.jsonl",
    ]

    bad_hits: list[str] = []
    for path in files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pat in SENSITIVE_PATTERNS:
            if pat.search(text):
                bad_hits.append(f"{path.relative_to(REPO_ROOT)} matches {pat.pattern}")

    assert not bad_hits, "Found sensitive data in beads backups: " + "; ".join(bad_hits)


def test_launchd_templates_do_not_use_literal_home_variable() -> None:
    launchd_dir = REPO_ROOT / "launchd"
    templates = sorted(launchd_dir.glob("*.plist.template"))
    assert templates, "Expected launchd plist templates in launchd/"

    bad: list[str] = []
    for template in templates:
        text = template.read_text(encoding="utf-8", errors="ignore")
        if "${HOME}" in text or "$HOME" in text:
            bad.append(str(template.relative_to(REPO_ROOT)))

    assert not bad, (
        "Launchd templates must use @HOME@ replacement, not literal ${HOME} or $HOME: "
        + ", ".join(bad)
    )


def test_install_launchagents_never_prefers_system_python_for_mem0() -> None:
    script_path = REPO_ROOT / "scripts" / "install-launchagents.sh"
    if not script_path.exists():
        import pytest
        pytest.skip("install-launchagents.sh not present")

    script = script_path.read_text(encoding="utf-8")
    # Regex: /usr/bin/python3 invoked directly with mem0/qdrant_client imports on the same line.
    # Matches regardless of quoting style or whitespace around arguments.
    bad_pattern = re.compile(
        r"/usr/bin/python3\s+[^\n]*import\s+mem0[^\n]*import\s+qdrant_client",
        re.IGNORECASE,
    )
    assert not bad_pattern.search(script), (
        "Script must not contain branches that use /usr/bin/python3 for mem0 detection"
    )
