"""Tests for AGENT_BASE_RULES.md content."""

import pytest
from pathlib import Path


def test_agent_base_rules_has_mcp_inbox_protocol() -> None:
    """Verify AGENT_BASE_RULES.md contains MCP mail inbox protocol."""
    rules_path = Path(__file__).parent.parent.parent / "workspace" / "AGENT_BASE_RULES.md"
    assert rules_path.exists(), f"AGENT_BASE_RULES.md not found at {rules_path}"
    
    content = rules_path.read_text()
    
    # Check for inbox protocol section
    assert "MCP Mail Inbox Protocol" in content, "Missing MCP Mail Inbox Protocol section"
    assert "fetch_inbox" in content, "Missing fetch_inbox call"
    assert "guidance" in content, "Missing guidance handling"
    assert "abort" in content, "Missing abort handling"


def test_agent_base_rules_has_mcp_send_protocol() -> None:
    """Verify AGENT_BASE_RULES.md contains MCP mail send protocol."""
    rules_path = Path(__file__).parent.parent.parent / "workspace" / "AGENT_BASE_RULES.md"
    content = rules_path.read_text()
    
    # Check for send protocol
    assert "send_message" in content, "Missing send_message protocol"
    assert "sender_name" in content, "Missing sender_name rule"


def test_agent_base_rules_has_green_definition() -> None:
    """Verify AGENT_BASE_RULES.md contains green/merge-ready definition."""
    rules_path = Path(__file__).parent.parent.parent / "workspace" / "AGENT_BASE_RULES.md"
    content = rules_path.read_text()
    
    # Check for green definition
    assert "MERGE-READY" in content or "merge-ready" in content, "Missing merge-ready definition"


def test_agent_base_rules_has_reply_before_resolve() -> None:
    """Verify AGENT_BASE_RULES.md contains reply-before-resolve protocol."""
    rules_path = Path(__file__).parent.parent.parent / "workspace" / "AGENT_BASE_RULES.md"
    content = rules_path.read_text()
    
    # Check for reply-before-resolve
    assert "Reply-Before-Resolve" in content or "reply to the thread" in content.lower(), "Missing reply-before-resolve protocol"
