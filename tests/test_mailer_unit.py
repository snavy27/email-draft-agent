"""Offline tests for the send-only mailer (no real SMTP connection).

Replaces `smtplib.SMTP` with a fake that records the login + message, then asserts: exactly one
recipient, the PDF attachment is present, the subject is set, and the password NEVER appears in the
serialized message or the config's log-safe summary.
"""

import smtplib
import ssl
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brief_agent import mailer  # noqa: E402
from brief_agent.mailer import (  # noqa: E402
    MissingSMTPConfigError,
    load_email_config,
    send_email,
)

_PW = "super-secret-app-password"


class _FakeSMTP:
    """Records what send_email does without touching the network."""

    captured: dict = {}

    def __init__(self, host, port, timeout=None):
        _FakeSMTP.captured = {"host": host, "port": port, "started_tls": False}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self, *a):
        pass

    def starttls(self, *a, **k):
        _FakeSMTP.captured["started_tls"] = True
        _FakeSMTP.captured["tls_context"] = k.get("context") or (a[0] if a else None)

    def login(self, user, password):
        _FakeSMTP.captured["login"] = (user, password)

    def send_message(self, msg):
        _FakeSMTP.captured["msg"] = msg


@pytest.fixture(autouse=True)
def _smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "sender@example.com")
    monkeypatch.setenv("SMTP_PASS", _PW)
    monkeypatch.setenv("BRIEF_FROM", "sender@example.com")
    monkeypatch.setenv("BRIEF_RECIPIENT", "boss@example.com")
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    yield


def test_load_config_defaults_and_override():
    cfg = load_email_config()
    assert cfg.host == "smtp.example.com" and cfg.port == 587
    assert cfg.recipient == "boss@example.com"
    assert load_email_config(to_override="other@x.com").recipient == "other@x.com"


def test_missing_credentials_fail_fast(monkeypatch):
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    with pytest.raises(MissingSMTPConfigError):
        load_email_config()


def test_send_email_one_recipient_with_attachment():
    cfg = load_email_config()
    send_email(cfg, "Meeting briefs — test", "body text here",
               [("briefs.pdf", b"%PDF-1.3 fake")])
    cap = _FakeSMTP.captured

    assert cap["started_tls"] is True
    # TLS must use a real verifying SSL context (not smtplib's context-less default).
    assert isinstance(cap["tls_context"], ssl.SSLContext)
    assert cap["tls_context"].verify_mode == ssl.CERT_REQUIRED
    assert cap["tls_context"].check_hostname is True
    assert cap["login"] == ("sender@example.com", _PW)

    msg = cap["msg"]
    # Exactly one recipient (no list expansion, no Cc/Bcc).
    assert msg["To"] == "boss@example.com" and "," not in msg["To"]
    assert msg["Cc"] is None and msg["Bcc"] is None
    assert msg["Subject"] == "Meeting briefs — test"

    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1
    att = attachments[0]
    assert att.get_content_type() == "application/pdf"
    assert att.get_filename() == "briefs.pdf"


def test_comma_recipient_rejected(monkeypatch):
    monkeypatch.setenv("BRIEF_RECIPIENT", "a@x.com, b@y.com")
    with pytest.raises(MissingSMTPConfigError):
        load_email_config()
    # also rejected via --to
    with pytest.raises(MissingSMTPConfigError):
        load_email_config(to_override="a@x.com,b@y.com")


def test_gmail_password_spaces_stripped(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_PASS", "abcd efgh ijkl mnop")
    assert load_email_config().password == "abcdefghijklmnop"


def test_non_gmail_password_spaces_preserved(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.fastmail.com")
    monkeypatch.setenv("SMTP_PASS", "  pa ss wd  ")
    # internal spaces preserved for non-Gmail; only the ends trimmed
    assert load_email_config().password == "pa ss wd"


def test_port_465_uses_implicit_tls(monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "465")
    captured = {}

    class _FakeSSL:
        def __init__(self, host, port, timeout=None, context=None):
            captured["host"] = host
            captured["port"] = port
            captured["context"] = context

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, u, p):
            captured["login"] = (u, p)

        def send_message(self, msg):
            captured["sent"] = True

    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSSL)
    cfg = load_email_config()
    send_email(cfg, "s", "b", [("a.pdf", b"%PDF")])
    assert captured["port"] == 465
    assert isinstance(captured["context"], ssl.SSLContext)
    assert captured["sent"] is True


def test_password_never_serialized():
    cfg = load_email_config()
    send_email(cfg, "subj", "body", [("a.pdf", b"%PDF data")])
    serialized = _FakeSMTP.captured["msg"].as_string()
    assert _PW not in serialized
    assert _PW not in cfg.safe_summary()


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
