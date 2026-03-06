"""Tests for vastly.ide -- IDE launch."""

from __future__ import annotations

import sys
from unittest.mock import patch

from vastly.ide import open_ide


class TestOpenIde:
    def test_launches_with_correct_args(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        with patch("subprocess.Popen") as mock_popen:
            open_ide("code", "my-instance", "/workspace/project")
            cmd = mock_popen.call_args[0][0]
            assert cmd == [
                "code",
                "--remote",
                "ssh-remote+my-instance",
                "/workspace/project",
            ]

    def test_shell_true_on_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        with patch("subprocess.Popen") as mock_popen:
            open_ide("code", "host", "/path")
            # Must pass a string (not a list) via list2cmdline for proper quoting
            cmd_arg = mock_popen.call_args[0][0]
            assert isinstance(cmd_arg, str)
            assert mock_popen.call_args[1]["shell"] is True

    def test_shell_false_on_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        with patch("subprocess.Popen") as mock_popen:
            open_ide("code", "host", "/path")
            # On Linux, Popen is called with a list (no shell), not a string
            cmd_arg = mock_popen.call_args[0][0]
            assert isinstance(cmd_arg, list)
            assert "shell" not in mock_popen.call_args[1]
