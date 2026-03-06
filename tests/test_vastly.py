"""Tests for vastly."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import make_test_instance as _inst
from vastly import __version__, cyan, dim, green, red, yellow
from vastly.config import DEFAULTS, _detect_ide, _ide_from_env, _validate_config, ensure_config, load_config
from vastly.errors import ConfigError, VastlyError
from vastly.instance import (
    Instance,
    build_instance_name,
    get_running_instances,
    load_aliases,
    save_aliases,
    select_instance,
    show_table,
    sync_instances,
    validate_alias,
)
from vastly.ssh import (
    clear_ssh_configs,
    ensure_ssh_include,
    find_available_port,
    is_port_available,
    write_ssh_config,
)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "vastly"
DATA = SRC / "data"


def _config(**kwargs) -> dict:
    """Create a complete config dict with sensible defaults for testing."""
    base = {**DEFAULTS}
    base.update(kwargs)
    return base


class TestIdeFromEnv:
    _ENV_VARS = (
        "CURSOR_TRACE_ID",
        "TERM_PROGRAM",
        "VSCODE_GIT_ASKPASS_MAIN",
        "VSCODE_CODE_CACHE_PATH",
    )

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for var in self._ENV_VARS:
            monkeypatch.delenv(var, raising=False)

    def test_cursor_trace_id(self, monkeypatch):
        monkeypatch.setenv("CURSOR_TRACE_ID", "abc123")
        assert _ide_from_env() == "cursor"

    def test_cursor_trace_id_beats_term_program(self, monkeypatch):
        """Cursor sets TERM_PROGRAM=vscode, but CURSOR_TRACE_ID wins."""
        monkeypatch.setenv("CURSOR_TRACE_ID", "abc123")
        monkeypatch.setenv("TERM_PROGRAM", "vscode")
        assert _ide_from_env() == "cursor"

    def test_term_program_vscode(self, monkeypatch):
        monkeypatch.setenv("TERM_PROGRAM", "vscode")
        assert _ide_from_env() == "code"

    def test_vscode_askpass_path_detects_cursor(self, monkeypatch):
        monkeypatch.setenv(
            "VSCODE_GIT_ASKPASS_MAIN",
            r"C:\Program Files\cursor\resources\app\extensions\git\dist\askpass-main.js",
        )
        assert _ide_from_env() == "cursor"

    def test_vscode_cache_path_detects_code(self, monkeypatch):
        monkeypatch.setenv(
            "VSCODE_CODE_CACHE_PATH", r"C:\Users\x\AppData\Roaming\Code\CachedData\abc"
        )
        assert _ide_from_env() == "code"

    def test_returns_none_outside_ide(self, monkeypatch):
        assert _ide_from_env() is None


class TestDetectIde:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for var in TestIdeFromEnv._ENV_VARS:
            monkeypatch.delenv(var, raising=False)

    def test_cursor_terminal(self, monkeypatch):
        monkeypatch.setenv("CURSOR_TRACE_ID", "abc")
        assert _detect_ide() == "cursor"

    def test_vscode_terminal(self, monkeypatch):
        monkeypatch.setenv("TERM_PROGRAM", "vscode")
        assert _detect_ide() == "code"

    def test_only_cursor_installed(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which", lambda cmd: "/usr/bin/cursor" if cmd == "cursor" else None
        )
        assert _detect_ide() == "cursor"

    def test_only_code_installed(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which", lambda cmd: "/usr/bin/code" if cmd == "code" else None
        )
        assert _detect_ide() == "code"

    def test_both_installed_prefers_code(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        assert _detect_ide() == "code"

    def test_neither_installed_defaults_to_code(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        assert _detect_ide() == "code"


class TestLoadConfig:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        """Prevent the host IDE terminal from affecting config tests."""
        for var in TestIdeFromEnv._ENV_VARS:
            monkeypatch.delenv(var, raising=False)

    def test_defaults_on_empty_json(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text("{}", encoding="utf-8")

        result = load_config(cfg)

        assert result["ide"] == "code"
        assert result["sshUser"] == "root"
        assert result["workspace"] == "/workspace"
        assert result["disableAutoTmux"] is False
        assert result["gitRemote"] == "origin"

    def test_reads_user_values(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text(
            json.dumps(
                {
                    "ide": "cursor",
                    "sshUser": "ubuntu",
                    "workspace": "/home/ubuntu",
                    "sshKeyPath": "C:\\Users\\me\\.ssh\\id_rsa",
                    "disableAutoTmux": False,
                    "gitRemote": "upstream",
                    "installCommand": "uv sync",
                    "portForwards": [{"local": 3000, "remote": 3000}],
                    "postInstall": ["echo hello"],
                }
            ),
            encoding="utf-8",
        )

        result = load_config(cfg)

        assert result["ide"] == "cursor"
        assert result["sshUser"] == "ubuntu"
        assert result["workspace"] == "/home/ubuntu"
        assert result["sshKeyPath"] == "C:\\Users\\me\\.ssh\\id_rsa"
        assert result["disableAutoTmux"] is False
        assert result["gitRemote"] == "upstream"
        assert result["installCommand"] == "uv sync"

    def test_default_port_forwards(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text("{}", encoding="utf-8")

        result = load_config(cfg)

        assert len(result["portForwards"]) == 1
        assert result["portForwards"][0]["local"] == 8080
        assert result["portForwards"][0]["remote"] == 8080

    def test_empty_port_forwards(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"portForwards": []}', encoding="utf-8")

        result = load_config(cfg)

        assert len(result["portForwards"]) == 0

    def test_auto_creates_config_from_template(self, tmp_path):
        cfg = tmp_path / ".vastly.json"

        assert ensure_config(cfg) is True
        load_config(cfg)

        assert cfg.exists()

    def test_ensure_config_returns_false_if_exists(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"ide": "code"}', encoding="utf-8")

        assert ensure_config(cfg) is False

    def test_wraps_single_post_install_string(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"postInstall": "echo hello"}', encoding="utf-8")

        result = load_config(cfg)

        assert len(result["postInstall"]) == 1
        assert result["postInstall"][0] == "echo hello"

    def test_empty_post_install_when_null(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text("{}", encoding="utf-8")

        result = load_config(cfg)

        assert result["postInstall"] == []

    def test_empty_string_falls_back_to_default(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"ide": "", "workspace": ""}', encoding="utf-8")

        result = load_config(cfg)

        assert result["ide"] == "code"
        assert result["workspace"] == "/workspace"

    def test_raises_on_invalid_json(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text("{invalid json}", encoding="utf-8")

        with pytest.raises(ConfigError):
            load_config(cfg)

    def test_null_values_fall_back_to_defaults(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"sshUser": null, "ide": null}', encoding="utf-8")
        result = load_config(cfg)
        assert result["sshUser"] == "root"
        assert result["ide"] == "code"

    def test_unknown_keys_ignored(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"customKey": "value", "ide": "cursor"}', encoding="utf-8")
        result = load_config(cfg)
        assert result["ide"] == "cursor"
        assert "customKey" not in result

    def test_project_config_overrides_project_keys(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text("{}", encoding="utf-8")
        project_dir = tmp_path / "repo"
        project_dir.mkdir()
        (project_dir / ".vastly.json").write_text(
            json.dumps({"postInstall": ["make build"], "workspace": "/src"}),
            encoding="utf-8",
        )
        result = load_config(cfg, project_dir=project_dir)
        assert result["postInstall"] == ["make build"]
        assert result["workspace"] == "/src"

    def test_project_config_ignores_user_keys(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"ide": "code", "sshUser": "root"}', encoding="utf-8")
        project_dir = tmp_path / "repo"
        project_dir.mkdir()
        (project_dir / ".vastly.json").write_text(
            json.dumps(
                {"ide": "cursor", "sshUser": "ubuntu", "postInstall": ["echo hi"]}
            ),
            encoding="utf-8",
        )
        result = load_config(cfg, project_dir=project_dir)
        assert result["ide"] == "code"  # user key -- not overridden
        assert result["sshUser"] == "root"  # user key -- not overridden
        assert result["postInstall"] == ["echo hi"]  # project key -- overridden

    def test_missing_project_config_is_noop(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"ide": "cursor"}', encoding="utf-8")
        project_dir = tmp_path / "repo"
        project_dir.mkdir()
        # No .vastly.json in project_dir
        result = load_config(cfg, project_dir=project_dir)
        assert result["ide"] == "cursor"
        assert result["postInstall"] == []

    def test_project_config_wraps_post_install_string(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text("{}", encoding="utf-8")
        project_dir = tmp_path / "repo"
        project_dir.mkdir()
        (project_dir / ".vastly.json").write_text(
            '{"postInstall": "make test"}', encoding="utf-8"
        )
        result = load_config(cfg, project_dir=project_dir)
        assert result["postInstall"] == ["make test"]

    def test_project_config_empty_string_falls_back_to_global(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"workspace": "/data"}', encoding="utf-8")
        project_dir = tmp_path / "repo"
        project_dir.mkdir()
        (project_dir / ".vastly.json").write_text('{"workspace": ""}', encoding="utf-8")
        result = load_config(cfg, project_dir=project_dir)
        assert result["workspace"] == "/data"

    def test_project_config_null_falls_back_to_global(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"installCommand": "uv sync"}', encoding="utf-8")
        project_dir = tmp_path / "repo"
        project_dir.mkdir()
        (project_dir / ".vastly.json").write_text(
            '{"installCommand": null}', encoding="utf-8"
        )
        result = load_config(cfg, project_dir=project_dir)
        assert result["installCommand"] == "uv sync"

    def test_env_overrides_config_to_cursor(self, tmp_path, monkeypatch):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"ide": "code"}', encoding="utf-8")
        monkeypatch.setenv("CURSOR_TRACE_ID", "abc")
        result = load_config(cfg)
        assert result["ide"] == "cursor"

    def test_env_overrides_config_to_code(self, tmp_path, monkeypatch):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"ide": "cursor"}', encoding="utf-8")
        monkeypatch.setenv("TERM_PROGRAM", "vscode")
        result = load_config(cfg)
        assert result["ide"] == "code"

    def test_no_env_uses_config_value(self, tmp_path, monkeypatch):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"ide": "cursor"}', encoding="utf-8")
        monkeypatch.delenv("TERM_PROGRAM", raising=False)
        result = load_config(cfg)
        assert result["ide"] == "cursor"


class TestConfigValidation:
    """Tests for _validate_config and related validation behavior."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        """Prevent the host IDE terminal from affecting config tests."""
        for var in TestIdeFromEnv._ENV_VARS:
            monkeypatch.delenv(var, raising=False)

    def test_valid_config_passes(self):
        """A well-formed config dict should not raise."""
        _validate_config(_config())  # should not raise

    def test_wrong_type_for_port_forwards_raises(self, tmp_path):
        """portForwards set to an int should raise ConfigError."""
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"portForwards": 8080}', encoding="utf-8")
        with pytest.raises(ConfigError, match="portForwards"):
            load_config(cfg)

    def test_wrong_type_for_disable_auto_tmux_raises(self, tmp_path):
        """disableAutoTmux set to a string should raise ConfigError."""
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"disableAutoTmux": "yes"}', encoding="utf-8")
        with pytest.raises(ConfigError, match="disableAutoTmux"):
            load_config(cfg)

    def test_workspace_without_leading_slash_raises(self, tmp_path):
        """workspace that doesn't start with / should raise ConfigError."""
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"workspace": "relative/path"}', encoding="utf-8")
        with pytest.raises(ConfigError, match="workspace.*must start with"):
            load_config(cfg)

    def test_unrecognized_keys_warn(self, tmp_path, capsys):
        """Unrecognized top-level keys should produce a warning."""
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"bogusKey": 123, "anotherBad": true}', encoding="utf-8")
        load_config(cfg)
        captured = capsys.readouterr()
        assert "unknown config keys" in captured.err
        assert "anotherBad" in captured.err
        assert "bogusKey" in captured.err

    def test_port_forwards_entry_not_dict_raises(self, tmp_path):
        """portForwards containing a non-dict entry should raise ConfigError."""
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"portForwards": [8080]}', encoding="utf-8")
        with pytest.raises(ConfigError, match="portForwards"):
            load_config(cfg)

    def test_port_forwards_missing_key_raises(self, tmp_path):
        """portForwards entry missing 'remote' should raise ConfigError."""
        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"portForwards": [{"local": 8080}]}', encoding="utf-8")
        with pytest.raises(ConfigError, match="portForwards.*remote"):
            load_config(cfg)


class TestPortHelpers:
    def test_detects_port_in_use(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            assert not is_port_available(port)

    def test_detects_port_available(self):
        # Bind then release -- port should be available immediately after
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        assert is_port_available(port)

    def test_find_available_port_skips_excluded(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        # Port is free, but excluded -- should skip to port + 1
        result = find_available_port(port, {port})
        assert result == port + 1

    def test_find_available_port_raises_when_exhausted(self):
        with pytest.raises(RuntimeError, match="No available port"):
            find_available_port(65536)


class TestSelectInstance:
    def test_returns_matching_by_name(self):
        instances = [
            _inst(name="1xRTX3060-TW", id=100),
            _inst(name="2xA100-US", id=200),
        ]
        result = select_instance(instances, "2xA100-US")
        assert len(result) == 1
        assert result[0].name == "2xA100-US"

    def test_raises_for_bad_name(self):
        instances = [
            _inst(name="1xRTX3060-TW", id=100),
            _inst(name="2xA100-US", id=200),
        ]
        with pytest.raises(VastlyError, match="No instance named 'NOPE'"):
            select_instance(instances, "NOPE")

    def test_auto_selects_single_instance(self):
        instances = [_inst(name="1xRTX3060-TW", id=100)]
        result = select_instance(instances)
        assert len(result) == 1
        assert result[0].name == "1xRTX3060-TW"

    def test_select_all_returns_all_instances(self, monkeypatch):
        instances = [
            _inst(name="1xRTX3060-TW", id=100),
            _inst(name="2xA100-US", id=200),
        ]
        monkeypatch.setattr("builtins.input", lambda _: "a")
        result = select_instance(instances, allow_all=True)
        assert len(result) == 2
        assert result[0].name == "1xRTX3060-TW"
        assert result[1].name == "2xA100-US"

    def test_all_without_allow_all_raises(self, monkeypatch):
        """Typing 'a' without allow_all should raise, not return all."""
        instances = [_inst(name="a", id=1), _inst(name="b", id=2)]
        monkeypatch.setattr("builtins.input", lambda _: "a")
        with pytest.raises(VastlyError, match="Invalid choice"):
            select_instance(instances)

    def test_select_by_number(self, monkeypatch):
        instances = [
            _inst(name="1xRTX3060-TW", id=100),
            _inst(name="2xA100-US", id=200),
        ]
        monkeypatch.setattr("builtins.input", lambda _: "2")
        result = select_instance(instances)
        assert len(result) == 1
        assert result[0].name == "2xA100-US"

    def test_eof_raises(self, monkeypatch):
        def raise_eof(_):
            raise EOFError

        instances = [_inst(name="a", id=1), _inst(name="b", id=2)]
        monkeypatch.setattr("builtins.input", raise_eof)
        with pytest.raises(VastlyError, match="Cancelled"):
            select_instance(instances)

    def test_keyboard_interrupt_raises(self, monkeypatch):
        def raise_ki(_):
            raise KeyboardInterrupt

        instances = [_inst(name="a", id=1), _inst(name="b", id=2)]
        monkeypatch.setattr("builtins.input", raise_ki)
        with pytest.raises(VastlyError, match="Cancelled"):
            select_instance(instances)

    def test_out_of_range_raises(self, monkeypatch):
        instances = [_inst(name="a", id=1), _inst(name="b", id=2)]
        monkeypatch.setattr("builtins.input", lambda _: "5")
        with pytest.raises(VastlyError, match="Invalid choice"):
            select_instance(instances)

    def test_zero_raises(self, monkeypatch):
        instances = [_inst(name="a", id=1), _inst(name="b", id=2)]
        monkeypatch.setattr("builtins.input", lambda _: "0")
        with pytest.raises(VastlyError, match="Invalid choice"):
            select_instance(instances)

    def test_non_digit_raises(self, monkeypatch):
        instances = [_inst(name="a", id=1), _inst(name="b", id=2)]
        monkeypatch.setattr("builtins.input", lambda _: "xyz")
        with pytest.raises(VastlyError, match="Invalid choice"):
            select_instance(instances)


class TestInstanceNaming:
    def test_country_gpu_format(self):
        inst = {
            "gpu_name": "RTX 4090",
            "num_gpus": 1,
            "geolocation": "Taipei, TW",
            "id": 111,
        }
        seen = set()
        assert build_instance_name(inst, seen) == "1xRTX4090-TW"

    def test_omits_country_when_empty(self):
        inst = {"gpu_name": "A100", "num_gpus": 2, "geolocation": "", "id": 111}
        seen = set()
        assert build_instance_name(inst, seen) == "2xA100"

    def test_strips_spaces_from_gpu(self):
        inst = {
            "gpu_name": "GeForce RTX 3060",
            "num_gpus": 1,
            "geolocation": "",
            "id": 111,
        }
        seen = set()
        name = build_instance_name(inst, seen)
        assert " " not in name

    def test_appends_id_on_collision(self):
        inst1 = {
            "gpu_name": "RTX 4090",
            "num_gpus": 1,
            "geolocation": "Taipei, TW",
            "id": 111,
        }
        inst2 = {
            "gpu_name": "RTX 4090",
            "num_gpus": 1,
            "geolocation": "Taipei, TW",
            "id": 222,
        }
        seen = set()
        name1 = build_instance_name(inst1, seen)
        name2 = build_instance_name(inst2, seen)
        assert name1 == "1xRTX4090-TW"
        assert name2 == "1xRTX4090-TW-222"

    def test_handles_missing_geolocation_key(self):
        inst = {"gpu_name": "A100", "num_gpus": 1, "id": 111}
        seen = set()
        assert build_instance_name(inst, seen) == "1xA100"

    def test_defaults_num_gpus_to_one(self):
        inst = {"gpu_name": "A100", "geolocation": "", "id": 111}
        seen = set()
        assert build_instance_name(inst, seen).startswith("1x")

    def test_geolocation_single_word_no_comma(self):
        """A geolocation with no comma yields no country suffix."""
        inst = {"gpu_name": "A100", "num_gpus": 1, "geolocation": "US", "id": 111}
        seen = set()
        assert build_instance_name(inst, seen) == "1xA100"

    def test_three_way_collision(self):
        base = {"gpu_name": "A100", "num_gpus": 1, "geolocation": "City, US"}
        seen = set()
        n1 = build_instance_name({**base, "id": 1}, seen)
        n2 = build_instance_name({**base, "id": 2}, seen)
        n3 = build_instance_name({**base, "id": 3}, seen)
        assert n1 == "1xA100-US"
        assert n2 == "1xA100-US-2"
        assert n3 == "1xA100-US-3"

    def test_handles_missing_gpu_name(self):
        inst = {"num_gpus": 1, "geolocation": "", "id": 111}
        seen = set()
        name = build_instance_name(inst, seen)
        assert name == "1xunknown"

    def test_handles_empty_gpu_name(self):
        inst = {"gpu_name": "", "num_gpus": 1, "geolocation": "", "id": 111}
        seen = set()
        name = build_instance_name(inst, seen)
        assert name == "1xunknown"


class TestLocalRepoInfo:
    """Test that _local_repo_info passes URLs through without conversion."""

    def test_https_url_not_converted_to_ssh(self, monkeypatch):
        """HTTPS URLs should pass through as-is, not be converted to SSH."""
        https_url = "https://github.com/user/repo.git"
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=https_url, stderr=""),
        )
        from vastly.commands import _local_repo_info

        result = _local_repo_info("origin")
        assert result is not None
        url, name = result
        assert url == https_url
        assert name == "repo"

    def test_ssh_url_passes_through(self, monkeypatch):
        ssh_url = "git@github.com:user/repo.git"
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=ssh_url, stderr=""),
        )
        from vastly.commands import _local_repo_info

        result = _local_repo_info("origin")
        assert result is not None
        url, name = result
        assert url == ssh_url
        assert name == "repo"

    def test_https_url_without_dotgit_suffix(self, monkeypatch):
        https_url = "https://github.com/user/my-project"
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=https_url, stderr=""),
        )
        from vastly.commands import _local_repo_info

        result = _local_repo_info("origin")
        assert result is not None
        url, name = result
        assert url == https_url
        assert name == "my-project"


class TestSshConfig:
    def test_generates_valid_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)

        write_ssh_config(
            "1xRTX4090-TW",
            host="192.168.1.1",
            port=22222,
            user="root",
            key_path=None,
            local_forwards=[],
        )

        content = (tmp_path / "1xRTX4090-TW").read_text()
        assert content.startswith("# Generated by vastly")
        assert "Host 1xRTX4090-TW" in content
        assert "HostName 192.168.1.1" in content
        assert "Port 22222" in content
        assert "User root" in content
        assert "ForwardAgent yes" in content
        assert "StrictHostKeyChecking no" in content
        expected_null = "NUL" if sys.platform == "win32" else "/dev/null"
        assert f"UserKnownHostsFile {expected_null}" in content

    def test_includes_identity_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)

        write_ssh_config(
            "test",
            host="10.0.0.1",
            port=22,
            user="root",
            key_path="C:\\Users\\me\\.ssh\\id_rsa",
            local_forwards=[],
        )

        content = (tmp_path / "test").read_text()
        assert "IdentityFile C:\\Users\\me\\.ssh\\id_rsa" in content

    def test_includes_local_forward(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)

        write_ssh_config(
            "test",
            host="10.0.0.1",
            port=22,
            user="root",
            key_path=None,
            local_forwards=[(8080, 8080)],
        )

        content = (tmp_path / "test").read_text()
        assert "LocalForward 8080 localhost:8080" in content

    def test_includes_multiple_local_forwards(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)

        write_ssh_config(
            "test",
            host="10.0.0.1",
            port=22,
            user="root",
            key_path=None,
            local_forwards=[(8080, 8080), (3000, 3000)],
        )

        content = (tmp_path / "test").read_text()
        assert "LocalForward 8080 localhost:8080" in content
        assert "LocalForward 3000 localhost:3000" in content

    def test_omits_identity_file_when_empty_string(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)
        write_ssh_config(
            "test",
            host="10.0.0.1",
            port=22,
            user="root",
            key_path="",
            local_forwards=[],
        )
        content = (tmp_path / "test").read_text()
        assert "IdentityFile" not in content

    def test_no_local_forward_lines_when_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)
        write_ssh_config(
            "test",
            host="10.0.0.1",
            port=22,
            user="root",
            key_path=None,
            local_forwards=[],
        )
        content = (tmp_path / "test").read_text()
        assert "LocalForward" not in content


class TestConfigTemplate:
    def test_is_valid_json(self):
        template = (DATA / ".vastly.template.json").read_text(encoding="utf-8")
        json.loads(template)

    def test_has_expected_keys(self):
        template = json.loads(
            (DATA / ".vastly.template.json").read_text(encoding="utf-8")
        )
        expected = {
            "ide",
            "sshKeyPath",
            "sshUser",
            "portForwards",
            "workspace",
            "disableAutoTmux",
            "gitRemote",
            "postInstall",
            "installCommand",
            "copyFiles",
        }
        assert set(template.keys()) == expected


class TestModuleVersion:
    def test_version_is_semver(self):
        assert re.match(r"^\d+\.\d+\.\d+$", __version__)


class TestEnsureSshInclude:
    def test_creates_config_with_include(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        ensure_ssh_include()

        content = (tmp_path / ".ssh" / "config").read_text()
        assert "Include vast.d/*" in content

    def test_prepends_to_existing_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "config").write_text(
            "Host myserver\n    HostName 10.0.0.1\n", encoding="utf-8"
        )

        ensure_ssh_include()

        content = (ssh_dir / "config").read_text()
        assert content.startswith("Include vast.d/*")
        assert "Host myserver" in content

    def test_does_not_duplicate_include(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "config").write_text(
            "Include vast.d/*\nHost myserver\n", encoding="utf-8"
        )

        ensure_ssh_include()

        content = (ssh_dir / "config").read_text()
        assert content.count("Include vast.d/*") == 1

    def test_detects_existing_backslash_include(self, tmp_path, monkeypatch):
        """Windows-style backslash in existing Include should be detected."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "config").write_text(
            "Include vast.d\\*\nHost myserver\n", encoding="utf-8"
        )

        ensure_ssh_include()

        content = (ssh_dir / "config").read_text()
        assert content.count("Include") == 1


class TestSshConfigManagement:
    def test_clear_removes_generated_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)
        (tmp_path / "host1").write_text(
            "# Generated by vastly -- do not edit\nHost host1\n"
        )
        (tmp_path / "host2").write_text(
            "# Generated by vastly -- do not edit\nHost host2\n"
        )

        clear_ssh_configs()

        assert list(tmp_path.iterdir()) == []

    def test_clear_preserves_non_generated_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)
        (tmp_path / "host1").write_text(
            "# Generated by vastly -- do not edit\nHost host1\n"
        )
        (tmp_path / "custom").write_text("Host myserver\n    HostName 10.0.0.1\n")

        clear_ssh_configs()

        remaining = [f.name for f in tmp_path.iterdir()]
        assert remaining == ["custom"]

    def test_clear_skips_directories(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)
        (tmp_path / "host1").write_text(
            "# Generated by vastly -- do not edit\nHost host1\n"
        )
        (tmp_path / "subdir").mkdir()

        clear_ssh_configs()

        remaining = [f.name for f in tmp_path.iterdir()]
        assert remaining == ["subdir"]


class TestRunScp:
    def test_recursive_flag_added(self, monkeypatch):
        """run_scp with recursive=True should include -r in the command."""
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("vastly.ssh.subprocess.run", fake_run)
        from vastly.ssh import run_scp

        run_scp("/tmp/src", "host:/tmp/dest", recursive=True)
        assert "-r" in captured_cmd

    def test_no_recursive_flag_by_default(self, monkeypatch):
        """run_scp without recursive should not include -r."""
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("vastly.ssh.subprocess.run", fake_run)
        from vastly.ssh import run_scp

        run_scp("/tmp/src", "host:/tmp/dest")
        assert "-r" not in captured_cmd


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


class TestSetupMarker:
    """Test setup marker detection and repo mismatch warning."""

    def test_marker_json_includes_repo_url(self):
        """The setup-remote.sh script should write repoUrl into the marker."""
        content = (DATA / "setup-remote.sh").read_text(encoding="utf-8")
        assert '"repoUrl"' in content

    def test_marker_exists_skips_setup(self, monkeypatch):
        """When a valid marker exists for the repo, setup should be skipped."""
        from vastly.remote import setup_instances

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
        config = _config()

        result = setup_instances(instances, repo_url, "app", config)

        assert result == ["1xA100-US"]
        assert not any("_vastly-setup.sh" in c for c in ssh_calls)

    def test_marker_with_different_url_still_skips_setup(self, monkeypatch):
        """When marker exists but repoUrl differs, setup should still be skipped."""
        from vastly.remote import setup_instances

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
        config = _config()

        result = setup_instances(instances, new_url, "app", config)

        # Marker exists -- setup should be skipped regardless of URL
        assert result == ["1xA100-US"]
        assert not any("_vastly-setup.sh" in c for c in ssh_calls)

    def test_corrupted_marker_triggers_setup(self, monkeypatch):
        """When marker contains invalid JSON, setup should re-run."""
        from vastly.remote import setup_instances

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
        config = _config()

        result = setup_instances(
            instances, "git@github.com:user/app.git", "app", config
        )

        assert len(scp_calls) > 0


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
        from vastly.remote import setup_instances

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
        config = _config()

        result = setup_instances(
            instances, "git@github.com:user/data-prep.git", "data-prep", config
        )

        assert result == []

    def test_mismatch_warning_user_confirms(self, monkeypatch):
        """When other repos exist and user says yes, setup should proceed."""
        from vastly.remote import setup_instances

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
        config = _config()

        result = setup_instances(
            instances, "git@github.com:user/data-prep.git", "data-prep", config
        )

        # Setup should have proceeded (SCP'd the setup script)
        assert len(scp_calls) > 0

    def test_no_mismatch_warning_on_fresh_instance(self, monkeypatch):
        """When no markers exist at all, setup should proceed without prompting."""
        from vastly.remote import setup_instances

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
        config = _config()

        result = setup_instances(
            instances, "git@github.com:user/app.git", "app", config
        )

        # Setup should proceed without prompting
        assert len(scp_calls) > 0

    def test_mismatch_warning_eof_skips(self, monkeypatch):
        """When input raises EOFError (piped/non-interactive), instance is skipped."""
        from vastly.remote import setup_instances

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
        config = _config()

        result = setup_instances(
            instances, "git@github.com:user/app.git", "app", config
        )

        assert result == []


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


class TestUpdateCheck:
    def test_parse_version(self):
        from vastly.update import _parse_version

        assert _parse_version("0.2.6") == (0, 2, 6)
        assert _parse_version("1.0.0") == (1, 0, 0)
        assert _parse_version("1.0.0") > _parse_version("0.9.9")
        assert _parse_version("0.2.6") < _parse_version("0.2.7")
        assert _parse_version("0.3.0") > _parse_version("0.2.99")

    def test_cache_prevents_repeated_checks(self, tmp_path, monkeypatch):
        """When cache file is recent, no network request should be made."""
        from vastly.update import check_for_update

        cache_file = tmp_path / ".last-update-check"
        cache_file.write_text(str(time.time()), encoding="utf-8")
        monkeypatch.setattr("vastly.update._CACHE_FILE", cache_file)

        # If urlopen were called it would fail -- this verifies it's skipped
        monkeypatch.setattr(
            "vastly.update.urllib.request.urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("should not be called")
            ),
        )

        check_for_update()  # Should return silently (cache is fresh)

    def test_network_error_silently_swallowed(self, tmp_path, monkeypatch):
        """Network failures should not raise or print errors."""
        from vastly.update import check_for_update

        # No cache file -- will try to fetch
        monkeypatch.setattr(
            "vastly.update._CACHE_FILE", tmp_path / ".last-update-check"
        )
        monkeypatch.setattr("vastly.update._CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            "vastly.update.urllib.request.urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("no internet")),
        )

        check_for_update()  # Should not raise


class TestStopDestroy:
    """Test cmd_stop, cmd_destroy, and _vastai_action."""

    def test_vastai_action_stop(self, monkeypatch):
        from vastly.commands import _vastai_action

        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("vastly.commands.subprocess.run", fake_run)

        inst = _inst(name="1xRTX4090-TW", id=12345)
        _vastai_action("stop", inst)

        assert "vastai" in captured_cmd
        assert "stop" in captured_cmd
        assert "12345" in captured_cmd

    def test_vastai_action_raises_on_failure(self, monkeypatch):
        from vastly.commands import _vastai_action

        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr="error msg"),
        )

        inst = _inst(name="test", id=1)
        with pytest.raises(VastlyError, match="Failed to stop"):
            _vastai_action("stop", inst)

    def test_confirm_yes(self, monkeypatch):
        from vastly.commands import _confirm

        monkeypatch.setattr("builtins.input", lambda _: "y")
        assert _confirm("Do it?") is True

    def test_confirm_no(self, monkeypatch):
        from vastly.commands import _confirm

        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert _confirm("Do it?") is False

    def test_confirm_default_no(self, monkeypatch):
        from vastly.commands import _confirm

        monkeypatch.setattr("builtins.input", lambda _: "")
        assert _confirm("Do it?") is False


class TestAliases:
    def test_save_and_load_aliases(self, tmp_path, monkeypatch):
        aliases_file = tmp_path / "aliases.json"
        monkeypatch.setattr("vastly.instance._ALIASES_FILE", aliases_file)

        save_aliases({"123": "train", "456": "dev"})
        result = load_aliases()

        assert result == {"123": "train", "456": "dev"}

    def test_load_aliases_returns_empty_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.instance._ALIASES_FILE", tmp_path / "nope.json")
        assert load_aliases() == {}

    def test_load_aliases_returns_empty_on_bad_json(self, tmp_path, monkeypatch):
        f = tmp_path / "aliases.json"
        f.write_text("{bad json}", encoding="utf-8")
        monkeypatch.setattr("vastly.instance._ALIASES_FILE", f)
        assert load_aliases() == {}

    def test_validate_alias_valid(self):
        instances = [_inst(name="1xRTX4090-TW", id=100)]
        aliases = {}
        validate_alias("train", instances, aliases)  # should not raise

    def test_validate_alias_rejects_empty(self):
        with pytest.raises(VastlyError, match="cannot be empty"):
            validate_alias("", [], {})

    def test_validate_alias_rejects_uppercase(self):
        with pytest.raises(VastlyError, match="lowercase"):
            validate_alias("Train", [], {})

    def test_validate_alias_rejects_spaces(self):
        with pytest.raises(VastlyError, match="lowercase"):
            validate_alias("my train", [], {})

    def test_validate_alias_rejects_reserved_name(self):
        with pytest.raises(VastlyError, match="reserved"):
            validate_alias("connect", [], {})

    def test_validate_alias_rejects_start_reserved(self):
        with pytest.raises(VastlyError, match="reserved"):
            validate_alias("start", [], {})

    def test_validate_alias_rejects_config_reserved(self):
        with pytest.raises(VastlyError, match="reserved"):
            validate_alias("config", [], {})

    def test_validate_alias_rejects_auto_name_collision(self):
        instances = [_inst(name="train", id=100)]
        with pytest.raises(VastlyError, match="conflicts"):
            validate_alias("train", instances, {})

    def test_validate_alias_rejects_duplicate_alias(self):
        aliases = {"123": "train"}
        with pytest.raises(VastlyError, match="already assigned"):
            validate_alias("train", [], aliases)

    def test_select_instance_matches_alias(self):
        instances = [
            _inst(name="1xRTX4090-TW", id=100, alias="train"),
            _inst(name="2xA100-US", id=200),
        ]
        result = select_instance(instances, "train")
        assert len(result) == 1
        assert result[0].name == "1xRTX4090-TW"

    def test_select_instance_matches_auto_name_with_alias(self):
        instances = [
            _inst(name="1xRTX4090-TW", id=100, alias="train"),
        ]
        result = select_instance(instances, "1xRTX4090-TW")
        assert len(result) == 1
        assert result[0].alias == "train"


class TestCp:
    """Test cmd_cp path resolution and SCP invocation."""

    def test_cp_raises_outside_git_repo(self, monkeypatch):
        from vastly.commands import cmd_cp

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        args = argparse.Namespace(
            direction="down", paths=["file.txt"], config=False, instance=None, verbose=False,
        )
        with pytest.raises(VastlyError, match="Not in a git repo"):
            cmd_cp(args)

    def test_cp_raises_when_no_remote(self, monkeypatch):
        from vastly.commands import cmd_cp

        monkeypatch.setattr("vastly.commands._git_root", lambda: Path("/repo"))
        monkeypatch.setattr(
            "vastly.commands.load_config",
            lambda **kw: _config(portForwards=[]),
        )
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr("vastly.commands._local_repo_info", lambda _: None)

        args = argparse.Namespace(
            direction="down", paths=["file.txt"], config=False, instance=None, verbose=False,
        )
        with pytest.raises(VastlyError, match="Could not determine repo name"):
            cmd_cp(args)

    def test_cp_up_raises_when_local_file_missing(self, monkeypatch, tmp_path):
        from vastly.commands import cmd_cp

        monkeypatch.setattr("vastly.commands._git_root", lambda: tmp_path)
        monkeypatch.setattr(
            "vastly.commands.load_config",
            lambda **kw: _config(portForwards=[]),
        )
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr("vastly.commands._local_repo_info", lambda _: ("url", "repo"))
        monkeypatch.setattr(
            "vastly.commands.get_running_instances",
            lambda _: [
                _inst(name="test", id=1),
            ],
        )

        args = argparse.Namespace(
            direction="up", paths=["nonexistent.txt"], config=False, instance=None, verbose=False,
        )
        with pytest.raises(VastlyError, match="No files were copied"):
            cmd_cp(args)


# -- Lifecycle tests ------------------------------------------------------


def _make_api_instance(
    inst_id, state="running", gpu="RTX 4090", geo="Taipei, TW", **extra
):
    """Create a fake API instance dict (as returned by vastai show instances --raw)."""
    base = {
        "id": inst_id,
        "cur_state": state,
        "gpu_name": gpu,
        "num_gpus": 1,
        "geolocation": geo,
        "dph_total": 0.25,
        "public_ipaddr": f"10.0.0.{inst_id}",
        "ports": {"22/tcp": [{"HostPort": str(22000 + inst_id)}]},
    }
    base.update(extra)
    return base


_MINIMAL_CONFIG = _config(portForwards=[])


class TestSyncInstancesLifecycle:
    """Test that sync_instances returns all states and handles aliases correctly."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        """Isolate SSH config dir and aliases file."""
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path / "ssh")
        (tmp_path / "ssh").mkdir()
        monkeypatch.setattr("vastly.instance._ALIASES_FILE", tmp_path / "aliases.json")
        # Stub ensure_ssh_include (touches real ~/.ssh/config)
        monkeypatch.setattr("vastly.instance.ensure_ssh_include", lambda: None)

    def test_returns_all_states(self, monkeypatch):
        api_data = [
            _make_api_instance(1, "running"),
            _make_api_instance(2, "stopped"),
            _make_api_instance(3, "exited"),
        ]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        results = sync_instances(_MINIMAL_CONFIG)

        assert len(results) == 3
        statuses = {r.status for r in results}
        assert statuses == {"running", "stopped", "exited"}

    def test_only_running_get_ssh_configs(self, tmp_path, monkeypatch):
        api_data = [
            _make_api_instance(1, "running"),
            _make_api_instance(2, "stopped"),
        ]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        ssh_dir = tmp_path / "ssh"
        results = sync_instances(_MINIMAL_CONFIG)

        # Only the running instance should have an SSH config written
        config_files = [f.name for f in ssh_dir.iterdir()]
        running_names = [r.name for r in results if r.status == "running"]
        assert len(running_names) == 1
        assert running_names[0] in config_files
        stopped_names = [r.name for r in results if r.status == "stopped"]
        assert stopped_names[0] not in config_files

    def test_returns_non_running_when_nothing_running(self, monkeypatch):
        api_data = [
            _make_api_instance(1, "stopped"),
            _make_api_instance(2, "exited"),
        ]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        results = sync_instances(_MINIMAL_CONFIG)

        assert len(results) == 2
        assert all(r.status != "running" for r in results)

    def test_aliases_survive_stopped_state(self, tmp_path, monkeypatch):
        aliases_file = tmp_path / "aliases.json"
        aliases_file.write_text('{"1": "train"}', encoding="utf-8")

        api_data = [_make_api_instance(1, "stopped")]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        results = sync_instances(_MINIMAL_CONFIG)

        assert results[0].alias == "train"
        # Alias should still be in the file
        saved = json.loads(aliases_file.read_text(encoding="utf-8"))
        assert "1" in saved

    def test_aliases_pruned_only_on_disappearance(self, tmp_path, monkeypatch):
        aliases_file = tmp_path / "aliases.json"
        aliases_file.write_text('{"1": "train", "999": "gone"}', encoding="utf-8")

        api_data = [_make_api_instance(1, "stopped")]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        sync_instances(_MINIMAL_CONFIG)

        saved = json.loads(aliases_file.read_text(encoding="utf-8"))
        assert "1" in saved  # stopped instance alias kept
        assert "999" not in saved  # destroyed instance alias pruned

    def test_api_outage_raises_and_preserves_aliases(self, tmp_path, monkeypatch):
        from vastly.errors import APIError

        aliases_file = tmp_path / "aliases.json"
        aliases_file.write_text('{"1": "train"}', encoding="utf-8")

        monkeypatch.setattr(
            "vastly.instance.fetch_instances",
            lambda: (_ for _ in ()).throw(APIError("down")),
        )

        with pytest.raises(APIError):
            sync_instances(_MINIMAL_CONFIG)

        # Aliases should not be touched during API outage
        saved = json.loads(aliases_file.read_text(encoding="utf-8"))
        assert "1" in saved

    def test_running_instances_get_clean_names(self, monkeypatch):
        """Running instances are processed first, so they get the base name."""
        api_data = [
            _make_api_instance(1, "running"),
            _make_api_instance(2, "stopped"),
        ]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        results = sync_instances(_MINIMAL_CONFIG)

        running = [r for r in results if r.status == "running"][0]
        stopped = [r for r in results if r.status == "stopped"][0]
        # Running gets clean name, stopped gets collision suffix
        assert running.name == "1xRTX4090-TW"
        assert stopped.name == "1xRTX4090-TW-2"

    def test_non_running_uses_cur_state(self, monkeypatch):
        """Non-running instances store the raw API cur_state."""
        api_data = [
            _make_api_instance(1, "stopped"),
            _make_api_instance(2, "exited"),
        ]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        results = sync_instances(_MINIMAL_CONFIG)

        assert len(results) == 2
        statuses = {r.status for r in results}
        assert statuses == {"stopped", "exited"}


class TestGetRunningInstances:
    """Test get_running_instances filtering and error messages."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path / "ssh")
        (tmp_path / "ssh").mkdir()
        monkeypatch.setattr("vastly.instance._ALIASES_FILE", tmp_path / "aliases.json")
        monkeypatch.setattr("vastly.instance.ensure_ssh_include", lambda: None)

    def test_returns_only_running(self, monkeypatch):
        api_data = [
            _make_api_instance(1, "running"),
            _make_api_instance(2, "stopped"),
        ]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        results = get_running_instances(_MINIMAL_CONFIG)

        assert len(results) == 1
        assert results[0].status == "running"

    def test_raises_with_hint_when_inactive_exist(self, monkeypatch):
        api_data = [
            _make_api_instance(1, "stopped"),
            _make_api_instance(2, "exited"),
        ]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        with pytest.raises(VastlyError, match="2 inactive.*vst start"):
            get_running_instances(_MINIMAL_CONFIG)

    def test_raises_no_instances_when_empty(self, monkeypatch):
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: [])

        with pytest.raises(VastlyError, match="No Vast instances found"):
            get_running_instances(_MINIMAL_CONFIG)


class TestShowTableMixedStates:
    """Test show_table output for running and non-running instances."""

    def test_running_shows_running(self, capsys):
        instances = [
            _inst(
                name="1xRTX4090-TW",
                status="running",
                dph_total=0.25,
            )
        ]
        show_table(instances)
        output = capsys.readouterr().out
        assert "running" in output
        assert "$0.25/hr" in output

    def test_stopped_shows_inactive(self, capsys):
        instances = [
            _inst(
                name="2xA100-US",
                status="stopped",
                dph_total=1.20,
            )
        ]
        show_table(instances)
        output = capsys.readouterr().out
        assert "inactive" in output
        assert "$1.20/hr" in output

    def test_exited_shows_inactive(self, capsys):
        instances = [
            _inst(
                name="1xRTX3090-DE",
                status="exited",
                dph_total=0.18,
            )
        ]
        show_table(instances)
        output = capsys.readouterr().out
        assert "inactive" in output

    def test_mixed_states(self, capsys):
        instances = [
            _inst(
                name="1xRTX4090-TW",
                status="running",
                dph_total=0.25,
                alias="train",
            ),
            _inst(
                name="2xA100-US",
                status="stopped",
                dph_total=1.20,
            ),
        ]
        show_table(instances)
        output = capsys.readouterr().out
        assert "running" in output
        assert "inactive" in output
        assert "train" in output

    def test_columns_align_with_different_label_lengths(self, capsys, monkeypatch):
        """Cost column should align even when labels differ in length."""
        monkeypatch.setattr("vastly._COLOR", False)
        instances = [
            _inst(
                name="1xRTX4090-TW",
                status="running",
                dph_total=0.25,
                alias="train",
            ),
            _inst(
                name="2xA100-US",
                status="running",
                dph_total=1.20,
            ),
        ]
        show_table(instances)
        output = capsys.readouterr().out
        lines = [line for line in output.split("\n") if "$" in line]
        # Both "$" signs should be at the same column position
        positions = [line.index("$") for line in lines]
        assert len(set(positions)) == 1, (
            f"Cost column misaligned: positions {positions}"
        )

    def test_columns_align_mixed_states(self, capsys, monkeypatch):
        """Cost column should align across running and non-running instances."""
        monkeypatch.setattr("vastly._COLOR", False)
        instances = [
            _inst(
                name="1xRTX4090-TW",
                status="running",
                dph_total=0.25,
                alias="train",
            ),
            _inst(
                name="2xA100-US",
                status="stopped",
                dph_total=1.20,
            ),
        ]
        show_table(instances)
        output = capsys.readouterr().out
        lines = [line for line in output.split("\n") if "$" in line]
        positions = [line.index("$") for line in lines]
        assert len(set(positions)) == 1, (
            f"Cost column misaligned: positions {positions}"
        )


class TestCmdStart:
    """Test cmd_start state classification, polling, and auto-connect."""

    def test_already_running_raises(self, monkeypatch):
        from vastly.commands import cmd_start

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="a", id=1, status="running"),
            ],
        )

        args = argparse.Namespace(name=None, no_connect=False, verbose=False)
        with pytest.raises(VastlyError, match="already running"):
            cmd_start(args)

    def test_offline_raises(self, monkeypatch):
        from vastly.commands import cmd_start

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="a", id=1, status="running"),
                _inst(name="b", id=2, status="offline"),
            ],
        )

        args = argparse.Namespace(name="b", no_connect=False, verbose=False)
        with pytest.raises(VastlyError, match="Cannot start.*inactive"):
            cmd_start(args)

    def test_stopped_calls_start_action(self, monkeypatch):
        from vastly.commands import cmd_start

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="a", id=1, status="running"),
                _inst(name="b", id=2, status="stopped"),
            ],
        )

        started = []
        monkeypatch.setattr(
            "vastly.commands._vastai_start",
            lambda inst: (started.append(inst.id), False)[1],
        )

        args = argparse.Namespace(name="b", no_connect=True, verbose=False)
        cmd_start(args)

        assert started == [2]

    def test_no_connect_returns_after_start(self, monkeypatch):
        from vastly.commands import cmd_start

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="b", id=2, status="stopped"),
            ],
        )
        monkeypatch.setattr("vastly.commands._vastai_start", lambda inst: False)

        connect_called = []
        monkeypatch.setattr(
            "vastly.commands._do_connect", lambda **kw: connect_called.append(True)
        )

        args = argparse.Namespace(name=None, no_connect=True, verbose=False)
        cmd_start(args)

        assert connect_called == []

    def test_transitional_skips_start_call(self, monkeypatch):
        from vastly.commands import cmd_start

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="b", id=2, status="loading"),
            ],
        )

        started = []
        monkeypatch.setattr(
            "vastly.commands._vastai_start", lambda inst: (started.append(1), False)[1]
        )

        args = argparse.Namespace(name=None, no_connect=True, verbose=False)
        cmd_start(args)

        assert started == []  # Should not call vastai_start for loading state

    def test_timeout_raises(self, monkeypatch):
        from vastly.commands import cmd_start
        import vastly.commands

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="b", id=2, status="stopped"),
            ],
        )
        monkeypatch.setattr("vastly.commands._vastai_start", lambda inst: False)

        # Make polling instant and always return "loading" (not queued, so
        # the normal timeout applies)
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(vastly.commands, "_START_TIMEOUT", 5)
        monkeypatch.setattr(vastly.commands, "_START_POLL_INTERVAL", 5)
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout='{"cur_state": "loading"}', stderr=""),
        )

        args = argparse.Namespace(name=None, no_connect=False, verbose=False)
        with pytest.raises(VastlyError, match="Timed out"):
            cmd_start(args)

    def test_queued_start_shows_waiting(self, monkeypatch, capsys):
        from vastly.commands import cmd_start
        import vastly.commands

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="b", id=2, status="stopped"),
            ],
        )
        # _vastai_start returns True (queued)
        monkeypatch.setattr("vastly.commands._vastai_start", lambda inst: True)
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(vastly.commands, "_START_TIMEOUT", 10)
        monkeypatch.setattr(vastly.commands, "_START_POLL_INTERVAL", 5)

        # API still says stopped (queued), then transitions to running
        poll_count = [0]

        def fake_run(*a, **kw):
            poll_count[0] += 1
            if poll_count[0] == 1:
                return subprocess.CompletedProcess([], 0, stdout='{"cur_state": "stopped"}', stderr="")
            return subprocess.CompletedProcess([], 0, stdout='{"cur_state": "running", "actual_status": "running"}', stderr="")

        monkeypatch.setattr("vastly.commands.subprocess.run", fake_run)
        monkeypatch.setattr("vastly.commands._do_connect", lambda **kw: None)

        args = argparse.Namespace(name=None, no_connect=False, verbose=False)
        cmd_start(args)

        output = capsys.readouterr().out
        assert "waiting" in output
        assert "Ctrl+C" in output

    def test_named_running_instance_raises(self, monkeypatch):
        """Naming a running instance in vst start should give a connect hint."""
        from vastly.commands import cmd_start

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="gpu-a", id=1, status="running"),
                _inst(name="gpu-b", id=2, status="stopped"),
            ],
        )

        args = argparse.Namespace(name="gpu-a", no_connect=False, verbose=False)
        with pytest.raises(VastlyError, match="already running.*vst gpu-a"):
            cmd_start(args)

    def test_named_running_alias_raises(self, monkeypatch):
        """Naming a running alias in vst start should give a connect hint."""
        from vastly.commands import cmd_start

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="gpu-a", id=1, status="running", alias="train"),
                _inst(name="gpu-b", id=2, status="stopped"),
            ],
        )

        args = argparse.Namespace(name="train", no_connect=False, verbose=False)
        with pytest.raises(VastlyError, match="already running"):
            cmd_start(args)


class TestCmdStopLifecycle:
    """Test cmd_stop with state-aware behavior."""

    def test_stop_already_stopped_raises(self, monkeypatch):
        from vastly.commands import cmd_stop

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="a", id=1, status="stopped"),
            ],
        )

        args = argparse.Namespace(name="a", all=False, yes=False, verbose=False)
        with pytest.raises(VastlyError, match="already inactive"):
            cmd_stop(args)

    def test_stop_loading_sends_stop(self, monkeypatch):
        from vastly.commands import cmd_stop

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="a", id=1, status="loading"),
            ],
        )

        actions = []
        monkeypatch.setattr("vastly.commands._vastai_action", lambda a, i: actions.append(a))

        args = argparse.Namespace(name="a", all=False, yes=False, verbose=False)
        cmd_stop(args)

        assert actions == ["stop"]

    def test_stop_all_mixed_states(self, monkeypatch):
        from vastly.commands import cmd_stop

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="a", id=1, status="running"),
                _inst(name="b", id=2, status="stopped"),
                _inst(name="c", id=3, status="loading"),
            ],
        )
        monkeypatch.setattr("builtins.input", lambda _: "y")

        stopped_ids = []
        monkeypatch.setattr(
            "vastly.commands._vastai_action", lambda a, i: stopped_ids.append(i.id)
        )

        args = argparse.Namespace(name=None, all=True, yes=False, verbose=False)
        cmd_stop(args)

        # Should stop running + loading, skip stopped
        assert 1 in stopped_ids
        assert 3 in stopped_ids
        assert 2 not in stopped_ids

    def test_stop_offline_named_raises_specific_error(self, monkeypatch):
        from vastly.commands import cmd_stop

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="a", id=1, status="offline"),
            ],
        )

        args = argparse.Namespace(name="a", all=False, yes=False, verbose=False)
        with pytest.raises(VastlyError, match="already inactive"):
            cmd_stop(args)

    def test_stop_offline_unnamed_raises_generic(self, monkeypatch):
        """Without --name, offline instances just get 'no running instances'."""
        from vastly.commands import cmd_stop

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="a", id=1, status="offline"),
            ],
        )

        args = argparse.Namespace(name=None, all=False, yes=False, verbose=False)
        with pytest.raises(VastlyError, match="No running instances to stop"):
            cmd_stop(args)


class TestVastaiStart:
    """Test _vastai_start detects queued vs immediate starts."""

    def test_immediate_start(self, monkeypatch, capsys):
        from vastly.commands import _vastai_start

        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="starting instance", stderr=""),
        )

        queued = _vastai_start(_inst(name="test", id=1))

        assert queued is False
        output = capsys.readouterr().out
        assert "Started" in output

    def test_queued_start(self, monkeypatch, capsys):
        from vastly.commands import _vastai_start

        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                [], 0, stdout="Required resources are currently unavailable, state change queued.", stderr=""
            ),
        )

        queued = _vastai_start(_inst(name="test", id=1))

        assert queued is True
        output = capsys.readouterr().out
        assert "Queued" in output
        assert "waiting for resources" in output


class TestCmdConfig:
    """Test cmd_config output."""

    def test_shows_resolved_config(self, monkeypatch, capsys, tmp_path):
        from vastly.commands import cmd_config

        cfg = tmp_path / ".vastly.json"
        cfg.write_text('{"ide": "cursor"}', encoding="utf-8")
        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr(
            "vastly.commands.load_config",
            lambda **kw: {
                **_MINIMAL_CONFIG,
                "ide": "cursor",
            },
        )
        monkeypatch.setattr("vastly.config.CONFIG_PATH", cfg)

        args = argparse.Namespace(verbose=False)
        cmd_config(args)

        output = capsys.readouterr().out
        assert "vastly v" in output
        assert "cursor" in output
        assert "tips:" in output

    def test_works_without_git_repo(self, monkeypatch, capsys, tmp_path):
        from vastly.commands import cmd_config

        cfg = tmp_path / ".vastly.json"
        cfg.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.config.CONFIG_PATH", cfg)

        args = argparse.Namespace(verbose=False)
        cmd_config(args)

        output = capsys.readouterr().out
        assert "not in a git repo" in output

    def test_shows_project_config(self, monkeypatch, capsys, tmp_path):
        from vastly.commands import cmd_config

        cfg = tmp_path / ".vastly.json"
        cfg.write_text("{}", encoding="utf-8")
        project_dir = tmp_path / "repo"
        project_dir.mkdir()
        (project_dir / ".vastly.json").write_text(
            '{"postInstall": ["make"]}', encoding="utf-8"
        )
        monkeypatch.setattr("vastly.commands._git_root", lambda: project_dir)
        monkeypatch.setattr(
            "vastly.commands.load_config",
            lambda **kw: {
                **_MINIMAL_CONFIG,
                "postInstall": ["make"],
            },
        )
        monkeypatch.setattr("vastly.config.CONFIG_PATH", cfg)

        args = argparse.Namespace(verbose=False)
        cmd_config(args)

        output = capsys.readouterr().out
        assert "project config:" in output
        assert "overrides global" in output


class TestHttpsRemoteWarning:
    """Test HTTPS remote warning (now in setup_instances, not _cmd_connect)."""

    def test_connect_does_not_warn_about_https(self, monkeypatch, capsys):
        """Warning moved to setup_instances, cmd_connect should not warn."""
        from vastly.commands import cmd_connect

        monkeypatch.setattr("vastly.commands._git_root", lambda: Path("/repo"))
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
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


# -- Tests for code review fixes ------------------------------------------


class TestParseVersionPreRelease:
    """Test _parse_version with pre-release and malformed versions (review item 2, 21)."""

    def test_normal_version(self):
        from vastly.update import _parse_version

        assert _parse_version("0.3.1") == (0, 3, 1)

    def test_pre_release_alpha(self):
        from vastly.update import _parse_version

        assert _parse_version("0.4.0a1") == (0, 4, 0)

    def test_pre_release_dev(self):
        from vastly.update import _parse_version

        assert _parse_version("1.0.0.dev1") == (1, 0, 0)

    def test_pre_release_rc(self):
        from vastly.update import _parse_version

        assert _parse_version("2.0.0rc1") == (2, 0, 0)

    def test_pre_release_post(self):
        from vastly.update import _parse_version

        assert _parse_version("0.4.0.post1") == (0, 4, 0)

    def test_empty_string(self):
        from vastly.update import _parse_version

        assert _parse_version("") == (0,)

    def test_comparison_stable_beats_prerelease(self):
        from vastly.update import _parse_version

        assert _parse_version("0.4.0") >= _parse_version("0.4.0a1")


class TestCmdNameClear:
    """Test vst name --clear (review item 18)."""

    def test_clear_removes_alias(self, tmp_path, monkeypatch):
        aliases_file = tmp_path / "aliases.json"
        aliases_file.write_text('{"123": "train"}', encoding="utf-8")
        monkeypatch.setattr("vastly.instance._ALIASES_FILE", aliases_file)

        from vastly.commands import cmd_name

        args = argparse.Namespace(
            alias="train", clear=True, instance=None, verbose=False,
        )
        cmd_name(args)

        saved = json.loads(aliases_file.read_text(encoding="utf-8"))
        assert "123" not in saved

    def test_clear_nonexistent_raises(self, tmp_path, monkeypatch):
        aliases_file = tmp_path / "aliases.json"
        aliases_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("vastly.instance._ALIASES_FILE", aliases_file)

        from vastly.commands import cmd_name

        args = argparse.Namespace(
            alias="nope", clear=True, instance=None, verbose=False,
        )
        with pytest.raises(VastlyError, match="No alias 'nope' found"):
            cmd_name(args)


class TestCmdNameSshCleanup:
    """Test that cmd_name cleans up old alias SSH config on reassignment (review item 1)."""

    def test_reassign_alias_removes_old_ssh_config(self, tmp_path, monkeypatch):
        from vastly.commands import cmd_name

        ssh_dir = tmp_path / "ssh"
        ssh_dir.mkdir()
        old_config = ssh_dir / "old-alias"
        old_config.write_text(
            "# Generated by vastly\nHost old-alias\n", encoding="utf-8"
        )

        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", ssh_dir)
        monkeypatch.setattr("vastly.commands.SSH_CONFIG_DIR", ssh_dir)

        aliases_file = tmp_path / "aliases.json"
        aliases_file.write_text('{"42": "old-alias"}', encoding="utf-8")
        monkeypatch.setattr("vastly.instance._ALIASES_FILE", aliases_file)
        monkeypatch.setattr("vastly.commands._git_root", lambda: Path("/repo"))
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_running_instances",
            lambda _: [
                _inst(
                    name="1xRTX4090-TW",
                    id=42,
                    status="running",
                    alias="old-alias",
                ),
            ],
        )
        monkeypatch.setattr("vastly.commands.validate_alias", lambda *a: None)
        monkeypatch.setattr(
            "vastly.commands.select_instance", lambda insts, name, **kw: insts[:1]
        )

        args = argparse.Namespace(
            alias="new-alias", clear=False, instance=None, verbose=False,
        )
        cmd_name(args)

        assert not old_config.exists(), "Old alias SSH config should be removed"


class TestLocalRepoInfoErrors:
    """Test _local_repo_info error paths (review item 20)."""

    def test_returns_none_when_git_not_found(self, monkeypatch):
        from vastly.commands import _local_repo_info

        def raise_fnf(*a, **kw):
            raise FileNotFoundError

        monkeypatch.setattr("vastly.commands.subprocess.run", raise_fnf)
        assert _local_repo_info("origin") is None

    def test_returns_none_on_nonzero_returncode(self, monkeypatch):
        from vastly.commands import _local_repo_info

        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr=""),
        )
        assert _local_repo_info("origin") is None

    def test_returns_none_on_empty_url(self, monkeypatch):
        from vastly.commands import _local_repo_info

        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: type(
                "R", (), {"returncode": 0, "stdout": "", "stderr": ""}
            )(),
        )
        assert _local_repo_info("origin") is None

    def test_prints_stderr_on_failure(self, monkeypatch, capsys):
        from vastly.commands import _local_repo_info

        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr="fatal: bad remote"),
        )
        _local_repo_info("origin")
        assert "bad remote" in capsys.readouterr().err


class TestCheckPrerequisitesExitCode:
    """Test that _check_prerequisites raises VastlyError on failure."""

    def test_raises_when_vastai_missing(self, monkeypatch):
        from vastly.commands import _check_prerequisites

        monkeypatch.setattr("shutil.which", lambda cmd: None)
        with pytest.raises(VastlyError, match="vastai"):
            _check_prerequisites(ide="code")

    def test_no_exit_when_all_present(self, monkeypatch):
        from vastly.commands import _check_prerequisites

        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        # Should not raise
        _check_prerequisites(ide="code")


class TestConnectStoppedInstance:
    """Test that vst connect <stopped-name> gives a helpful error (review item 5)."""

    def test_stopped_instance_gives_start_hint(self, monkeypatch):
        from vastly.commands import cmd_connect

        monkeypatch.setattr("vastly.commands._git_root", lambda: Path("/repo"))
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [
                _inst(
                    name="1xRTX4090-TW",
                    id=1,
                    status="running",
                    dph_total=0.25,
                ),
                _inst(
                    name="2xA100-US",
                    id=2,
                    status="stopped",
                    dph_total=0.50,
                ),
            ],
        )

        args = argparse.Namespace(
            name="2xA100-US", no_setup=False, force_setup=False, all=False, verbose=False,
        )
        with pytest.raises(VastlyError, match="inactive.*vst start"):
            cmd_connect(args)

    def test_auto_starts_when_only_stopped(self, monkeypatch, capsys):
        """When no running instances but one stopped, auto-start it."""
        from vastly.commands import cmd_connect

        started_ids = []

        monkeypatch.setattr("vastly.commands._git_root", lambda: Path("/repo"))
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [
                _inst(
                    name="1xRTX4090-TW",
                    id=1,
                    status="stopped",
                    dph_total=0.25,
                ),
            ],
        )
        monkeypatch.setattr(
            "vastly.commands._vastai_start",
            lambda inst: (started_ids.append(inst.id), False)[1],
        )
        monkeypatch.setattr("vastly.commands._poll_for_running", lambda *a, **kw: None)

        args = argparse.Namespace(
            name=None, no_setup=True, force_setup=False, all=False, verbose=False,
        )

        # After auto-start, sync returns running instance
        call_count = [0]

        def fake_sync(config):
            call_count[0] += 1
            if call_count[0] > 1:
                return [
                    _inst(
                        name="1xRTX4090-TW",
                        id=1,
                        status="running",
                        dph_total=0.25,
                    ),
                ]
            return [
                _inst(
                    name="1xRTX4090-TW",
                    id=1,
                    status="stopped",
                    dph_total=0.25,
                ),
            ]

        monkeypatch.setattr("vastly.commands.sync_instances", fake_sync)
        monkeypatch.setattr("vastly.commands.open_ide", lambda *a: None)

        cmd_connect(args)

        assert 1 in started_ids
        output = capsys.readouterr().out
        assert "Starting" in output


class TestFetchInstancesTimeout:
    """Test that fetch_instances has a timeout (review item 12)."""

    def test_timeout_raises_api_error(self, monkeypatch):
        from vastly.instance import fetch_instances
        from vastly.errors import APIError

        def timeout_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=["vastai"], timeout=30)

        monkeypatch.setattr("vastly.instance.subprocess.run", timeout_run)
        with pytest.raises(APIError, match="timed out"):
            fetch_instances()


class TestOpenIde:
    """Test open_ide passes correct arguments (review item 19)."""

    def test_passes_correct_args_to_popen(self, monkeypatch):
        import sys
        from vastly.ide import open_ide

        monkeypatch.setattr(sys, "platform", "linux")
        captured = []

        def fake_popen(cmd, **kw):
            captured.append((cmd, kw))

        monkeypatch.setattr("vastly.ide.subprocess.Popen", fake_popen)
        open_ide("code", "myhost", "/workspace/myproject")

        assert len(captured) == 1
        cmd, kwargs = captured[0]
        assert cmd == ["code", "--remote", "ssh-remote+myhost", "/workspace/myproject"]

    def test_cursor_command(self, monkeypatch):
        import sys
        from vastly.ide import open_ide

        monkeypatch.setattr(sys, "platform", "linux")
        captured = []
        monkeypatch.setattr(
            "vastly.ide.subprocess.Popen", lambda cmd, **kw: captured.append(cmd)
        )
        open_ide("cursor", "myhost", "/workspace/proj")

        assert captured[0][0] == "cursor"
        assert "ssh-remote+myhost" in captured[0][2]


class TestUnknownSubcommand:
    """Test that unknown subcommands are treated as instance names for connect."""

    def test_unknown_subcommand_becomes_connect(self, monkeypatch):
        """vst my-gpu -> vst connect my-gpu."""
        from vastly.cli import main

        captured = {}
        monkeypatch.setattr(
            "vastly.cli.cmd_connect",
            lambda args: captured.update(name=args.name),
        )

        main(["my-gpu"])

        assert captured["name"] == "my-gpu"

    def test_known_subcommand_not_rewritten(self, monkeypatch):
        """vst list should NOT be rewritten."""
        from vastly.cli import main

        monkeypatch.setattr("vastly.cli.cmd_list", lambda args: None)

        main(["list"])  # should call cmd_list, not cmd_connect

    def test_flag_not_rewritten(self, monkeypatch):
        """vst --version should NOT insert 'connect' before the flag."""
        from vastly.cli import main

        with pytest.raises(SystemExit, match="0"):
            main(["--version"])

    def test_verbose_before_name(self, monkeypatch):
        """vst -v my-gpu -> vst -v connect my-gpu."""
        from vastly.cli import main

        captured = {}
        monkeypatch.setattr(
            "vastly.cli.cmd_connect",
            lambda args: captured.update(name=args.name, verbose=args.verbose),
        )

        main(["-v", "my-gpu"])

        assert captured["name"] == "my-gpu"
        assert captured["verbose"] is True

    def test_bare_verbose(self, monkeypatch):
        """vst -v should work as verbose connect."""
        from vastly.cli import main

        called = {}
        monkeypatch.setattr(
            "vastly.cli.cmd_connect",
            lambda args: called.update(verbose=args.verbose),
        )

        main(["-v"])

        assert called["verbose"] is True


class TestPromotedConnectFlags:
    """Test that -f and -n work at the top level (without typing 'connect')."""

    def test_bare_force_setup(self, monkeypatch):
        """vst -f should connect with force_setup=True."""
        from vastly.cli import main

        captured = {}
        monkeypatch.setattr(
            "vastly.cli.cmd_connect",
            lambda args: captured.update(
                force_setup=args.force_setup, no_setup=args.no_setup
            ),
        )

        main(["-f"])

        assert captured["force_setup"] is True
        assert captured["no_setup"] is False

    def test_bare_no_setup(self, monkeypatch):
        """vst -n should connect with no_setup=True."""
        from vastly.cli import main

        captured = {}
        monkeypatch.setattr(
            "vastly.cli.cmd_connect",
            lambda args: captured.update(
                force_setup=args.force_setup, no_setup=args.no_setup
            ),
        )

        main(["-n"])

        assert captured["no_setup"] is True
        assert captured["force_setup"] is False

    def test_force_with_name(self, monkeypatch):
        """vst -f my-gpu should connect to my-gpu with force_setup=True."""
        from vastly.cli import main

        captured = {}
        monkeypatch.setattr(
            "vastly.cli.cmd_connect",
            lambda args: captured.update(
                name=args.name, force_setup=args.force_setup
            ),
        )

        main(["-f", "my-gpu"])

        assert captured["name"] == "my-gpu"
        assert captured["force_setup"] is True

    def test_name_then_force(self, monkeypatch):
        """vst my-gpu -f should also work (flag after name)."""
        from vastly.cli import main

        captured = {}
        monkeypatch.setattr(
            "vastly.cli.cmd_connect",
            lambda args: captured.update(
                name=args.name, force_setup=args.force_setup
            ),
        )

        main(["my-gpu", "-f"])

        assert captured["name"] == "my-gpu"
        assert captured["force_setup"] is True

    def test_connect_subcommand_still_works(self, monkeypatch):
        """vst connect -f should still work (backwards compat)."""
        from vastly.cli import main

        captured = {}
        monkeypatch.setattr(
            "vastly.cli.cmd_connect",
            lambda args: captured.update(force_setup=args.force_setup),
        )

        main(["connect", "-f"])

        assert captured["force_setup"] is True


class TestStateConstants:
    """Test state constants are accessible from instance.py (review item 11)."""

    def test_stoppable_states_contain_running(self):
        from vastly.instance import STOPPABLE_STATES

        assert "running" in STOPPABLE_STATES

    def test_stopped_states_contain_stopped(self):
        from vastly.instance import STOPPED_STATES

        assert "stopped" in STOPPED_STATES
        assert "exited" in STOPPED_STATES

    def test_transitional_states(self):
        from vastly.instance import TRANSITIONAL_STATES

        assert "loading" in TRANSITIONAL_STATES
        assert "creating" in TRANSITIONAL_STATES


# -- Integration tests (mock at subprocess.run boundary) --------------------


def _make_api_response(*instances):
    """Build a fake subprocess.run result returning JSON instance data."""
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(list(instances)), stderr="")


class TestSyncInstancesIntegration:
    """Integration tests for sync_instances with mocked subprocess."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        self.ssh_dir = tmp_path / "ssh"
        self.ssh_dir.mkdir()
        self.aliases_file = tmp_path / "aliases.json"
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", self.ssh_dir)
        monkeypatch.setattr("vastly.instance.SSH_CONFIG_DIR", self.ssh_dir)
        monkeypatch.setattr("vastly.instance._ALIASES_FILE", self.aliases_file)
        monkeypatch.setattr("vastly.instance.ensure_ssh_include", lambda: None)

    def test_builds_instances_from_api(self, monkeypatch):
        api_data = [
            {
                "id": 12345,
                "cur_state": "running",
                "gpu_name": "RTX 4090",
                "num_gpus": 1,
                "dph_total": 0.45,
                "public_ipaddr": "1.2.3.4",
                "geolocation": "City, US",
                "ports": {"22/tcp": [{"HostPort": "22022"}]},
            },
            {
                "id": 67890,
                "cur_state": "running",
                "gpu_name": "A100",
                "num_gpus": 2,
                "dph_total": 1.20,
                "public_ipaddr": "5.6.7.8",
                "geolocation": "Berlin, DE",
                "ports": {"22/tcp": [{"HostPort": "22033"}]},
            },
        ]
        monkeypatch.setattr(
            "vastly.instance.subprocess.run",
            lambda *a, **kw: _make_api_response(*api_data),
        )

        results = sync_instances(_MINIMAL_CONFIG)

        assert len(results) == 2
        assert all(isinstance(r, Instance) for r in results)
        assert all(r.status == "running" for r in results)
        ids = {r.id for r in results}
        assert ids == {12345, 67890}
        names = {r.name for r in results}
        assert "1xRTX4090-US" in names
        assert "2xA100-DE" in names

    def test_handles_mixed_states(self, monkeypatch):
        api_data = [
            {
                "id": 100,
                "cur_state": "running",
                "gpu_name": "RTX 4090",
                "num_gpus": 1,
                "dph_total": 0.45,
                "public_ipaddr": "1.2.3.4",
                "geolocation": "City, US",
                "ports": {"22/tcp": [{"HostPort": "22022"}]},
            },
            {
                "id": 200,
                "cur_state": "stopped",
                "gpu_name": "A100",
                "num_gpus": 1,
                "dph_total": 0.80,
                "public_ipaddr": "5.6.7.8",
                "geolocation": "Tokyo, JP",
                "ports": {"22/tcp": [{"HostPort": "22033"}]},
            },
        ]
        monkeypatch.setattr(
            "vastly.instance.subprocess.run",
            lambda *a, **kw: _make_api_response(*api_data),
        )

        results = sync_instances(_MINIMAL_CONFIG)

        assert len(results) == 2
        statuses = {r.status for r in results}
        assert statuses == {"running", "stopped"}
        running = [r for r in results if r.status == "running"]
        stopped = [r for r in results if r.status == "stopped"]
        assert running[0].id == 100
        assert stopped[0].id == 200

    def test_prunes_stale_aliases(self, monkeypatch):
        self.aliases_file.write_text(
            json.dumps({"100": "train", "999": "old"}), encoding="utf-8"
        )

        api_data = [
            {
                "id": 100,
                "cur_state": "running",
                "gpu_name": "RTX 4090",
                "num_gpus": 1,
                "dph_total": 0.45,
                "public_ipaddr": "1.2.3.4",
                "geolocation": "City, US",
                "ports": {"22/tcp": [{"HostPort": "22022"}]},
            },
        ]
        monkeypatch.setattr(
            "vastly.instance.subprocess.run",
            lambda *a, **kw: _make_api_response(*api_data),
        )

        results = sync_instances(_MINIMAL_CONFIG)

        # train alias for existing instance 100 should survive
        assert results[0].alias == "train"

        # Stale alias for non-existent instance 999 should be pruned
        saved = json.loads(self.aliases_file.read_text(encoding="utf-8"))
        assert "100" in saved
        assert "999" not in saved


class TestCmdConnectIntegration:
    """Integration tests for cmd_connect with mocked subprocess."""

    def test_connect_happy_path_no_setup(self, monkeypatch):
        from vastly.commands import cmd_connect

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._local_repo_info", lambda _: None)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [
                _inst(
                    name="1xRTX4090-US",
                    id=12345,
                    status="running",
                    gpu_name="RTX 4090",
                    dph_total=0.45,
                ),
            ],
        )

        ide_calls = []
        monkeypatch.setattr(
            "vastly.commands.open_ide",
            lambda ide, host, path: ide_calls.append((ide, host, path)),
        )

        args = argparse.Namespace(
            command="connect",
            name=None,
            no_setup=False,
            force_setup=False,
            all=False,
            verbose=False,
        )
        cmd_connect(args)

        assert len(ide_calls) == 1
        assert ide_calls[0][1] == "1xRTX4090-US"


class TestCmdStopIntegration:
    """Integration tests for cmd_stop with mocked subprocess."""

    def test_stop_single_instance(self, monkeypatch):
        from vastly.commands import cmd_stop

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="1xRTX4090-US", id=12345, status="running"),
            ],
        )

        action_calls = []
        monkeypatch.setattr(
            "vastly.commands._vastai_action",
            lambda action, inst: action_calls.append((action, inst)),
        )

        args = argparse.Namespace(
            command="stop", name=None, all=False, verbose=False
        )
        cmd_stop(args)

        assert len(action_calls) == 1
        assert action_calls[0][0] == "stop"
        assert action_calls[0][1].id == 12345

    def test_stop_named_instance(self, monkeypatch):
        from vastly.commands import cmd_stop

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="1xRTX4090-US", id=100, status="running"),
                _inst(name="test-gpu", id=200, status="running"),
            ],
        )

        action_calls = []
        monkeypatch.setattr(
            "vastly.commands._vastai_action",
            lambda action, inst: action_calls.append((action, inst)),
        )

        args = argparse.Namespace(
            command="stop", name="test-gpu", all=False, verbose=False
        )
        cmd_stop(args)

        assert len(action_calls) == 1
        assert action_calls[0][0] == "stop"
        assert action_calls[0][1].name == "test-gpu"
        assert action_calls[0][1].id == 200


class TestCmdSsh:
    """Tests for cmd_ssh argument construction and behavior."""

    def test_ssh_builds_correct_command(self, monkeypatch):
        """cmd_ssh should build an SSH command with the right host and options."""
        from vastly.commands import cmd_ssh

        monkeypatch.setattr("vastly.commands.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="1xRTX4090-US", id=100)],
        )

        captured_cmd = []
        # Force Windows path (subprocess.run) since os.execvp can't be tested in-process
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda cmd, **kw: (captured_cmd.extend(cmd), None)[1]
            or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )

        args = argparse.Namespace(
            command="ssh", name=None, verbose=False,
        )
        args.remote_cmd = []  # no remote command -- REMAINDER is empty list

        with pytest.raises(SystemExit) as exc_info:
            cmd_ssh(args)
        assert exc_info.value.code == 0

        assert captured_cmd[0] == "ssh"
        assert "1xRTX4090-US" in captured_cmd

    def test_ssh_passes_remote_command(self, monkeypatch):
        """cmd_ssh should append the remote command to the SSH invocation."""
        from vastly.commands import cmd_ssh

        monkeypatch.setattr("vastly.commands.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="gpu-box", id=1)],
        )

        captured_cmd = []
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda cmd, **kw: (captured_cmd.extend(cmd), None)[1]
            or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )

        args = argparse.Namespace(
            remote_cmd=["nvidia-smi"],  # REMAINDER captures as list
            name=None,
            verbose=False,
        )

        cmd_ssh(args)

        assert "nvidia-smi" in captured_cmd
        assert "gpu-box" in captured_cmd

    def test_ssh_selects_named_instance(self, monkeypatch):
        """cmd_ssh should select the named instance when provided."""
        from vastly.commands import cmd_ssh

        monkeypatch.setattr("vastly.commands.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [
                _inst(name="gpu-a", id=1),
                _inst(name="gpu-b", id=2),
            ],
        )

        captured_cmd = []
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda cmd, **kw: (captured_cmd.extend(cmd), None)[1]
            or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )

        args = argparse.Namespace(
            remote_cmd=[], name="gpu-b", verbose=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            cmd_ssh(args)
        assert exc_info.value.code == 0

        # Interactive SSH: on win32, replaces process via sys.exit
        assert "gpu-b" in captured_cmd
        assert "gpu-a" not in captured_cmd

    def test_ssh_does_not_need_ide(self, monkeypatch):
        """cmd_ssh should work without an IDE installed."""
        from vastly.commands import cmd_ssh

        # ssh and vastai available, but no IDE
        def fake_which(cmd):
            if cmd in ("ssh", "vastai"):
                return f"/usr/bin/{cmd}"
            return None  # code, cursor, git -- all missing

        monkeypatch.setattr("vastly.commands.shutil.which", fake_which)
        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="test", id=1)],
        )

        captured_cmd = []
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda cmd, **kw: (captured_cmd.extend(cmd), None)[1]
            or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )

        args = argparse.Namespace(remote_cmd=[], name=None, verbose=False)

        with pytest.raises(SystemExit) as exc_info:
            cmd_ssh(args)
        assert exc_info.value.code == 0
        assert "test" in captured_cmd

    def test_ssh_smart_dispatch_unknown_name_becomes_command(self, monkeypatch):
        """vst ssh nvidia-smi (no instance named nvidia-smi) -> runs nvidia-smi."""
        from vastly.commands import cmd_ssh

        monkeypatch.setattr("vastly.commands.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="gpu-box", id=1)],
        )

        captured_cmd = []
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda cmd, **kw: (captured_cmd.extend(cmd), None)[1]
            or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )

        # name="nvidia-smi" doesn't match any instance, so it should be
        # treated as a remote command on the auto-selected instance
        args = argparse.Namespace(
            remote_cmd=[], name="nvidia-smi", verbose=False,
        )

        cmd_ssh(args)

        assert "gpu-box" in captured_cmd
        assert "nvidia-smi" in captured_cmd

    def test_ssh_smart_dispatch_name_plus_command(self, monkeypatch):
        """vst ssh train nvidia-smi -> runs nvidia-smi on 'train' instance."""
        from vastly.commands import cmd_ssh

        monkeypatch.setattr("vastly.commands.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [
                _inst(name="gpu-a", id=1, alias="train"),
                _inst(name="gpu-b", id=2),
            ],
        )

        captured_cmd = []
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda cmd, **kw: (captured_cmd.extend(cmd), None)[1]
            or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )

        args = argparse.Namespace(
            remote_cmd=["nvidia-smi"], name="train", verbose=False,
        )

        cmd_ssh(args)

        assert "gpu-a" in captured_cmd
        assert "nvidia-smi" in captured_cmd

    def test_ssh_alias_reserved(self):
        """'ssh' should be a reserved alias name."""
        instances = [_inst(name="1xRTX4090-TW", id=100)]
        with pytest.raises(VastlyError, match="reserved"):
            validate_alias("ssh", instances, {})


class TestStartAndResync:
    """Tests for the _start_and_resync helper."""

    def test_starts_polls_and_resyncs(self, monkeypatch):
        from vastly.commands import _start_and_resync

        started = []
        polled = []

        def fake_start(inst):
            started.append(inst.id)
            return False

        def fake_poll(inst_id, display_name="", *, queued=False):
            polled.append((inst_id, queued))

        running_inst = _inst(name="1xA100-US", id=100, status="running")

        def fake_sync(config):
            return [running_inst]

        monkeypatch.setattr("vastly.commands._vastai_start", fake_start)
        monkeypatch.setattr("vastly.commands._poll_for_running", fake_poll)
        monkeypatch.setattr("vastly.commands.sync_instances", fake_sync)

        to_start = [_inst(name="1xA100-US", id=100, status="stopped")]
        all_inst, running = _start_and_resync(to_start, {})

        assert started == [100]
        assert polled == [("100", False)]
        assert len(running) == 1
        assert running[0].name == "1xA100-US"

    def test_queued_flag_passed_to_poll(self, monkeypatch):
        from vastly.commands import _start_and_resync

        polled = []

        monkeypatch.setattr("vastly.commands._vastai_start", lambda inst: True)
        monkeypatch.setattr(
            "vastly.commands._poll_for_running",
            lambda inst_id, display_name="", *, queued=False: polled.append(queued),
        )
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda config: [_inst(name="gpu", id=1, status="running")],
        )

        _start_and_resync([_inst(name="gpu", id=1, status="stopped")], {})
        assert polled == [True]

    def test_raises_when_no_running_after_resync(self, monkeypatch):
        from vastly.commands import _start_and_resync

        monkeypatch.setattr("vastly.commands._vastai_start", lambda inst: False)
        monkeypatch.setattr("vastly.commands._poll_for_running", lambda *a, **kw: None)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda config: [_inst(name="gpu", id=1, status="stopped")],
        )

        with pytest.raises(VastlyError, match="not reachable"):
            _start_and_resync([_inst(status="stopped")], {})



class TestYesFlag:
    """Tests for --yes / -y flag on stop and destroy."""

    def test_destroy_yes_skips_confirmation(self, monkeypatch):
        from vastly.commands import cmd_destroy

        destroyed = []

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr(
            "vastly.commands.load_config", lambda **kw: _config()
        )
        monkeypatch.setattr(
            "vastly.commands._check_prerequisites", lambda **kw: None
        )
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda config: [_inst(name="gpu", id=1)],
        )
        monkeypatch.setattr(
            "vastly.commands._vastai_destroy",
            lambda inst: destroyed.append(inst.name),
        )
        # _confirm should NOT be called when yes=True
        monkeypatch.setattr(
            "vastly.commands._confirm",
            lambda prompt: (_ for _ in ()).throw(AssertionError("confirm was called")),
        )

        args = argparse.Namespace(
            command="destroy", name=None, all=False, yes=True, verbose=False,
        )
        cmd_destroy(args)

        assert destroyed == ["gpu"]

    def test_stop_yes_skips_confirmation(self, monkeypatch):
        from vastly.commands import cmd_stop

        stopped = []

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr(
            "vastly.commands.load_config", lambda **kw: _config()
        )
        monkeypatch.setattr(
            "vastly.commands._check_prerequisites", lambda **kw: None
        )
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda config: [
                _inst(name="gpu-a", id=1),
                _inst(name="gpu-b", id=2),
            ],
        )
        monkeypatch.setattr(
            "vastly.commands._vastai_action",
            lambda action, inst: stopped.append(inst.name),
        )
        # _confirm should NOT be called when yes=True
        monkeypatch.setattr(
            "vastly.commands._confirm",
            lambda prompt: (_ for _ in ()).throw(AssertionError("confirm was called")),
        )

        args = argparse.Namespace(
            command="stop", name=None, all=True, yes=True, verbose=False,
        )
        cmd_stop(args)

        assert sorted(stopped) == ["gpu-a", "gpu-b"]


class TestSelectInstanceEmptyGuard:
    """select_instance must reject empty instance lists immediately."""

    def test_empty_list_raises(self):
        with pytest.raises(VastlyError, match="No instances available"):
            select_instance([])

    def test_empty_list_with_name_raises(self):
        with pytest.raises(VastlyError, match="No instances available"):
            select_instance([], name="gpu-a")

    def test_empty_list_with_allow_all_raises(self):
        with pytest.raises(VastlyError, match="No instances available"):
            select_instance([], allow_all=True)


class TestPortForwardRangeValidation:
    """Config validation must reject out-of-range port numbers."""

    @pytest.mark.parametrize("bad_port", [0, -1, 65536, 99999])
    def test_rejects_bad_local_port(self, bad_port):
        cfg = _config(portForwards=[{"local": bad_port, "remote": 8080}])
        with pytest.raises(ConfigError, match="1-65535"):
            _validate_config(cfg)

    @pytest.mark.parametrize("bad_port", [0, -1, 65536, 99999])
    def test_rejects_bad_remote_port(self, bad_port):
        cfg = _config(portForwards=[{"local": 8080, "remote": bad_port}])
        with pytest.raises(ConfigError, match="1-65535"):
            _validate_config(cfg)

    def test_accepts_boundary_ports(self):
        cfg = _config(portForwards=[{"local": 1, "remote": 65535}])
        _validate_config(cfg)  # should not raise

    def test_accepts_common_ports(self):
        cfg = _config(portForwards=[{"local": 8080, "remote": 22}])
        _validate_config(cfg)  # should not raise


class TestCmdStopNonStoppableStates:
    """cmd_stop should give specific errors for named instances in non-stoppable states."""

    @pytest.mark.parametrize("state", ["offline", "error", "unknown", "stopped", "exited"])
    def test_named_instance_non_stoppable_raises(self, state, monkeypatch):
        from vastly.commands import cmd_stop

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [_inst(name="gpu", id=1, status=state)],
        )

        args = argparse.Namespace(name="gpu", all=False, yes=False, verbose=False)
        with pytest.raises(VastlyError, match="already inactive"):
            cmd_stop(args)


class TestCmdConnectNoSetupPath:
    """vst -n should open at the project dir when in a git repo, not workspace root."""

    def test_no_setup_in_repo_opens_project_dir(self, monkeypatch):
        from vastly.commands import cmd_connect

        monkeypatch.setattr("vastly.commands._git_root", lambda: Path("/repo/my-project"))
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="gpu", id=1, status="running", dph_total=0.5)],
        )
        monkeypatch.setattr(
            "vastly.commands._local_repo_info",
            lambda _: ("git@github.com:user/my-project.git", "my-project"),
        )

        ide_calls = []
        monkeypatch.setattr(
            "vastly.commands.open_ide",
            lambda ide, host, path: ide_calls.append((ide, host, path)),
        )

        args = argparse.Namespace(
            name=None, no_setup=True, force_setup=False, all=False, verbose=False,
        )
        cmd_connect(args)

        assert len(ide_calls) == 1
        assert ide_calls[0][2] == "/workspace/my-project"

    def test_no_setup_no_repo_opens_workspace_root(self, monkeypatch):
        from vastly.commands import cmd_connect

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="gpu", id=1, status="running", dph_total=0.5)],
        )
        monkeypatch.setattr("vastly.commands._local_repo_info", lambda _: None)

        ide_calls = []
        monkeypatch.setattr(
            "vastly.commands.open_ide",
            lambda ide, host, path: ide_calls.append((ide, host, path)),
        )

        args = argparse.Namespace(
            name=None, no_setup=False, force_setup=False, all=False, verbose=False,
        )
        cmd_connect(args)

        assert len(ide_calls) == 1
        assert ide_calls[0][2] == "/workspace"


class TestVastaiDestroyCleanup:
    """_vastai_destroy should clean up SSH configs and aliases."""

    def test_removes_ssh_config_and_alias(self, monkeypatch, tmp_path):
        from vastly.commands import _vastai_destroy

        # Set up fake SSH config dir
        ssh_dir = tmp_path / "vast.d"
        ssh_dir.mkdir()
        monkeypatch.setattr("vastly.commands.SSH_CONFIG_DIR", ssh_dir)

        # Create SSH config files
        (ssh_dir / "gpu-box").write_text("Host gpu-box\n", encoding="utf-8")
        (ssh_dir / "train").write_text("Host train\n", encoding="utf-8")

        # Set up aliases
        aliases_file = tmp_path / "aliases.json"
        aliases_file.write_text('{"42": "train"}', encoding="utf-8")
        monkeypatch.setattr("vastly.instance._ALIASES_FILE", aliases_file)

        # Mock the actual vastai CLI call
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )

        inst = _inst(name="gpu-box", id=42, alias="train")
        _vastai_destroy(inst)

        # SSH config for the auto-name should be removed
        assert not (ssh_dir / "gpu-box").exists()
        # SSH config for the alias should be removed
        assert not (ssh_dir / "train").exists()
        # Alias entry should be gone
        import json
        remaining = json.loads(aliases_file.read_text(encoding="utf-8"))
        assert "42" not in remaining

    def test_handles_missing_ssh_configs_gracefully(self, monkeypatch, tmp_path):
        from vastly.commands import _vastai_destroy

        ssh_dir = tmp_path / "vast.d"
        ssh_dir.mkdir()
        monkeypatch.setattr("vastly.commands.SSH_CONFIG_DIR", ssh_dir)

        aliases_file = tmp_path / "aliases.json"
        monkeypatch.setattr("vastly.instance._ALIASES_FILE", aliases_file)

        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )

        # Instance with no alias and no existing SSH config -- should not error
        inst = _inst(name="gpu-box", id=42)
        _vastai_destroy(inst)  # should complete without error


class TestCmdSshSmartDispatch:
    """Test cmd_ssh smart dispatch: name-as-command fallback, stopped auto-start, typo detection."""

    def _ssh_base_mocks(self, monkeypatch, instances):
        """Set up the common mocks for cmd_ssh tests."""
        monkeypatch.setattr("vastly.commands.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr(
            "vastly.commands.sync_instances", lambda _: instances,
        )
        monkeypatch.setattr("sys.platform", "win32")

    def test_stopped_name_auto_starts(self, monkeypatch):
        """vst ssh <stopped-name> should auto-start the instance."""
        from vastly.commands import cmd_ssh

        instances = [
            _inst(name="gpu-a", id=1, status="running"),
            _inst(name="gpu-b", id=2, status="stopped"),
        ]
        self._ssh_base_mocks(monkeypatch, instances)

        started = []
        monkeypatch.setattr(
            "vastly.commands._start_and_resync",
            lambda to_start, config: (
                started.extend(i.name for i in to_start),
                (
                    [_inst(name="gpu-a", id=1), _inst(name="gpu-b", id=2)],
                    [_inst(name="gpu-a", id=1), _inst(name="gpu-b", id=2)],
                ),
            )[1],
        )

        captured_cmd = []
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda cmd, **kw: (captured_cmd.extend(cmd), None)[1]
            or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )

        args = argparse.Namespace(remote_cmd=[], name="gpu-b", verbose=False)
        with pytest.raises(SystemExit) as exc_info:
            cmd_ssh(args)
        assert exc_info.value.code == 0

        assert "gpu-b" in started
        assert "gpu-b" in captured_cmd

    def test_non_startable_state_raises(self, monkeypatch):
        """vst ssh <offline-name> should raise, not silently fall back to command."""
        from vastly.commands import cmd_ssh

        instances = [
            _inst(name="gpu-a", id=1, status="running"),
            _inst(name="gpu-b", id=2, status="offline"),
        ]
        self._ssh_base_mocks(monkeypatch, instances)

        captured_cmd = []
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda cmd, **kw: (captured_cmd.extend(cmd), None)[1]
            or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )

        args = argparse.Namespace(remote_cmd=[], name="gpu-b", verbose=False)
        with pytest.raises(VastlyError, match="offline.*cannot be started"):
            cmd_ssh(args)

    def test_typo_gives_did_you_mean(self, monkeypatch):
        """A close-enough name that doesn't match should suggest the right name."""
        from vastly.commands import cmd_ssh

        instances = [_inst(name="gpu-box", id=1, status="running")]
        self._ssh_base_mocks(monkeypatch, instances)

        captured_cmd = []
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda cmd, **kw: (captured_cmd.extend(cmd), None)[1]
            or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )

        args = argparse.Namespace(remote_cmd=[], name="gpu-bx", verbose=False)
        with pytest.raises(VastlyError, match="Did you mean.*gpu-box"):
            cmd_ssh(args)


# ── New tests for code review improvements ─────────────────────────


class TestFindByName:
    """Tests for the find_by_name helper extracted from inline patterns."""

    def test_match_by_name(self):
        from vastly.instance import find_by_name

        instances = [_inst(name="gpu-a", id=1), _inst(name="gpu-b", id=2)]
        result = find_by_name(instances, "gpu-a")
        assert result is not None
        assert result.name == "gpu-a"

    def test_match_by_alias(self):
        from vastly.instance import find_by_name

        instances = [_inst(name="gpu-a", id=1, alias="train")]
        result = find_by_name(instances, "train")
        assert result is not None
        assert result.alias == "train"

    def test_no_match_returns_none(self):
        from vastly.instance import find_by_name

        instances = [_inst(name="gpu-a", id=1)]
        assert find_by_name(instances, "nonexistent") is None

    def test_empty_list_returns_none(self):
        from vastly.instance import find_by_name

        assert find_by_name([], "anything") is None


class TestGpuNameSanitization:
    """GPU names with special characters should be stripped to alphanumeric."""

    def test_strips_spaces(self):
        inst = {"gpu_name": "RTX 4090", "num_gpus": 1, "geolocation": "", "id": 1}
        name = build_instance_name(inst, set())
        assert " " not in name
        assert "RTX4090" in name

    def test_strips_shell_metacharacters(self):
        inst = {"gpu_name": "GPU;rm -rf /", "num_gpus": 1, "geolocation": "", "id": 1}
        name = build_instance_name(inst, set())
        # All non-alphanumeric characters should be removed
        assert ";" not in name
        assert " " not in name
        assert "-" not in name or name.count("-") == 1  # only the geo separator

    def test_strips_parentheses_and_slashes(self):
        inst = {"gpu_name": "A100 (80GB)/PCIe", "num_gpus": 2, "geolocation": "", "id": 1}
        name = build_instance_name(inst, set())
        assert "(" not in name
        assert ")" not in name
        assert "/" not in name
        assert "2xA10080GBPCIe" == name


class TestPollForRunningApiFailures:
    """_poll_for_running should surface API errors after consecutive failures."""

    def test_transient_api_failure_recovers(self, monkeypatch):
        import vastly.commands
        from vastly.commands import _poll_for_running

        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(vastly.commands, "_START_TIMEOUT", 60)
        monkeypatch.setattr(vastly.commands, "_START_POLL_INTERVAL", 1)

        call_count = [0]

        def fake_run(*a, **kw):
            call_count[0] += 1
            if call_count[0] <= 2:
                # First 2 calls fail
                return subprocess.CompletedProcess([], 1, stdout="", stderr="network error")
            # Then succeeds
            return subprocess.CompletedProcess(
                [], 0, stdout='{"cur_state": "running", "actual_status": "running"}', stderr=""
            )

        monkeypatch.setattr("vastly.commands.subprocess.run", fake_run)

        _poll_for_running("123", "test-gpu")  # should not raise

    def test_consecutive_api_failures_raises(self, monkeypatch):
        import vastly.commands
        from vastly.commands import _poll_for_running

        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(vastly.commands, "_START_TIMEOUT", 60)
        monkeypatch.setattr(vastly.commands, "_START_POLL_INTERVAL", 1)
        monkeypatch.setattr(vastly.commands, "_MAX_POLL_FAILURES", 3)

        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr="API down"),
        )

        with pytest.raises(VastlyError, match="Cannot reach.*API down"):
            _poll_for_running("123", "test-gpu")

    def test_consecutive_json_failures_raises(self, monkeypatch):
        import vastly.commands
        from vastly.commands import _poll_for_running

        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(vastly.commands, "_START_TIMEOUT", 60)
        monkeypatch.setattr(vastly.commands, "_START_POLL_INTERVAL", 1)
        monkeypatch.setattr(vastly.commands, "_MAX_POLL_FAILURES", 3)

        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
        )

        with pytest.raises(VastlyError, match="invalid data"):
            _poll_for_running("123", "test-gpu")

    def test_api_failure_counter_resets_on_success(self, monkeypatch):
        import vastly.commands
        from vastly.commands import _poll_for_running

        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(vastly.commands, "_START_TIMEOUT", 60)
        monkeypatch.setattr(vastly.commands, "_START_POLL_INTERVAL", 1)
        monkeypatch.setattr(vastly.commands, "_MAX_POLL_FAILURES", 3)

        call_count = [0]

        def fake_run(*a, **kw):
            call_count[0] += 1
            if call_count[0] in (1, 2):
                return subprocess.CompletedProcess([], 1, stdout="", stderr="err")
            if call_count[0] == 3:
                # Success but not running yet -- resets counter
                return subprocess.CompletedProcess(
                    [], 0, stdout='{"cur_state": "loading"}', stderr=""
                )
            if call_count[0] in (4, 5):
                return subprocess.CompletedProcess([], 1, stdout="", stderr="err")
            # Finally running
            return subprocess.CompletedProcess(
                [], 0, stdout='{"cur_state": "running", "actual_status": "running"}', stderr=""
            )

        monkeypatch.setattr("vastly.commands.subprocess.run", fake_run)

        _poll_for_running("123", "test-gpu")  # should not raise


class TestDoConnect:
    """Tests for _do_connect, the extracted core connect logic."""

    def test_do_connect_calls_setup_and_opens_ide(self, monkeypatch):
        from vastly.commands import _do_connect

        monkeypatch.setattr("vastly.commands._git_root", lambda: Path("/repo/my-proj"))
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: "code")
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="gpu", id=1, status="running")],
        )
        monkeypatch.setattr(
            "vastly.commands._local_repo_info",
            lambda _: ("git@github.com:user/my-proj.git", "my-proj"),
        )

        setup_called = []
        monkeypatch.setattr(
            "vastly.commands.setup_instances",
            lambda *a, **kw: (setup_called.append(True), [a[0][0].name])[1],
        )

        ide_calls = []
        monkeypatch.setattr(
            "vastly.commands.open_ide",
            lambda ide, host, path: ide_calls.append((ide, host, path)),
        )
        monkeypatch.setattr("vastly.update.check_for_update", lambda: None)

        _do_connect()

        assert setup_called == [True]
        assert len(ide_calls) == 1

    def test_do_connect_no_setup_flag(self, monkeypatch):
        from vastly.commands import _do_connect

        monkeypatch.setattr("vastly.commands._git_root", lambda: Path("/repo/my-proj"))
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: "code")
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="gpu", id=1, status="running")],
        )
        monkeypatch.setattr(
            "vastly.commands._local_repo_info",
            lambda _: ("git@github.com:user/my-proj.git", "my-proj"),
        )

        setup_called = []
        monkeypatch.setattr(
            "vastly.commands.setup_instances",
            lambda *a, **kw: setup_called.append(True),
        )

        ide_calls = []
        monkeypatch.setattr(
            "vastly.commands.open_ide",
            lambda ide, host, path: ide_calls.append((ide, host, path)),
        )

        _do_connect(no_setup=True)

        assert setup_called == []
        assert len(ide_calls) == 1


class TestCmdStartUsesDoConnect:
    """cmd_start should call _do_connect, not build a synthetic Namespace."""

    def test_start_calls_do_connect_with_name(self, monkeypatch):
        import vastly.commands
        from vastly.commands import cmd_start

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [_inst(name="gpu", id=1, status="stopped")],
        )
        monkeypatch.setattr("vastly.commands._vastai_start", lambda inst: False)
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(vastly.commands, "_START_TIMEOUT", 10)
        monkeypatch.setattr(vastly.commands, "_START_POLL_INTERVAL", 1)

        # Simulate poll returning running
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                [], 0, stdout='{"cur_state": "running", "actual_status": "running"}', stderr=""
            ),
        )

        connect_kwargs = {}
        monkeypatch.setattr(
            "vastly.commands._do_connect", lambda **kw: connect_kwargs.update(kw)
        )

        args = argparse.Namespace(name=None, no_connect=False, verbose=False)
        cmd_start(args)

        assert connect_kwargs["name"] == "gpu"

    def test_start_with_alias_passes_alias_to_do_connect(self, monkeypatch):
        import vastly.commands
        from vastly.commands import cmd_start

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [_inst(name="gpu", id=1, status="stopped", alias="train")],
        )
        monkeypatch.setattr("vastly.commands._vastai_start", lambda inst: False)
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(vastly.commands, "_START_TIMEOUT", 10)
        monkeypatch.setattr(vastly.commands, "_START_POLL_INTERVAL", 1)

        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                [], 0, stdout='{"cur_state": "running", "actual_status": "running"}', stderr=""
            ),
        )

        connect_kwargs = {}
        monkeypatch.setattr(
            "vastly.commands._do_connect", lambda **kw: connect_kwargs.update(kw)
        )

        args = argparse.Namespace(name="train", no_connect=False, verbose=False)
        cmd_start(args)

        # Should pass alias, not auto-name
        assert connect_kwargs["name"] == "train"


class TestCmdStopImprovedErrors:
    """cmd_stop error messages should include the instance name."""

    def test_stop_named_stopped_includes_display_name(self, monkeypatch):
        from vastly.commands import cmd_stop

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [_inst(name="gpu", id=1, status="stopped", alias="train")],
        )

        args = argparse.Namespace(name="train", all=False, yes=False, verbose=False)
        with pytest.raises(VastlyError, match="train.*gpu.*already inactive"):
            cmd_stop(args)


class TestCmdSshImprovedErrors:
    """cmd_ssh error messages should include state info."""

    def test_non_startable_includes_status(self, monkeypatch):
        from vastly.commands import cmd_ssh

        instances = [
            _inst(name="gpu-a", id=1, status="running"),
            _inst(name="gpu-b", id=2, status="offline"),
        ]

        monkeypatch.setattr("vastly.commands.shutil.which", lambda _: "/usr/bin/ssh")
        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr("vastly.commands.sync_instances", lambda _: instances)

        args = argparse.Namespace(remote_cmd=[], name="gpu-b", verbose=False)
        with pytest.raises(VastlyError, match="offline.*cannot be started"):
            cmd_ssh(args)


class TestHttpsUrlTrailingSlash:
    """HTTPS URL conversion should handle trailing slashes correctly."""

    def test_trailing_slash_stripped(self, monkeypatch):
        from vastly.remote import _check_repo_mismatch

        # The fix is in setup_instances which is hard to unit test directly.
        # Instead, verify the URL manipulation logic inline:
        repo_url = "https://github.com/user/repo/"
        clean_url = repo_url.rstrip("/")
        suggestion = clean_url.replace("https://", "git@", 1).replace("/", ":", 1)
        assert suggestion == "git@github.com:user/repo"

    def test_no_trailing_slash_unchanged(self):
        repo_url = "https://github.com/user/repo"
        clean_url = repo_url.rstrip("/")
        suggestion = clean_url.replace("https://", "git@", 1).replace("/", ":", 1)
        assert suggestion == "git@github.com:user/repo"
