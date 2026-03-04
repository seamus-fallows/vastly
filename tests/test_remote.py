"""Tests for vastly.remote -- setup orchestration."""

from __future__ import annotations

import subprocess

import pytest

from vastly.instance import Instance
from vastly.remote import setup_instances

_SEP = "__VASTLY_SEP__"


def _inst(name="gpu-1"):
    """Create a minimal Instance for testing."""
    return Instance(
        name=name, id=1, dph_total=0.50, gpu_name="RTX 4090",
        num_gpus=1, start_date=None, cached=False, status="running", alias=None,
    )


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
        """Build a run_ssh mock with configurable behavior.

        The probe command contains 'cat ...json' + separator + 'ls ...' in one call.
        already_setup=True returns valid JSON before the separator.
        reachable=False returns non-zero for the probe.
        """

        def mock(host, command, **kwargs):
            # Combined probe: cat marker + separator + ls setup dir
            if _SEP in command:
                if not reachable:
                    return subprocess.CompletedProcess([], 255, stdout="", stderr="")
                if already_setup:
                    marker = '{"timestamp": "2024-01-01"}'
                    return subprocess.CompletedProcess(
                        [], 0,
                        stdout=f"{marker}\n{_SEP}\nr.json\n",
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    [], 0, stdout=f"\n{_SEP}\n\n", stderr=""
                )
            # Force-setup rm command
            if command.startswith("rm -f"):
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")
            # Setup script execution or other commands
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
            [_inst("gpu-1")], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == ["gpu-1"]

    def test_unreachable_instance_skipped(self, monkeypatch):
        monkeypatch.setattr(
            "vastly.remote.run_ssh", self._make_ssh_mock(reachable=False)
        )
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock())
        result = setup_instances(
            [_inst("gpu-1")], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == []

    def test_already_setup_succeeds_without_running(self, monkeypatch):
        monkeypatch.setattr(
            "vastly.remote.run_ssh", self._make_ssh_mock(already_setup=True)
        )
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock())
        result = setup_instances(
            [_inst("gpu-1")], "git@github.com:u/r.git", "r", self._base_config()
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
            [_inst("gpu-1")],
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
            [_inst("gpu-1")], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == []

    def test_scp_failure_skips(self, monkeypatch):
        monkeypatch.setattr("vastly.remote.run_ssh", self._make_ssh_mock())
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock(success=False))
        result = setup_instances(
            [_inst("gpu-1")], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == []

    def test_setup_script_nonzero_exit_skips(self, monkeypatch):
        monkeypatch.setattr("vastly.remote.run_ssh", self._make_ssh_mock(setup_rc=1))
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock())
        result = setup_instances(
            [_inst("gpu-1")], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == []

    def test_multiple_instances_mixed_results(self, monkeypatch):
        def mixed_ssh(host, command, **kwargs):
            if _SEP in command:
                if host == "fail-gpu":
                    return subprocess.CompletedProcess([], 255, stdout="", stderr="")
                return subprocess.CompletedProcess(
                    [], 0, stdout=f"\n{_SEP}\n\n", stderr=""
                )
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("vastly.remote.run_ssh", mixed_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", self._make_scp_mock())
        result = setup_instances(
            [_inst("ok-gpu"), _inst("fail-gpu")],
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
        setup_instances([_inst("gpu-1")], "git@github.com:u/r.git", "r", config)
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
            [_inst("gpu-1")], "git@github.com:u/r.git", "r", self._base_config()
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
            [_inst("gpu-1")], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert "true" in setup_cmds[0]
        # disableAutoTmux: False -> "false"
        setup_cmds.clear()
        config = {**self._base_config(), "disableAutoTmux": False}
        setup_instances([_inst("gpu-1")], "git@github.com:u/r.git", "r", config)
        assert "false" in setup_cmds[0]
