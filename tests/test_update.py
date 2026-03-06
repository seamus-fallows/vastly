"""Tests for vastly.update -- version checking and parsing."""

from __future__ import annotations

import time

import pytest


class TestUpdateCheck:
    def test_parse_version(self):
        from vastly.update import _parse_version

        assert _parse_version("0.2.6") == (0, 2, 6)
        assert _parse_version("1.0.0") == (1, 0, 0)
        assert _parse_version("1.0.0") > _parse_version("0.9.9")
        assert _parse_version("0.2.6") < _parse_version("0.2.7")
        assert _parse_version("0.3.0") > _parse_version("0.2.99")

    def test_cache_prevents_repeated_checks(self, tmp_path, monkeypatch):
        """When cache file is recent, no network request should be made."""
        from vastly.update import check_for_update

        cache_file = tmp_path / ".last-update-check"
        cache_file.write_text(str(time.time()), encoding="utf-8")
        monkeypatch.setattr("vastly.update._CACHE_FILE", cache_file)

        # If urlopen were called it would fail -- this verifies it's skipped
        monkeypatch.setattr(
            "vastly.update.urllib.request.urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("should not be called")
            ),
        )

        check_for_update()  # Should return silently (cache is fresh)

    def test_network_error_silently_swallowed(self, tmp_path, monkeypatch):
        """Network failures should not raise or print errors."""
        from vastly.update import check_for_update

        # No cache file -- will try to fetch
        monkeypatch.setattr(
            "vastly.update._CACHE_FILE", tmp_path / ".last-update-check"
        )
        monkeypatch.setattr("vastly.update._CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            "vastly.update.urllib.request.urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("no internet")),
        )

        check_for_update()  # Should not raise


class TestParseVersionPreRelease:
    """Test _parse_version with pre-release and malformed versions."""

    def test_normal_version(self):
        from vastly.update import _parse_version

        assert _parse_version("0.3.1") == (0, 3, 1)

    def test_pre_release_alpha(self):
        from vastly.update import _parse_version

        assert _parse_version("0.4.0a1") == (0, 4, 0)

    def test_pre_release_dev(self):
        from vastly.update import _parse_version

        assert _parse_version("1.0.0.dev1") == (1, 0, 0)

    def test_pre_release_rc(self):
        from vastly.update import _parse_version

        assert _parse_version("2.0.0rc1") == (2, 0, 0)

    def test_pre_release_post(self):
        from vastly.update import _parse_version

        assert _parse_version("0.4.0.post1") == (0, 4, 0)

    def test_empty_string(self):
        from vastly.update import _parse_version

        assert _parse_version("") == (0,)

    def test_comparison_stable_beats_prerelease(self):
        from vastly.update import _parse_version

        assert _parse_version("0.4.0") >= _parse_version("0.4.0a1")
