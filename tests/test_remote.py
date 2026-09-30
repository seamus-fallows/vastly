"""Tests for vastly.remote -- setup orchestration."""

from __future__ import annotations

import io
import json
import shlex
import shutil
import subprocess
import sys
import tarfile
import types
from pathlib import Path

import pytest
from conftest import make_test_config
from conftest import make_test_instance as _inst

from vastly.remote import _PROBE_SEP, _upload, setup_instances

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "vastly"
DATA = SRC / "data"


def _find_bash() -> str | None:
    """A bash that can syntax-check scripts, or None.

    On Windows, prefer Git's bash (next to git.exe): C:\\Windows\\System32\\bash.exe
    is the WSL launcher, which fails when no Linux distro is installed -- as on
    GitHub's Windows runners, where it comes first on PATH.
    """
    if sys.platform != "win32":
        return shutil.which("bash")
    git = shutil.which("git")
    if git:
        git_bash = Path(git).resolve().parents[1] / "bin" / "bash.exe"
        if git_bash.exists():
            return str(git_bash)
    bash = shutil.which("bash")
    if bash and Path(bash).parent.name.lower() == "system32":
        return None
    return bash


def _setup_args(remote_cmd: str) -> list[str]:
    """Positional args passed to setup-remote.sh in a setup SSH command."""
    quoted = remote_cmd.split("setup-remote.sh ", 1)[1].split("; e=$?", 1)[0]
    return shlex.split(quoted)


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
                        [],
                        0,
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

    def _make_upload_mock(self, *, success=True):
        return lambda host, script, files: success

    def _base_config(self):
        return {
            "workspace": "/workspace",
            "disableAutoTmux": True,
            "installCommand": None,
            "postInstall": [],
            "copyFiles": [],
            "gitAuth": "agent",
        }

    def test_successful_setup(self, monkeypatch):
        monkeypatch.setattr("vastly.remote.run_ssh", self._make_ssh_mock())
        monkeypatch.setattr("vastly.remote._upload", self._make_upload_mock())
        result = setup_instances(
            [_inst("gpu-1")], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == ["gpu-1"]

    def test_unreachable_instance_skipped(self, monkeypatch):
        monkeypatch.setattr(
            "vastly.remote.run_ssh", self._make_ssh_mock(reachable=False)
        )
        monkeypatch.setattr("vastly.remote._upload", self._make_upload_mock())
        result = setup_instances(
            [_inst("gpu-1")], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == []

    def test_changed_host_key_is_explained_not_retried(self, monkeypatch, capsys):
        probes = []

        def refused(host, command, **kwargs):
            probes.append(command)
            return subprocess.CompletedProcess(
                [], 255, stdout="", stderr="Host key verification failed.\r\n"
            )

        monkeypatch.setattr("vastly.remote.run_ssh", refused)
        monkeypatch.setattr("vastly.remote._upload", self._make_upload_mock())
        result = setup_instances(
            [_inst("gpu-1", inst_id=42)],
            "git@github.com:u/r.git",
            "r",
            self._base_config(),
        )
        assert result == []
        assert len(probes) == 1
        out = capsys.readouterr().out
        assert "host key has changed" in out
        assert "ssh-keygen -R vastly-42" in out

    def test_already_setup_succeeds_without_running(self, monkeypatch):
        monkeypatch.setattr(
            "vastly.remote.run_ssh", self._make_ssh_mock(already_setup=True)
        )
        monkeypatch.setattr("vastly.remote._upload", self._make_upload_mock())
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
        monkeypatch.setattr("vastly.remote._upload", self._make_upload_mock())
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
        monkeypatch.setattr("vastly.remote._upload", self._make_upload_mock())
        result = setup_instances(
            [_inst("gpu-1")], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == []

    def test_git_identity_read_from_project_repo(self, monkeypatch, tmp_path):
        """Identity comes from the repo's effective git config, not just --global."""
        git_calls = []

        def recording_run(cmd, **kwargs):
            git_calls.append((cmd, kwargs.get("cwd")))
            return subprocess.CompletedProcess(cmd, 0, stdout="Repo User\n", stderr="")

        monkeypatch.setattr("subprocess.run", recording_run)
        monkeypatch.setattr("vastly.remote.run_ssh", self._make_ssh_mock())
        monkeypatch.setattr("vastly.remote._upload", self._make_upload_mock())
        setup_instances(
            [_inst("gpu-1")],
            "git@github.com:u/r.git",
            "r",
            self._base_config(),
            project_dir=tmp_path,
        )
        assert [cmd for cmd, _ in git_calls] == [
            ["git", "config", "user.name"],
            ["git", "config", "user.email"],
        ]
        assert all(cwd == tmp_path for _, cwd in git_calls)

    def test_upload_failure_skips(self, monkeypatch):
        monkeypatch.setattr("vastly.remote.run_ssh", self._make_ssh_mock())
        monkeypatch.setattr(
            "vastly.remote._upload", self._make_upload_mock(success=False)
        )
        result = setup_instances(
            [_inst("gpu-1")], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == []

    def test_setup_script_nonzero_exit_skips(self, monkeypatch):
        monkeypatch.setattr("vastly.remote.run_ssh", self._make_ssh_mock(setup_rc=1))
        monkeypatch.setattr("vastly.remote._upload", self._make_upload_mock())
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
        monkeypatch.setattr("vastly.remote._upload", self._make_upload_mock())
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
        monkeypatch.setattr("vastly.remote._upload", self._make_upload_mock())
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
        monkeypatch.setattr("vastly.remote._upload", self._make_upload_mock())
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
        monkeypatch.setattr("vastly.remote._upload", self._make_upload_mock())
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

    def test_copy_files_go_into_the_repo_after_setup(self, monkeypatch, tmp_path):
        """copyFiles travel with the setup script and are merged into the repo."""
        (tmp_path / ".claude").mkdir()
        (tmp_path / "cfg" / "sub").mkdir(parents=True)
        (tmp_path / ".env").write_text("X=1")
        uploads, setup_cmds = [], []
        base_ssh = self._make_ssh_mock()

        def recording_ssh(host, command, **kwargs):
            if "setup-remote.sh" in command:
                setup_cmds.append(command)
            return base_ssh(host, command, **kwargs)

        monkeypatch.setattr("vastly.remote.run_ssh", recording_ssh)
        monkeypatch.setattr(
            "vastly.remote._upload",
            lambda host, script, files: uploads.append((host, files)) or True,
        )
        config = {
            **self._base_config(),
            "copyFiles": [".claude/", "cfg\\sub", ".env", "missing.txt", "../x"],
        }
        setup_instances(
            [_inst("gpu-1")],
            "git@github.com:u/r.git",
            "r",
            config,
            project_dir=tmp_path,
        )
        assert uploads == [
            (
                "gpu-1",
                [
                    (tmp_path / ".claude", ".claude"),
                    (tmp_path / "cfg/sub", "cfg/sub"),
                    (tmp_path / ".env", ".env"),
                ],
            )
        ]
        # Copied (merged, never nested) only once setup has cloned the repo
        assert "cp -a /tmp/vastly-setup/files/. /workspace/r/" in setup_cmds[0]
        assert setup_cmds[0].index("setup-remote.sh") < setup_cmds[0].index("cp -a")

    # ── Git access (gitAuth) ──

    def _run_git_auth_setup(
        self,
        monkeypatch,
        mode,
        *,
        repo_url="git@github.com:u/r.git",
        blocker=None,
        add_error=None,
        instances=None,
        live_ids=None,
    ):
        """Run setup with gitAuth=*mode*. Returns (result, setup args, forward calls)."""
        from vastly import gitauth

        setup_cmds = []
        base_mock = self._make_ssh_mock()

        def recording_ssh(host, command, **kwargs):
            if "bash /tmp/" in command:
                setup_cmds.append(command)
            return base_mock(host, command, **kwargs)

        def fake_add(repo, title, key):
            if add_error:
                raise gitauth.GitHubError(add_error)
            return 42

        forwards = []
        monkeypatch.setattr("vastly.remote.run_ssh", recording_ssh)
        monkeypatch.setattr("vastly.remote._upload", self._make_upload_mock())
        self.blocker_calls = []
        monkeypatch.setattr(
            "vastly.gitauth.deploy_key_blocker",
            lambda repo: self.blocker_calls.append(repo) or blocker,
        )
        monkeypatch.setattr("vastly.gitauth.account_id", lambda: 99)
        self.key_titles = []
        monkeypatch.setattr(
            "vastly.gitauth.ensure_instance_key",
            lambda host, name, title: (
                self.key_titles.append(title) or "ssh-ed25519 AAAA vastly-1"
            ),
        )
        self.prune_calls = []
        monkeypatch.setattr(
            "vastly.gitauth.prune_repo_keys",
            lambda repo, account, live_ids: self.prune_calls.append(
                (repo, account, live_ids)
            ),
        )
        monkeypatch.setattr("vastly.gitauth.add_deploy_key", fake_add)
        monkeypatch.setattr(
            "vastly.remote.set_forward_agent",
            lambda host, enabled: forwards.append((host, enabled)),
        )
        config = {**self._base_config(), "gitAuth": mode}
        result = setup_instances(
            instances or [_inst("gpu-1")], repo_url, "r", config, live_ids=live_ids
        )
        args = [_setup_args(cmd) for cmd in setup_cmds]
        return result, args, forwards

    def test_agent_mode_skips_github_checks(self, monkeypatch):
        def unexpected(repo):
            raise AssertionError("deploy_key_blocker called in agent mode")

        monkeypatch.setattr("vastly.gitauth.deploy_key_blocker", unexpected)
        setup_cmds = []
        base_mock = self._make_ssh_mock()

        def recording_ssh(host, command, **kwargs):
            if "bash /tmp/" in command:
                setup_cmds.append(command)
            return base_mock(host, command, **kwargs)

        monkeypatch.setattr("vastly.remote.run_ssh", recording_ssh)
        monkeypatch.setattr("vastly.remote._upload", self._make_upload_mock())
        result = setup_instances(
            [_inst("gpu-1")], "git@github.com:u/r.git", "r", self._base_config()
        )
        assert result == ["gpu-1"]
        assert _setup_args(setup_cmds[0])[8] == "agent"

    def test_auto_uses_deploy_key_when_possible(self, monkeypatch, capsys):
        from vastly import gitauth

        result, args, forwards = self._run_git_auth_setup(monkeypatch, "auto")

        assert result == ["gpu-1"]
        assert args[0][0] == "git@github.com:u/r.git"
        assert args[0][8] == "deploy-key"
        assert gitauth.load_state()["1"] == {
            "agent": False,
            "keys": [{"repo": "u/r", "id": 42}],
            "account": 99,
        }
        assert forwards == [("gpu-1", False)]
        assert "using a deploy key for u/r" in capsys.readouterr().out

    def test_deploy_key_clones_https_remote_over_ssh(self, monkeypatch, capsys):
        _, args, _ = self._run_git_auth_setup(
            monkeypatch, "auto", repo_url="https://github.com/u/r"
        )
        assert args[0][0] == "git@github.com:u/r.git"
        assert "HTTPS remote" not in capsys.readouterr().out

    def test_auto_falls_back_to_agent_when_not_admin(self, monkeypatch, capsys):
        from vastly import gitauth

        result, args, forwards = self._run_git_auth_setup(
            monkeypatch, "auto", blocker="you're not an admin of u/r"
        )

        assert result == ["gpu-1"]
        assert args[0][0] == "git@github.com:u/r.git"
        assert args[0][8] == "agent"
        assert gitauth.load_state()["1"]["agent"] is True
        assert forwards == [("gpu-1", True)]
        out = capsys.readouterr().out
        assert "using SSH agent forwarding -- you're not an admin of u/r" in out

    def test_auto_falls_back_when_github_rejects_key(self, monkeypatch, capsys):
        _, args, _ = self._run_git_auth_setup(
            monkeypatch, "auto", add_error="Deploy keys are disabled (HTTP 422)"
        )
        assert args[0][8] == "agent"
        assert "GitHub rejected the key" in capsys.readouterr().out

    def test_deploy_key_mode_stops_when_not_possible(self, monkeypatch, capsys):
        result, args, _ = self._run_git_auth_setup(
            monkeypatch, "deploy-key", blocker="you're not an admin of u/r"
        )
        assert result == []
        assert args == []  # setup script never ran
        out = capsys.readouterr().out
        assert "can't use a deploy key -- you're not an admin of u/r" in out
        assert "--git-auth agent" in out

    def test_agent_run_on_deploy_key_instance_keeps_forwarding(self, monkeypatch):
        """vst -f --git-auth agent on a deploy-key instance: later auto runs forward."""
        from vastly import gitauth

        gitauth.record_deploy_key(1, "u/r", 42, account=99)
        _, args, _ = self._run_git_auth_setup(monkeypatch, "agent")
        assert args[0][8] == "agent"
        assert gitauth.forward_agent(1, "auto") is True

    def test_agent_mode_creates_no_state(self, monkeypatch):
        from vastly import gitauth

        self._run_git_auth_setup(monkeypatch, "agent")
        assert gitauth.load_state() == {}

    def test_deploy_key_title_includes_account_and_prunes_once(self, monkeypatch):
        _, _, _ = self._run_git_auth_setup(
            monkeypatch,
            "auto",
            instances=[_inst("gpu-1", 1), _inst("gpu-2", 2)],
            live_ids={1, 2, 3},
        )
        assert self.key_titles == ["vastly-99-1", "vastly-99-2"]
        assert self.prune_calls == [("u/r", 99, {1, 2, 3})]

    def test_no_pruning_without_live_ids(self, monkeypatch):
        self._run_git_auth_setup(monkeypatch, "auto")
        assert self.prune_calls == []

    def test_deploy_key_possibility_checked_once(self, monkeypatch):
        result, args, _ = self._run_git_auth_setup(
            monkeypatch, "auto", instances=[_inst("gpu-1", 1), _inst("gpu-2", 2)]
        )
        assert result == ["gpu-1", "gpu-2"]
        assert [a[8] for a in args] == ["deploy-key", "deploy-key"]
        assert self.blocker_calls == ["u/r"]


# ── TestSetupRemoteScript ────────────────────────────────────────────


class _CapturedStdin(io.BytesIO):
    """A pipe that keeps what was written to it after it's closed."""

    def close(self):
        self.data = self.getvalue()
        super().close()


class _BrokenStdin(_CapturedStdin):
    """The pipe to an ssh that has already exited."""

    def write(self, data):
        raise BrokenPipeError


class TestUpload:
    """_upload streams the setup script and copyFiles as one tar archive."""

    @pytest.fixture
    def ssh(self, monkeypatch):
        """Stand-in for the ssh process: records its command and what's streamed to it."""
        fake = types.SimpleNamespace(procs=[], returncode=0, stdin=_CapturedStdin)

        class FakeSsh:
            def __init__(self, cmd, **kwargs):
                self.cmd = cmd
                self.stdin = fake.stdin()
                fake.procs.append(self)

            def wait(self, timeout=None):
                return fake.returncode

        monkeypatch.setattr("vastly.remote.subprocess.Popen", FakeSsh)
        return fake

    @staticmethod
    def _tar(proc) -> tarfile.TarFile:
        return tarfile.open(fileobj=io.BytesIO(proc.stdin.data))

    @pytest.fixture
    def script(self, tmp_path):
        path = tmp_path / "setup-remote.sh"
        path.write_bytes(b"#!/usr/bin/env bash\r\necho hi\r\n")
        return path

    def test_one_connection_carries_everything(self, ssh, script, tmp_path):
        (tmp_path / "cfg").mkdir()
        (tmp_path / "cfg" / "a.yaml").write_text("a: 1")
        (tmp_path / ".env").write_text("X=1")

        files = [(tmp_path / "cfg", "cfg"), (tmp_path / ".env", ".env")]
        assert _upload("gpu-1", script, files) is True

        [proc] = ssh.procs
        assert proc.cmd[-2] == "gpu-1"
        assert "tar -xf - -C /tmp/vastly-setup" in proc.cmd[-1]
        with self._tar(proc) as tar:
            assert set(tar.getnames()) == {
                "setup-remote.sh",
                "files/cfg",
                "files/cfg/a.yaml",
                "files/.env",
            }
            # Unix line endings, whatever git did to the script on Windows
            script_data = tar.extractfile("setup-remote.sh").read()
            assert script_data == b"#!/usr/bin/env bash\necho hi\n"

    def test_files_belong_to_the_ssh_user_with_plain_modes(self, ssh, script, tmp_path):
        (tmp_path / "d").mkdir()
        (tmp_path / "d" / "f").write_text("x")

        _upload("gpu-1", script, [(tmp_path / "d", "d")])

        with self._tar(ssh.procs[0]) as tar:
            members = {m.name: m for m in tar.getmembers()}
        assert all(m.uid == 0 and m.uname == "root" for m in members.values())
        assert members["files/d"].mode == 0o755
        assert members["files/d/f"].mode == 0o644

    def test_ssh_failure_is_reported(self, ssh, script):
        ssh.returncode = 255
        assert _upload("gpu-1", script, []) is False

    def test_unreadable_copy_file_is_named(self, ssh, script, monkeypatch, capsys):
        def unreadable(*args, **kwargs):
            raise PermissionError(13, "Permission denied", "secret.env")

        monkeypatch.setattr("tarfile.TarFile.add", unreadable)
        ssh.returncode = 2  # tar on the instance gets a cut-off archive

        assert _upload("gpu-1", script, [(script, "secret.env")]) is False
        assert "Can't read secret.env" in capsys.readouterr().out

    def test_ssh_exiting_early_is_reported(self, ssh, script):
        ssh.stdin, ssh.returncode = _BrokenStdin, 255
        assert _upload("gpu-1", script, []) is False


class TestSetupRemoteScript:
    def test_bundled_script_exists(self):
        assert (DATA / "setup-remote.sh").exists()

    def test_valid_bash_syntax(self):
        bash = _find_bash()
        if not bash:
            pytest.skip("bash not available")
        result = subprocess.run(
            [bash, "-n", str(DATA / "setup-remote.sh")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, f"{bash}: {result.stderr or result.stdout}"

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
                return subprocess.CompletedProcess(
                    [], 0, stdout=probe_output, stderr=""
                )
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr(
            "vastly.remote._upload", lambda *_a: pytest.fail("uploaded")
        )

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
                return subprocess.CompletedProcess(
                    [], 0, stdout=probe_output, stderr=""
                )
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr(
            "vastly.remote._upload", lambda *_a: pytest.fail("uploaded")
        )

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
                return subprocess.CompletedProcess(
                    [], 0, stdout=probe_output, stderr=""
                )
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        uploads = []

        def fake_upload(*args):
            uploads.append(args)
            return True

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr("vastly.remote._upload", fake_upload)
        monkeypatch.setattr(
            "vastly.remote.subprocess.run",
            lambda *_a, **_kw: subprocess.CompletedProcess(
                [], 0, stdout="Test User\n", stderr=""
            ),
        )

        instances = [_inst(name="1xA100-US")]
        config = make_test_config()

        result = setup_instances(
            instances, "git@github.com:user/app.git", "app", config
        )

        assert uploads


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
                return subprocess.CompletedProcess(
                    [], 0, stdout=probe_output, stderr=""
                )
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr(
            "vastly.remote._upload", lambda *_a: pytest.fail("uploaded")
        )
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
                return subprocess.CompletedProcess(
                    [], 0, stdout=probe_output, stderr=""
                )
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        uploads = []

        def fake_upload(*args):
            uploads.append(args)
            return True

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr("vastly.remote._upload", fake_upload)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        monkeypatch.setattr(
            "vastly.remote.subprocess.run",
            lambda *_a, **_kw: subprocess.CompletedProcess(
                [], 0, stdout="Test User\n", stderr=""
            ),
        )

        instances = [_inst(name="1xA100-US")]
        config = make_test_config()

        result = setup_instances(
            instances, "git@github.com:user/data-prep.git", "data-prep", config
        )

        # Setup should have proceeded (SCP'd the setup script)
        assert uploads

    def test_no_mismatch_warning_on_fresh_instance(self, monkeypatch):
        """When no markers exist at all, setup should proceed without prompting."""
        # Empty marker, empty listing
        probe_output = "\n__VASTLY_SEP__\n"

        def fake_ssh(name, cmd, **kwargs):
            if "cat ~/.vastly/setup/" in cmd:
                return subprocess.CompletedProcess(
                    [], 0, stdout=probe_output, stderr=""
                )
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        uploads = []

        def fake_upload(*args):
            uploads.append(args)
            return True

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr("vastly.remote._upload", fake_upload)
        # input should NOT be called -- if it is, this will fail
        monkeypatch.setattr(
            "builtins.input",
            lambda _: (_ for _ in ()).throw(AssertionError("should not prompt")),
        )
        monkeypatch.setattr(
            "vastly.remote.subprocess.run",
            lambda *_a, **_kw: subprocess.CompletedProcess(
                [], 0, stdout="Test User\n", stderr=""
            ),
        )

        instances = [_inst(name="1xA100-US")]
        config = make_test_config()

        result = setup_instances(
            instances, "git@github.com:user/app.git", "app", config
        )

        # Setup should proceed without prompting
        assert uploads

    def test_mismatch_warning_eof_skips(self, monkeypatch):
        """When input raises EOFError (piped/non-interactive), instance is skipped."""
        probe_output = "\n__VASTLY_SEP__\nother-repo.json\n"

        def fake_ssh(name, cmd, **kwargs):
            if "cat ~/.vastly/setup/" in cmd:
                return subprocess.CompletedProcess(
                    [], 0, stdout=probe_output, stderr=""
                )
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        def raise_eof(_):
            raise EOFError

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr(
            "vastly.remote._upload", lambda *_a: pytest.fail("uploaded")
        )
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
        monkeypatch.setattr(
            "vastly.commands.load_config",
            lambda **kw: make_test_config(portForwards=[]),
        )
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
        monkeypatch.setattr(
            "vastly.commands.setup_instances", lambda *_a, **_kw: ["test"]
        )
        monkeypatch.setattr("vastly.commands.open_ide", lambda *a: None)
        monkeypatch.setattr("vastly.update.check_for_update", lambda: None)

        import argparse

        args = argparse.Namespace(
            name=None,
            no_setup=False,
            force_setup=False,
            all=False,
            verbose=False,
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
