"""Tests for vastly.remote -- setup orchestration."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import make_test_config, make_test_instance as _inst
from vastly.remote import _PROBE_SEP, setup_instances

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "vastly"
DATA = SRC / "data"


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
            if _PROBE_SEP in command:
                if not reachable:
                    return subprocess.CompletedProcess([], 255, stdout="", stderr="")
                if already_setup:
                    marker = '{"timestamp": "2024-01-01"}'
                    return subprocess.CompletedProcess(
                        [], 0,
                        stdout=f"{marker}\n{_PROBE_SEP}\nr.json\n",
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    [], 0, stdout=f"\n{_PROBE_SEP}\n\n", stderr=""
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
            "copyFiles": [],
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
            if _PROBE_SEP in command:
                if host == "fail-gpu":
                    return subprocess.CompletedProcess([], 255, stdout="", stderr="")
                return subprocess.CompletedProcess(
                    [], 0, stdout=f"\n{_PROBE_SEP}\n\n", stderr=""
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


# ── TestSetupRemoteScript ────────────────────────────────────────────


class TestSetupRemoteScript:
    def test_bundled_script_exists(self):
        assert (DATA / "setup-remote.sh").exists()

    def test_valid_bash_syntax(self):
        if not shutil.which("bash"):
            pytest.skip("bash not available")
        result = subprocess.run(
            ["bash", "-n", str(DATA / "setup-remote.sh")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_script_uses_strict_mode(self):
        content = (DATA / "setup-remote.sh").read_text(encoding="utf-8")
        assert "set -euo pipefail" in content


# ── TestVscodeSettingsMerge ─────────────────────────────────────────


def _merge_vscode_settings(vscode_dir: Path, python_path: str) -> None:
    """Replicate the Python merge logic from setup-remote.sh for testing.

    This must stay in sync with the inline python3 -c snippet in Step 9.
    """
    import json as _json

    p = vscode_dir / "settings.json"
    settings = {}
    if p.exists():
        try:
            settings = _json.loads(p.read_text())
        except (_json.JSONDecodeError, OSError):
            return  # JSONC or unreadable -- leave it alone
    settings["python.defaultInterpreterPath"] = python_path
    settings["terminal.integrated.defaultProfile.linux"] = "bash (login)"
    settings["terminal.integrated.profiles.linux"] = {
        "bash (login)": {"path": "/bin/bash", "args": ["-l"]}
    }
    p.write_text(_json.dumps(settings, indent=4) + "\n")


class TestVscodeSettingsMerge:
    """Test the .vscode/settings.json merge logic from setup-remote.sh."""

    def test_creates_fresh_when_no_file(self, tmp_path):
        vscode = tmp_path / ".vscode"
        vscode.mkdir()
        _merge_vscode_settings(vscode, "/usr/bin/python3")
        result = json.loads((vscode / "settings.json").read_text())
        assert result["python.defaultInterpreterPath"] == "/usr/bin/python3"
        assert result["terminal.integrated.defaultProfile.linux"] == "bash (login)"

    def test_preserves_existing_keys(self, tmp_path):
        vscode = tmp_path / ".vscode"
        vscode.mkdir()
        existing = {
            "editor.fontSize": 14,
            "python.linting.enabled": True,
            "[python]": {"editor.formatOnSave": True},
        }
        (vscode / "settings.json").write_text(json.dumps(existing))
        _merge_vscode_settings(vscode, "/venv/main/bin/python")
        result = json.loads((vscode / "settings.json").read_text())
        # Our keys are set
        assert result["python.defaultInterpreterPath"] == "/venv/main/bin/python"
        # Existing keys are preserved
        assert result["editor.fontSize"] == 14
        assert result["python.linting.enabled"] is True
        assert result["[python]"] == {"editor.formatOnSave": True}

    def test_overwrites_conflicting_keys(self, tmp_path):
        vscode = tmp_path / ".vscode"
        vscode.mkdir()
        existing = {"python.defaultInterpreterPath": "/old/python"}
        (vscode / "settings.json").write_text(json.dumps(existing))
        _merge_vscode_settings(vscode, "/new/python")
        result = json.loads((vscode / "settings.json").read_text())
        assert result["python.defaultInterpreterPath"] == "/new/python"

    def test_leaves_jsonc_untouched(self, tmp_path):
        vscode = tmp_path / ".vscode"
        vscode.mkdir()
        jsonc_content = '{\n  // User comment\n  "editor.fontSize": 14\n}'
        (vscode / "settings.json").write_text(jsonc_content)
        _merge_vscode_settings(vscode, "/usr/bin/python3")
        # File should be unchanged -- merge bailed out
        assert (vscode / "settings.json").read_text() == jsonc_content

    def test_leaves_malformed_json_untouched(self, tmp_path):
        vscode = tmp_path / ".vscode"
        vscode.mkdir()
        bad_content = "{not valid json"
        (vscode / "settings.json").write_text(bad_content)
        _merge_vscode_settings(vscode, "/usr/bin/python3")
        assert (vscode / "settings.json").read_text() == bad_content

    def test_script_contains_merge_not_overwrite(self):
        """Verify setup-remote.sh uses the Python merge, not cat >."""
        content = (DATA / "setup-remote.sh").read_text(encoding="utf-8")
        assert "python3 -c" in content
        assert "json.loads" in content
        # Should NOT contain the old cat overwrite pattern
        assert "cat > .vscode/settings.json" not in content


# ── TestSetupMarker ──────────────────────────────────────────────────


class TestSetupMarker:
    """Test setup marker detection and repo mismatch warning."""

    def test_marker_json_includes_repo_url(self):
        """The setup-remote.sh script should write repoUrl into the marker."""
        content = (DATA / "setup-remote.sh").read_text(encoding="utf-8")
        assert '"repoUrl"' in content

    def test_marker_exists_skips_setup(self, monkeypatch):
        """When a valid marker exists for the repo, setup should be skipped."""
        repo_url = "git@github.com:user/app.git"
        marker_json = json.dumps({"repoUrl": repo_url, "timestamp": "2025-01-01"})
        probe_output = f"{marker_json}\n__VASTLY_SEP__\napp.json\n"

        ssh_calls = []

        def fake_ssh(name, cmd, **kwargs):
            ssh_calls.append(cmd)
            if "cat ~/.vastly/setup/" in cmd:
                return subprocess.CompletedProcess([], 0, stdout=probe_output, stderr="")
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", lambda *a, **kw: None)

        instances = [_inst(name="1xA100-US")]
        config = make_test_config()

        result = setup_instances(instances, repo_url, "app", config)

        assert result == ["1xA100-US"]
        assert not any("_vastly-setup.sh" in c for c in ssh_calls)

    def test_marker_with_different_url_still_skips_setup(self, monkeypatch):
        """When marker exists but repoUrl differs, setup should still be skipped."""
        old_url = "git@github.com:old-org/app.git"
        new_url = "git@github.com:new-org/app.git"
        marker_json = json.dumps({"repoUrl": old_url, "timestamp": "2025-01-01"})
        probe_output = f"{marker_json}\n__VASTLY_SEP__\napp.json\n"

        ssh_calls = []

        def fake_ssh(name, cmd, **kwargs):
            ssh_calls.append(cmd)
            if "cat ~/.vastly/setup/" in cmd:
                return subprocess.CompletedProcess([], 0, stdout=probe_output, stderr="")
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", lambda *a, **kw: None)

        instances = [_inst(name="1xA100-US")]
        config = make_test_config()

        result = setup_instances(instances, new_url, "app", config)

        # Marker exists -- setup should be skipped regardless of URL
        assert result == ["1xA100-US"]
        assert not any("_vastly-setup.sh" in c for c in ssh_calls)

    def test_corrupted_marker_triggers_setup(self, monkeypatch):
        """When marker contains invalid JSON, setup should re-run."""
        probe_output = "not valid json{\n__VASTLY_SEP__\napp.json\n"

        def fake_ssh(name, cmd, **kwargs):
            if "cat ~/.vastly/setup/" in cmd:
                return subprocess.CompletedProcess([], 0, stdout=probe_output, stderr="")
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        scp_calls = []

        def fake_scp(*args, **kwargs):
            scp_calls.append(args)
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", fake_scp)
        monkeypatch.setattr(
            "vastly.remote.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="Test User\n", stderr=""),
        )

        instances = [_inst(name="1xA100-US")]
        config = make_test_config()

        result = setup_instances(
            instances, "git@github.com:user/app.git", "app", config
        )

        assert len(scp_calls) > 0


# ── TestRepoMismatchWarning ──────────────────────────────────────────


class TestRepoMismatchWarning:
    """Test the repo mismatch detection and warning prompt."""

    def test_check_repo_mismatch_detects_other_repos(self):
        from vastly.remote import _check_repo_mismatch

        result = _check_repo_mismatch("app", ["training-pipeline.json"])
        assert result == ["training-pipeline"]

    def test_check_repo_mismatch_no_mismatch_when_current_repo_present(self):
        from vastly.remote import _check_repo_mismatch

        result = _check_repo_mismatch("app", ["app.json"])
        assert result == []

    def test_check_repo_mismatch_no_mismatch_on_empty_listing(self):
        from vastly.remote import _check_repo_mismatch

        result = _check_repo_mismatch("app", [])
        assert result == []

    def test_check_repo_mismatch_filters_non_json_files(self):
        from vastly.remote import _check_repo_mismatch

        result = _check_repo_mismatch("app", ["readme.txt", "other.json"])
        assert result == ["other"]

    def test_check_repo_mismatch_multiple_other_repos(self):
        from vastly.remote import _check_repo_mismatch

        result = _check_repo_mismatch("app", ["train.json", "eval.json", "app.json"])
        assert result == ["train", "eval"]

    def test_mismatch_warning_user_declines(self, monkeypatch):
        """When other repos exist and user says no, instance should be skipped."""
        # No marker for current repo, but another repo is set up
        probe_output = "\n__VASTLY_SEP__\ntraining-pipeline.json\n"

        def fake_ssh(name, cmd, **kwargs):
            if "cat ~/.vastly/setup/" in cmd:
                return subprocess.CompletedProcess([], 0, stdout=probe_output, stderr="")
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", lambda *a, **kw: None)
        monkeypatch.setattr("builtins.input", lambda _: "n")

        instances = [_inst(name="1xA100-US")]
        config = make_test_config()

        result = setup_instances(
            instances, "git@github.com:user/data-prep.git", "data-prep", config
        )

        assert result == []

    def test_mismatch_warning_user_confirms(self, monkeypatch):
        """When other repos exist and user says yes, setup should proceed."""
        probe_output = "\n__VASTLY_SEP__\ntraining-pipeline.json\n"

        def fake_ssh(name, cmd, **kwargs):
            if "cat ~/.vastly/setup/" in cmd:
                return subprocess.CompletedProcess([], 0, stdout=probe_output, stderr="")
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        scp_calls = []

        def fake_scp(*args, **kwargs):
            scp_calls.append(args)
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", fake_scp)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        monkeypatch.setattr(
            "vastly.remote.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="Test User\n", stderr=""),
        )

        instances = [_inst(name="1xA100-US")]
        config = make_test_config()

        result = setup_instances(
            instances, "git@github.com:user/data-prep.git", "data-prep", config
        )

        # Setup should have proceeded (SCP'd the setup script)
        assert len(scp_calls) > 0

    def test_no_mismatch_warning_on_fresh_instance(self, monkeypatch):
        """When no markers exist at all, setup should proceed without prompting."""
        # Empty marker, empty listing
        probe_output = "\n__VASTLY_SEP__\n"

        def fake_ssh(name, cmd, **kwargs):
            if "cat ~/.vastly/setup/" in cmd:
                return subprocess.CompletedProcess([], 0, stdout=probe_output, stderr="")
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        scp_calls = []

        def fake_scp(*args, **kwargs):
            scp_calls.append(args)
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", fake_scp)
        # input should NOT be called -- if it is, this will fail
        monkeypatch.setattr(
            "builtins.input",
            lambda _: (_ for _ in ()).throw(AssertionError("should not prompt")),
        )
        monkeypatch.setattr(
            "vastly.remote.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="Test User\n", stderr=""),
        )

        instances = [_inst(name="1xA100-US")]
        config = make_test_config()

        result = setup_instances(
            instances, "git@github.com:user/app.git", "app", config
        )

        # Setup should proceed without prompting
        assert len(scp_calls) > 0

    def test_mismatch_warning_eof_skips(self, monkeypatch):
        """When input raises EOFError (piped/non-interactive), instance is skipped."""
        probe_output = "\n__VASTLY_SEP__\nother-repo.json\n"

        def fake_ssh(name, cmd, **kwargs):
            if "cat ~/.vastly/setup/" in cmd:
                return subprocess.CompletedProcess([], 0, stdout=probe_output, stderr="")
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        def raise_eof(_):
            raise EOFError

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", lambda *a, **kw: None)
        monkeypatch.setattr("builtins.input", raise_eof)

        instances = [_inst(name="1xA100-US")]
        config = make_test_config()

        result = setup_instances(
            instances, "git@github.com:user/app.git", "app", config
        )

        assert result == []


# ── TestHttpsRemoteWarning ───────────────────────────────────────────


class TestHttpsRemoteWarning:
    """Test HTTPS remote warning (now in setup_instances, not _cmd_connect)."""

    def test_connect_does_not_warn_about_https(self, monkeypatch, capsys):
        """Warning moved to setup_instances, cmd_connect should not warn."""
        from vastly.commands import cmd_connect

        monkeypatch.setattr("vastly.commands._git_root", lambda: Path("/repo"))
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: make_test_config(portForwards=[]))
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [
                _inst(
                    name="test",
                    id=1,
                    status="running",
                    dph_total=0.25,
                ),
            ],
        )
        monkeypatch.setattr(
            "vastly.commands._local_repo_info",
            lambda _: ("https://github.com/user/repo.git", "repo"),
        )
        monkeypatch.setattr("vastly.commands.setup_instances", lambda *a, **kw: ["test"])
        monkeypatch.setattr("vastly.commands.open_ide", lambda *a: None)
        monkeypatch.setattr("vastly.update.check_for_update", lambda: None)

        import argparse
        args = argparse.Namespace(
            name=None, no_setup=False, force_setup=False, all=False, verbose=False,
        )
        cmd_connect(args)

        output = capsys.readouterr().out
        assert "HTTPS" not in output

    def test_https_url_to_ssh_conversion(self):
        """HTTPS URL should be converted to SSH suggestion correctly."""
        url = "https://github.com/user/repo.git"
        suggestion = url.replace("https://", "git@", 1).replace("/", ":", 1)
        assert suggestion == "git@github.com:user/repo.git"

    def test_https_url_conversion_appends_git_suffix(self):
        """HTTPS URLs without .git should get it appended."""
        url = "https://github.com/user/repo"
        suggestion = url.replace("https://", "git@", 1).replace("/", ":", 1)
        fix_url = suggestion if suggestion.endswith(".git") else suggestion + ".git"
        assert fix_url == "git@github.com:user/repo.git"
