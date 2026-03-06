"""Tests for vastly.config -- loading, validation, defaults, and templates."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conftest import make_test_config
from vastly.config import (
    DEFAULTS,
    _detect_ide,
    _ide_from_env,
    _validate_config,
    ensure_config,
    load_config,
)
from vastly.errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "vastly"
DATA = SRC / "data"

# Environment variables that IDE detection checks
_ENV_VARS = (
    "CURSOR_TRACE_ID",
    "TERM_PROGRAM",
    "VSCODE_GIT_ASKPASS_MAIN",
    "VSCODE_CODE_CACHE_PATH",
)


class TestIdeFromEnv:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for var in _ENV_VARS:
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
        for var in _ENV_VARS:
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
        for var in _ENV_VARS:
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
        for var in _ENV_VARS:
            monkeypatch.delenv(var, raising=False)

    def test_valid_config_passes(self):
        """A well-formed config dict should not raise."""
        _validate_config(make_test_config())  # should not raise

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
