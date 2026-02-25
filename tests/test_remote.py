"""Tests for vastly.remote -- setup orchestration."""

from __future__ import annotations

import subprocess

import pytest

from vastly.remote import setup_instances


class TestSetupInstances:
    """Test remote setup orchestration with mocked SSH/SCP."""

    @pytest.fixture(autouse=True)
    def _mock_env(self, monkeypatch):
        """Mock git config subprocess calls and time.sleep."""
        git_responses = {
            "user.name": "Test User",
            "user.email": "test@example.com",
        }

        def mock_subprocess_run(cmd, **kwargs):
            if cmd[:2] == ["git", "config"]:
                key = cmd[-1]  # e.g. "user.name"
                val = git_responses.get(key, "")
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{val}\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", mock_subprocess_run)
        monkeypatch.setattr("time.sleep", lambda s: None)
        self._git_responses = git_responses

    def _make_ssh_mock(self, *, reachable=True, already_setup=False, setup_rc=0):
        """Build a run_ssh mock with configurable behavior."""

        def mock(host, command, **kwargs):
            if "echo ok" in command:
                rc = 0 if reachable else 255
                return subprocess.CompletedProcess(
                    [], rc, stdout="ok\n" if rc == 0 else "", stderr=""
                )
            if "test -f" in command and "echo done" in command:
                if already_setup:
                    return subprocess.CompletedProcess(
                        [], 0, stdout="done\n", stderr=""
                    )
                return subprocess.CompletedProcess([], 1, stdout="", stderr="")
            if command.startswith("rm -f"):
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")
            # setup command or anything else
            return subprocess.CompletedProcess([], setup_rc, stdout="", stderr="")

        return mock

    def _make_scp_mock(self, *, success=True):
        def mock(src, dest, **kwargs):
            return subprocess.CompletedProcess(
                [], 0 if success else 1, stdout="", stderr=""
            )

        return mock

    def _base_config(self):
        return {
            "workspace": "/workspace",
            "disableAutoTmux": True,
            "installCommand": None,
            "postInstall": [],
        }

    def test_successful_setup(self, monkeypatch):
        monkeypatch.setattr("vastly.remote.run_ssh", self._make_ssh_mock())
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock())
        result = setup_instances(
            [{"name": "gpu-1"}], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == ["gpu-1"]

    def test_unreachable_instance_skipped(self, monkeypatch):
        monkeypatch.setattr(
            "vastly.remote.run_ssh", self._make_ssh_mock(reachable=False)
        )
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock())
        result = setup_instances(
            [{"name": "gpu-1"}], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == []

    def test_already_setup_succeeds_without_running(self, monkeypatch):
        monkeypatch.setattr(
            "vastly.remote.run_ssh", self._make_ssh_mock(already_setup=True)
        )
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock())
        result = setup_instances(
            [{"name": "gpu-1"}], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == ["gpu-1"]

    def test_force_setup_deletes_marker(self, monkeypatch):
        commands = []
        base_mock = self._make_ssh_mock()

        def recording_ssh(host, command, **kwargs):
            commands.append(command)
            return base_mock(host, command, **kwargs)

        monkeypatch.setattr("vastly.remote.run_ssh", recording_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock())
        setup_instances(
            [{"name": "gpu-1"}],
            "git@github.com:u/r.git",
            "r",
            self._base_config(),
            force_setup=True,
        )
        assert any("rm -f" in cmd and "r.json" in cmd for cmd in commands)

    def test_missing_git_identity_skips(self, monkeypatch):
        self._git_responses.clear()
        monkeypatch.setattr("vastly.remote.run_ssh", self._make_ssh_mock())
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock())
        result = setup_instances(
            [{"name": "gpu-1"}], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == []

    def test_scp_failure_skips(self, monkeypatch):
        monkeypatch.setattr("vastly.remote.run_ssh", self._make_ssh_mock())
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock(success=False))
        result = setup_instances(
            [{"name": "gpu-1"}], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == []

    def test_setup_script_nonzero_exit_skips(self, monkeypatch):
        monkeypatch.setattr("vastly.remote.run_ssh", self._make_ssh_mock(setup_rc=1))
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock())
        result = setup_instances(
            [{"name": "gpu-1"}], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == []

    def test_multiple_instances_mixed_results(self, monkeypatch):
        def mixed_ssh(host, command, **kwargs):
            if "echo ok" in command:
                if host == "fail-gpu":
                    return subprocess.CompletedProcess([], 255, stdout="", stderr="")
                return subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")
            if "test -f" in command:
                return subprocess.CompletedProcess([], 1, stdout="", stderr="")
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("vastly.remote.run_ssh", mixed_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock())
        result = setup_instances(
            [{"name": "ok-gpu"}, {"name": "fail-gpu"}],
            "git@github.com:u/r.git",
            "r",
            self._base_config(),
        )
        assert result == ["ok-gpu"]

    def test_post_install_commands_included_in_setup_args(self, monkeypatch):
        setup_cmds = []
        base_mock = self._make_ssh_mock()

        def recording_ssh(host, command, **kwargs):
            if "bash /tmp/" in command:
                setup_cmds.append(command)
            return base_mock(host, command, **kwargs)

        monkeypatch.setattr("vastly.remote.run_ssh", recording_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock())
        config = {**self._base_config(), "postInstall": ["pip install black"]}
        setup_instances([{"name": "gpu-1"}], "git@github.com:u/r.git", "r", config)
        assert setup_cmds
        assert "pip install black" in setup_cmds[0]

    def test_install_command_auto_when_none(self, monkeypatch):
        """installCommand: null in config should be passed as 'auto'."""
        setup_cmds = []
        base_mock = self._make_ssh_mock()

        def recording_ssh(host, command, **kwargs):
            if "bash /tmp/" in command:
                setup_cmds.append(command)
            return base_mock(host, command, **kwargs)

        monkeypatch.setattr("vastly.remote.run_ssh", recording_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock())
        setup_instances(
            [{"name": "gpu-1"}], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert setup_cmds
        assert "auto" in setup_cmds[0]

    def test_disable_tmux_passed_as_string(self, monkeypatch):
        setup_cmds = []
        base_mock = self._make_ssh_mock()

        def recording_ssh(host, command, **kwargs):
            if "bash /tmp/" in command:
                setup_cmds.append(command)
            return base_mock(host, command, **kwargs)

        monkeypatch.setattr("vastly.remote.run_ssh", recording_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock())
        # disableAutoTmux: True -> "true"
        setup_instances(
            [{"name": "gpu-1"}], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert "true" in setup_cmds[0]
        # disableAutoTmux: False -> "false"
        setup_cmds.clear()
        config = {**self._base_config(), "disableAutoTmux": False}
        setup_instances([{"name": "gpu-1"}], "git@github.com:u/r.git", "r", config)
        assert "false" in setup_cmds[0]
