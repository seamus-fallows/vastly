"""Shared test fixtures for vastly."""

from __future__ import annotations

import pytest

from vastly.config import DEFAULTS
from vastly.instance import Instance


# ── Non-fixture helpers (usable without pytest fixtures) ─────────────


def make_test_instance(name="test", inst_id=1, **kwargs):
    """Create an Instance with sensible defaults for testing."""
    defaults = dict(
        name=name, id=inst_id, dph_total=0.50, gpu_name="RTX 4090",
        num_gpus=1, status="running", alias=None,
    )
    defaults.update(kwargs)
    return Instance(**defaults)


def make_test_config(**overrides) -> dict:
    """Create a complete config dict with sensible defaults for testing."""
    base = {**DEFAULTS}
    base.update(overrides)
    return base


# ── Fixtures ─────────────────────────────────────────────────────────


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
        }
        base.update(overrides)
        return base

    return _make


@pytest.fixture
def make_config():
    """Factory for vastly config dicts with sensible defaults."""

    def _make(**overrides):
        base = {**DEFAULTS}
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
