"""Tests for vastly."""
# TODO: Add tests for CLI entry point (argument parsing, command routing, error output)

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vastly import __version__, cyan, dim, green, red, yellow
from vastly.config import _detect_ide, _ide_from_env, _validate_config, load_config
from vastly.errors import ConfigError, VastlyError
from vastly.instance import build_instance_name, format_uptime, select_instance
from vastly.ssh import (
    cached_config_names,
    clear_ssh_configs,
    ensure_ssh_include,
    find_available_port,
    is_port_available,
    write_ssh_config,
)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "vastly"
DATA = SRC / "data"


class TestFormatUptime:
    def test_returns_question_mark_for_none(self):
        assert format_uptime(None) == "?"

    def test_returns_question_mark_for_zero(self):
        assert format_uptime(0) == "?"

    def test_returns_hours_for_old_timestamps(self):
        ts = datetime.now(tz=timezone.utc).timestamp() - 7200
        result = format_uptime(ts)
        assert re.match(r"^\d+(\.\d)?h$", result)

    def test_returns_minutes_for_recent_timestamps(self):
        ts = datetime.now(tz=timezone.utc).timestamp() - 600
        result = format_uptime(ts)
        assert re.match(r"^\d+m$", result)

    def test_boundary_at_exactly_one_hour(self):
        ts = datetime.now(tz=timezone.utc).timestamp() - 3600
        result = format_uptime(ts)
        assert result.endswith("h")

    def test_very_recent_timestamp(self):
        ts = datetime.now(tz=timezone.utc).timestamp() - 5
        assert format_uptime(ts) == "0m"

    def test_large_uptime(self):
        ts = datetime.now(tz=timezone.utc).timestamp() - (100 * 3600)
        result = format_uptime(ts)
        assert float(result.rstrip("h")) >= 99

    def test_returns_question_mark_for_empty_string(self):
        assert format_uptime("") == "?"


class TestIdeFromEnv:
    _ENV_VARS = (
        "CURSOR_TRACE_ID", "TERM_PROGRAM",
        "VSCODE_GIT_ASKPASS_MAIN", "VSCODE_CODE_CACHE_PATH",
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
        monkeypatch.setenv("VSCODE_GIT_ASKPASS_MAIN", r"C:\Program Files\cursor\resources\app\extensions\git\dist\askpass-main.js")
        assert _ide_from_env() == "cursor"

    def test_vscode_cache_path_detects_code(self, monkeypatch):
        monkeypatch.setenv("VSCODE_CODE_CACHE_PATH", r"C:\Users\x\AppData\Roaming\Code\CachedData\abc")
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
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/cursor" if cmd == "cursor" else None)
        assert _detect_ide() == "cursor"

    def test_only_code_installed(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/code" if cmd == "code" else None)
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

        load_config(cfg)

        assert cfg.exists()

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
            json.dumps({"ide": "cursor", "sshUser": "ubuntu", "postInstall": ["echo hi"]}),
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
        (project_dir / ".vastly.json").write_text(
            '{"workspace": ""}', encoding="utf-8"
        )
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
        config = {
            "ide": "code",
            "sshUser": "root",
            "sshKeyPath": None,
            "workspace": "/workspace",
            "disableAutoTmux": False,
            "gitRemote": "origin",
            "portForwards": [{"local": 8080, "remote": 8080}],
            "postInstall": [],
            "installCommand": None,
            "copyFiles": [],
        }
        _validate_config(config)  # should not raise

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

    def test_unrecognized_keys_warn_to_stderr(self, tmp_path, capsys):
        """Unrecognized top-level keys should produce a warning on stderr."""
        cfg = tmp_path / ".vastly.json"
        cfg.write_text(
            '{"bogusKey": 123, "anotherBad": true}', encoding="utf-8"
        )
        load_config(cfg)
        captured = capsys.readouterr()
        assert "unrecognized config keys" in captured.err
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
            {"name": "1xRTX3060-TW", "id": 100},
            {"name": "2xA100-US", "id": 200},
        ]
        result = select_instance(instances, "2xA100-US")
        assert len(result) == 1
        assert result[0]["name"] == "2xA100-US"

    def test_raises_for_bad_name(self):
        instances = [
            {"name": "1xRTX3060-TW", "id": 100},
            {"name": "2xA100-US", "id": 200},
        ]
        with pytest.raises(VastlyError, match="No instance named 'NOPE'"):
            select_instance(instances, "NOPE")

    def test_auto_selects_single_instance(self):
        instances = [{"name": "1xRTX3060-TW", "id": 100}]
        result = select_instance(instances)
        assert len(result) == 1
        assert result[0]["name"] == "1xRTX3060-TW"

    def test_select_all_returns_all_instances(self, monkeypatch):
        instances = [
            {"name": "1xRTX3060-TW", "id": 100},
            {"name": "2xA100-US", "id": 200},
        ]
        monkeypatch.setattr("builtins.input", lambda _: "a")
        result = select_instance(instances)
        assert len(result) == 2
        assert result[0]["name"] == "1xRTX3060-TW"
        assert result[1]["name"] == "2xA100-US"

    def test_select_by_number(self, monkeypatch):
        instances = [
            {"name": "1xRTX3060-TW", "id": 100},
            {"name": "2xA100-US", "id": 200},
        ]
        monkeypatch.setattr("builtins.input", lambda _: "2")
        result = select_instance(instances)
        assert len(result) == 1
        assert result[0]["name"] == "2xA100-US"

    def test_eof_returns_empty(self, monkeypatch):
        def raise_eof(_):
            raise EOFError

        instances = [{"name": "a", "id": 1}, {"name": "b", "id": 2}]
        monkeypatch.setattr("builtins.input", raise_eof)
        assert select_instance(instances) == []

    def test_keyboard_interrupt_returns_empty(self, monkeypatch):
        def raise_ki(_):
            raise KeyboardInterrupt

        instances = [{"name": "a", "id": 1}, {"name": "b", "id": 2}]
        monkeypatch.setattr("builtins.input", raise_ki)
        assert select_instance(instances) == []

    def test_out_of_range_returns_empty(self, monkeypatch):
        instances = [{"name": "a", "id": 1}, {"name": "b", "id": 2}]
        monkeypatch.setattr("builtins.input", lambda _: "5")
        assert select_instance(instances) == []

    def test_zero_returns_empty(self, monkeypatch):
        instances = [{"name": "a", "id": 1}, {"name": "b", "id": 2}]
        monkeypatch.setattr("builtins.input", lambda _: "0")
        assert select_instance(instances) == []

    def test_non_digit_returns_empty(self, monkeypatch):
        instances = [{"name": "a", "id": 1}, {"name": "b", "id": 2}]
        monkeypatch.setattr("builtins.input", lambda _: "xyz")
        assert select_instance(instances) == []


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
        assert name == "1x"


class TestLocalRepoInfo:
    """Test that _local_repo_info passes URLs through without conversion."""

    def test_https_url_not_converted_to_ssh(self, monkeypatch):
        """HTTPS URLs should pass through as-is, not be converted to SSH."""
        https_url = "https://github.com/user/repo.git"
        monkeypatch.setattr(
            "vastly.cli.subprocess.run",
            lambda *a, **kw: type(
                "R", (), {"returncode": 0, "stdout": https_url, "stderr": ""}
            )(),
        )
        from vastly.cli import _local_repo_info

        result = _local_repo_info("origin")
        assert result is not None
        url, name = result
        assert url == https_url
        assert name == "repo"

    def test_ssh_url_passes_through(self, monkeypatch):
        ssh_url = "git@github.com:user/repo.git"
        monkeypatch.setattr(
            "vastly.cli.subprocess.run",
            lambda *a, **kw: type(
                "R", (), {"returncode": 0, "stdout": ssh_url, "stderr": ""}
            )(),
        )
        from vastly.cli import _local_repo_info

        result = _local_repo_info("origin")
        assert result is not None
        url, name = result
        assert url == ssh_url
        assert name == "repo"

    def test_https_url_without_dotgit_suffix(self, monkeypatch):
        https_url = "https://github.com/user/my-project"
        monkeypatch.setattr(
            "vastly.cli.subprocess.run",
            lambda *a, **kw: type(
                "R", (), {"returncode": 0, "stdout": https_url, "stderr": ""}
            )(),
        )
        from vastly.cli import _local_repo_info

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
        assert "UserKnownHostsFile /dev/null" in content

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
        (tmp_path / "host1").write_text("# Generated by vastly -- do not edit\nHost host1\n")
        (tmp_path / "host2").write_text("# Generated by vastly -- do not edit\nHost host2\n")

        clear_ssh_configs()

        assert list(tmp_path.iterdir()) == []

    def test_clear_preserves_non_generated_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)
        (tmp_path / "host1").write_text("# Generated by vastly -- do not edit\nHost host1\n")
        (tmp_path / "custom").write_text("Host myserver\n    HostName 10.0.0.1\n")

        clear_ssh_configs()

        remaining = [f.name for f in tmp_path.iterdir()]
        assert remaining == ["custom"]

    def test_clear_skips_directories(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)
        (tmp_path / "host1").write_text("# Generated by vastly -- do not edit\nHost host1\n")
        (tmp_path / "subdir").mkdir()

        clear_ssh_configs()

        remaining = [f.name for f in tmp_path.iterdir()]
        assert remaining == ["subdir"]

    def test_cached_names_returns_file_names(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)
        (tmp_path / "1xRTX4090-TW").write_text("config")
        (tmp_path / "US-2xA100").write_text("config")

        names = cached_config_names()

        assert set(names) == {"1xRTX4090-TW", "US-2xA100"}

    def test_cached_names_empty_when_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path / "nonexistent")

        assert cached_config_names() == []


class TestRunScp:
    def test_recursive_flag_added(self, monkeypatch):
        """run_scp with recursive=True should include -r in the command."""
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("vastly.ssh.subprocess.run", fake_run)
        from vastly.ssh import run_scp

        run_scp("/tmp/src", "host:/tmp/dest", recursive=True)
        assert "-r" in captured_cmd

    def test_no_recursive_flag_by_default(self, monkeypatch):
        """run_scp without recursive should not include -r."""
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

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
    """Test that setup markers store and compare repoUrl correctly."""

    def test_marker_json_includes_repo_url(self):
        """The setup-remote.sh script should write repoUrl into the marker."""
        content = (DATA / "setup-remote.sh").read_text(encoding="utf-8")
        assert '"repoUrl"' in content

    def test_matching_repo_url_skips_setup(self, monkeypatch):
        """When marker contains the same repoUrl, setup should be skipped."""
        from vastly.remote import setup_instances

        repo_url = "git@github.com:user/app.git"
        marker_json = json.dumps({"repoUrl": repo_url, "timestamp": "2025-01-01"})

        ssh_calls = []

        def fake_ssh(name, cmd, **kwargs):
            ssh_calls.append(cmd)
            if "echo ok" in cmd:
                return type("R", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
            if "cat ~/.vastly/setup/" in cmd:
                return type("R", (), {"returncode": 0, "stdout": marker_json, "stderr": ""})()
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", lambda *a, **kw: None)

        instances = [{"name": "1xA100-US"}]
        config = {
            "installCommand": None,
            "disableAutoTmux": False,
            "workspace": "/workspace",
            "postInstall": [],
        }

        result = setup_instances(instances, repo_url, "app", config)

        assert result == ["1xA100-US"]
        # Should NOT have run the setup script (no scp call for setup script)
        assert not any("_vastly-setup.sh" in c for c in ssh_calls)

    def test_mismatched_repo_url_triggers_setup(self, monkeypatch):
        """When marker repoUrl differs, setup should re-run."""
        from vastly.remote import setup_instances

        old_url = "git@github.com:old-org/app.git"
        new_url = "git@github.com:new-org/app.git"
        marker_json = json.dumps({"repoUrl": old_url, "timestamp": "2025-01-01"})

        def fake_ssh(name, cmd, **kwargs):
            if "echo ok" in cmd:
                return type("R", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
            if "cat ~/.vastly/setup/" in cmd:
                return type("R", (), {"returncode": 0, "stdout": marker_json, "stderr": ""})()
            # Setup script execution
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        scp_calls = []

        def fake_scp(*args, **kwargs):
            scp_calls.append(args)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", fake_scp)
        monkeypatch.setattr(
            "vastly.remote.subprocess.run",
            lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "Test User\n", "stderr": ""})(),
        )

        instances = [{"name": "1xA100-US"}]
        config = {
            "installCommand": None,
            "disableAutoTmux": False,
            "workspace": "/workspace",
            "postInstall": [],
        }

        result = setup_instances(instances, new_url, "app", config)

        # Setup script should have been SCP'd (re-run triggered)
        assert len(scp_calls) > 0

    def test_corrupted_marker_triggers_setup(self, monkeypatch):
        """When marker contains invalid JSON, setup should re-run."""
        from vastly.remote import setup_instances

        def fake_ssh(name, cmd, **kwargs):
            if "echo ok" in cmd:
                return type("R", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
            if "cat ~/.vastly/setup/" in cmd:
                return type("R", (), {"returncode": 0, "stdout": "not valid json{", "stderr": ""})()
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        scp_calls = []

        def fake_scp(*args, **kwargs):
            scp_calls.append(args)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("vastly.remote.run_ssh", fake_ssh)
        monkeypatch.setattr("vastly.remote.run_scp", fake_scp)
        monkeypatch.setattr(
            "vastly.remote.subprocess.run",
            lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "Test User\n", "stderr": ""})(),
        )

        instances = [{"name": "1xA100-US"}]
        config = {
            "installCommand": None,
            "disableAutoTmux": False,
            "workspace": "/workspace",
            "postInstall": [],
        }

        result = setup_instances(instances, "git@github.com:user/app.git", "app", config)

        assert len(scp_calls) > 0


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
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not be called")),
        )

        check_for_update()  # Should return silently (cache is fresh)

    def test_network_error_silently_swallowed(self, tmp_path, monkeypatch):
        """Network failures should not raise or print errors."""
        from vastly.update import check_for_update

        # No cache file -- will try to fetch
        monkeypatch.setattr("vastly.update._CACHE_FILE", tmp_path / ".last-update-check")
        monkeypatch.setattr("vastly.update._CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            "vastly.update.urllib.request.urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("no internet")),
        )

        check_for_update()  # Should not raise
