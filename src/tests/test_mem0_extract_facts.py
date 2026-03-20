"""Unit tests for mem0_extract_facts — project_filter validation.

ORCH-lpcn: path traversal and dot-segment bypass protection.
Tests import and exercise the production validate_project_filter() directly.
"""
from __future__ import annotations

import pytest

from scripts.mem0_extract_facts import validate_project_filter


class TestProjectFilterValidation:
    """Test path traversal rejection via production validate_project_filter."""

    def test_valid_hash_accepted(self) -> None:
        validate_project_filter("-Users-jleechan--openclaw")  # should not raise

    def test_valid_simple_name_accepted(self) -> None:
        validate_project_filter("myproject")

    def test_none_accepted(self) -> None:
        validate_project_filter(None)  # None means no filter

    def test_double_dot_rejected(self) -> None:
        with pytest.raises(ValueError, match="simple name"):
            validate_project_filter("../evil")

    def test_slash_rejected(self) -> None:
        with pytest.raises(ValueError, match="simple name"):
            validate_project_filter("foo/bar")

    def test_backslash_rejected(self) -> None:
        with pytest.raises(ValueError, match="simple name"):
            validate_project_filter("foo\\bar")

    def test_single_dot_rejected(self) -> None:
        with pytest.raises(ValueError, match="simple name"):
            validate_project_filter(".")

    def test_dot_slash_prefix_rejected(self) -> None:
        with pytest.raises(ValueError, match="simple name"):
            validate_project_filter("./evil")

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="simple name"):
            validate_project_filter("")
