"""Offline tests for the best-effort heartbeat ping (no real network)."""

import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brief_agent import heartbeat  # noqa: E402


class _FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_ping_empty_url_is_noop():
    assert heartbeat.ping("") is False


def test_ping_success(monkeypatch):
    called = {}

    def fake_urlopen(url, timeout=None):
        called["url"] = url
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert heartbeat.ping("https://hc-ping.example.com/abc") is True
    assert called["url"] == "https://hc-ping.example.com/abc"


def test_ping_swallows_errors(monkeypatch):
    def boom(url, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    # Must NOT raise — best-effort. Returns False.
    assert heartbeat.ping("https://hc-ping.example.com/abc") is False


def test_heartbeat_url_reads_env(monkeypatch):
    monkeypatch.setenv("HEARTBEAT_URL", "  https://hc-ping.example.com/x  ")
    assert heartbeat.heartbeat_url() == "https://hc-ping.example.com/x"
    monkeypatch.delenv("HEARTBEAT_URL", raising=False)
    assert heartbeat.heartbeat_url() == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
