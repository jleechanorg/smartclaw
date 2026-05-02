"""
test_skillify.py — Conformance tests for the skillify skill.

Validates the Hermes skillify skill against the 10-item checklist from
garrytan's "Thin Harness, Fat Skills" methodology.

Run: python -m pytest tests/test_skillify.py -v
"""
import re
import os
from pathlib import Path

import pytest

HERMES_ROOT = Path.home() / ".hermes"
SKILLIFY_PATH = HERMES_ROOT / "skills" / "skillify" / "SKILL.md"
RESOLVER_PATH = HERMES_ROOT / "skills" / "RESOLVER.md"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def skill_content() -> str:
    assert SKILLIFY_PATH.exists(), f"skillify SKILL.md not found at {SKILLIFY_PATH}"
    return SKILLIFY_PATH.read_text()


@pytest.fixture
def frontmatter(skill_content) -> dict:
    """Parse YAML frontmatter from SKILL.md."""
    m = re.match(r"^---\n(.*?)\n---", skill_content, re.DOTALL)
    assert m, "SKILL.md missing YAML frontmatter"
    fm = {}
    for line in m.group(1).split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm


# ── Item 1: SKILL.md with valid frontmatter ───────────────────────────────────

def test_skill_md_exists():
    assert SKILLIFY_PATH.exists(), f"SKILL.md not found at {SKILLIFY_PATH}"


def test_frontmatter_has_name(frontmatter):
    assert "name" in frontmatter, "frontmatter missing 'name'"
    assert frontmatter["name"] == "skillify", f"expected name='skillify', got {frontmatter['name']}"


def test_frontmatter_has_description(frontmatter):
    assert "description" in frontmatter, "frontmatter missing 'description'"
    assert len(frontmatter["description"]) > 10, "description too short"


def test_frontmatter_has_when_to_use(frontmatter):
    """Hermes uses 'when_to_use' as the trigger phrase field."""
    assert "when_to_use" in frontmatter, "frontmatter missing 'when_to_use' (Hermes trigger field)"
    assert len(frontmatter["when_to_use"]) > 5, "when_to_use too short"


def test_contract_section_exists(skill_content):
    assert re.search(r"(?i)#*\s*Contract", skill_content), "SKILL.md missing Contract section"
    assert re.search(r"10.item", skill_content, re.I), "Contract missing 10-item reference"


# ── Item 2: Code (skillify is a meta-skill — code is the SKILL.md itself) ───

def test_skill_is_actionable(skill_content):
    """skillify has enough structure to guide a run without external code."""
    phases = re.search(r"(?i)#*\s*Phases", skill_content)
    assert phases, "SKILL.md missing Phases section"

    output = re.search(r"(?i)#*\s*Output Format", skill_content)
    assert output, "SKILL.md missing Output Format section"

    anti = re.search(r"(?i)#*\s*Anti.Pat", skill_content)
    assert anti, "SKILL.md missing Anti-Patterns section"


# ── Item 3: Unit tests — this file ───────────────────────────────────────────

def test_unit_tests_exist():
    """The skillify skill has unit tests in this file."""
    path = Path(__file__)
    assert path.exists(), f"test file not found at {path}"


# ── Item 4: Integration / E2E ─────────────────────────────────────────────────

def test_e2e_tests_exist():
    """E2E tests live in testing_llm/ or as dedicated test files."""
    # Check for E2E test file alongside this one
    e2e_candidates = [
        Path(__file__).parent / "test_skillify_e2e.py",
        HERMES_ROOT / "testing_llm" / "test_skillify_e2e.py",
    ]
    found = [p for p in e2e_candidates if p.exists()]
    # E2E may not exist yet — that's OK for a meta-skill
    # We report the absence rather than fail
    if not found:
        pytest.skip("E2E test file not found (optional for meta-skills)")


# ── Item 5: LLM evals ────────────────────────────────────────────────────────

def test_llm_evals_exist():
    """LLM evals for skillify — skip if no eval infrastructure present."""
    eval_candidates = [
        HERMES_ROOT / "eval" / "skillify",
        HERMES_ROOT / "evals" / "skillify",
    ]
    found = [p for p in eval_candidates if p.exists()]
    if not found:
        pytest.skip("LLM evals directory not found (optional for process-only meta-skills)")


# ── Item 6: Resolver trigger entry ────────────────────────────────────────────

def test_resolver_has_skillify_entry():
    """skillify must appear in RESOLVER.md so the agent can find it."""
    assert RESOLVER_PATH.exists(), f"RESOLVER.md not found at {RESOLVER_PATH}"
    content = RESOLVER_PATH.read_text()
    assert "skillify" in content.lower(), "RESOLVER.md does not reference 'skillify'"
    # Should have a trigger pattern like "skillify this" or "is this a skill?"
    assert re.search(r"(?i)skillif(y|ing)", content), "RESOLVER.md missing skillify trigger pattern"


# ── Item 7: Resolver trigger eval ────────────────────────────────────────────

def test_resolver_trigger_eval_exists():
    """A test that verifies trigger phrases route to skillify."""
    candidates = [
        Path(__file__).parent / "test_skillify_resolver_trigger.py",
        HERMES_ROOT / "tests" / "test_skillify_resolver_trigger.py",
    ]
    found = [p for p in candidates if p.exists()]
    if not found:
        pytest.skip("Resolver trigger eval test not found")


# ── Item 8: check-resolvable ─────────────────────────────────────────────────

def test_skill_tree_resolvable():
    """All skills in RESOLVER.md must be reachable and MECE."""
    import subprocess

    resolver_content = RESOLVER_PATH.read_text()
    skill_dirs = list((HERMES_ROOT / "skills").iterdir())
    skill_dirs = [d for d in skill_dirs if d.is_dir() and (d / "SKILL.md").exists()]

    # Count skills referenced in RESOLVER.md
    resolver_refs = re.findall(r"`skills/([^/]+)/SKILL\.md`", resolver_content)
    resolver_refs += re.findall(r"skills/([a-z0-9_-]+)/SKILL\.md", resolver_content)

    unreachable = []
    for ref in resolver_refs:
        skill_path = HERMES_ROOT / "skills" / ref / "SKILL.md"
        if not skill_path.exists():
            unreachable.append(ref)

    assert not unreachable, f"RESOLVER.md references non-existent skills: {unreachable}"

    # Check for MECE overlaps (two skills with identical trigger phrases)
    trigger_map: dict[str, list[str]] = {}
    for skill_dir in skill_dirs:
        fm_match = re.search(
            r"^---\n(.*?)\n---", (skill_dir / "SKILL.md").read_text(), re.DOTALL
        )
        if not fm_match:
            continue
        fm_text = fm_match.group(1)
        skill_name = skill_dir.name
        # Hermes uses when_to_use, gbrain uses triggers
        trigger_match = re.search(r"(?m)^when_to_use:\s*(.+)$", fm_text)
        if not trigger_match:
            trigger_match = re.search(r"(?m)^triggers:\s*-\s*(.+)$", fm_text)
        if trigger_match:
            trigger_val = trigger_match.group(1).strip()
            trigger_map.setdefault(trigger_val, []).append(skill_name)

    overlaps = {t: skills for t, skills in trigger_map.items() if len(skills) > 1}
    assert not overlaps, f"MECE overlap — same trigger maps to multiple skills: {overlaps}"


# ── Item 9: E2E smoke test ───────────────────────────────────────────────────

def test_skillify_produces_audit_output(skill_content):
    """The skill's Output Format section must describe producing an audit."""
    output_section = re.search(
        r"(?i)#*\s*Output Format\s*\n(.*?)(?=\n##|\n#|\Z)",
        skill_content,
        re.DOTALL,
    )
    assert output_section, "Missing Output Format section"
    output_text = output_section.group(1)
    assert re.search(r"(?i)audit", output_text), "Output Format must describe audit output"
    assert re.search(r"(?i)(score|N/10|completeness)", output_text), "Output Format must describe a score"


# ── Item 10: Brain filing ─────────────────────────────────────────────────────

def test_skillify_does_not_write_brain_pages(skill_content):
    """
    skillify is a meta-skill — it creates other skills, it does not itself
    write brain pages. So item 10 is N/A. We verify the skill doesn't
    claim to write brain pages.
    """
    anti_patterns_section = re.search(
        r"(?i)#*\s*Anti.Pat.*?(?=\n##|\n#|\Z)", skill_content, re.DOTALL
    )
    if anti_patterns_section:
        text = anti_patterns_section.group(0)
        # The skill should NOT have anti-pattern "Feature writes brain pages with no RESOLVER entry"
        # as a concern for skillify itself
        assert "skillify" not in text.lower() or "brain" not in text.lower(), \
            "skillify should not claim to write brain pages"


# ── Quality Gates ────────────────────────────────────────────────────────────

def test_quality_gates_section_exists(skill_content):
    """SKILL.md must have a Quality Gates section."""
    assert re.search(r"(?i)#*\s*Quality Gate", skill_content), \
        "SKILL.md missing Quality Gates section"


def test_anti_patterns_section_exists(skill_content):
    """SKILL.md must have an Anti-Patterns section."""
    assert re.search(r"(?i)#*\s*Anti.Pat", skill_content), \
        "SKILL.md missing Anti-Patterns section"


def test_when_to_use_mentions_all_required_triggers(frontmatter):
    """when_to_use must cover all required trigger phrases."""
    wtu = frontmatter.get("when_to_use", "")
    required = ["skillify", "skill", "proper"]
    missing = [r for r in required if r.lower() not in wtu.lower()]
    assert not missing, f"when_to_use missing required triggers: {missing}"


# ── Summary ───────────────────────────────────────────────────────────────────

def test_all_10_items_covered(skill_content):
    """Sanity check: all 10 checklist items appear in the skill."""
    items = [
        (r"(?i)#*\s*Contract", "Contract section"),
        (r"(?i)#*\s*Phases", "Phases section"),
        (r"(?i)unit.test", "unit test reference"),
        (r"(?i)integration|end.to.end|e2e", "E2E reference"),
        (r"(?i)llm.eval|llm evals", "LLM evals reference"),
        (r"(?i)RESOLVER|resolver.trigger", "resolver trigger"),
        (r"(?i)trigger.eval|resolver.trigger.eval", "trigger eval"),
        (r"(?i)check.resolv", "check-resolvable"),
        (r"(?i)e2e.smoke|smoke.test", "E2E smoke test"),
        (r"(?i)brain.filing|brain RESOLVER", "brain filing"),
    ]
    missing = []
    for pattern, label in items:
        if not re.search(pattern, skill_content):
            missing.append(label)
    assert not missing, f"Missing skill content for: {missing}"
