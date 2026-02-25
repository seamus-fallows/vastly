"""Shared test fixtures for vastly."""

from __future__ import annotations

import pytest


@pytest.fixture
def make_instance():
    """Factory for Vast.ai instance dicts with sensible defaults."""

    def _make(**overrides):
        base = {
            "id": 12345,
            "gpu_name": "RTX 4090",
            "num_gpus": 1,
            "geolocation": "San Jose, CA, US",
            "cur_state": "running",
            "public_ipaddr": "203.0.113.1",
            "ports": {"22/tcp": [{"HostPort": "22222"}]},
            "dph_total": 0.50,
            "start_date": None,
        }
        base.update(overrides)
        return base

    return _make


@pytest.fixture
def make_config():
    """Factory for vastly config dicts with sensible defaults."""

    def _make(**overrides):
        base = {
            "ide": "code",
            "sshKeyPath": None,
            "sshUser": "root",
            "portForwards": [{"local": 8080, "remote": 8080}],
            "workspace": "/workspace",
            "disableAutoTmux": True,
            "gitRemote": "origin",
            "postInstall": [],
            "installCommand": None,
        }
        base.update(overrides)
        return base

    return _make


@pytest.fixture
def ssh_config_dir(tmp_path, monkeypatch):
    """Redirect SSH_CONFIG_DIR to a temp directory for safe testing.

    Patches the reference in both vastly.ssh and vastly.instance since
    instance.py imports it with ``from vastly.ssh import SSH_CONFIG_DIR``.
    """
    d = tmp_path / "vast.d"
    d.mkdir()
    monkeypatch.setattr("vastly.ssh.SSH_CONFIG_DIR", d)
    monkeypatch.setattr("vastly.instance.SSH_CONFIG_DIR", d)
    return d
