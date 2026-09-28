"""Shared test fixtures for vastly."""

from __future__ import annotations

import pytest

from vastly.config import DEFAULTS
from vastly.instance import Instance


# ── Non-fixture helpers (usable without pytest fixtures) ─────────────


def make_test_instance(name="test", inst_id=1, **kwargs):
    """Create an Instance with sensible defaults for testing."""
    defaults = dict(
        name=name,
        id=inst_id,
        dph_total=0.50,
        gpu_name="RTX 4090",
        num_gpus=1,
        status="running",
        alias=None,
    )
    defaults.update(kwargs)
    return Instance(**defaults)


def make_test_config(**overrides) -> dict:
    """Create a complete config dict with sensible defaults for testing."""
    base = {**DEFAULTS}
    base.update(overrides)
    return base


def make_api_instance(
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


@pytest.fixture(autouse=True)
def _isolate_gitauth(tmp_path, monkeypatch):
    """Keep every test away from real deploy keys.

    Deploy key state goes to a temp file, and any GitHub CLI or account
    lookup a test hasn't explicitly mocked fails loudly instead of running.
    """

    def _unmocked(*_args, **_kwargs):
        raise AssertionError("unmocked GitHub CLI / Vast.ai account call in a test")

    monkeypatch.setattr("vastly.gitauth.STATE_FILE", tmp_path / "deploy-keys.json")
    monkeypatch.setattr("vastly.gitauth._gh", _unmocked)
    monkeypatch.setattr("vastly.gitauth.account_id", _unmocked)


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
