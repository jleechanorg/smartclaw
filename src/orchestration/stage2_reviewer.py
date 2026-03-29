"""Stage 2 Independent Evidence Review — dispatches to a different model family.

Reads a Stage 1 evidence bundle (claims.md + artifacts/) and asks an independent
LLM (Codex → Claude → Gemini fallback) to verify that claims have supporting evidence.

The dispatcher (this module) writes the independence attestation fields in verdict.json,
NOT the reviewer itself. This prevents a compromised reviewer from forging independence.

Usage:
    python -m orchestration.stage2_reviewer <verdict_json_path> [--model codex|claude|gemini]
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Model families — stage 2 must use a different family than stage 1
MODEL_FAMILIES = {
    "codex": "openai",
    "claude": "anthropic",
    "gemini": "google",
}

# CLI commands for each reviewer (tried in order)
REVIEWER_CONFIGS = [
    {
        "name": "codex",
        "family": "openai",
        "cmd": ["codex", "exec", "--yolo"],
        "binary": "codex",
    },
    {
        "name": "gemini",
        "family": "google",
        "cmd": ["gemini"],
        "binary": "gemini",
    },
    {
        "name": "claude",
        "family": "anthropic",
        "cmd": ["claude", "-p"],
        "binary": "claude",
    },
]

REVIEW_PROMPT_TEMPLATE = """You are an independent evidence reviewer for PR #{pr_number} in {repo}.

Your job: verify that the claims in claims.md are supported by the artifacts in artifacts/.
You have NO context from the coding agent — only what's in this evidence bundle.

## Evidence Bundle Location
{bundle_dir}

## claims.md contents:
{claims}

## Artifacts available:
{artifact_list}

## Artifact contents (summaries):
{artifact_summaries}

## Your Task

For each claim in claims.md:
1. Find the supporting artifact(s)
2. Rate the evidence: STRONG (clear proof), WEAK (partial/indirect), MISSING (no evidence)
3. Check for:
   - Circular citations (claim cites itself as evidence)
   - Empty or missing artifacts
   - Statistical weakness (e.g., "tests passed" but output shows failures)
   - Unverified assertions (claim with no artifact)

## Output Format

Write your review as markdown with this structure:

# Independent Evidence Review — PR #{pr_number}

## Claim Verification

| # | Claim | Evidence | Rating |
|---|-------|----------|--------|
| 1 | <claim> | <artifact and what it shows> | STRONG/WEAK/MISSING |
| ... | ... | ... | ... |

## Findings

- List any issues found (empty if clean)

## Verdict

**PASS** or **FAIL** — with one-line justification.

A PASS requires: all claims rated STRONG or WEAK with no MISSING, no circular citations,
no contradicted claims. A single MISSING or contradicted claim = FAIL.
"""


@dataclass
class Stage2Result:
    """Result from Stage 2 independent review."""

    status: str  # PASS or FAIL
    reviewer_model: str  # e.g., "codex", "gemini", "claude"
    reviewer_family: str  # e.g., "openai", "google", "anthropic"
    findings: list[str]
    review_text: str
    confidence: float


def _read_bundle(bundle_dir: Path) -> tuple[str, str, str]:
    """Read claims.md and artifact summaries from the bundle."""
    claims_path = bundle_dir / "claims.md"
    claims = claims_path.read_text() if claims_path.exists() else "(claims.md not found)"

    artifacts_dir = bundle_dir / "artifacts"
    artifact_list: list[str] = []
    artifact_summaries: list[str] = []

    if artifacts_dir.exists():
        for f in sorted(artifacts_dir.iterdir()):
            artifact_list.append(f"- {f.name} ({f.stat().st_size:,} bytes)")
            try:
                content = f.read_text(errors="replace")
                # Cap each artifact summary at 2000 chars
                if len(content) > 2000:
                    summary = content[:2000] + f"\n... (truncated, {len(content):,} total chars)"
                else:
                    summary = content
                artifact_summaries.append(f"### {f.name}\n```\n{summary}\n```")
            except Exception:
                artifact_summaries.append(f"### {f.name}\n(could not read)")

    return claims, "\n".join(artifact_list), "\n\n".join(artifact_summaries)


def _parse_verdict_from_review(review_text: str) -> tuple[str, list[str]]:
    """Parse PASS/FAIL verdict and findings from the reviewer's markdown output."""
    lines = review_text.strip().split("\n")

    # Find verdict line
    status = "FAIL"  # default fail-closed
    for line in reversed(lines):
        upper = line.upper().strip()
        if "**PASS**" in upper or "VERDICT: PASS" in upper or upper.startswith("PASS"):
            status = "PASS"
            break
        if "**FAIL**" in upper or "VERDICT: FAIL" in upper or upper.startswith("FAIL"):
            status = "FAIL"
            break

    # Extract findings section
    findings: list[str] = []
    in_findings = False
    for line in lines:
        if "## Findings" in line or "## Issues" in line:
            in_findings = True
            continue
        if in_findings:
            if line.startswith("## "):
                break
            stripped = line.strip().lstrip("- ").strip()
            if stripped and stripped != "(none)" and stripped.lower() != "none":
                findings.append(stripped)

    return status, findings


def _invoke_reviewer(
    config: dict,
    prompt: str,
    timeout: int = 300,
) -> str | None:
    """Invoke a reviewer CLI and return its output, or None on failure."""
    binary = config["binary"]
    if not shutil.which(binary):
        logger.info("Reviewer %s not available (binary %s not found)", config["name"], binary)
        return None

    cmd = config["cmd"] + [prompt]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            cwd=os.path.expanduser("~"),
            env={
                **os.environ,
                "TERM": "dumb",  # prevent interactive prompts
            },
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")

        if result.returncode != 0:
            logger.warning(
                "Reviewer %s exited %d: %s",
                config["name"],
                result.returncode,
                stderr[:500],
            )
            # Still return stdout if it has content — some CLIs exit non-zero but produce output
            if stdout.strip() and len(stdout.strip()) > 100:
                return stdout
            return None

        return stdout
    except subprocess.TimeoutExpired:
        logger.warning("Reviewer %s timed out after %ds", config["name"], timeout)
        return None
    except Exception as e:
        logger.warning("Reviewer %s failed: %s", config["name"], e)
        return None


def run_stage2(
    verdict_path: Path,
    preferred_model: str | None = None,
    stage1_family: str = "anthropic",
    timeout: int = 300,
) -> Stage2Result:
    """Run Stage 2 independent review on an evidence bundle.

    Args:
        verdict_path: Path to verdict.json (bundle_dir is its parent)
        preferred_model: Force a specific model ("codex", "claude", "gemini")
        stage1_family: Model family used for stage 1 (to ensure independence)
        timeout: Max seconds for the reviewer CLI

    Returns:
        Stage2Result with verdict, findings, and review text.
    """
    bundle_dir = verdict_path.parent

    # Read current verdict
    verdict = json.loads(verdict_path.read_text())
    pr_number = verdict.get("pr", "?")
    repo = verdict.get("repo", "?")

    # Read stage1 family from verdict (trusted metadata) — override caller default
    persisted_family = verdict.get("stage1", {}).get("model_family")
    if persisted_family:
        stage1_family = persisted_family

    # Read bundle contents
    claims, artifact_list, artifact_summaries = _read_bundle(bundle_dir)

    # Build prompt — use relative path from docs/ to avoid leaking local paths
    # Bundle dirs follow: <repo_root>/docs/evidence/<repo>/PR-<N>/<timestamp>/
    bundle_str = str(bundle_dir)
    docs_idx = bundle_str.find("docs/evidence/")
    relative_bundle = bundle_str[docs_idx:] if docs_idx >= 0 else str(bundle_dir.name)

    prompt = REVIEW_PROMPT_TEMPLATE.format(
        pr_number=pr_number,
        repo=repo,
        bundle_dir=relative_bundle,
        claims=claims,
        artifact_list=artifact_list,
        artifact_summaries=artifact_summaries,
    )

    # Select reviewer — must be different family than stage 1
    reviewers = REVIEWER_CONFIGS.copy()
    if preferred_model:
        # Put preferred model first
        reviewers.sort(key=lambda r: r["name"] != preferred_model)

    # Filter out same-family reviewers
    eligible = [r for r in reviewers if r["family"] != stage1_family]
    if not eligible:
        # If all filtered out (shouldn't happen), allow any
        eligible = reviewers

    review_text: str | None = None
    used_config: dict | None = None

    for config in eligible:
        logger.info("Trying reviewer: %s (family: %s)", config["name"], config["family"])
        review_text = _invoke_reviewer(config, prompt, timeout=timeout)
        if review_text:
            used_config = config
            break

    if not review_text or not used_config:
        return Stage2Result(
            status="FAIL",
            reviewer_model="none",
            reviewer_family="none",
            findings=["No independent reviewer available — all CLI dispatches failed"],
            review_text="",
            confidence=0.0,
        )

    # Parse verdict from review output
    status, findings = _parse_verdict_from_review(review_text)

    # Write independent_review.md
    (bundle_dir / "independent_review.md").write_text(review_text)

    # Dispatcher writes independence attestation (NOT the reviewer)
    model_family_differs = used_config["family"] != stage1_family
    result = Stage2Result(
        status=status,
        reviewer_model=used_config["name"],
        reviewer_family=used_config["family"],
        findings=findings,
        review_text=review_text,
        confidence=0.95 if status == "PASS" and not findings else 0.7,
    )

    # Update verdict.json — dispatcher controls these fields
    verdict["stage2"] = {
        "status": status,
        "reviewer": "independent",
        "model": f"{used_config['family']}/{used_config['name']}",
        "findings": findings,
        "confidence": result.confidence,
        "independence_verified": True,
        "model_family_differs_from_stage1": model_family_differs,
    }
    verdict["overall"] = "PASS" if (
        verdict.get("stage1", {}).get("status") == "PASS"
        and status == "PASS"
        and model_family_differs
    ) else "FAIL"
    verdict["timestamp"] = datetime.now(timezone.utc).isoformat()

    verdict_path.write_text(json.dumps(verdict, indent=2) + "\n")

    logger.info(
        "Stage 2 complete: %s by %s (family: %s, independence: %s)",
        status,
        used_config["name"],
        used_config["family"],
        model_family_differs,
    )

    return result


def main() -> int:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Run Stage 2 independent evidence review")
    parser.add_argument("verdict_json", help="Path to verdict.json")
    parser.add_argument("--model", choices=["codex", "claude", "gemini"], help="Preferred reviewer model")
    parser.add_argument("--stage1-family", default="anthropic", help="Model family used in stage 1")
    parser.add_argument("--timeout", type=int, default=300, help="Reviewer timeout in seconds")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    verdict_path = Path(args.verdict_json)
    if not verdict_path.exists():
        print(f"Error: {verdict_path} not found", file=sys.stderr)
        return 1

    result = run_stage2(
        verdict_path,
        preferred_model=args.model,
        stage1_family=args.stage1_family,
        timeout=args.timeout,
    )

    print(f"\nStage 2 Verdict: {result.status}")
    print(f"Reviewer: {result.reviewer_model} ({result.reviewer_family})")
    if result.findings:
        print("Findings:")
        for f in result.findings:
            print(f"  - {f}")
    print(f"\nUpdated verdict.json at: {verdict_path}")

    return 0 if result.status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
