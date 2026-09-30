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

    def test_vastai_cli_not_needed(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which", lambda x: None if x == "vastai" else f"/usr/bin/{x}"
        )
        monkeypatch.setattr("vastly.commands.check_ide", lambda x: True)
        _check_prerequisites(need_ide=True, ide="code")  # should not raise

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
        assert "git" in msg
        assert "ssh" in msg.lower()
        assert "code" in msg

    def test_falls_back_to_other_ide(self, monkeypatch):
        """If configured IDE is missing but the other is installed, fall back."""
        monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
        monkeypatch.setattr("vastly.commands.check_ide", lambda x: x == "cursor")
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
            lambda *a, **_kw: subprocess.CompletedProcess(
                a[0], 0, stdout="https://github.com/user/my-project.git\n", stderr=""
            ),
        )
        result = _local_repo_info("origin")
        assert result == ("https://github.com/user/my-project.git", "my-project")

    def test_returns_none_when_not_in_git_repo(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **_kw: subprocess.CompletedProcess(
                a[0], 128, stdout="", stderr="fatal: not a git repository"
            ),
        )
        assert _local_repo_info("origin") is None

    def test_prints_error_for_non_repo_stderr(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **_kw: subprocess.CompletedProcess(
                a[0], 1, stdout="", stderr="fatal: No such remote 'upstream'"
            ),
        )
        assert _local_repo_info("upstream") is None
        assert "No such remote" in capsys.readouterr().err

    def test_suppresses_not_a_git_repo_message(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **_kw: subprocess.CompletedProcess(
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
            lambda *a, **_kw: subprocess.CompletedProcess(
                a[0], 0, stdout="", stderr=""
            ),
        )
        assert _local_repo_info("origin") is None

    def test_returns_none_when_git_binary_missing(self, monkeypatch):
        def raise_fnf(*_a, **_kw):
            raise FileNotFoundError

        monkeypatch.setattr("subprocess.run", raise_fnf)
        assert _local_repo_info("origin") is None

    def test_preserves_already_ssh_url(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **_kw: subprocess.CompletedProcess(
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


class TestGitAuthFlag:
    """`--git-auth MODE` overrides gitAuth for one connect, before or after the name."""

    @pytest.fixture(autouse=True)
    def _isolate_config(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("vastly.config.CONFIG_PATH", cfg)

    @pytest.mark.parametrize(
        "argv, name, mode",
        [
            (["--git-auth", "agent"], None, "agent"),
            (["--git-auth=auto"], None, "auto"),
            (["my-gpu", "--git-auth", "deploy-key"], "my-gpu", "deploy-key"),
            (["--git-auth", "agent", "my-gpu"], "my-gpu", "agent"),
            (["-f", "--git-auth", "agent", "connect", "my-gpu"], "my-gpu", "agent"),
            (["my-gpu"], "my-gpu", None),
        ],
    )
    def test_parses(self, argv, name, mode):
        with patch("vastly.cli.cmd_connect") as mock:
            main(argv=argv)
        args = mock.call_args[0][0]
        assert args.name == name
        assert getattr(args, "git_auth", None) == mode

    def test_rejects_unknown_mode(self):
        with patch("vastly.cli.cmd_connect") as mock:
            with pytest.raises(SystemExit) as exc_info:
                main(argv=["--git-auth", "token"])
        assert exc_info.value.code == 2
        mock.assert_not_called()


class TestFlagEdgeCases:
    @pytest.fixture(autouse=True)
    def _existing_config(self):
        from vastly import config

        config.CONFIG_PATH.write_text("{}", encoding="utf-8")

    @pytest.mark.parametrize(
        "argv", [["-f", "connect", "-n"], ["-n", "my-gpu", "-f"], ["-f", "-n"]]
    )
    def test_force_and_no_setup_rejected(self, argv, capsys):
        with patch("vastly.cli.cmd_connect") as mock:
            with pytest.raises(SystemExit) as exc_info:
                main(argv=argv)
        assert exc_info.value.code == 2
        mock.assert_not_called()
        # Our check, or argparse's own when both flags are top-level
        err = capsys.readouterr().err
        assert "can't be used together" in err or "not allowed with" in err

    @pytest.mark.parametrize(
        "argv, mode",
        [
            (["start", "--git-auth", "agent"], "agent"),
            (["--git-auth", "auto", "start"], "auto"),
            (["start", "train"], None),
        ],
    )
    def test_start_accepts_git_auth(self, argv, mode):
        with patch("vastly.cli.cmd_start") as mock:
            main(argv=argv)
        assert getattr(mock.call_args[0][0], "git_auth", None) == mode

    def test_config_creation_error_is_explained(self, monkeypatch, capsys):
        def fail():
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("vastly.cli.ensure_config", fail)
        with pytest.raises(SystemExit) as exc_info:
            main(argv=["list"])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "can't create" in err and "Permission denied" in err


class TestSshHelpPassthrough:
    """`vst ssh` passes -h/--help after the first positional to the remote command."""

    @pytest.fixture(autouse=True)
    def _isolate_config(self, tmp_path, monkeypatch):
        # main() calls ensure_config(); an existing file keeps it off ~/.vastly
        cfg = tmp_path / "config.json"
        cfg.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("vastly.config.CONFIG_PATH", cfg)

    def test_help_flag_in_remote_cmd_passes_through(self):
        with patch("vastly.cli.cmd_ssh") as mock:
            main(argv=["ssh", "my-gpu", "df", "-h"])
        args = mock.call_args[0][0]
        assert args.command == "ssh"
        assert args.name == "my-gpu"
        assert args.remote_cmd == ["df", "-h"]

    def test_help_flag_after_double_dash_passes_through(self):
        with patch("vastly.cli.cmd_ssh") as mock:
            main(argv=["ssh", "my-gpu", "--", "ls", "-h"])
        args = mock.call_args[0][0]
        assert args.name == "my-gpu"
        assert args.remote_cmd == ["ls", "-h"]

    def test_long_help_flag_in_remote_cmd_passes_through(self):
        with patch("vastly.cli.cmd_ssh") as mock:
            main(argv=["-v", "ssh", "my-gpu", "ls", "--help"])
        args = mock.call_args[0][0]
        assert args.name == "my-gpu"
        assert args.remote_cmd == ["ls", "--help"]

    def test_help_flag_after_bare_double_dash_passes_through(self):
        with patch("vastly.cli.cmd_ssh") as mock:
            main(argv=["ssh", "--", "-h"])
        args = mock.call_args[0][0]
        # argparse puts the lone arg in `name`; cmd_ssh folds an unmatched
        # name into the remote command
        assert [args.name, *args.remote_cmd] == ["-h"]

    @pytest.mark.parametrize(
        "argv",
        [
            ["ssh", "-h"],
            ["-h", "ssh"],
            ["-v", "ssh", "--help"],
            ["ssh", "-h", "my-gpu"],
        ],
    )
    def test_help_before_first_positional_shows_ssh_help(self, argv, capsys):
        with patch("vastly.cli.cmd_ssh") as mock:
            with pytest.raises(SystemExit) as exc_info:
                main(argv=argv)
        assert exc_info.value.code == 0
        assert "vst ssh [name] [command...]" in capsys.readouterr().out
        mock.assert_not_called()

    def test_other_subcommands_still_show_help_anywhere(self, capsys):
        with patch("vastly.cli.cmd_stop") as mock:
            with pytest.raises(SystemExit) as exc_info:
                main(argv=["stop", "my-gpu", "-h"])
        assert exc_info.value.code == 0
        assert "vst stop [name]" in capsys.readouterr().out
        mock.assert_not_called()
