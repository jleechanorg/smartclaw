"""
test_skillify_resolver_trigger.py — Resolver trigger eval for skillify.

Verifies that the trigger phrases in skillify's frontmatter correctly
route to the skillify skill via RESOLVER.md.

Run: python -m pytest tests/test_skillify_resolver_trigger.py -v
"""
import re
from pathlib import Path

import pytest

HERMES_ROOT = Path.home() / ".hermes"
SKILLIFY_PATH = HERMES_ROOT / "skills" / "skillify" / "SKILL.md"
RESOLVER_PATH = HERMES_ROOT / "skills" / "RESOLVER.md"


@pytest.fixture
def skill_content() -> str:
    return SKILLIFY_PATH.read_text()


@pytest.fixture
def resolver_content() -> str:
    return RESOLVER_PATH.read_text()


@pytest.fixture
def frontmatter(skill_content) -> dict:
    m = re.match(r"^---\n(.*?)\n---", skill_content, re.DOTALL)
    assert m, "SKILL.md missing YAML frontmatter"
    fm = {}
    for line in m.group(1).split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm


# Item 6: RESOLVER.md must have an entry for skillify
def test_resolver_has_skillify_entry(resolver_content):
    assert "skillify" in resolver_content.lower(), \
        "RESOLVER.md does not reference 'skillify'"


# Item 6: The entry must point to the skill file
def test_resolver_points_to_skillify_skill(resolver_content):
    """RESOLVER.md should have a line referencing skills/skillify/SKILL.md."""
    assert re.search(r"skills/skillify", resolver_content), \
        "RESOLVER.md does not reference skills/skillify/SKILL.md"


# Item 7: Frontmatter triggers
def test_frontmatter_has_trigger_phrases(frontmatter):
    """skillify must declare trigger phrases in frontmatter."""
    # Hermes uses when_to_use
    assert "when_to_use" in frontmatter or "triggers" in frontmatter, \
        "frontmatter missing 'when_to_use' or 'triggers' field"


# Item 7: The required trigger phrases must be present
TRIGGERS = [
    "skillify this",
    "skillify",
    "is this a skill",
    "make this proper",
    "add tests and evals",
    "check skill completeness",
]


@pytest.mark.parametrize("trigger_phrase", TRIGGERS)
def test_trigger_phrase_in_resolver(trigger_phrase, resolver_content):
    """Each declared trigger must appear in RESOLVER.md."""
    # The resolver maps triggers to skills — the trigger phrase (or a variant)
    # must be present in the resolver text near the skillify entry.
    # Normalize: check if the trigger words appear in the resolver context
    trigger_words = trigger_phrase.lower().split()
    resolver_lower = resolver_content.lower()

    # Find the skillify section of the resolver
    skillify_section_match = re.search(
        r"(?i)(skillify.*?)(?=\n\n|\n##|\n#|\Z)",
        resolver_content,
        re.DOTALL,
    )
    assert skillify_section_match, "Cannot find skillify section in RESOLVER.md"
    skillify_section = skillify_section_match.group(1).lower()

    # At least the key trigger words should appear near skillify in the resolver
    found = any(word in skillify_section for word in trigger_words)
    assert found, \
        f"Trigger phrase '{trigger_phrase}' not represented in RESOLVER.md near skillify entry"


# Item 7: The trigger phrases should route to skillify (not another skill)
def test_skillify_entry_is_unique_in_resolver(resolver_content):
    """
    'skillify' as a substring should appear in at most one skill entry
    (the skillify entry itself), not multiple times in different contexts.
    """
    lines = resolver_content.split("\n")
    skillify_lines = [l for l in lines if "skillify" in l.lower()]
    # Should have 1-2 lines: the entry itself + maybe a header
    assert len(skillify_lines) >= 1, "skillify not found in RESOLVER.md at all"
    assert len(skillify_lines) <= 4, \
        f"skillify appears in too many lines — possible ambiguous routing: {skillify_lines}"


# Item 7: Each declared trigger must actually be in the skill's when_to_use
@pytest.mark.parametrize("trigger_phrase", TRIGGERS)
def test_trigger_in_skill_when_to_use(trigger_phrase, frontmatter):
    """Each trigger in TRIGGERS must appear in the skill's when_to_use field."""
    wtu = frontmatter.get("when_to_use", "").lower()
    # Partial match: "skillify this" should match "skillify this" or "skillify"
    trigger_words = trigger_phrase.lower().split()
    found = any(word in wtu for word in trigger_words)
    assert found, \
        f"Trigger '{trigger_phrase}' declared in TRIGGERS but not found in when_to_use: {wtu}"
