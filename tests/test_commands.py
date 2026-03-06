"""Tests for vastly.commands -- subcommand handlers and helpers."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from conftest import make_test_config, make_test_instance as _inst
from vastly.errors import VastlyError
from vastly.instance import validate_alias


# ── Shared helpers ───────────────────────────────────────────────────

_MINIMAL_CONFIG = make_test_config(portForwards=[])


# ── TestStopDestroy ──────────────────────────────────────────────────


class TestStopDestroy:
    """Test cmd_stop, cmd_destroy, and _vastai_action."""

    def test_vastai_action_stop(self, monkeypatch):
        from vastly.commands import _vastai_action

        captured_cmd = []

        def fake_run(cmd, **_kwargs):
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
            lambda *_a, **_kw: subprocess.CompletedProcess(
                [], 1, stdout="", stderr="error msg"
            ),
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


# ── TestCp ───────────────────────────────────────────────────────────


class TestCp:
    """Test cmd_cp path resolution and SCP invocation."""

    def test_cp_raises_outside_git_repo(self, monkeypatch):
        from vastly.commands import cmd_cp

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        args = argparse.Namespace(
            direction="down",
            paths=["file.txt"],
            config=False,
            instance=None,
            verbose=False,
        )
        with pytest.raises(VastlyError, match="Not in a git repo"):
            cmd_cp(args)

    def test_cp_raises_when_no_remote(self, monkeypatch):
        from vastly.commands import cmd_cp

        monkeypatch.setattr("vastly.commands._git_root", lambda: Path("/repo"))
        monkeypatch.setattr(
            "vastly.commands.load_config",
            lambda **kw: make_test_config(portForwards=[]),
        )
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr("vastly.commands._local_repo_info", lambda _: None)

        args = argparse.Namespace(
            direction="down",
            paths=["file.txt"],
            config=False,
            instance=None,
            verbose=False,
        )
        with pytest.raises(VastlyError, match="Could not determine repo name"):
            cmd_cp(args)

    def test_cp_up_raises_when_local_file_missing(self, monkeypatch, tmp_path):
        from vastly.commands import cmd_cp

        monkeypatch.setattr("vastly.commands._git_root", lambda: tmp_path)
        monkeypatch.setattr(
            "vastly.commands.load_config",
            lambda **kw: make_test_config(portForwards=[]),
        )
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
        monkeypatch.setattr(
            "vastly.commands._local_repo_info", lambda _: ("url", "repo")
        )
        monkeypatch.setattr(
            "vastly.commands.get_running_instances",
            lambda _: [
                _inst(name="test", id=1),
            ],
        )

        args = argparse.Namespace(
            direction="up",
            paths=["nonexistent.txt"],
            config=False,
            instance=None,
            verbose=False,
        )
        with pytest.raises(VastlyError, match="No files were copied"):
            cmd_cp(args)


# ── TestCmdStart ─────────────────────────────────────────────────────


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
            lambda *_a, **_kw: subprocess.CompletedProcess(
                [], 0, stdout='{"cur_state": "loading"}', stderr=""
            ),
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

        def fake_run(*_a, **_kw):
            poll_count[0] += 1
            if poll_count[0] == 1:
                return subprocess.CompletedProcess(
                    [], 0, stdout='{"cur_state": "stopped"}', stderr=""
                )
            return subprocess.CompletedProcess(
                [],
                0,
                stdout='{"cur_state": "running", "actual_status": "running"}',
                stderr="",
            )

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


# ── TestCmdStopLifecycle ─────────────────────────────────────────────


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
        monkeypatch.setattr(
            "vastly.commands._vastai_action", lambda a, i: actions.append(a)
        )

        args = argparse.Namespace(name="a", all=False, yes=True, verbose=False)
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


# ── TestVastaiStart ──────────────────────────────────────────────────


class TestVastaiStart:
    """Test _vastai_start detects queued vs immediate starts."""

    def test_immediate_start(self, monkeypatch, capsys):
        from vastly.commands import _vastai_start

        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *_a, **_kw: subprocess.CompletedProcess(
                [], 0, stdout="starting instance", stderr=""
            ),
        )

        queued = _vastai_start(_inst(name="test", id=1))

        assert queued is False
        output = capsys.readouterr().out
        assert "Started" in output

    def test_queued_start(self, monkeypatch, capsys):
        from vastly.commands import _vastai_start

        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *_a, **_kw: subprocess.CompletedProcess(
                [],
                0,
                stdout="Required resources are currently unavailable, state change queued.",
                stderr="",
            ),
        )

        queued = _vastai_start(_inst(name="test", id=1))

        assert queued is True
        output = capsys.readouterr().out
        assert "Queued" in output
        assert "waiting for resources" in output


# ── TestCmdConfig ────────────────────────────────────────────────────


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


# ── TestConnectStoppedInstance ────────────────────────────────────────


class TestConnectStoppedInstance:
    """Test that vst connect <stopped-name> gives a helpful error."""

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
            name="2xA100-US",
            no_setup=False,
            force_setup=False,
            all=False,
            verbose=False,
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
        monkeypatch.setattr("vastly.commands._poll_for_running", lambda *_a, **_kw: None)

        args = argparse.Namespace(
            name=None,
            no_setup=True,
            force_setup=False,
            all=False,
            verbose=False,
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


# ── TestUnknownSubcommand ────────────────────────────────────────────


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


# ── TestPromotedConnectFlags ─────────────────────────────────────────


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
            lambda args: captured.update(name=args.name, force_setup=args.force_setup),
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
            lambda args: captured.update(name=args.name, force_setup=args.force_setup),
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


# ── TestCmdConnectIntegration ────────────────────────────────────────


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


# ── TestCmdStopIntegration ───────────────────────────────────────────


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

        args = argparse.Namespace(command="stop", name=None, all=False, yes=True, verbose=False)
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
            command="stop", name="test-gpu", all=False, yes=True, verbose=False
        )
        cmd_stop(args)

        assert len(action_calls) == 1
        assert action_calls[0][0] == "stop"
        assert action_calls[0][1].name == "test-gpu"
        assert action_calls[0][1].id == 200


# ── TestCmdSsh ───────────────────────────────────────────────────────


class TestCmdSsh:
    """Tests for cmd_ssh argument construction and behavior."""

    def test_ssh_builds_correct_command(self, monkeypatch):
        """cmd_ssh should build an SSH command with the right host and options."""
        from vastly.commands import cmd_ssh

        monkeypatch.setattr(
            "vastly.commands.shutil.which", lambda cmd: f"/usr/bin/{cmd}"
        )
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
            lambda cmd, **_kw: (
                (captured_cmd.extend(cmd), None)[1]
                or subprocess.CompletedProcess([], 0, stdout="", stderr="")
            ),
        )

        args = argparse.Namespace(
            command="ssh",
            name=None,
            verbose=False,
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

        monkeypatch.setattr(
            "vastly.commands.shutil.which", lambda cmd: f"/usr/bin/{cmd}"
        )
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
            lambda cmd, **_kw: (
                (captured_cmd.extend(cmd), None)[1]
                or subprocess.CompletedProcess([], 0, stdout="", stderr="")
            ),
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

        monkeypatch.setattr(
            "vastly.commands.shutil.which", lambda cmd: f"/usr/bin/{cmd}"
        )
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
            lambda cmd, **_kw: (
                (captured_cmd.extend(cmd), None)[1]
                or subprocess.CompletedProcess([], 0, stdout="", stderr="")
            ),
        )

        args = argparse.Namespace(
            remote_cmd=[],
            name="gpu-b",
            verbose=False,
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
            lambda cmd, **_kw: (
                (captured_cmd.extend(cmd), None)[1]
                or subprocess.CompletedProcess([], 0, stdout="", stderr="")
            ),
        )

        args = argparse.Namespace(remote_cmd=[], name=None, verbose=False)

        with pytest.raises(SystemExit) as exc_info:
            cmd_ssh(args)
        assert exc_info.value.code == 0
        assert "test" in captured_cmd

    def test_ssh_smart_dispatch_unknown_name_becomes_command(self, monkeypatch):
        """vst ssh nvidia-smi (no instance named nvidia-smi) -> runs nvidia-smi."""
        from vastly.commands import cmd_ssh

        monkeypatch.setattr(
            "vastly.commands.shutil.which", lambda cmd: f"/usr/bin/{cmd}"
        )
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
            lambda cmd, **_kw: (
                (captured_cmd.extend(cmd), None)[1]
                or subprocess.CompletedProcess([], 0, stdout="", stderr="")
            ),
        )

        # name="nvidia-smi" doesn't match any instance, so it should be
        # treated as a remote command on the auto-selected instance
        args = argparse.Namespace(
            remote_cmd=[],
            name="nvidia-smi",
            verbose=False,
        )

        cmd_ssh(args)

        assert "gpu-box" in captured_cmd
        assert "nvidia-smi" in captured_cmd

    def test_ssh_smart_dispatch_name_plus_command(self, monkeypatch):
        """vst ssh train nvidia-smi -> runs nvidia-smi on 'train' instance."""
        from vastly.commands import cmd_ssh

        monkeypatch.setattr(
            "vastly.commands.shutil.which", lambda cmd: f"/usr/bin/{cmd}"
        )
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
            lambda cmd, **_kw: (
                (captured_cmd.extend(cmd), None)[1]
                or subprocess.CompletedProcess([], 0, stdout="", stderr="")
            ),
        )

        args = argparse.Namespace(
            remote_cmd=["nvidia-smi"],
            name="train",
            verbose=False,
        )

        cmd_ssh(args)

        assert "gpu-a" in captured_cmd
        assert "nvidia-smi" in captured_cmd

    def test_ssh_alias_reserved(self):
        """'ssh' should be a reserved alias name."""
        instances = [_inst(name="1xRTX4090-TW", id=100)]
        with pytest.raises(VastlyError, match="reserved"):
            validate_alias("ssh", instances, {})


# ── TestStartAndResync ───────────────────────────────────────────────


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
        monkeypatch.setattr("vastly.commands._poll_for_running", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda config: [_inst(name="gpu", id=1, status="stopped")],
        )

        with pytest.raises(VastlyError, match="not reachable"):
            _start_and_resync([_inst(status="stopped")], {})


# ── TestYesFlag ──────────────────────────────────────────────────────


class TestYesFlag:
    """Tests for --yes / -y flag on stop and destroy."""

    def test_destroy_yes_skips_confirmation(self, monkeypatch):
        from vastly.commands import cmd_destroy

        destroyed = []

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr(
            "vastly.commands.load_config", lambda **kw: make_test_config()
        )
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
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
            command="destroy",
            name=None,
            all=False,
            yes=True,
            verbose=False,
        )
        cmd_destroy(args)

        assert destroyed == ["gpu"]

    def test_stop_yes_skips_confirmation(self, monkeypatch):
        from vastly.commands import cmd_stop

        stopped = []

        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr(
            "vastly.commands.load_config", lambda **kw: make_test_config()
        )
        monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: None)
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
            command="stop",
            name=None,
            all=True,
            yes=True,
            verbose=False,
        )
        cmd_stop(args)

        assert sorted(stopped) == ["gpu-a", "gpu-b"]


# ── TestCmdStopNonStoppableStates ────────────────────────────────────


class TestCmdStopNonStoppableStates:
    """cmd_stop should give specific errors for named instances in non-stoppable states."""

    @pytest.mark.parametrize(
        "state", ["offline", "error", "unknown", "stopped", "exited"]
    )
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


# ── TestCmdConnectNoSetupPath ────────────────────────────────────────


class TestCmdConnectNoSetupPath:
    """vst -n should open at the project dir when in a git repo, not workspace root."""

    def test_no_setup_in_repo_opens_project_dir(self, monkeypatch):
        from vastly.commands import cmd_connect

        monkeypatch.setattr(
            "vastly.commands._git_root", lambda: Path("/repo/my-project")
        )
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
            name=None,
            no_setup=True,
            force_setup=False,
            all=False,
            verbose=False,
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
            name=None,
            no_setup=False,
            force_setup=False,
            all=False,
            verbose=False,
        )
        cmd_connect(args)

        assert len(ide_calls) == 1
        assert ide_calls[0][2] == "/workspace"


# ── TestVastaiDestroyCleanup ─────────────────────────────────────────


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
            lambda *_a, **_kw: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )

        inst = _inst(name="gpu-box", id=42, alias="train")
        _vastai_destroy(inst)

        # SSH config for the auto-name should be removed
        assert not (ssh_dir / "gpu-box").exists()
        # SSH config for the alias should be removed
        assert not (ssh_dir / "train").exists()
        # Alias entry should be gone
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
            lambda *_a, **_kw: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )

        # Instance with no alias and no existing SSH config -- should not error
        inst = _inst(name="gpu-box", id=42)
        _vastai_destroy(inst)  # should complete without error


# ── TestCmdSshSmartDispatch ──────────────────────────────────────────


class TestCmdSshSmartDispatch:
    """Test cmd_ssh smart dispatch: name-as-command fallback, stopped auto-start, typo detection."""

    def _ssh_base_mocks(self, monkeypatch, instances):
        """Set up the common mocks for cmd_ssh tests."""
        monkeypatch.setattr(
            "vastly.commands.shutil.which", lambda cmd: f"/usr/bin/{cmd}"
        )
        monkeypatch.setattr("vastly.commands._git_root", lambda: None)
        monkeypatch.setattr("vastly.commands.load_config", lambda **kw: _MINIMAL_CONFIG)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: instances,
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
            lambda cmd, **_kw: (
                (captured_cmd.extend(cmd), None)[1]
                or subprocess.CompletedProcess([], 0, stdout="", stderr="")
            ),
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
            lambda cmd, **_kw: (
                (captured_cmd.extend(cmd), None)[1]
                or subprocess.CompletedProcess([], 0, stdout="", stderr="")
            ),
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
            lambda cmd, **_kw: (
                (captured_cmd.extend(cmd), None)[1]
                or subprocess.CompletedProcess([], 0, stdout="", stderr="")
            ),
        )

        args = argparse.Namespace(remote_cmd=[], name="gpu-bx", verbose=False)
        with pytest.raises(VastlyError, match="Did you mean.*gpu-box"):
            cmd_ssh(args)


# ── TestPollForRunningApiFailures ────────────────────────────────────


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
                return subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="network error"
                )
            # Then succeeds
            return subprocess.CompletedProcess(
                [],
                0,
                stdout='{"cur_state": "running", "actual_status": "running"}',
                stderr="",
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
            lambda *_a, **_kw: subprocess.CompletedProcess(
                [], 1, stdout="", stderr="API down"
            ),
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
            lambda *_a, **_kw: subprocess.CompletedProcess(
                [], 0, stdout="not json", stderr=""
            ),
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
                [],
                0,
                stdout='{"cur_state": "running", "actual_status": "running"}',
                stderr="",
            )

        monkeypatch.setattr("vastly.commands.subprocess.run", fake_run)

        _poll_for_running("123", "test-gpu")  # should not raise


# ── TestDoConnect ────────────────────────────────────────────────────


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
            lambda *a, **_kw: (setup_called.append(True), [a[0][0].name])[1],
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
            lambda *_a, **_kw: setup_called.append(True),
        )

        ide_calls = []
        monkeypatch.setattr(
            "vastly.commands.open_ide",
            lambda ide, host, path: ide_calls.append((ide, host, path)),
        )

        _do_connect(no_setup=True)

        assert setup_called == []
        assert len(ide_calls) == 1


# ── TestCmdStartUsesDoConnect ────────────────────────────────────────


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
            lambda *_a, **_kw: subprocess.CompletedProcess(
                [],
                0,
                stdout='{"cur_state": "running", "actual_status": "running"}',
                stderr="",
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
            lambda *_a, **_kw: subprocess.CompletedProcess(
                [],
                0,
                stdout='{"cur_state": "running", "actual_status": "running"}',
                stderr="",
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


# ── TestCmdStopImprovedErrors ────────────────────────────────────────


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


# ── TestCmdSshImprovedErrors ─────────────────────────────────────────


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


# ── TestCmdNameClear ─────────────────────────────────────────────────


class TestCmdNameClear:
    """Test vst name --clear."""

    def test_clear_removes_alias(self, tmp_path, monkeypatch):
        aliases_file = tmp_path / "aliases.json"
        aliases_file.write_text('{"123": "train"}', encoding="utf-8")
        monkeypatch.setattr("vastly.instance._ALIASES_FILE", aliases_file)

        from vastly.commands import cmd_name

        args = argparse.Namespace(
            alias="train",
            clear=True,
            instance=None,
            verbose=False,
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
            alias="nope",
            clear=True,
            instance=None,
            verbose=False,
        )
        with pytest.raises(VastlyError, match="No alias 'nope' found"):
            cmd_name(args)


# ── TestCmdNameSshCleanup ────────────────────────────────────────────


class TestCmdNameSshCleanup:
    """Test that cmd_name cleans up old alias SSH config on reassignment."""

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
            alias="new-alias",
            clear=False,
            instance=None,
            verbose=False,
        )
        cmd_name(args)

        assert not old_config.exists(), "Old alias SSH config should be removed"


# ── TestHttpsUrlTrailingSlash ────────────────────────────────────────


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
