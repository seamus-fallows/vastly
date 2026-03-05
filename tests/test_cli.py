"""Tests for vastly CLI -- prerequisites, repo info, and argument parsing."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from vastly.cli import main
from vastly.commands import _check_prerequisites, _local_repo_info
from vastly.errors import VastlyError


class TestCheckPrerequisites:
    def test_all_tools_present(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
        monkeypatch.setattr("vastly.commands.check_ide", lambda x: True)
        _check_prerequisites(need_ide=True, ide="code")  # should not raise

    def test_missing_vastai(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which", lambda x: None if x == "vastai" else f"/usr/bin/{x}"
        )
        monkeypatch.setattr("vastly.commands.check_ide", lambda x: True)
        with pytest.raises(VastlyError, match="vastai"):
            _check_prerequisites(need_ide=True, ide="code")

    def test_missing_git(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which", lambda x: None if x == "git" else f"/usr/bin/{x}"
        )
        monkeypatch.setattr("vastly.commands.check_ide", lambda x: True)
        with pytest.raises(VastlyError, match="git"):
            _check_prerequisites(need_ide=True, ide="code")

    def test_missing_ssh(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which", lambda x: None if x == "ssh" else f"/usr/bin/{x}"
        )
        monkeypatch.setattr("vastly.commands.check_ide", lambda x: True)
        with pytest.raises(VastlyError, match="(?i)ssh"):
            _check_prerequisites(need_ide=True, ide="code")

    def test_missing_ide_when_needed(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
        monkeypatch.setattr("vastly.commands.check_ide", lambda x: False)
        with pytest.raises(VastlyError, match="code"):
            _check_prerequisites(need_ide=True, ide="code")

    def test_skips_ide_check_when_not_needed(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
        # check_ide returns False, but need_ide=False so it's never called
        monkeypatch.setattr("vastly.commands.check_ide", lambda x: False)
        _check_prerequisites(need_ide=False, ide="code")  # should not raise

    def test_reports_all_missing_tools_at_once(self, monkeypatch):
        """No short-circuit -- all missing tools are reported in one pass."""
        monkeypatch.setattr("shutil.which", lambda x: None)
        monkeypatch.setattr("vastly.commands.check_ide", lambda x: False)
        with pytest.raises(VastlyError) as exc_info:
            _check_prerequisites(need_ide=True, ide="code")
        msg = str(exc_info.value)
        assert "vastai" in msg
        assert "git" in msg
        assert "ssh" in msg.lower()
        assert "code" in msg

    def test_falls_back_to_other_ide(self, monkeypatch):
        """If configured IDE is missing but the other is installed, fall back."""
        monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
        monkeypatch.setattr(
            "vastly.commands.check_ide", lambda x: x == "cursor"
        )
        result = _check_prerequisites(need_ide=True, ide="code")
        assert result == "cursor"

    def test_falls_back_preserves_configured_when_found(self, monkeypatch):
        """If configured IDE is installed, return it unchanged."""
        monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
        monkeypatch.setattr("vastly.commands.check_ide", lambda x: True)
        result = _check_prerequisites(need_ide=True, ide="code")
        assert result == "code"

    def test_unknown_ide_missing_raises(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
        monkeypatch.setattr("vastly.commands.check_ide", lambda x: False)
        with pytest.raises(VastlyError, match="zed"):
            _check_prerequisites(need_ide=True, ide="zed")


class TestLocalRepoInfo:
    def test_returns_https_url_and_repo_name(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0], 0, stdout="https://github.com/user/my-project.git\n", stderr=""
            ),
        )
        result = _local_repo_info("origin")
        assert result == ("https://github.com/user/my-project.git", "my-project")

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
        assert "No such remote" in capsys.readouterr().err

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
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

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
    """Test the subcommand argument parsing in main()."""

    def test_bare_vst_dispatches_to_connect(self):
        with patch("vastly.cli.cmd_connect") as mock:
            main(argv=[])
        args = mock.call_args[0][0]
        assert args.command == "connect"
        assert args.name is None

    def test_unknown_name_becomes_connect(self):
        with patch("vastly.cli.cmd_connect") as mock:
            main(argv=["my-instance"])
        args = mock.call_args[0][0]
        assert args.command == "connect"
        assert args.name == "my-instance"

    def test_list_subcommand(self):
        with patch("vastly.cli.cmd_list") as mock:
            main(argv=["list"])
        mock.assert_called_once()

    def test_no_setup_flag(self):
        with patch("vastly.cli.cmd_connect") as mock:
            main(argv=["-n"])
        args = mock.call_args[0][0]
        assert args.no_setup is True

    def test_force_setup_flag(self):
        with patch("vastly.cli.cmd_connect") as mock:
            main(argv=["-f"])
        args = mock.call_args[0][0]
        assert args.force_setup is True

    def test_no_setup_and_force_setup_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            main(argv=["-n", "-f"])

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(argv=["--version"])
        assert exc_info.value.code == 0
        assert "vastly" in capsys.readouterr().out

    def test_verbose_flag_sets_global(self, monkeypatch):
        import vastly

        monkeypatch.setattr(vastly, "VERBOSE", False)
        with patch("vastly.cli.cmd_list"):
            main(argv=["-v", "list"])
        assert vastly.VERBOSE is True
