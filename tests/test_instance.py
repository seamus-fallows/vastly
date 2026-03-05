"""Tests for vastly.instance -- fetch, sync, display, and selection."""

from __future__ import annotations

import subprocess

import pytest

from conftest import make_test_instance as _inst
from vastly.errors import APIError, VastlyError
from vastly.instance import (
    Instance,
    fetch_instances,
    get_synced_instances,
    show_table,
    sync_instances,
)


class TestFetchInstances:
    def test_returns_list_on_success(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0], 0, stdout='[{"id": 1}]', stderr=""
            ),
        )
        assert fetch_instances() == [{"id": 1}]

    def test_raises_on_nonzero_exit_with_stderr(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0], 1, stdout="", stderr="auth error"
            ),
        )
        with pytest.raises(APIError, match="auth error"):
            fetch_instances()

    def test_raises_api_key_hint_when_stderr_empty(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, stdout="", stderr=""),
        )
        with pytest.raises(APIError, match="API key"):
            fetch_instances()

    def test_raises_on_invalid_json(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0], 0, stdout="not json", stderr=""
            ),
        )
        with pytest.raises(APIError, match="invalid data"):
            fetch_instances()

    def test_raises_when_json_is_object(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0], 0, stdout='{"error": "msg"}', stderr=""
            ),
        )
        with pytest.raises(APIError, match="invalid data"):
            fetch_instances()

    def test_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a[0], 0, stdout="[]", stderr=""
            ),
        )
        assert fetch_instances() == []


class TestSyncInstances:
    @pytest.fixture(autouse=True)
    def _mock_deps(self, monkeypatch, ssh_config_dir):
        """Mock the external deps that touch the filesystem or network."""
        monkeypatch.setattr("vastly.instance.ensure_ssh_include", lambda: None)
        monkeypatch.setattr("vastly.instance.clear_ssh_configs", lambda: None)
        monkeypatch.setattr("vastly.instance.write_ssh_config", lambda *a, **kw: None)

    def test_syncs_running_instances(self, monkeypatch, make_instance, make_config):
        monkeypatch.setattr(
            "vastly.instance.fetch_instances", lambda: [make_instance()]
        )
        result = sync_instances(make_config())
        running = [i for i in result if i.status == "running"]
        assert len(running) == 1
        assert running[0].name == "1xRTX4090-US"
        assert running[0].cached is False

    def test_returns_all_instances_including_non_running(
        self, monkeypatch, make_instance, make_config
    ):
        monkeypatch.setattr(
            "vastly.instance.fetch_instances",
            lambda: [
                make_instance(id=1, cur_state="running"),
                make_instance(id=2, cur_state="exited"),
            ],
        )
        result = sync_instances(make_config())
        assert len(result) == 2
        running = [i for i in result if i.status == "running"]
        assert len(running) == 1

    def test_skips_running_instance_without_port_22(
        self, monkeypatch, make_instance, make_config
    ):
        monkeypatch.setattr(
            "vastly.instance.fetch_instances",
            lambda: [make_instance(ports={})],
        )
        result = sync_instances(make_config())
        # Running instance without port 22 is skipped entirely
        assert result == []

    def test_skips_running_instance_with_malformed_ports(
        self, monkeypatch, make_instance, make_config
    ):
        monkeypatch.setattr(
            "vastly.instance.fetch_instances",
            lambda: [make_instance(ports={"22/tcp": []})],  # empty list
        )
        result = sync_instances(make_config())
        assert result == []

    def test_falls_back_to_cache_when_api_fails(
        self, monkeypatch, make_config, ssh_config_dir
    ):
        def raise_api_error():
            raise APIError("timeout")

        monkeypatch.setattr("vastly.instance.fetch_instances", raise_api_error)
        monkeypatch.setattr(
            "vastly.instance.cached_config_names",
            lambda: ["1xRTX4090-US"],
        )
        result = sync_instances(make_config())
        assert len(result) == 1
        assert result[0].cached is True
        assert result[0].name == "1xRTX4090-US"

    def test_raises_when_api_fails_and_no_cache(self, monkeypatch, make_config):
        def raise_api_error():
            raise APIError("timeout")

        monkeypatch.setattr("vastly.instance.fetch_instances", raise_api_error)
        monkeypatch.setattr("vastly.instance.cached_config_names", lambda: [])
        with pytest.raises(APIError):
            sync_instances(make_config())

    def test_non_running_instances_included_in_results(
        self, monkeypatch, make_instance, make_config
    ):
        monkeypatch.setattr(
            "vastly.instance.fetch_instances",
            lambda: [make_instance(cur_state="exited")],
        )
        result = sync_instances(make_config())
        assert len(result) == 1
        assert result[0].status == "exited"

    def test_port_forwards_avoid_collisions(
        self, monkeypatch, make_instance, make_config
    ):
        """Two instances with the same configured port should get different local ports."""
        monkeypatch.setattr(
            "vastly.instance.fetch_instances",
            lambda: [
                make_instance(id=1, geolocation="City, US"),
                make_instance(id=2, geolocation="City, DE"),
            ],
        )
        writes = []
        monkeypatch.setattr(
            "vastly.instance.write_ssh_config",
            lambda *a, **kw: writes.append(kw),
        )
        sync_instances(make_config())
        assert len(writes) == 2
        port1 = writes[0]["local_forwards"][0][0]
        port2 = writes[1]["local_forwards"][0][0]
        assert port1 != port2

    def test_passes_config_values_to_write(
        self, monkeypatch, make_instance, make_config
    ):
        monkeypatch.setattr(
            "vastly.instance.fetch_instances", lambda: [make_instance()]
        )
        writes = []
        monkeypatch.setattr(
            "vastly.instance.write_ssh_config",
            lambda *a, **kw: writes.append(kw),
        )
        sync_instances(make_config(sshUser="ubuntu", sshKeyPath="/my/key"))
        assert writes[0]["user"] == "ubuntu"
        assert writes[0]["key_path"] == "/my/key"


class TestGetSyncedInstances:
    def test_raises_when_api_unreachable_and_no_cache(self, monkeypatch, make_config):
        def raise_api(cfg):
            raise APIError("timeout")

        monkeypatch.setattr("vastly.instance.sync_instances", raise_api)
        with pytest.raises(APIError):
            get_synced_instances(make_config())

    def test_raises_when_no_instances(self, monkeypatch, make_config):
        monkeypatch.setattr("vastly.instance.sync_instances", lambda cfg: [])
        with pytest.raises(VastlyError, match="No Vast instances"):
            get_synced_instances(make_config())

    def test_returns_instances_when_found(self, monkeypatch, make_config):
        instances = [_inst()]
        monkeypatch.setattr("vastly.instance.sync_instances", lambda cfg: instances)
        assert get_synced_instances(make_config()) == instances


class TestShowTable:
    def test_live_instance_format(self, capsys):
        instances = [_inst(name="1xRTX4090-US", dph_total=0.55)]
        show_table(instances)
        out = capsys.readouterr().out
        assert "1xRTX4090-US" in out
        assert "$0.55/hr" in out

    def test_cached_instance_format(self, capsys):
        instances = [_inst(name="1xRTX4090-US", cached=True, dph_total=0)]
        show_table(instances)
        out = capsys.readouterr().out
        assert "1xRTX4090-US" in out
        assert "(cached)" in out

    def test_multiple_instances(self, capsys):
        instances = [
            _inst(name="gpu-1", dph_total=0.50),
            _inst(name="gpu-2", id=2, cached=True, dph_total=0),
        ]
        show_table(instances)
        out = capsys.readouterr().out
        assert "gpu-1" in out
        assert "gpu-2" in out
