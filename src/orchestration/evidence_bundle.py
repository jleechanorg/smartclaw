"""PR evidence bundle generator — Stage 1 of the two-stage evidence review pipeline.

Generates evidence bundles at docs/evidence/{repo}/PR-{N}/{date}_{time}_utc/
containing claims, artifacts, self-review, and verdict.json.

Usage:
    python -m orchestration.evidence_bundle <owner> <repo> <pr_number> [--repo-root PATH]

See roadmap/EVIDENCE_REVIEW_SCHEMA.md for the full pipeline design.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from orchestration.code_path_classifier import is_code_path
from orchestration.merge_gate import run_gh


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M_utc")


@dataclass
class EvidenceBundle:
    """Represents a PR evidence bundle directory."""

    owner: str
    repo: str
    pr_number: int
    repo_root: Path
    timestamp: str = field(default_factory=_utcnow)

    @property
    def bundle_dir(self) -> Path:
        return self.repo_root / "docs" / "evidence" / self.repo / f"PR-{self.pr_number}" / self.timestamp

    @property
    def artifacts_dir(self) -> Path:
        return self.bundle_dir / "artifacts"

    def create_dirs(self) -> None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def collect_ci(self) -> dict:
        """Fetch CI check-runs and save to artifacts."""
        rc, stdout, _ = run_gh(
            "api", f"repos/{self.owner}/{self.repo}/pulls/{self.pr_number}",
            "--jq", ".head.sha",
        )
        if rc != 0:
            return {"error": "Failed to get head SHA"}

        head_sha = stdout.strip()
        rc, stdout, _ = run_gh(
            "api", f"repos/{self.owner}/{self.repo}/commits/{head_sha}/check-runs",
        )
        if rc == 0:
            try:
                data = json.loads(stdout)
                checks = data.get("check_runs", [])
                # Sanitize: strip tokens/URLs, keep only name/status/conclusion
                sanitized = {"check_runs": [
                    {"name": c.get("name"), "status": c.get("status"), "conclusion": c.get("conclusion")}
                    for c in checks
                ]}
                (self.artifacts_dir / "ci_check_runs.json").write_text(json.dumps(sanitized, indent=2))
                return {
                    "total": len(checks),
                    "passed": sum(1 for c in checks if c.get("conclusion") in ("success", "neutral", "skipped")),
                    "failed": sum(1 for c in checks if c.get("conclusion") not in ("success", "neutral", "skipped", None)),
                    "pending": sum(1 for c in checks if c.get("status") != "completed"),
                }
            except json.JSONDecodeError:
                return {"error": "Failed to parse CI data"}
        return {"error": "Failed to fetch CI"}

    def collect_cr_review(self) -> dict:
        """Fetch CodeRabbit reviews and save to artifacts.

        Also checks issue comments for CR "reviews paused" notices.
        """
        rc, stdout, _ = run_gh(
            "api", f"repos/{self.owner}/{self.repo}/pulls/{self.pr_number}/reviews",
        )
        if rc == 0:
            try:
                reviews = json.loads(stdout)
                # Sanitize: strip tokens, keep only user.login, state, submitted_at
                sanitized = [
                    {"user": {"login": r.get("user", {}).get("login", "")},
                     "state": r.get("state"), "submitted_at": r.get("submitted_at")}
                    for r in reviews
                ]
                (self.artifacts_dir / "coderabbit_review.json").write_text(json.dumps(sanitized, indent=2))
                cr = [r for r in reviews if "coderabbit" in r.get("user", {}).get("login", "").lower()]
                if cr:
                    latest = cr[-1]
                    state = latest.get("state", "NONE")
                    # Check if CR paused reviews (posted as issue comment)
                    if self._is_cr_paused():
                        return {"state": "PAUSED", "count": len(cr), "note": "CR paused reviews — stale review state"}
                    return {"state": state, "count": len(cr)}
                return {"state": "NONE", "count": 0}
            except json.JSONDecodeError:
                return {"error": "Failed to parse reviews"}
        return {"error": "Failed to fetch reviews"}

    def _is_cr_paused(self) -> bool:
        """Check if CodeRabbit paused reviews via issue comment."""
        rc, stdout, _ = run_gh(
            "api", f"repos/{self.owner}/{self.repo}/issues/{self.pr_number}/comments",
            "--jq", '[.[] | select(.user.login == "coderabbitai[bot]") | .body]',
        )
        if rc != 0 or not stdout.strip():
            return False
        try:
            bodies = json.loads(stdout) if stdout.strip() else []
        except json.JSONDecodeError:
            return False
        if not bodies:
            return False
        latest = bodies[-1].lower()
        return "reviews paused" in latest or "auto_pause" in latest

    def collect_pr_diff(self) -> dict:
        """Fetch PR diff and save to artifacts (capped at 5 MB to avoid bloat)."""
        rc, stdout, _ = run_gh("pr", "diff", str(self.pr_number), "--repo", f"{self.owner}/{self.repo}")
        if rc == 0:
            lines = stdout.strip().split("\n")
            added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
            # Cap saved diff at 5 MB to prevent GitHub file-size rejections
            max_bytes = 5 * 1024 * 1024
            saved = stdout[:max_bytes] if len(stdout) > max_bytes else stdout
            (self.artifacts_dir / "pr_diff.patch").write_text(saved)
            truncated = len(stdout) > max_bytes
            return {"lines_added": added, "lines_removed": removed, "total_lines": len(lines), "truncated": truncated}
        return {"error": "Failed to fetch diff"}

    def run_pytest(self, code_files: list[str] | None = None) -> dict:
        """Run pytest and save output to artifacts.

        If code_files is provided, derives test file paths from the changed
        source modules (e.g., merge_gate.py → test_merge_gate.py). Falls back
        to running the full test suite if no specific test files are found.
        """
        test_targets: list[str] = []
        if code_files:
            for f in code_files:
                basename = Path(f).stem  # e.g., "merge_gate"
                candidate = self.repo_root / "src" / "tests" / f"test_{basename}.py"
                if candidate.exists():
                    test_targets.append(str(candidate.relative_to(self.repo_root)))

        cmd = [sys.executable, "-m", "pytest"] + (test_targets or ["src/tests/"]) + ["-v", "--tb=short"]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=120,
                cwd=str(self.repo_root),
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "PYTHONPATH": str(self.repo_root / "src"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            output = result.stdout + result.stderr
            # Redact absolute paths to avoid leaking workstation details
            output = output.replace(str(self.repo_root), "<repo_root>")
            output = output.replace(str(Path.home()), "~")
            (self.artifacts_dir / "pytest_output.txt").write_text(output)
            return {
                "returncode": result.returncode,
                "passed": result.returncode == 0,
                "output_lines": len(output.split("\n")),
                "scoped": bool(test_targets),
                "test_files": test_targets or ["src/tests/"],
            }
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return {"error": str(e)}

    def collect_unresolved_threads(self) -> dict:
        """Check unresolved review threads (paginated)."""
        all_threads: list[dict] = []
        cursor: str | None = None

        for _ in range(10):  # safety limit on pages
            after_clause = f', after: "{cursor}"' if cursor else ""
            query = (
                "query($owner: String!, $name: String!, $pr: Int!) {"
                "  repository(owner: $owner, name: $name) {"
                "    pullRequest(number: $pr) {"
                f"      reviewThreads(first: 100{after_clause}) {{"
                "        pageInfo { hasNextPage endCursor }"
                "        nodes { isResolved }"
                "      }"
                "    }"
                "  }"
                "}"
            )
            rc, stdout, _ = run_gh(
                "api", "graphql",
                "-F", f"owner={self.owner}", "-F", f"name={self.repo}", "-F", f"pr={self.pr_number}",
                "-f", f"query={query}",
            )
            if rc != 0:
                return {"error": "Failed to fetch threads"}
            try:
                data = json.loads(stdout)
                threads_data = data["data"]["repository"]["pullRequest"]["reviewThreads"]
                all_threads.extend(threads_data["nodes"])
                page_info = threads_data["pageInfo"]
                if not page_info.get("hasNextPage", False):
                    break
                cursor = page_info.get("endCursor")
            except (json.JSONDecodeError, KeyError):
                return {"error": "Failed to parse threads"}

        unresolved = sum(1 for t in all_threads if not t.get("isResolved", True))
        result = {"total": len(all_threads), "unresolved": unresolved}
        (self.artifacts_dir / "review_threads.json").write_text(json.dumps(result, indent=2))
        return result

    def get_pr_files(self) -> list[str] | None:
        """Get list of files changed in the PR.

        Returns ``None`` when the API call fails or the response cannot be
        parsed so callers can fail-closed.
        """
        rc, stdout, _ = run_gh(
            "api", f"repos/{self.owner}/{self.repo}/pulls/{self.pr_number}/files",
            "--paginate", "--jq", ".[].filename",
        )
        if rc == 0 and stdout.strip():
            return [line for line in stdout.strip().split("\n") if line]
        if rc == 0:
            return []
        return None

    def generate(self) -> dict:
        """Generate the full evidence bundle. Returns verdict dict."""
        self.create_dirs()

        pr_files = self.get_pr_files()
        if pr_files is None:
            stage1_pass = False
            pr_file_count = "unknown"
            code_files: list[str] = []
            stage1_findings = ["Failed to determine changed files"]
        else:
            pr_file_count = len(pr_files)
            code_files = [f for f in pr_files if is_code_path(f)]
            stage1_pass = True
            stage1_findings = []

        # Save file list artifact for reviewability
        if pr_files is not None:
            file_list = {"total": len(pr_files), "code_files": code_files, "all_files": pr_files}
            (self.artifacts_dir / "pr_files.json").write_text(json.dumps(file_list, indent=2))

        # Collect all artifacts
        ci = self.collect_ci()
        cr = self.collect_cr_review()
        diff = self.collect_pr_diff()
        threads = self.collect_unresolved_threads()

        # Only run pytest if code files changed
        pytest_result = self.run_pytest(code_files) if code_files else {"skipped": True}

        # Write claims.md — human-readable, reviewable by stage 2
        ci_summary = f"{ci.get('passed', '?')}/{ci.get('total', '?')} passed" if "error" not in ci else ci.get("error", "unknown")
        cr_summary = f"state={cr.get('state', 'NONE')}, {cr.get('count', 0)} CodeRabbit reviews (artifact has all reviewers)" if "error" not in cr else cr.get("error", "unknown")
        if "error" not in diff:
            diff_summary = f"+{diff.get('lines_added', '?')}/-{diff.get('lines_removed', '?')}"
            if diff.get("truncated"):
                diff_summary += " (NOTE: pr_diff.patch is truncated at 5MB for GitHub limits; these line counts were computed from the full diff before truncation and cannot be verified from the truncated patch alone)"
        else:
            diff_summary = diff.get("error", "unknown")
        thread_summary = f"{threads.get('unresolved', '?')} unresolved / {threads.get('total', '?')} total" if "error" not in threads else threads.get("error", "unknown")
        if pytest_result.get("skipped"):
            pytest_summary = "skipped (no code files)"
        elif "error" in pytest_result:
            pytest_summary = pytest_result["error"]
        else:
            pytest_summary = f"{'passed' if pytest_result.get('passed') else 'FAILED'} (rc={pytest_result.get('returncode')})"

        claims = [
            f"PR #{self.pr_number} in {self.owner}/{self.repo}",
            f"Files changed: {pr_file_count} total, {len(code_files)} code files",
            f"CI: {ci_summary} (see artifacts/ci_check_runs.json)",
            f"CodeRabbit: {cr_summary} (see artifacts/coderabbit_review.json)",
            f"Diff: {diff_summary} (see artifacts/pr_diff.patch)",
            f"Review threads: {thread_summary} (see artifacts/review_threads.json)",
            f"Pytest: {pytest_summary} (see artifacts/pytest_output.txt)",
        ]
        (self.bundle_dir / "claims.md").write_text(
            f"# Evidence Claims — PR #{self.pr_number}\n\n"
            + "\n".join(f"- {c}" for c in claims) + "\n"
        )

        # Determine stage 1 verdict

        # Check for collection errors first — fail closed
        if "error" in ci:
            stage1_pass = False
            stage1_findings.append(f"CI collection error: {ci['error']}")
        if "error" in diff:
            stage1_pass = False
            stage1_findings.append(f"PR diff collection error: {diff['error']}")
        if ci.get("failed", 0) > 0:
            stage1_pass = False
            stage1_findings.append(f"CI has {ci['failed']} failed checks")
        if ci.get("pending", 0) > 0:
            stage1_pass = False
            stage1_findings.append(f"CI has {ci['pending']} pending checks")
        if "error" in threads:
            stage1_pass = False
            stage1_findings.append(f"Thread collection error: {threads['error']}")
        if threads.get("unresolved", 0) > 0:
            stage1_pass = False
            stage1_findings.append(f"{threads['unresolved']} unresolved review threads")
        if "error" in pytest_result:
            stage1_pass = False
            stage1_findings.append(f"Pytest error: {pytest_result['error']}")
        if pytest_result.get("passed") is False:
            stage1_pass = False
            stage1_findings.append("Pytest failed")

        # Write self_review.md
        status = "PASS" if stage1_pass else "FAIL"
        review_lines = [
            f"# Self-Review — PR #{self.pr_number}",
            f"\n**Status: {status}**\n",
        ]
        if stage1_findings:
            review_lines.append("## Findings\n")
            review_lines.extend(f"- {f}" for f in stage1_findings)
        else:
            review_lines.append("No issues found in stage 1 self-review.")
        (self.bundle_dir / "self_review.md").write_text("\n".join(review_lines) + "\n")

        # Write verdict.json
        verdict = {
            "pr": self.pr_number,
            "repo": f"{self.owner}/{self.repo}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage1": {
                "status": status,
                "reviewer": "self",
                "model": "claude-opus-4-6",
                "model_family": "anthropic",
                "findings": stage1_findings,
                "claims_verified": len(claims),
                "claims_failed": len(stage1_findings),
            },
            "stage2": {
                "status": "PENDING",
                "reviewer": None,
                "model": None,
                "findings": [],
                "independence_verified": False,
                "model_family_differs_from_stage1": False,
            },
            "coderabbit": {
                "status": cr.get("state", "NONE"),
                "critical_findings": 0,
                "major_findings": 0,
            },
            "overall": "FAIL",  # Will be corrected by validate_verdict
        }
        verdict = validate_verdict(verdict)
        (self.bundle_dir / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")

        return verdict

    def commit_and_push(self, branch: str | None = None) -> bool:
        """Commit the evidence bundle to the PR branch and push.

        If branch is None, fetches the PR head ref from GitHub.
        Returns True if commit+push succeeded.
        """
        if branch is None:
            rc, stdout, _ = run_gh(
                "api", f"repos/{self.owner}/{self.repo}/pulls/{self.pr_number}",
                "--jq", ".head.ref",
            )
            if rc != 0 or not stdout.strip():
                return False
            branch = stdout.strip()

        bundle_rel = self.bundle_dir.relative_to(self.repo_root)
        try:
            subprocess.run(
                ["git", "add", str(bundle_rel)],
                cwd=str(self.repo_root), check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m",
                 f"evidence: stage 1 bundle for PR #{self.pr_number}\n\n"
                 f"Generated at {self.timestamp} by evidence_bundle.py"],
                cwd=str(self.repo_root), check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "push", "origin", branch],
                cwd=str(self.repo_root), check=True, capture_output=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False


def validate_verdict(verdict: dict) -> dict:
    """Enforce verdict invariants — recompute overall from stage1/stage2 status.

    Rules:
    - overall=PASS requires stage1=PASS AND stage2=PASS AND independence_verified
      AND model_family_differs_from_stage1
    - overall=PENDING when stage1=PASS but stage2 incomplete
    - overall=FAIL when stage1=FAIL (regardless of stage2)

    Returns the verdict dict with corrected overall field.
    """
    s1 = verdict.get("stage1", {}).get("status", "FAIL")
    s2 = verdict.get("stage2", {})
    s2_status = s2.get("status", "PENDING")
    s2_independent = s2.get("independence_verified", False)
    s2_family_differs = s2.get("model_family_differs_from_stage1", False)

    if s1 != "PASS":
        verdict["overall"] = "FAIL"
    elif s2_status == "PASS" and s2_independent and s2_family_differs:
        verdict["overall"] = "PASS"
    elif s2_status == "FAIL":
        verdict["overall"] = "FAIL"
    else:
        verdict["overall"] = "PENDING"

    return verdict


def main() -> int:
    """CLI entry point."""
    import argparse
    parser = argparse.ArgumentParser(description="Generate PR evidence bundle (stage 1 + optional stage 2)")
    parser.add_argument("owner", help="Repository owner")
    parser.add_argument("repo", help="Repository name")
    parser.add_argument("pr_number", type=int, help="PR number")
    parser.add_argument("--repo-root", default=os.path.expanduser("~/.openclaw"), help="Repository root path")
    parser.add_argument("--push", action="store_true", help="Commit and push bundle to PR branch")
    parser.add_argument("--stage2", action="store_true", help="Run stage 2 independent review after stage 1 PASS")
    parser.add_argument("--stage2-model", choices=["codex", "claude", "gemini"], help="Preferred stage 2 reviewer")
    parser.add_argument("--stage2-timeout", type=int, default=300, help="Stage 2 reviewer timeout (seconds)")
    args = parser.parse_args()

    bundle = EvidenceBundle(
        owner=args.owner, repo=args.repo, pr_number=args.pr_number,
        repo_root=Path(args.repo_root),
    )
    verdict = bundle.generate()
    print(json.dumps(verdict, indent=2))

    # Run stage 2 if requested and stage 1 passed
    if args.stage2 and verdict["stage1"]["status"] == "PASS":
        from orchestration.stage2_reviewer import run_stage2

        verdict_path = bundle.bundle_dir / "verdict.json"
        print("\n--- Stage 2: Independent Review ---")
        result = run_stage2(
            verdict_path,
            preferred_model=args.stage2_model,
            stage1_family="anthropic",
            timeout=args.stage2_timeout,
        )
        # Re-read updated verdict and enforce invariants
        verdict = validate_verdict(json.loads(verdict_path.read_text()))
        verdict_path.write_text(json.dumps(verdict, indent=2) + "\n")
        print(json.dumps(verdict, indent=2))
        print(f"\nStage 2: {result.status} by {result.reviewer_model} ({result.reviewer_family})")

    if args.push:
        if bundle.commit_and_push():
            print(f"\nEvidence bundle committed and pushed to PR #{args.pr_number}")
        else:
            print("\nFailed to commit/push evidence bundle", file=sys.stderr)
            return 1

    overall = verdict.get("overall", "FAIL")
    stage1 = verdict.get("stage1", {}).get("status", "FAIL")
    # Stage-1-only runs (no --stage2) leave overall=PENDING — exit 0 if stage1 passed
    if overall == "PASS":
        return 0
    if overall == "PENDING" and stage1 == "PASS":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
