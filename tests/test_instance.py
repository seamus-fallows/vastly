"""Tests for vastly.instance -- fetch, sync, display, and selection."""

from __future__ import annotations

import json
import subprocess

import pytest

from conftest import make_api_instance, make_test_config, make_test_instance as _inst
from vastly.errors import APIError, VastlyError
from vastly.instance import (
    Instance,
    build_instance_name,
    fetch_instances,
    get_running_instances,
    get_synced_instances,
    load_aliases,
    save_aliases,
    select_instance,
    show_table,
    sync_instances,
    validate_alias,
)


class TestFetchInstances:
    def test_returns_list_on_success(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **_kw: subprocess.CompletedProcess(
                a[0], 0, stdout='[{"id": 1}]', stderr=""
            ),
        )
        assert fetch_instances() == [{"id": 1}]

    def test_raises_on_nonzero_exit_with_stderr(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **_kw: subprocess.CompletedProcess(
                a[0], 1, stdout="", stderr="auth error"
            ),
        )
        with pytest.raises(APIError, match="auth error"):
            fetch_instances()

    def test_raises_api_key_hint_when_stderr_empty(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **_kw: subprocess.CompletedProcess(a[0], 1, stdout="", stderr=""),
        )
        with pytest.raises(APIError, match="API key"):
            fetch_instances()

    def test_raises_on_invalid_json(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **_kw: subprocess.CompletedProcess(
                a[0], 0, stdout="not json", stderr=""
            ),
        )
        with pytest.raises(APIError, match="invalid data"):
            fetch_instances()

    def test_raises_when_json_is_object(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **_kw: subprocess.CompletedProcess(
                a[0], 0, stdout='{"error": "msg"}', stderr=""
            ),
        )
        with pytest.raises(APIError, match="invalid data"):
            fetch_instances()

    def test_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **_kw: subprocess.CompletedProcess(
                a[0], 0, stdout="[]", stderr=""
            ),
        )
        assert fetch_instances() == []


class TestSyncInstances:
    @pytest.fixture(autouse=True)
    def _mock_deps(self, monkeypatch, ssh_config_dir):  # noqa: ARG002
        """Mock the external deps that touch the filesystem or network."""
        monkeypatch.setattr("vastly.instance.ensure_ssh_include", lambda: None)
        monkeypatch.setattr("vastly.instance.prune_ssh_configs", lambda keep: None)
        monkeypatch.setattr("vastly.instance.write_ssh_config", lambda *_a, **_kw: None)

    def test_syncs_running_instances(self, monkeypatch):
        monkeypatch.setattr(
            "vastly.instance.fetch_instances", lambda: [make_api_instance(1)]
        )
        result = sync_instances(make_test_config())
        running = [i for i in result if i.status == "running"]
        assert len(running) == 1
        assert running[0].name == "1xRTX4090-TW"

    def test_returns_all_instances_including_non_running(self, monkeypatch):
        monkeypatch.setattr(
            "vastly.instance.fetch_instances",
            lambda: [
                make_api_instance(1),
                make_api_instance(2, "exited"),
            ],
        )
        result = sync_instances(make_test_config())
        assert len(result) == 2
        running = [i for i in result if i.status == "running"]
        assert len(running) == 1

    def test_running_without_port_22_uses_actual_status(self, monkeypatch):
        monkeypatch.setattr(
            "vastly.instance.fetch_instances",
            lambda: [make_api_instance(1, ports={}, actual_status="loading")],
        )
        result = sync_instances(make_test_config())
        assert len(result) == 1
        assert result[0].status == "loading"

    def test_running_with_malformed_ports_uses_actual_status(self, monkeypatch):
        monkeypatch.setattr(
            "vastly.instance.fetch_instances",
            lambda: [make_api_instance(1, ports={"22/tcp": []})],  # empty list
        )
        result = sync_instances(make_test_config())
        assert len(result) == 1
        assert result[0].status == "loading"  # default when actual_status missing

    def test_raises_when_api_fails(self, monkeypatch):
        def raise_api_error():
            raise APIError("timeout")

        monkeypatch.setattr("vastly.instance.fetch_instances", raise_api_error)
        with pytest.raises(APIError):
            sync_instances(make_test_config())

    def test_non_running_instances_included_in_results(self, monkeypatch):
        monkeypatch.setattr(
            "vastly.instance.fetch_instances",
            lambda: [make_api_instance(1, "exited")],
        )
        result = sync_instances(make_test_config())
        assert len(result) == 1
        assert result[0].status == "exited"

    def test_port_forwards_avoid_collisions(self, monkeypatch):
        """Two instances with the same configured port should get different local ports."""
        monkeypatch.setattr(
            "vastly.instance.fetch_instances",
            lambda: [
                make_api_instance(1, geo="City, US"),
                make_api_instance(2, geo="City, DE"),
            ],
        )
        writes = []
        monkeypatch.setattr(
            "vastly.instance.write_ssh_config",
            lambda *_a, **kw: writes.append(kw),
        )
        sync_instances(make_test_config())
        assert len(writes) == 2
        port1 = writes[0]["local_forwards"][0][0]
        port2 = writes[1]["local_forwards"][0][0]
        assert port1 != port2

    def test_passes_config_values_to_write(self, monkeypatch):
        monkeypatch.setattr(
            "vastly.instance.fetch_instances", lambda: [make_api_instance(1)]
        )
        writes = []
        monkeypatch.setattr(
            "vastly.instance.write_ssh_config",
            lambda *_a, **kw: writes.append(kw),
        )
        sync_instances(make_test_config(sshUser="ubuntu", sshKeyPath="/my/key"))
        assert writes[0]["user"] == "ubuntu"
        assert writes[0]["key_path"] == "/my/key"


class TestGetSyncedInstances:
    def test_raises_when_api_unreachable_and_no_cache(self, monkeypatch):
        def raise_api(cfg):
            raise APIError("timeout")

        monkeypatch.setattr("vastly.instance.sync_instances", raise_api)
        with pytest.raises(APIError):
            get_synced_instances(make_test_config())

    def test_raises_when_no_instances(self, monkeypatch):
        monkeypatch.setattr("vastly.instance.sync_instances", lambda cfg: [])
        with pytest.raises(VastlyError, match="No Vast instances"):
            get_synced_instances(make_test_config())

    def test_returns_instances_when_found(self, monkeypatch):
        instances = [_inst()]
        monkeypatch.setattr("vastly.instance.sync_instances", lambda cfg: instances)
        assert get_synced_instances(make_test_config()) == instances


class TestShowTable:
    def test_live_instance_format(self, capsys):
        instances = [_inst(name="1xRTX4090-US", dph_total=0.55)]
        show_table(instances)
        out = capsys.readouterr().out
        assert "1xRTX4090-US" in out
        assert "$0.55/hr" in out

    def test_stopped_instance_format(self, capsys):
        instances = [_inst(name="1xRTX4090-US", status="stopped", dph_total=0.25)]
        show_table(instances)
        out = capsys.readouterr().out
        assert "1xRTX4090-US" in out
        assert "inactive" in out

    def test_multiple_instances(self, capsys):
        instances = [
            _inst(name="gpu-1", dph_total=0.50),
            _inst(name="gpu-2", inst_id=2, status="stopped", dph_total=0),
        ]
        show_table(instances)
        out = capsys.readouterr().out
        assert "gpu-1" in out
        assert "gpu-2" in out


_MINIMAL_CONFIG = make_test_config(portForwards=[])


def _make_api_response(*instances):
    """Build a fake subprocess.run result returning JSON instance data."""
    return subprocess.CompletedProcess(
        [], 0, stdout=json.dumps(list(instances)), stderr=""
    )


# ── TestSelectInstance ───────────────────────────────────────────────


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


# ── TestInstanceNaming ───────────────────────────────────────────────


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


# ── TestAliases ──────────────────────────────────────────────────────


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


# ── TestSyncInstancesLifecycle ───────────────────────────────────────


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
            make_api_instance(1, "running"),
            make_api_instance(2, "stopped"),
            make_api_instance(3, "exited"),
        ]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        results = sync_instances(_MINIMAL_CONFIG)

        assert len(results) == 3
        statuses = {r.status for r in results}
        assert statuses == {"running", "stopped", "exited"}

    def test_only_running_get_ssh_configs(self, tmp_path, monkeypatch):
        api_data = [
            make_api_instance(1, "running"),
            make_api_instance(2, "stopped"),
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
            make_api_instance(1, "stopped"),
            make_api_instance(2, "exited"),
        ]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        results = sync_instances(_MINIMAL_CONFIG)

        assert len(results) == 2
        assert all(r.status != "running" for r in results)

    def test_aliases_survive_stopped_state(self, tmp_path, monkeypatch):
        aliases_file = tmp_path / "aliases.json"
        aliases_file.write_text('{"1": "train"}', encoding="utf-8")

        api_data = [make_api_instance(1, "stopped")]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        results = sync_instances(_MINIMAL_CONFIG)

        assert results[0].alias == "train"
        # Alias should still be in the file
        saved = json.loads(aliases_file.read_text(encoding="utf-8"))
        assert "1" in saved

    def test_aliases_pruned_only_on_disappearance(self, tmp_path, monkeypatch):
        aliases_file = tmp_path / "aliases.json"
        aliases_file.write_text('{"1": "train", "999": "gone"}', encoding="utf-8")

        api_data = [make_api_instance(1, "stopped")]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        sync_instances(_MINIMAL_CONFIG)

        saved = json.loads(aliases_file.read_text(encoding="utf-8"))
        assert "1" in saved  # stopped instance alias kept
        assert "999" not in saved  # destroyed instance alias pruned

    def test_api_outage_raises_and_preserves_aliases(self, tmp_path, monkeypatch):
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
            make_api_instance(1, "running"),
            make_api_instance(2, "stopped"),
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
            make_api_instance(1, "stopped"),
            make_api_instance(2, "exited"),
        ]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        results = sync_instances(_MINIMAL_CONFIG)

        assert len(results) == 2
        statuses = {r.status for r in results}
        assert statuses == {"stopped", "exited"}


# ── TestGetRunningInstances ──────────────────────────────────────────


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
            make_api_instance(1, "running"),
            make_api_instance(2, "stopped"),
        ]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        results = get_running_instances(_MINIMAL_CONFIG)

        assert len(results) == 1
        assert results[0].status == "running"

    def test_raises_with_hint_when_inactive_exist(self, monkeypatch):
        api_data = [
            make_api_instance(1, "stopped"),
            make_api_instance(2, "exited"),
        ]
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: api_data)

        with pytest.raises(VastlyError, match="2 inactive.*vst start"):
            get_running_instances(_MINIMAL_CONFIG)

    def test_raises_no_instances_when_empty(self, monkeypatch):
        monkeypatch.setattr("vastly.instance.fetch_instances", lambda: [])

        with pytest.raises(VastlyError, match="No Vast instances found"):
            get_running_instances(_MINIMAL_CONFIG)


# ── TestShowTableMixedStates ─────────────────────────────────────────


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


# ── TestSyncInstancesIntegration ─────────────────────────────────────


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
            lambda *_a, **_kw: _make_api_response(*api_data),
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
            lambda *_a, **_kw: _make_api_response(*api_data),
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
            lambda *_a, **_kw: _make_api_response(*api_data),
        )

        results = sync_instances(_MINIMAL_CONFIG)

        # train alias for existing instance 100 should survive
        assert results[0].alias == "train"

        # Stale alias for non-existent instance 999 should be pruned
        saved = json.loads(self.aliases_file.read_text(encoding="utf-8"))
        assert "100" in saved
        assert "999" not in saved


# ── TestFetchInstancesTimeout ────────────────────────────────────────


class TestFetchInstancesTimeout:
    """Test that fetch_instances has a timeout."""

    def test_timeout_raises_api_error(self, monkeypatch):
        def timeout_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=["vastai"], timeout=30)

        monkeypatch.setattr("vastly.instance.subprocess.run", timeout_run)
        with pytest.raises(APIError, match="timed out"):
            fetch_instances()


# ── TestStateConstants ───────────────────────────────────────────────


class TestStateConstants:
    """Test state constants are accessible from instance.py."""

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


# ── TestPortForwardRangeValidation ───────────────────────────────────


class TestPortForwardRangeValidation:
    """Config validation must reject out-of-range port numbers."""

    @pytest.mark.parametrize("bad_port", [0, -1, 65536, 99999])
    def test_rejects_bad_local_port(self, bad_port):
        from vastly.config import _validate_config
        from vastly.errors import ConfigError

        cfg = make_test_config(portForwards=[{"local": bad_port, "remote": 8080}])
        with pytest.raises(ConfigError, match="1-65535"):
            _validate_config(cfg)

    @pytest.mark.parametrize("bad_port", [0, -1, 65536, 99999])
    def test_rejects_bad_remote_port(self, bad_port):
        from vastly.config import _validate_config
        from vastly.errors import ConfigError

        cfg = make_test_config(portForwards=[{"local": 8080, "remote": bad_port}])
        with pytest.raises(ConfigError, match="1-65535"):
            _validate_config(cfg)

    def test_accepts_boundary_ports(self):
        from vastly.config import _validate_config

        cfg = make_test_config(portForwards=[{"local": 1, "remote": 65535}])
        _validate_config(cfg)  # should not raise

    def test_accepts_common_ports(self):
        from vastly.config import _validate_config

        cfg = make_test_config(portForwards=[{"local": 8080, "remote": 22}])
        _validate_config(cfg)  # should not raise


# ── TestFindByName ───────────────────────────────────────────────────


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


# ── TestGpuNameSanitization ──────────────────────────────────────────


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
        inst = {
            "gpu_name": "A100 (80GB)/PCIe",
            "num_gpus": 2,
            "geolocation": "",
            "id": 1,
        }
        name = build_instance_name(inst, set())
        assert "(" not in name
        assert ")" not in name
        assert "/" not in name
        assert "2xA10080GBPCIe" == name
