"""Tests for vastly.vast -- the Vast.ai REST API client and the API key."""

from __future__ import annotations

import json
import ssl
import sys
import types
import urllib.error

import pytest

from vastly import vast
from vastly.errors import APIError, VastlyError
from vastly.vast import _send as real_send  # before conftest swaps it out

USER = {"id": 7, "username": "alice", "email": "alice@example.com"}


class FakeApi:
    """Stands in for vast._send: gives each reply in turn and records the requests."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, method, url, body, key):
        self.requests.append(
            types.SimpleNamespace(method=method, url=url, body=body, key=key)
        )
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        status, data = reply
        return status, data if isinstance(data, bytes) else json.dumps(data).encode()


@pytest.fixture
def api(monkeypatch):
    """Install a FakeApi with the given replies; retries don't wait."""
    monkeypatch.setattr("vastly.vast._RETRY_DELAYS", (0.0, 0.0))

    def install(*replies):
        fake = FakeApi(*replies)
        monkeypatch.setattr("vastly.vast._send", fake)
        return fake

    return install


@pytest.fixture
def no_key(monkeypatch):
    """No VAST_API_KEY and no key file (conftest points both at a temp home)."""
    monkeypatch.delenv("VAST_API_KEY")


@pytest.fixture
def terminal(monkeypatch):
    """Pretend vst runs in a terminal, where it may ask for a key."""
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))


# ── API key ─────────────────────────────────────────────────────────


class TestSavedKey:
    def test_environment_wins(self):
        vast.key_file().parent.mkdir(parents=True)
        vast.key_file().write_text("from-file", encoding="utf-8")
        assert vast.saved_key() == "test-key"  # VAST_API_KEY, set by conftest

    def test_reads_the_vastai_cli_key_file(self, no_key, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "vastai").mkdir()
        (tmp_path / "vastai" / "vast_api_key").write_text(" abc\n", encoding="utf-8")

        assert vast.key_file() == tmp_path / "vastai" / "vast_api_key"
        assert vast.saved_key() == "abc"

    def test_falls_back_to_the_old_key_file(self, no_key):
        vast.LEGACY_KEY_FILE.write_text("old\n", encoding="utf-8")
        assert vast.saved_key() == "old"

    def test_none_when_there_is_no_key(self, no_key):
        assert vast.saved_key() is None


class TestAskForKey:
    def test_asked_for_checked_and_saved_on_first_use(
        self, no_key, terminal, api, monkeypatch, capsys
    ):
        monkeypatch.setattr("getpass.getpass", lambda prompt: " new-key \n")
        fake = api((200, USER))

        assert vast.api_key() == "new-key"

        assert fake.requests[0].key == "new-key"  # checked before it's saved
        assert vast.key_file().read_text(encoding="utf-8") == "new-key"
        assert "alice (alice@example.com)" in capsys.readouterr().out
        assert vast.saved_key() == "new-key"

    def test_rejected_key_is_not_saved(self, no_key, terminal, api, monkeypatch):
        monkeypatch.setattr("getpass.getpass", lambda prompt: "typo")
        api((401, {"msg": "invalid key"}))

        with pytest.raises(APIError, match="refused the API key"):
            vast.ask_for_key()
        assert not vast.key_file().exists()

    def test_reads_a_visible_line_where_getpass_cant_work(
        self, no_key, api, monkeypatch
    ):
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
        monkeypatch.setattr("getpass.getpass", pytest.fail)
        monkeypatch.setattr("builtins.input", lambda prompt: " piped-key ")
        api((200, USER))

        assert vast.ask_for_key() == "piped-key"

    def test_no_input_explains_how_to_add_one(self, no_key, monkeypatch):
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))

        def eof(prompt):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        with pytest.raises(VastlyError, match="vst config --api-key"):
            vast.api_key()

    def test_empty_answer_cancels(self, no_key, terminal, monkeypatch):
        monkeypatch.setattr("getpass.getpass", lambda prompt: "")
        with pytest.raises(VastlyError, match="No key"):
            vast.ask_for_key()

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix permissions")
    def test_saved_key_is_private(self):
        vast.save_key("secret")
        assert vast.key_file().stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "user, expected",
    [
        (
            {"username": "moirai", "email": "team@example.com", "is_team": True},
            "moirai (team@example.com, team)",
        ),
        (
            {"username": "alice", "email": "alice@example.com", "is_team": False},
            "alice (alice@example.com)",
        ),
        ({"email": "alice@example.com"}, "alice@example.com"),
        ({"username": "alice"}, "alice"),
        ({"id": 123}, None),
    ],
)
def test_account_label(user, expected):
    assert vast.account_label(user) == expected


# ── Requests ────────────────────────────────────────────────────────


class TestSend:
    """_send, the one function that talks to the network."""

    @pytest.fixture
    def urlopen(self, monkeypatch):
        seen = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(request, timeout):
            seen.update(request=request, timeout=timeout)
            return Response()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        return seen

    def test_sends_the_key_and_a_json_body(self, urlopen):
        status, _ = real_send("PUT", "https://x/y", {"state": "running"}, "k")

        request = urlopen["request"]
        assert status == 200
        assert request.get_method() == "PUT"
        assert request.get_header("Authorization") == "Bearer k"
        assert request.get_header("User-agent").startswith("vastly/")
        assert json.loads(request.data) == {"state": "running"}
        assert urlopen["timeout"]  # never hangs forever

    def test_get_has_no_body(self, urlopen):
        real_send("GET", "https://x/y", None, "k")
        assert urlopen["request"].data is None


class TestRequestErrors:
    def test_server_errors_are_retried(self, api):
        fake = api((502, b"bad gateway"), (200, {"instances": []}))
        assert vast.list_instances() == []
        assert len(fake.requests) == 2

    def test_connection_problems_are_retried_then_explained(self, api):
        refused = urllib.error.URLError(ConnectionRefusedError("refused"))
        fake = api(refused)

        with pytest.raises(APIError, match="Couldn't reach Vast.ai .*refused"):
            vast.list_instances()
        assert len(fake.requests) == 3

    def test_missing_certificates_are_explained_not_retried(self, api):
        fake = api(urllib.error.URLError(ssl.SSLCertVerificationError("no CA")))

        with pytest.raises(APIError, match="certificate"):
            vast.list_instances()
        assert len(fake.requests) == 1

    def test_rejected_key_says_how_to_fix_it(self, api):
        fake = api((401, {"msg": "invalid api key"}))

        with pytest.raises(APIError, match="vst config --api-key") as e:
            vast.list_instances()
        assert e.value.status == 401
        assert len(fake.requests) == 1

    def test_other_errors_carry_vasts_reason(self, api):
        api((400, {"success": False, "msg": "bad request"}))
        with pytest.raises(APIError, match="HTTP 400: bad request"):
            vast.list_instances()

    def test_invalid_reply(self, api):
        api((200, b"<html>maintenance</html>"))
        with pytest.raises(APIError, match="invalid data"):
            vast.list_instances()


# ── Endpoints ───────────────────────────────────────────────────────


class TestEndpoints:
    def test_list_instances(self, api):
        fake = api((200, {"instances": [{"id": 1}, {"id": 2}, "junk"]}))

        assert vast.list_instances() == [{"id": 1}, {"id": 2}]
        request = fake.requests[0]
        assert request.method == "GET"
        assert request.url.endswith("/api/v0/instances/?owner=me")
        assert request.key == "test-key"

    def test_get_instance(self, api):
        fake = api((200, {"instances": {"id": 5, "cur_state": "running"}}))
        assert vast.get_instance(5) == {"id": 5, "cur_state": "running"}
        assert "/api/v0/instances/5/" in fake.requests[0].url

    @pytest.mark.parametrize(
        "reply", [(404, {"msg": "not found"}), (200, {"instances": None})]
    )
    def test_get_instance_that_no_longer_exists(self, api, reply):
        api(reply)
        assert vast.get_instance(5) is None

    def test_get_instance_glitch_is_not_gone(self, api):
        api((200, {"unexpected": True}))
        with pytest.raises(APIError):
            vast.get_instance(5)

    def test_start(self, api):
        fake = api((200, {"success": True}))
        assert vast.start_instance(5) is False
        assert fake.requests[0].method == "PUT"
        assert fake.requests[0].body == {"state": "running"}

    def test_start_queued_when_the_machine_is_busy(self, api):
        msg = "Required resources are currently unavailable, state change queued."
        api((200, {"success": False, "msg": msg}))
        assert vast.start_instance(5) is True

    def test_start_refused(self, api):
        api((200, {"success": False, "msg": "insufficient credit"}))
        with pytest.raises(APIError, match="insufficient credit"):
            vast.start_instance(5)

    def test_stop(self, api):
        fake = api((200, {"success": True}))
        vast.stop_instance(5)
        assert fake.requests[0].body == {"state": "stopped"}

    def test_stop_refused(self, api):
        api((200, {"success": False, "msg": "no such instance"}))
        with pytest.raises(APIError, match="no such instance"):
            vast.stop_instance(5)

    def test_destroy(self, api):
        fake = api((200, {"success": True}))
        vast.destroy_instance(5)
        assert fake.requests[0].method == "DELETE"

    def test_destroy_something_already_gone(self, api):
        api((404, {"msg": "not found"}))
        vast.destroy_instance(5)  # should not raise

    def test_destroy_failure(self, api):
        api((200, {"success": False, "msg": "locked"}))
        with pytest.raises(APIError, match="locked"):
            vast.destroy_instance(5)

    def test_current_user_with_a_given_key(self, api):
        fake = api((200, USER))
        assert vast.current_user(key="other")["username"] == "alice"
        assert fake.requests[0].key == "other"
