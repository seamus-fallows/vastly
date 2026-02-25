"""Tests for vastly.cli -- prerequisites, repo info, and argument parsing."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from vastly.cli import _check_prerequisites, _local_repo_info, main


class TestCheckPrerequisites:
    def test_all_tools_present(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
        monkeypatch.setattr("vastly.cli.check_ide", lambda x: True)
        assert _check_prerequisites(need_ide=True, ide="code") is True

    def test_missing_vastai(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "shutil.which", lambda x: None if x == "vastai" else f"/usr/bin/{x}"
        )
        monkeypatch.setattr("vastly.cli.check_ide", lambda x: True)
        assert _check_prerequisites(need_ide=True, ide="code") is False
        assert "vastai" in capsys.readouterr().out

    def test_missing_git(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "shutil.which", lambda x: None if x == "git" else f"/usr/bin/{x}"
        )
        monkeypatch.setattr("vastly.cli.check_ide", lambda x: True)
        assert _check_prerequisites(need_ide=True, ide="code") is False
        out = capsys.readouterr().out
        assert "git" in out
        assert "git-scm.com" in out

    def test_missing_ssh(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "shutil.which", lambda x: None if x == "ssh" else f"/usr/bin/{x}"
        )
        monkeypatch.setattr("vastly.cli.check_ide", lambda x: True)
        assert _check_prerequisites(need_ide=True, ide="code") is False
        assert "ssh" in capsys.readouterr().out.lower()

    def test_missing_ide_when_needed(self, monkeypatch, capsys):
        monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
        monkeypatch.setattr("vastly.cli.check_ide", lambda x: False)
        assert _check_prerequisites(need_ide=True, ide="code") is False
        assert "code" in capsys.readouterr().out

    def test_skips_ide_check_when_not_needed(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
        # check_ide returns False, but need_ide=False so it's never called
        monkeypatch.setattr("vastly.cli.check_ide", lambda x: False)
        assert _check_prerequisites(need_ide=False, ide="code") is True

    def test_reports_all_missing_tools_at_once(self, monkeypatch, capsys):
        """No short-circuit -- all missing tools are reported in one pass."""
        monkeypatch.setattr("shutil.which", lambda x: None)
        monkeypatch.setattr("vastly.cli.check_ide", lambda x: False)
        _check_prerequisites(need_ide=True, ide="code")
        out = capsys.readouterr().out
        assert "vastai" in out
        assert "git" in out
        assert "ssh" in out.lower()
        assert "code" in out

    def test_known_ide_includes_download_url(self, monkeypatch, capsys):
        monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
        monkeypatch.setattr("vastly.cli.check_ide", lambda x: False)
        _check_prerequisites(need_ide=True, ide="cursor")
        assert "cursor.com" in capsys.readouterr().out

    def test_unknown_ide_omits_url(self, monkeypatch, capsys):
        monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
        monkeypatch.setattr("vastly.cli.check_ide", lambda x: False)
        _check_prerequisites(need_ide=True, ide="zed")
        out = capsys.readouterr().out
        assert "zed" in out
        assert "Download from" not in out


class TestLocalRepoInfo:
    def test_returns_ssh_url_and_repo_name(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0], 0, stdout="https://github.com/user/my-project.git\n", stderr=""
            ),
        )
        result = _local_repo_info("origin")
        assert result == ("git@github.com:user/my-project.git", "my-project")

    def test_returns_none_when_not_in_git_repo(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0], 128, stdout="", stderr="fatal: not a git repository"
            ),
        )
        assert _local_repo_info("origin") is None

    def test_prints_error_for_non_repo_stderr(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0], 1, stdout="", stderr="fatal: No such remote 'upstream'"
            ),
        )
        assert _local_repo_info("upstream") is None
        assert "No such remote" in capsys.readouterr().out

    def test_suppresses_not_a_git_repo_message(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0],
                128,
                stdout="",
                stderr="fatal: Not a git repository (or any parent)",
            ),
        )
        _local_repo_info("origin")
        assert capsys.readouterr().out == ""

    def test_returns_none_on_empty_stdout(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""),
        )
        assert _local_repo_info("origin") is None

    def test_returns_none_when_git_binary_missing(self, monkeypatch):
        def raise_fnf(*a, **kw):
            raise FileNotFoundError

        monkeypatch.setattr("subprocess.run", raise_fnf)
        assert _local_repo_info("origin") is None

    def test_preserves_already_ssh_url(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0], 0, stdout="git@github.com:user/repo.git\n", stderr=""
            ),
        )
        url, name = _local_repo_info("origin")
        assert url == "git@github.com:user/repo.git"
        assert name == "repo"

    def test_passes_configured_remote_name(self, monkeypatch):
        calls = []

        def capture(*a, **kw):
            calls.append(a[0])
            return subprocess.CompletedProcess(
                a[0], 0, stdout="git@github.com:u/r.git\n", stderr=""
            )

        monkeypatch.setattr("subprocess.run", capture)
        _local_repo_info("upstream")
        assert calls[0] == ["git", "remote", "get-url", "upstream"]


class TestMainArgParsing:
    def test_default_args(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["vst"])
        with patch("vastly.cli._connect") as mock_connect:
            main()
        mock_connect.assert_called_once_with(
            None, no_setup=False, force_setup=False, list_only=False
        )

    def test_positional_name(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["vst", "my-instance"])
        with patch("vastly.cli._connect") as mock_connect:
            main()
        mock_connect.assert_called_once_with(
            "my-instance", no_setup=False, force_setup=False, list_only=False
        )

    def test_list_flag(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["vst", "--list"])
        with patch("vastly.cli._connect") as mock_connect:
            main()
        mock_connect.assert_called_once_with(
            None, no_setup=False, force_setup=False, list_only=True
        )

    def test_no_setup_flag(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["vst", "--no-setup"])
        with patch("vastly.cli._connect") as mock_connect:
            main()
        mock_connect.assert_called_once_with(
            None, no_setup=True, force_setup=False, list_only=False
        )

    def test_force_setup_flag(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["vst", "--force-setup"])
        with patch("vastly.cli._connect") as mock_connect:
            main()
        mock_connect.assert_called_once_with(
            None, no_setup=False, force_setup=True, list_only=False
        )

    def test_no_setup_and_force_setup_are_mutually_exclusive(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["vst", "--no-setup", "--force-setup"])
        with pytest.raises(SystemExit):
            main()

    def test_version_flag(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["vst", "--version"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        assert "vst" in capsys.readouterr().out
