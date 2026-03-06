"""Orchestration tests for complex, branch-heavy command functions.

Tests _do_connect, cmd_start, cmd_destroy, and _pick_startable by mocking
at the boundary (sync_instances, setup_instances, open_ide, subprocess, etc.)
and verifying the orchestration logic for each conditional path.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from conftest import make_test_instance as _inst
from vastly.config import DEFAULTS
from vastly.errors import VastlyError


def _config(**kwargs) -> dict:
    """Create a complete config dict with sensible defaults for testing."""
    base = {**DEFAULTS}
    base.update(kwargs)
    return base


_MINIMAL_CONFIG = _config(portForwards=[])


# ---------------------------------------------------------------------------
# Common monkeypatch helpers
# ---------------------------------------------------------------------------

def _patch_base(monkeypatch, *, git_root=None, config=None):
    """Patch the common prerequisites shared by most command functions."""
    monkeypatch.setattr("vastly.commands._git_root", lambda: git_root)
    monkeypatch.setattr(
        "vastly.commands.load_config", lambda **kw: config or _MINIMAL_CONFIG
    )
    monkeypatch.setattr("vastly.commands._check_prerequisites", lambda **kw: "code")


def _patch_do_connect_base(monkeypatch, *, git_root=None, config=None):
    """Patch prerequisites specific to _do_connect (includes repo info)."""
    _patch_base(monkeypatch, git_root=git_root or Path("/repo/my-proj"), config=config)
    monkeypatch.setattr(
        "vastly.commands._local_repo_info",
        lambda _: ("git@github.com:user/my-proj.git", "my-proj"),
    )


# ===========================================================================
# _pick_startable
# ===========================================================================


class TestPickStartable:
    """Tests for _pick_startable -- the helper that selects instances to start."""

    def test_single_instance_auto_selects(self, capsys):
        from vastly.commands import _pick_startable

        inst = _inst(name="gpu", id=1, status="stopped")
        result = _pick_startable([inst])

        assert result == [inst]
        output = capsys.readouterr().out
        assert "Starting gpu" in output

    def test_single_instance_with_alias_shows_display_name(self, capsys):
        from vastly.commands import _pick_startable

        inst = _inst(name="gpu", id=1, status="stopped", alias="train")
        result = _pick_startable([inst])

        assert result == [inst]
        output = capsys.readouterr().out
        assert "train" in output

    def test_multiple_instances_prompts_user(self, monkeypatch):
        from vastly.commands import _pick_startable

        inst_a = _inst(name="gpu-a", id=1, status="stopped")
        inst_b = _inst(name="gpu-b", id=2, status="stopped")

        # select_instance is called when multiple instances need selection
        monkeypatch.setattr(
            "vastly.commands.select_instance",
            lambda instances: [instances[0]],
        )

        result = _pick_startable([inst_a, inst_b])

        assert len(result) == 1
        assert result[0] is inst_a

    def test_select_all_returns_all(self, capsys):
        from vastly.commands import _pick_startable

        instances = [
            _inst(name="gpu-a", id=1, status="stopped"),
            _inst(name="gpu-b", id=2, status="exited"),
        ]

        result = _pick_startable(instances, select_all=True)

        assert result == instances
        output = capsys.readouterr().out
        assert "Starting all 2" in output

    def test_select_all_single_returns_all(self, capsys):
        """select_all=True with a single instance still takes the select_all branch."""
        from vastly.commands import _pick_startable

        inst = _inst(name="gpu", id=1, status="stopped")
        result = _pick_startable([inst], select_all=True)

        assert result == [inst]
        output = capsys.readouterr().out
        # select_all branch prints "Starting all 1"
        assert "Starting all 1" in output


# ===========================================================================
# _do_connect
# ===========================================================================


class TestDoConnectOrchestration:
    """Tests for _do_connect -- the core connect flow orchestration."""

    def test_happy_path_setup_and_ide(self, monkeypatch):
        """Running instances found -> setup -> open IDE."""
        from vastly.commands import _do_connect

        _patch_do_connect_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="gpu", id=1, status="running")],
        )

        setup_calls = []
        monkeypatch.setattr(
            "vastly.commands.setup_instances",
            lambda *a, **kw: (setup_calls.append(a[0]), [a[0][0].name])[1],
        )

        ide_calls = []
        monkeypatch.setattr(
            "vastly.commands.open_ide",
            lambda ide, host, path: ide_calls.append((ide, host, path)),
        )
        monkeypatch.setattr("vastly.update.check_for_update", lambda: None)

        _do_connect()

        assert len(setup_calls) == 1
        assert len(ide_calls) == 1
        assert ide_calls[0] == ("code", "gpu", "/workspace/my-proj")

    def test_auto_start_when_only_stopped(self, monkeypatch):
        """No running instances, only stopped -> auto-start kicks in."""
        from vastly.commands import _do_connect

        _patch_do_connect_base(monkeypatch)

        started_ids = []
        polled_ids = []

        monkeypatch.setattr(
            "vastly.commands._vastai_start",
            lambda inst: (started_ids.append(inst.id), False)[1],
        )
        monkeypatch.setattr(
            "vastly.commands._poll_for_running",
            lambda inst_id, display_name="", *, queued=False: polled_ids.append(inst_id),
        )

        call_count = [0]

        def fake_sync(config):
            call_count[0] += 1
            if call_count[0] == 1:
                return [_inst(name="gpu", id=1, status="stopped")]
            return [_inst(name="gpu", id=1, status="running")]

        monkeypatch.setattr("vastly.commands.sync_instances", fake_sync)
        monkeypatch.setattr(
            "vastly.commands.setup_instances",
            lambda *a, **kw: [a[0][0].name],
        )
        monkeypatch.setattr("vastly.commands.open_ide", lambda *a: None)
        monkeypatch.setattr("vastly.update.check_for_update", lambda: None)

        _do_connect()

        assert started_ids == [1]
        assert polled_ids == ["1"]

    def test_no_instances_raises(self, monkeypatch):
        """Empty sync result -> VastlyError."""
        from vastly.commands import _do_connect

        _patch_do_connect_base(monkeypatch)
        monkeypatch.setattr("vastly.commands.sync_instances", lambda _: [])

        with pytest.raises(VastlyError, match="No Vast instances"):
            _do_connect()

    def test_named_instance_not_found_raises(self, monkeypatch):
        """Named instance doesn't match any instance -> VastlyError."""
        from vastly.commands import _do_connect

        _patch_do_connect_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="gpu-a", id=1, status="running")],
        )

        with pytest.raises(VastlyError, match="No instance named 'nonexistent'"):
            _do_connect(name="nonexistent")

    def test_named_inactive_instance_gives_start_hint(self, monkeypatch):
        """Named non-running instance that exists -> helpful start hint."""
        from vastly.commands import _do_connect

        _patch_do_connect_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [
                _inst(name="gpu-a", id=1, status="running"),
                _inst(name="gpu-b", id=2, status="stopped"),
            ],
        )

        with pytest.raises(VastlyError, match="inactive.*vst start"):
            _do_connect(name="gpu-b")

    def test_no_setup_skips_setup_calls_ide(self, monkeypatch):
        """no_setup=True -> setup_instances NOT called, open_ide IS called."""
        from vastly.commands import _do_connect

        _patch_do_connect_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="gpu", id=1, status="running")],
        )

        setup_calls = []
        monkeypatch.setattr(
            "vastly.commands.setup_instances",
            lambda *a, **kw: setup_calls.append(True),
        )

        ide_calls = []
        monkeypatch.setattr(
            "vastly.commands.open_ide",
            lambda ide, host, path: ide_calls.append((ide, host, path)),
        )

        _do_connect(no_setup=True)

        assert setup_calls == []
        assert len(ide_calls) == 1
        assert ide_calls[0][2] == "/workspace/my-proj"

    def test_no_setup_outside_repo_opens_workspace(self, monkeypatch):
        """no_setup with no git repo -> opens workspace root."""
        from vastly.commands import _do_connect

        _patch_base(monkeypatch, git_root=None)
        monkeypatch.setattr("vastly.commands._local_repo_info", lambda _: None)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="gpu", id=1, status="running")],
        )

        ide_calls = []
        monkeypatch.setattr(
            "vastly.commands.open_ide",
            lambda ide, host, path: ide_calls.append((ide, host, path)),
        )

        _do_connect(no_setup=True)

        assert len(ide_calls) == 1
        assert ide_calls[0][2] == "/workspace"

    def test_failed_setup_skips_ide_for_failed_instances(self, monkeypatch, capsys):
        """When setup_instances returns empty success list, IDE is NOT opened."""
        from vastly.commands import _do_connect

        _patch_do_connect_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="gpu", id=1, status="running")],
        )

        # setup_instances returns empty list (all failed)
        monkeypatch.setattr(
            "vastly.commands.setup_instances",
            lambda *a, **kw: [],
        )

        ide_calls = []
        monkeypatch.setattr(
            "vastly.commands.open_ide",
            lambda ide, host, path: ide_calls.append(True),
        )
        monkeypatch.setattr("vastly.update.check_for_update", lambda: None)

        _do_connect()

        assert ide_calls == []
        output = capsys.readouterr().out
        assert "Skipping" in output
        assert "setup failed" in output

    def test_partial_setup_failure_opens_ide_for_successes(self, monkeypatch, capsys):
        """When some instances fail setup, IDE opens only for successful ones."""
        from vastly.commands import _do_connect

        _patch_do_connect_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [
                _inst(name="gpu-a", id=1, status="running"),
                _inst(name="gpu-b", id=2, status="running"),
            ],
        )

        # Only gpu-a succeeds setup
        monkeypatch.setattr(
            "vastly.commands.setup_instances",
            lambda *a, **kw: ["gpu-a"],
        )
        monkeypatch.setattr(
            "vastly.commands.select_instance",
            lambda instances, name=None, allow_all=False, prompt="Select instance:": instances,
        )

        ide_calls = []
        monkeypatch.setattr(
            "vastly.commands.open_ide",
            lambda ide, host, path: ide_calls.append(host),
        )
        monkeypatch.setattr("vastly.update.check_for_update", lambda: None)

        _do_connect(select_all=True)

        assert ide_calls == ["gpu-a"]
        output = capsys.readouterr().out
        assert "Skipping gpu-b" in output

    def test_select_all_opens_ide_for_all_running(self, monkeypatch):
        """select_all=True -> opens IDE for every running instance."""
        from vastly.commands import _do_connect

        _patch_do_connect_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [
                _inst(name="gpu-a", id=1, status="running"),
                _inst(name="gpu-b", id=2, status="running"),
            ],
        )
        monkeypatch.setattr(
            "vastly.commands.setup_instances",
            lambda *a, **kw: [inst.name for inst in a[0]],
        )

        ide_calls = []
        monkeypatch.setattr(
            "vastly.commands.open_ide",
            lambda ide, host, path: ide_calls.append(host),
        )
        monkeypatch.setattr("vastly.update.check_for_update", lambda: None)

        _do_connect(select_all=True)

        assert sorted(ide_calls) == ["gpu-a", "gpu-b"]

    def test_named_stopped_auto_starts(self, monkeypatch):
        """Named stopped instance with no running -> auto-start it."""
        from vastly.commands import _do_connect

        _patch_do_connect_base(monkeypatch)

        started_ids = []
        monkeypatch.setattr(
            "vastly.commands._vastai_start",
            lambda inst: (started_ids.append(inst.id), False)[1],
        )
        monkeypatch.setattr(
            "vastly.commands._poll_for_running",
            lambda *a, **kw: None,
        )

        call_count = [0]

        def fake_sync(config):
            call_count[0] += 1
            if call_count[0] == 1:
                return [_inst(name="gpu", id=1, status="stopped")]
            return [_inst(name="gpu", id=1, status="running")]

        monkeypatch.setattr("vastly.commands.sync_instances", fake_sync)
        monkeypatch.setattr(
            "vastly.commands.setup_instances",
            lambda *a, **kw: [a[0][0].name],
        )
        monkeypatch.setattr("vastly.commands.open_ide", lambda *a: None)
        monkeypatch.setattr("vastly.update.check_for_update", lambda: None)

        _do_connect(name="gpu")

        assert started_ids == [1]

    def test_named_non_existent_stopped_raises(self, monkeypatch):
        """Named instance that doesn't exist at all -> clear error."""
        from vastly.commands import _do_connect

        _patch_do_connect_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="gpu-a", id=1, status="stopped")],
        )

        # _start_and_resync returns the instance as running
        monkeypatch.setattr(
            "vastly.commands._start_and_resync",
            lambda to_start, config: (
                [_inst(name="gpu-a", id=1, status="running")],
                [_inst(name="gpu-a", id=1, status="running")],
            ),
        )

        with pytest.raises(VastlyError, match="No instance named 'missing'"):
            _do_connect(name="missing")

    def test_named_inactive_non_startable_raises(self, monkeypatch):
        """Named instance in inactive, non-startable state -> clear error."""
        from vastly.commands import _do_connect

        _patch_do_connect_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="gpu", id=1, status="offline")],
        )

        # "offline" is not in STARTABLE_STATES, so find_by_name(startable, name)
        # fails, but find_by_name(all_instances, name) succeeds -> specific error
        with pytest.raises(VastlyError, match="inactive and cannot be started"):
            _do_connect(name="gpu")

    def test_named_truly_missing_raises(self, monkeypatch):
        """Named instance that doesn't exist in any state -> 'No instance named'."""
        from vastly.commands import _do_connect

        _patch_do_connect_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.sync_instances",
            lambda _: [_inst(name="gpu-a", id=1, status="stopped")],
        )
        # Need to mock _start_and_resync since there ARE startable instances
        # but the name doesn't match any of them
        with pytest.raises(VastlyError, match="No instance named 'nonexistent'"):
            _do_connect(name="nonexistent")


# ===========================================================================
# cmd_start
# ===========================================================================


class TestCmdStartOrchestration:
    """Tests for cmd_start -- state routing, polling, and auto-connect."""

    def test_happy_path_stopped_starts_polls_connects(self, monkeypatch):
        """Stopped instance -> _vastai_start -> poll -> _do_connect."""
        import vastly.commands
        from vastly.commands import cmd_start

        _patch_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [_inst(name="gpu", id=1, status="stopped")],
        )

        started_ids = []
        monkeypatch.setattr(
            "vastly.commands._vastai_start",
            lambda inst: (started_ids.append(inst.id), False)[1],
        )

        # Make polling instant
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(vastly.commands, "_START_TIMEOUT", 10)
        monkeypatch.setattr(vastly.commands, "_START_POLL_INTERVAL", 1)
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                [], 0,
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

        assert started_ids == [1]
        assert connect_kwargs["name"] == "gpu"

    def test_already_running_named_raises(self, monkeypatch):
        """Named instance that's already running -> VastlyError with connect hint."""
        from vastly.commands import cmd_start

        _patch_base(monkeypatch)
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

    def test_transitional_state_skips_start_call_waits(self, monkeypatch, capsys):
        """Instance in 'loading' state -> skip _vastai_start, just poll."""
        import vastly.commands
        from vastly.commands import cmd_start

        _patch_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [_inst(name="gpu", id=1, status="loading")],
        )

        started_ids = []
        monkeypatch.setattr(
            "vastly.commands._vastai_start",
            lambda inst: (started_ids.append(inst.id), False)[1],
        )

        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(vastly.commands, "_START_TIMEOUT", 10)
        monkeypatch.setattr(vastly.commands, "_START_POLL_INTERVAL", 1)
        monkeypatch.setattr(
            "vastly.commands.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                [], 0,
                stdout='{"cur_state": "running", "actual_status": "running"}',
                stderr="",
            ),
        )
        monkeypatch.setattr("vastly.commands._do_connect", lambda **kw: None)

        args = argparse.Namespace(name=None, no_connect=False, verbose=False)
        cmd_start(args)

        assert started_ids == []  # _vastai_start NOT called
        output = capsys.readouterr().out
        assert "already starting" in output

    def test_no_connect_returns_after_start(self, monkeypatch):
        """no_connect=True -> returns immediately after start, no poll."""
        from vastly.commands import cmd_start

        _patch_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [_inst(name="gpu", id=1, status="stopped")],
        )
        monkeypatch.setattr("vastly.commands._vastai_start", lambda inst: False)

        polled = []
        monkeypatch.setattr(
            "vastly.commands._poll_for_running",
            lambda *a, **kw: polled.append(True),
        )
        connect_calls = []
        monkeypatch.setattr(
            "vastly.commands._do_connect",
            lambda **kw: connect_calls.append(True),
        )

        args = argparse.Namespace(name=None, no_connect=True, verbose=False)
        cmd_start(args)

        assert polled == []
        assert connect_calls == []

    def test_exited_instance_starts(self, monkeypatch):
        """Exited instances are in STOPPED_STATES and should call _vastai_start."""
        from vastly.commands import cmd_start

        _patch_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [_inst(name="gpu", id=1, status="exited")],
        )

        started_ids = []
        monkeypatch.setattr(
            "vastly.commands._vastai_start",
            lambda inst: (started_ids.append(inst.id), False)[1],
        )

        args = argparse.Namespace(name=None, no_connect=True, verbose=False)
        cmd_start(args)

        assert started_ids == [1]

    def test_all_running_raises(self, monkeypatch):
        """All instances running -> VastlyError."""
        from vastly.commands import cmd_start

        _patch_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [_inst(name="gpu", id=1, status="running")],
        )

        args = argparse.Namespace(name=None, no_connect=False, verbose=False)
        with pytest.raises(VastlyError, match="All instances are already running"):
            cmd_start(args)

    def test_offline_non_startable_raises(self, monkeypatch):
        """Offline instance (not in STOPPED_STATES or TRANSITIONAL_STATES) -> error."""
        from vastly.commands import cmd_start

        _patch_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="gpu-a", id=1, status="running"),
                _inst(name="gpu-b", id=2, status="offline"),
            ],
        )

        args = argparse.Namespace(name="gpu-b", no_connect=False, verbose=False)
        with pytest.raises(VastlyError, match="Cannot start.*inactive"):
            cmd_start(args)

    def test_start_with_alias_passes_alias_to_connect(self, monkeypatch):
        """Instance with alias -> _do_connect receives alias, not auto-name."""
        import vastly.commands
        from vastly.commands import cmd_start

        _patch_base(monkeypatch)
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
                [], 0,
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

        assert connect_kwargs["name"] == "train"


# ===========================================================================
# cmd_destroy
# ===========================================================================


class TestCmdDestroyOrchestration:
    """Tests for cmd_destroy -- confirmation, --yes flag, bulk operations."""

    def test_single_instance_confirmation_accepted(self, monkeypatch):
        """Single instance + user confirms -> _vastai_destroy called."""
        from vastly.commands import cmd_destroy

        _patch_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [_inst(name="gpu", id=1)],
        )

        destroyed = []
        monkeypatch.setattr(
            "vastly.commands._vastai_destroy",
            lambda inst: destroyed.append(inst.name),
        )
        monkeypatch.setattr("vastly.commands._confirm", lambda prompt: True)

        args = argparse.Namespace(name=None, all=False, yes=False, verbose=False)
        cmd_destroy(args)

        assert destroyed == ["gpu"]

    def test_single_instance_confirmation_declined(self, monkeypatch):
        """Single instance + user declines -> _vastai_destroy NOT called."""
        from vastly.commands import cmd_destroy

        _patch_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [_inst(name="gpu", id=1)],
        )

        destroyed = []
        monkeypatch.setattr(
            "vastly.commands._vastai_destroy",
            lambda inst: destroyed.append(inst.name),
        )
        monkeypatch.setattr("vastly.commands._confirm", lambda prompt: False)

        args = argparse.Namespace(name=None, all=False, yes=False, verbose=False)
        cmd_destroy(args)

        assert destroyed == []

    def test_yes_flag_skips_confirmation(self, monkeypatch):
        """--yes flag -> _confirm NOT called, _vastai_destroy called."""
        from vastly.commands import cmd_destroy

        _patch_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [_inst(name="gpu", id=1)],
        )

        destroyed = []
        monkeypatch.setattr(
            "vastly.commands._vastai_destroy",
            lambda inst: destroyed.append(inst.name),
        )
        # _confirm should NOT be called when yes=True
        monkeypatch.setattr(
            "vastly.commands._confirm",
            lambda prompt: (_ for _ in ()).throw(
                AssertionError("_confirm was called with --yes")
            ),
        )

        args = argparse.Namespace(name=None, all=False, yes=True, verbose=False)
        cmd_destroy(args)

        assert destroyed == ["gpu"]

    def test_multiple_instances_prompts_bulk_confirmation(self, monkeypatch):
        """Multiple instances selected -> bulk confirmation prompt."""
        from vastly.commands import cmd_destroy

        _patch_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="gpu-a", id=1),
                _inst(name="gpu-b", id=2),
            ],
        )

        confirm_prompts = []

        def fake_confirm(prompt):
            confirm_prompts.append(prompt)
            return True

        monkeypatch.setattr("vastly.commands._confirm", fake_confirm)

        destroyed = []
        monkeypatch.setattr(
            "vastly.commands._vastai_destroy",
            lambda inst: destroyed.append(inst.name),
        )

        args = argparse.Namespace(name=None, all=True, yes=False, verbose=False)
        cmd_destroy(args)

        assert len(confirm_prompts) == 1
        assert "2 instances" in confirm_prompts[0]
        assert sorted(destroyed) == ["gpu-a", "gpu-b"]

    def test_multiple_instances_bulk_declined(self, monkeypatch):
        """Multiple instances selected + user declines bulk -> none destroyed."""
        from vastly.commands import cmd_destroy

        _patch_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="gpu-a", id=1),
                _inst(name="gpu-b", id=2),
            ],
        )

        monkeypatch.setattr("vastly.commands._confirm", lambda prompt: False)

        destroyed = []
        monkeypatch.setattr(
            "vastly.commands._vastai_destroy",
            lambda inst: destroyed.append(inst.name),
        )

        args = argparse.Namespace(name=None, all=True, yes=False, verbose=False)
        cmd_destroy(args)

        assert destroyed == []

    def test_multiple_instances_with_yes_flag(self, monkeypatch):
        """Multiple instances + --yes -> all destroyed without confirmation."""
        from vastly.commands import cmd_destroy

        _patch_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="gpu-a", id=1),
                _inst(name="gpu-b", id=2),
                _inst(name="gpu-c", id=3),
            ],
        )

        destroyed = []
        monkeypatch.setattr(
            "vastly.commands._vastai_destroy",
            lambda inst: destroyed.append(inst.name),
        )
        monkeypatch.setattr(
            "vastly.commands._confirm",
            lambda prompt: (_ for _ in ()).throw(
                AssertionError("_confirm was called with --yes")
            ),
        )

        args = argparse.Namespace(name=None, all=True, yes=True, verbose=False)
        cmd_destroy(args)

        assert sorted(destroyed) == ["gpu-a", "gpu-b", "gpu-c"]

    def test_named_instance_destroy(self, monkeypatch):
        """Destroy a specific named instance."""
        from vastly.commands import cmd_destroy

        _patch_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [
                _inst(name="gpu-a", id=1),
                _inst(name="gpu-b", id=2),
            ],
        )

        destroyed = []
        monkeypatch.setattr(
            "vastly.commands._vastai_destroy",
            lambda inst: destroyed.append(inst.name),
        )
        monkeypatch.setattr("vastly.commands._confirm", lambda prompt: True)

        args = argparse.Namespace(name="gpu-b", all=False, yes=False, verbose=False)
        cmd_destroy(args)

        assert destroyed == ["gpu-b"]

    def test_confirmation_prompt_mentions_irreversible(self, monkeypatch):
        """Confirmation prompt should mention 'irreversible'."""
        from vastly.commands import cmd_destroy

        _patch_base(monkeypatch)
        monkeypatch.setattr(
            "vastly.commands.get_synced_instances",
            lambda _: [_inst(name="gpu", id=1)],
        )

        confirm_prompts = []

        def fake_confirm(prompt):
            confirm_prompts.append(prompt)
            return False

        monkeypatch.setattr("vastly.commands._confirm", fake_confirm)
        monkeypatch.setattr(
            "vastly.commands._vastai_destroy", lambda inst: None,
        )

        args = argparse.Namespace(name=None, all=False, yes=False, verbose=False)
        cmd_destroy(args)

        assert len(confirm_prompts) == 1
        assert "irreversible" in confirm_prompts[0].lower()
