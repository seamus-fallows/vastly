"""Tests for vastly.__init__ -- version, color functions."""

from __future__ import annotations

import re

import pytest

from vastly import __version__, cyan, dim, green, red, yellow


class TestColorFunctions:
    def test_identity_when_color_disabled(self, monkeypatch):
        monkeypatch.setattr("vastly._COLOR", False)
        assert red("test") == "test"
        assert green("test") == "test"
        assert yellow("test") == "test"
        assert cyan("test") == "test"
        assert dim("test") == "test"

    def test_ansi_codes_when_color_enabled(self, monkeypatch):
        monkeypatch.setattr("vastly._COLOR", True)
        assert red("test") == "\033[31mtest\033[0m"
        assert green("test") == "\033[32mtest\033[0m"
        assert yellow("test") == "\033[33mtest\033[0m"
        assert cyan("test") == "\033[36mtest\033[0m"
        assert dim("test") == "\033[90mtest\033[0m"

    def test_handles_non_string_input(self, monkeypatch):
        monkeypatch.setattr("vastly._COLOR", False)
        assert red(42) == 42
        monkeypatch.setattr("vastly._COLOR", True)
        assert "42" in red(42)


class TestModuleVersion:
    def test_version_is_semver(self):
        assert re.match(r"^\d+\.\d+\.\d+$", __version__)
