"""Integration tests requiring a live Vast.ai instance.

These tests verify the full pipeline against real infrastructure.
They require:
  - vastai CLI installed and configured (vastai set api-key <key>)
  - At least one running Vast.ai instance
  - SSH agent with appropriate keys loaded

Run with:
    pytest tests/test_integration.py -v

Remote setup tests (TestRemoteSetup) modify remote state -- they clone a
repo, write markers, patch bashrc, and set git identity. These are skipped
by default. Run them on a disposable instance with:

    VASTLY_DESTRUCTIVE=1 pytest tests/test_integration.py -v

Cost: requires an active Vast.ai instance. These tests do NOT create or
destroy instances -- they use whatever is currently running.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from vastly.config import DEFAULTS
from vastly.instance import fetch_instances, sync_instances
from vastly.ssh import cached_config_names, run_ssh


def _vastai_configured() -> bool:
    """Check if the vastai CLI is installed and has an API key."""
    if not shutil.which("vastai"):
        return False
    # vastai stores the key in different locations depending on version/platform
    key_locations = [
        Path.home() / ".vast_api_key",
        Path.home() / ".config" / "vastai" / "vast_api_key",
    ]
    return any(p.exists() for p in key_locations)


pytestmark = pytest.mark.skipif(
    not _vastai_configured(),
    reason="vastai CLI not configured -- run: pip install vastai && vastai set api-key <key>",
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def synced_instances():
    """Sync instances once for the whole module. Skip if none available."""
    config = {**DEFAULTS, "copyFiles": []}
    result = sync_instances(config)
    if not result:
        pytest.skip("No running instances available")
    return result


@pytest.fixture(scope="module")
def live_instance(synced_instances):
    """Return the first running non-cached instance, or skip."""
    for inst in synced_instances:
        if not inst.cached and inst.status == "running":
            return inst
    pytest.skip("No live running instances available")


@pytest.fixture(scope="module")
def test_repo():
    """Return (repo_url, repo_name) using the current repo's origin remote."""
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
    )
    url = result.stdout.strip() if result.returncode == 0 else ""
    if not url:
        pytest.skip("No git remote configured")
    name = url.rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
    return url, name


# ---------------------------------------------------------------------------
# API connectivity
# ---------------------------------------------------------------------------


class TestAPIConnectivity:
    """Verify that the Vast.ai API is reachable and returns valid data."""

    def test_fetch_returns_list(self):
        result = fetch_instances()
        assert isinstance(result, list), "fetch_instances should return a list"

    def test_instances_have_expected_schema(self):
        instances = fetch_instances()
        if not instances:
            pytest.skip("No instances returned by API")
        required_keys = {"id", "gpu_name", "cur_state"}
        for inst in instances:
            missing = required_keys - set(inst.keys())
            assert not missing, f"Instance {inst.get('id')} missing keys: {missing}"


# ---------------------------------------------------------------------------
# SSH connectivity
# ---------------------------------------------------------------------------


class TestSSHConnectivity:
    """Verify SSH works through the generated configs."""

    def test_ssh_echo(self, live_instance):
        result = run_ssh(live_instance.name, "echo ok")
        assert result.returncode == 0
        assert result.stdout.strip() == "ok"

    def test_ssh_config_file_exists(self, live_instance):
        names = cached_config_names()
        assert live_instance.name in names

    def test_ssh_environment_has_basic_tools(self, live_instance):
        result = run_ssh(live_instance.name, "which bash && which cat")
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Remote environment
# ---------------------------------------------------------------------------


class TestRemoteEnvironment:
    """Verify the remote instance has expected tools and paths."""

    def test_python_available(self, live_instance):
        result = run_ssh(live_instance.name, "which python3 || which python")
        assert result.returncode == 0

    def test_git_available(self, live_instance):
        result = run_ssh(live_instance.name, "which git")
        assert result.returncode == 0

    def test_workspace_creatable(self, live_instance):
        result = run_ssh(live_instance.name, "mkdir -p /workspace && echo ok")
        assert result.returncode == 0
        assert result.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# Full remote setup
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    "VASTLY_DESTRUCTIVE" not in os.environ,
    reason="Modifies remote state. Run with: VASTLY_DESTRUCTIVE=1 pytest tests/test_integration.py",
)
class TestRemoteSetup:
    """Test the full setup pipeline on a real instance.

    Clones the current repo's origin remote via SSH agent forwarding.
    Modifies bashrc, git identity, and clones a repo on the remote.
    Run only on disposable instances.
    """

    def test_fresh_setup(self, live_instance, test_repo):
        """Force-setup from scratch and verify it succeeds."""
        from vastly.remote import setup_instances

        repo_url, repo_name = test_repo
        config = {**DEFAULTS, "postInstall": [], "copyFiles": []}

        result = setup_instances(
            [live_instance], repo_url, repo_name, config, force_setup=True
        )
        assert live_instance.name in result, (
            f"Setup failed for {live_instance.name}"
        )

    def test_marker_file_written(self, live_instance, test_repo):
        """After setup, the marker JSON should exist and be valid."""
        _, repo_name = test_repo
        result = run_ssh(
            live_instance.name,
            f"cat ~/.vastly/setup/{repo_name}.json",
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "timestamp" in data
        assert "installMethod" in data
        assert "moduleVersion" in data

    def test_repo_cloned(self, live_instance, test_repo):
        _, repo_name = test_repo
        result = run_ssh(
            live_instance.name,
            f"test -d /workspace/{repo_name}/.git && echo ok",
        )
        assert result.stdout.strip() == "ok"

    def test_git_identity_configured(self, live_instance):
        name_result = run_ssh(live_instance.name, "git config --global user.name")
        assert name_result.returncode == 0
        assert name_result.stdout.strip(), "git user.name should not be empty"

        email_result = run_ssh(live_instance.name, "git config --global user.email")
        assert email_result.returncode == 0
        assert email_result.stdout.strip(), "git user.email should not be empty"

    def test_bashrc_has_vastly_managed_lines(self, live_instance):
        result = run_ssh(live_instance.name, "grep 'vastly-managed' ~/.bashrc")
        assert result.returncode == 0, "~/.bashrc should contain vastly-managed lines"

    def test_vscode_settings_written(self, live_instance, test_repo):
        _, repo_name = test_repo
        result = run_ssh(
            live_instance.name,
            f"cat /workspace/{repo_name}/.vscode/settings.json",
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "python.defaultInterpreterPath" in data

    def test_idempotent_rerun(self, live_instance, test_repo):
        """Running setup again should detect the marker and skip."""
        from vastly.remote import setup_instances

        repo_url, repo_name = test_repo
        config = {**DEFAULTS, "postInstall": [], "copyFiles": []}

        result = setup_instances([live_instance], repo_url, repo_name, config)
        assert live_instance.name in result


# ---------------------------------------------------------------------------
# Port forwarding (smoke test)
# ---------------------------------------------------------------------------


class TestPortForwarding:
    """Verify that port-forward SSH config entries are generated."""

    def test_ssh_config_contains_local_forward(self, live_instance):
        from vastly.ssh import SSH_CONFIG_DIR

        config_file = SSH_CONFIG_DIR / live_instance.name
        if not config_file.exists():
            pytest.skip("SSH config file not found")
        content = config_file.read_text()
        assert "LocalForward" in content
