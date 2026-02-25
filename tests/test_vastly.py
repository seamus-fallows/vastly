"""Tests for vastly."""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vastly import __version__, cyan, dim, green, red, yellow
from vastly.config import load_config
from vastly.instance import build_instance_name, format_uptime, select_instance
from vastly.remote import convert_to_ssh_url
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



class TestLoadConfig:

    def test_defaults_on_empty_json(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text("{}", encoding="utf-8")

        result = load_config(cfg)

        assert result["ide"] == "code"
        assert result["sshUser"] == "root"
        assert result["workspace"] == "/workspace"
        assert result["disableAutoTmux"] is True
        assert result["gitRemote"] == "origin"

    def test_reads_user_values(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text(json.dumps({
            "ide": "cursor",
            "sshUser": "ubuntu",
            "workspace": "/home/ubuntu",
            "sshKeyPath": "C:\\Users\\me\\.ssh\\id_rsa",
            "disableAutoTmux": False,
            "gitRemote": "upstream",
            "installCommand": "uv sync",
            "portForwards": [{"local": 3000, "remote": 3000}],
            "postInstall": ["echo hello"],
        }), encoding="utf-8")

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

    def test_exits_on_invalid_json(self, tmp_path):
        cfg = tmp_path / ".vastly.json"
        cfg.write_text("{invalid json}", encoding="utf-8")

        with pytest.raises(SystemExit):
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

    def test_returns_empty_for_bad_name(self):
        instances = [
            {"name": "1xRTX3060-TW", "id": 100},
            {"name": "2xA100-US", "id": 200},
        ]
        result = select_instance(instances, "NOPE")
        assert result == []

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
        inst = {"gpu_name": "RTX 4090", "num_gpus": 1, "geolocation": "Taipei, TW", "id": 111}
        seen = set()
        assert build_instance_name(inst, seen) == "1xRTX4090-TW"

    def test_omits_country_when_empty(self):
        inst = {"gpu_name": "A100", "num_gpus": 2, "geolocation": "", "id": 111}
        seen = set()
        assert build_instance_name(inst, seen) == "2xA100"

    def test_strips_spaces_from_gpu(self):
        inst = {"gpu_name": "GeForce RTX 3060", "num_gpus": 1, "geolocation": "", "id": 111}
        seen = set()
        name = build_instance_name(inst, seen)
        assert " " not in name

    def test_appends_id_on_collision(self):
        inst1 = {"gpu_name": "RTX 4090", "num_gpus": 1, "geolocation": "Taipei, TW", "id": 111}
        inst2 = {"gpu_name": "RTX 4090", "num_gpus": 1, "geolocation": "Taipei, TW", "id": 222}
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


class TestUrlConversion:

    def test_converts_github_https_to_ssh(self):
        assert convert_to_ssh_url("https://github.com/user/repo.git") == "git@github.com:user/repo.git"

    def test_converts_gitlab_https_to_ssh(self):
        assert convert_to_ssh_url("https://gitlab.com/user/repo.git") == "git@gitlab.com:user/repo.git"

    def test_converts_self_hosted_https_to_ssh(self):
        assert convert_to_ssh_url("https://git.example.com/org/repo.git") == "git@git.example.com:org/repo.git"

    def test_strips_trailing_slash(self):
        assert convert_to_ssh_url("https://github.com/user/repo/") == "git@github.com:user/repo"

    def test_leaves_ssh_urls_unchanged(self):
        assert convert_to_ssh_url("git@github.com:user/repo.git") == "git@github.com:user/repo.git"

    def test_http_url_passes_through(self):
        """Only https:// is converted, not http://."""
        url = "http://github.com/user/repo.git"
        assert convert_to_ssh_url(url) == url

    def test_empty_string_passes_through(self):
        assert convert_to_ssh_url("") == ""

    def test_nested_gitlab_path(self):
        result = convert_to_ssh_url("https://gitlab.com/group/subgroup/repo.git")
        assert result == "git@gitlab.com:group/subgroup/repo.git"


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
            "test", host="10.0.0.1", port=22, user="root",
            key_path="", local_forwards=[])
        content = (tmp_path / "test").read_text()
        assert "IdentityFile" not in content

    def test_no_local_forward_lines_when_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)
        write_ssh_config(
            "test", host="10.0.0.1", port=22, user="root",
            key_path=None, local_forwards=[])
        content = (tmp_path / "test").read_text()
        assert "LocalForward" not in content



class TestConfigTemplate:

    def test_is_valid_json(self):
        template = (DATA / ".vastly.template.json").read_text(encoding="utf-8")
        json.loads(template)

    def test_has_expected_keys(self):
        template = json.loads((DATA / ".vastly.template.json").read_text(encoding="utf-8"))
        expected = {"ide", "sshKeyPath", "sshUser", "portForwards", "workspace",
                    "disableAutoTmux", "gitRemote", "postInstall", "installCommand"}
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
        (ssh_dir / "config").write_text("Host myserver\n    HostName 10.0.0.1\n", encoding="utf-8")

        ensure_ssh_include()

        content = (ssh_dir / "config").read_text()
        assert content.startswith("Include vast.d/*")
        assert "Host myserver" in content

    def test_does_not_duplicate_include(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "config").write_text("Include vast.d/*\nHost myserver\n", encoding="utf-8")

        ensure_ssh_include()

        content = (ssh_dir / "config").read_text()
        assert content.count("Include vast.d/*") == 1

    def test_detects_existing_backslash_include(self, tmp_path, monkeypatch):
        """Windows-style backslash in existing Include should be detected."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "config").write_text("Include vast.d\\*\nHost myserver\n", encoding="utf-8")

        ensure_ssh_include()

        content = (ssh_dir / "config").read_text()
        assert content.count("Include") == 1


class TestSshConfigManagement:

    def test_clear_removes_all_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)
        (tmp_path / "host1").write_text("config1")
        (tmp_path / "host2").write_text("config2")

        clear_ssh_configs()

        assert list(tmp_path.iterdir()) == []

    def test_clear_skips_directories(self, tmp_path, monkeypatch):
        monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", tmp_path)
        (tmp_path / "host1").write_text("config1")
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


class TestSetupRemoteScript:

    def test_bundled_script_exists(self):
        assert (DATA / "setup-remote.sh").exists()

    def test_valid_bash_syntax(self):
        if not shutil.which("bash"):
            pytest.skip("bash not available")
        result = subprocess.run(
            ["bash", "-n", str(DATA / "setup-remote.sh")],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_script_uses_strict_mode(self):
        content = (DATA / "setup-remote.sh").read_text(encoding="utf-8")
        assert "set -euo pipefail" in content


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
