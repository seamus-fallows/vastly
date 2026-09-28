"""Tests for vastly.gitauth -- deploy keys vs SSH agent forwarding."""

from __future__ import annotations

import json
import subprocess

import pytest

from vastly import gitauth
from vastly.gitauth import GitHubError


def _done(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


class FakeGh:
    """Stand-in for gitauth._gh: answers by the first matching args prefix."""

    def __init__(self, responses: dict[tuple[str, ...], subprocess.CompletedProcess]):
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(self, *args, stdin=None):
        self.calls.append((args, stdin))
        for prefix, result in self.responses.items():
            if args[: len(prefix)] == prefix:
                return result
        raise AssertionError(f"unexpected gh call: {args}")


@pytest.fixture
def gh_installed(monkeypatch):
    monkeypatch.setattr("vastly.gitauth.shutil.which", lambda name: f"/usr/bin/{name}")


# ── TestGithubRepo ───────────────────────────────────────────────────


class TestGithubRepo:
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("git@github.com:owner/repo.git", "owner/repo"),
            ("git@github.com:owner/repo", "owner/repo"),
            ("https://github.com/owner/repo", "owner/repo"),
            ("https://github.com/owner/repo.git/", "owner/repo"),
            ("https://user:token@github.com/owner/repo.git", "owner/repo"),
            ("ssh://git@github.com/owner/repo.git", "owner/repo"),
            ("https://github.com/owner/my.repo.git", "owner/my.repo"),
            ("git@gitlab.com:owner/repo.git", None),
            ("https://github.com/owner", None),
            ("https://github.com.evil.com/owner/repo", None),
        ],
    )
    def test_parses_github_urls(self, url, expected):
        assert gitauth.github_repo(url) == expected

    def test_ssh_url(self):
        assert gitauth.ssh_url("owner/repo") == "git@github.com:owner/repo.git"


# ── TestDeployKeyBlocker ─────────────────────────────────────────────


class TestDeployKeyBlocker:
    def test_not_github(self):
        assert "isn't on GitHub" in gitauth.deploy_key_blocker(None)

    def test_gh_not_installed(self, monkeypatch):
        monkeypatch.setattr("vastly.gitauth.shutil.which", lambda name: None)
        assert "isn't installed" in gitauth.deploy_key_blocker("o/r")

    def test_gh_not_logged_in(self, monkeypatch, gh_installed):
        monkeypatch.setattr("vastly.gitauth._gh", FakeGh({("auth",): _done(rc=1)}))
        assert "gh auth login" in gitauth.deploy_key_blocker("o/r")

    def test_repo_lookup_fails(self, monkeypatch, gh_installed):
        fake = FakeGh(
            {
                ("auth",): _done(),
                ("api",): _done(rc=1, stderr="HTTP 404: Not Found"),
            }
        )
        monkeypatch.setattr("vastly.gitauth._gh", fake)
        assert "HTTP 404" in gitauth.deploy_key_blocker("o/r")

    def test_not_admin(self, monkeypatch, gh_installed):
        fake = FakeGh({("auth",): _done(), ("api",): _done("false\n")})
        monkeypatch.setattr("vastly.gitauth._gh", fake)
        assert gitauth.deploy_key_blocker("o/r") == "you're not an admin of o/r"

    def test_admin(self, monkeypatch, gh_installed):
        fake = FakeGh({("auth",): _done(), ("api",): _done("true\n")})
        monkeypatch.setattr("vastly.gitauth._gh", fake)
        assert gitauth.deploy_key_blocker("o/r") is None
        assert fake.calls[1][0] == ("api", "repos/o/r", "--jq", ".permissions.admin")


# ── TestAddDeleteDeployKey ───────────────────────────────────────────

_PUB = "ssh-ed25519 AAAAC3Nza vastly-7"


class TestAddDeployKey:
    def test_reuses_existing_key_with_same_material(self, monkeypatch):
        existing = [{"id": 11, "key": "ssh-ed25519 AAAAC3Nza"}]
        fake = FakeGh(
            {("api", "repos/o/r/keys?per_page=100"): _done(json.dumps(existing))}
        )
        monkeypatch.setattr("vastly.gitauth._gh", fake)

        assert gitauth.add_deploy_key("o/r", "vastly-7", _PUB) == 11
        assert len(fake.calls) == 1  # no POST

    def test_creates_read_write_key(self, monkeypatch):
        fake = FakeGh(
            {
                ("api", "repos/o/r/keys?per_page=100"): _done("[]"),
                ("api", "-X", "POST"): _done('{"id": 42}'),
            }
        )
        monkeypatch.setattr("vastly.gitauth._gh", fake)

        assert gitauth.add_deploy_key("o/r", "vastly-7", _PUB) == 42
        body = json.loads(fake.calls[1][1])
        assert body == {
            "title": "vastly-7",
            "key": "ssh-ed25519 AAAAC3Nza",
            "read_only": False,
        }

    def test_listing_failure_raises(self, monkeypatch):
        fake = FakeGh({("api",): _done(rc=1, stderr="HTTP 403: Forbidden")})
        monkeypatch.setattr("vastly.gitauth._gh", fake)
        with pytest.raises(GitHubError, match="403"):
            gitauth.add_deploy_key("o/r", "vastly-7", _PUB)

    def test_rejected_key_raises_with_githubs_reason(self, monkeypatch):
        fake = FakeGh(
            {
                ("api", "repos/o/r/keys?per_page=100"): _done("[]"),
                ("api", "-X", "POST"): _done(
                    rc=1,
                    stderr="Deploy keys are disabled for this repository (HTTP 422)",
                ),
            }
        )
        monkeypatch.setattr("vastly.gitauth._gh", fake)
        with pytest.raises(GitHubError, match="disabled"):
            gitauth.add_deploy_key("o/r", "vastly-7", _PUB)


class TestDeleteDeployKey:
    @pytest.mark.parametrize(
        "result, expected",
        [
            (_done(), True),
            (_done(rc=1, stderr="gh: Not Found (HTTP 404)"), True),
            (_done(rc=1, stderr="error connecting to api.github.com"), False),
        ],
    )
    def test_result(self, monkeypatch, result, expected):
        monkeypatch.setattr("vastly.gitauth._gh", lambda *a, **kw: result)
        assert gitauth.delete_deploy_key("o/r", 42) is expected


# ── TestEnsureInstanceKey ────────────────────────────────────────────


class TestEnsureInstanceKey:
    def test_returns_public_key(self, monkeypatch):
        commands = []

        def fake_ssh(host, command, **kwargs):
            commands.append((host, command))
            return _done(f"Generating...\n{_PUB}\n")

        monkeypatch.setattr("vastly.gitauth.run_ssh", fake_ssh)
        assert gitauth.ensure_instance_key("gpu-1", "repo", "vastly-99-7") == _PUB
        host, command = commands[0]
        assert host == "gpu-1"
        assert "~/.ssh/vastly-repo" in command
        assert "ssh-keygen" in command and "-C vastly-99-7" in command

    def test_failure_raises(self, monkeypatch):
        monkeypatch.setattr(
            "vastly.gitauth.run_ssh", lambda *a, **kw: _done(rc=255, stderr="timeout")
        )
        with pytest.raises(GitHubError, match="create a key"):
            gitauth.ensure_instance_key("gpu-1", "repo", "vastly-99-7")


class TestKeyTitle:
    def test_includes_account(self):
        assert gitauth.key_title(7, 99) == "vastly-99-7"

    def test_without_account(self):
        assert gitauth.key_title(7, None) == "vastly-7"


class TestPruneRepoKeys:
    """Stale keys on GitHub are removed even if another machine created them."""

    def _keys(self, monkeypatch, keys):
        fake = FakeGh({("api", "repos/o/r/keys?per_page=100"): _done(json.dumps(keys))})
        monkeypatch.setattr("vastly.gitauth._gh", fake)
        deleted = []
        monkeypatch.setattr(
            "vastly.gitauth.delete_deploy_key",
            lambda repo, key_id: deleted.append(key_id) or True,
        )
        return deleted

    def test_deletes_only_this_accounts_keys_for_gone_instances(self, monkeypatch):
        deleted = self._keys(
            monkeypatch,
            [
                {"id": 1, "title": "vastly-99-7"},  # live instance: keep
                {"id": 2, "title": "vastly-99-8"},  # gone: delete
                {"id": 3, "title": "vastly-55-8"},  # other account: keep
                {"id": 4, "title": "vastly-8"},  # no account in title: keep
                {"id": 5, "title": "my laptop"},  # not vastly's: keep
            ],
        )
        gitauth.prune_repo_keys("o/r", account=99, live_ids={7})
        assert deleted == [2]

    def test_listing_failure_is_quiet(self, monkeypatch):
        monkeypatch.setattr("vastly.gitauth._gh", lambda *a, **kw: _done(rc=1))
        gitauth.prune_repo_keys("o/r", account=99, live_ids=set())  # no raise


# ── TestState ────────────────────────────────────────────────────────


class TestForwardAgent:
    def test_agent_mode_always_forwards(self):
        gitauth.record_deploy_key(7, "o/r", 42, account=1)
        assert gitauth.forward_agent(7, "agent") is True

    def test_unknown_instance_forwards(self):
        # Not set up yet, or set up before deploy keys existed
        assert gitauth.forward_agent(7, "auto") is True

    def test_deploy_key_only_instance_does_not_forward(self):
        gitauth.record_deploy_key(7, "o/r", 42, account=1)
        assert gitauth.forward_agent(7, "auto") is False
        assert gitauth.forward_agent(7, "deploy-key") is False

    def test_instance_with_an_agent_repo_forwards(self):
        gitauth.record_deploy_key(7, "o/r", 42, account=1)
        gitauth.record_agent(7, account=1)
        assert gitauth.forward_agent(7, "auto") is True

    def test_uses_given_state(self):
        state = {"7": {"agent": False, "keys": []}}
        assert gitauth.forward_agent(7, "auto", state) is False


class TestRecordDeployKey:
    def test_replaces_key_for_same_repo(self):
        gitauth.record_deploy_key(7, "o/r", 1, account=5)
        gitauth.record_deploy_key(7, "o/r", 2, account=5)
        gitauth.record_deploy_key(7, "o/other", 3, account=5)
        entry = gitauth.load_state()["7"]
        assert entry["account"] == 5
        assert sorted((k["repo"], k["id"]) for k in entry["keys"]) == [
            ("o/other", 3),
            ("o/r", 2),
        ]

    def test_corrupt_state_file_is_ignored(self):
        gitauth.STATE_FILE.write_text("{not json", encoding="utf-8")
        assert gitauth.load_state() == {}


# ── TestCleanup ──────────────────────────────────────────────────────


class TestRemoveInstanceKeys:
    def test_deletes_keys_and_forgets_instance(self, monkeypatch, gh_installed, capsys):
        gitauth.record_deploy_key(7, "o/r", 42, account=1)
        deleted = []
        monkeypatch.setattr(
            "vastly.gitauth.delete_deploy_key",
            lambda repo, key_id: deleted.append((repo, key_id)) or True,
        )

        gitauth.remove_instance_keys(7)

        assert deleted == [("o/r", 42)]
        assert "7" not in gitauth.load_state()
        assert "Removed deploy key from o/r" in capsys.readouterr().out

    def test_failed_delete_points_to_github_settings(
        self, monkeypatch, gh_installed, capsys
    ):
        gitauth.record_deploy_key(7, "o/r", 42, account=1)
        monkeypatch.setattr("vastly.gitauth.delete_deploy_key", lambda *a: False)

        gitauth.remove_instance_keys(7)

        assert "https://github.com/o/r/settings/keys" in capsys.readouterr().out

    def test_unknown_instance_is_a_no_op(self):
        gitauth.remove_instance_keys(7)  # no state, no gh calls


class TestPrune:
    def test_forgets_gone_instance_without_keys_without_lookups(self):
        gitauth.record_agent(7, account=None)
        gitauth.prune(existing_ids=set())  # account_id() would raise if called
        assert gitauth.load_state() == {}

    def test_keeps_existing_instances(self):
        gitauth.record_deploy_key(7, "o/r", 42, account=1)
        gitauth.prune(existing_ids={"7"})
        assert "7" in gitauth.load_state()

    def test_deletes_keys_of_gone_instance_on_current_account(
        self, monkeypatch, gh_installed
    ):
        gitauth.record_deploy_key(7, "o/r", 42, account=1)
        monkeypatch.setattr("vastly.gitauth.account_id", lambda: 1)
        deleted = []
        monkeypatch.setattr(
            "vastly.gitauth.delete_deploy_key",
            lambda repo, key_id: deleted.append(key_id) or True,
        )

        gitauth.prune(existing_ids=set())

        assert deleted == [42]
        assert gitauth.load_state() == {}

    def test_keeps_keys_of_instance_on_another_account(self, monkeypatch):
        # Missing from this account's list doesn't mean destroyed -- it may
        # belong to the account whose API key you switched away from
        gitauth.record_deploy_key(7, "o/r", 42, account=1)
        monkeypatch.setattr("vastly.gitauth.account_id", lambda: 2)

        gitauth.prune(existing_ids=set())

        assert "7" in gitauth.load_state()

    def test_keeps_keys_when_account_unknown(self, monkeypatch):
        gitauth.record_deploy_key(7, "o/r", 42, account=1)
        monkeypatch.setattr("vastly.gitauth.account_id", lambda: None)

        gitauth.prune(existing_ids=set())

        assert "7" in gitauth.load_state()
