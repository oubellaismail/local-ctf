"""
Unit tests for flag derivation.
"""

import os
import re
import pytest
from flag import derive_flag


class TestFlagDerivation:
    """Test flag derivation functionality."""

    def test_flag_format(self, monkeypatch):
        """Test that derived flags match expected format."""
        monkeypatch.setenv("CTF_MASTER_SECRET", "test-secret")

        flag = derive_flag("3", "web-sqli")
        assert re.match(r"^infodays\{[0-9a-f]{32}\}$", flag), f"Flag format invalid: {flag}"

    def test_different_teams_different_flags(self, monkeypatch):
        """Test that different team IDs produce different flags."""
        monkeypatch.setenv("CTF_MASTER_SECRET", "test-secret")

        flag1 = derive_flag("1", "web-sqli")
        flag2 = derive_flag("2", "web-sqli")

        assert flag1 != flag2, "Different teams should produce different flags"

    def test_different_challenges_different_flags(self, monkeypatch):
        """Test that different challenge IDs produce different flags."""
        monkeypatch.setenv("CTF_MASTER_SECRET", "test-secret")

        flag1 = derive_flag("3", "web-sqli")
        flag2 = derive_flag("3", "pwn-bof")

        assert flag1 != flag2, "Different challenges should produce different flags"

    def test_same_inputs_same_flag(self, monkeypatch):
        """Test that same inputs always produce the same flag."""
        monkeypatch.setenv("CTF_MASTER_SECRET", "test-secret")

        flag1 = derive_flag("42", "misc-riddle")
        flag2 = derive_flag("42", "misc-riddle")

        assert flag1 == flag2, "Same inputs should produce same flag"

    def test_missing_secret_raises_error(self, monkeypatch):
        """Test that missing CTF_MASTER_SECRET raises KeyError."""
        monkeypatch.delenv("CTF_MASTER_SECRET", raising=False)

        with pytest.raises(KeyError):
            derive_flag("1", "web-sqli")

    def test_flag_content(self, monkeypatch):
        """Test that flag contains expected prefix."""
        monkeypatch.setenv("CTF_MASTER_SECRET", "test-secret")

        flag = derive_flag("99", "test-challenge")
        assert flag.startswith("infodays{"), "Flag should start with 'infodays{'"
        assert flag.endswith("}"), "Flag should end with '}'"
