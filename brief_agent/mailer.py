"""Send-only email delivery — the agent's single outbound action.

Deliberately narrow: one recipient, send-only (SMTP submission over STARTTLS, no IMAP, no folder
access, no list expansion). Everything else in the pipeline is read-only; this module is the one
place the agent reaches outward, so it is kept as small and auditable as possible.

Credentials come from the environment (loaded from `.env` by `config.load_env`) and are NEVER
printed or logged — error messages here are deliberately secret-free, mirroring `config.py`.

Gmail: set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER=<you>@gmail.com`, and
`SMTP_PASS=<16-char App Password>` (a normal account password will not work with 2FA).
"""

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

_DEFAULT_RECIPIENT = "shardanavalika@gmail.com"

_MISSING_SMTP_MESSAGE = (
    "SMTP credentials are not set. Add SMTP_USER and SMTP_PASS to .env (for Gmail, SMTP_PASS is a "
    "16-character App Password, not your login password) — see .env.example. Email is the only "
    "outbound action and has no fallback."
)


class MissingSMTPConfigError(RuntimeError):
    """Raised when SMTP_USER / SMTP_PASS are absent — email cannot be sent."""


@dataclass
class EmailConfig:
    """Resolved SMTP settings + the single sender/recipient pair. Holds the password in memory only."""

    host: str
    port: int
    user: str
    password: str
    sender: str
    recipient: str

    def safe_summary(self) -> str:
        """A log-safe one-liner — host/port/sender/recipient only, never the password."""
        return f"{self.host}:{self.port} as {self.user} -> {self.recipient}"


def load_email_config(to_override: str | None = None) -> EmailConfig:
    """Read SMTP settings from the environment, failing fast if user/password are missing.

    `to_override` (the `--to` flag) replaces the recipient for a one-off send. Defaults:
    host=smtp.gmail.com, port=587, sender=SMTP_USER, recipient=BRIEF_RECIPIENT or a fixed address.
    """
    user = os.environ.get("SMTP_USER", "").strip()
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com"

    # Gmail App Passwords are displayed as 4 space-separated groups ("abcd efgh ijkl mnop") but the
    # real secret is the 16 chars with no spaces. Strip internal spaces ONLY for Gmail; for any other
    # provider just trim the ends, so a legitimate password containing spaces is never mangled.
    raw_pass = os.environ.get("SMTP_PASS", "")
    password = "".join(raw_pass.split()) if host.lower() == "smtp.gmail.com" else raw_pass.strip()
    if not user or not password:
        raise MissingSMTPConfigError(_MISSING_SMTP_MESSAGE)

    try:
        port = int(os.environ.get("SMTP_PORT", "587").strip() or "587")
    except ValueError as exc:
        raise MissingSMTPConfigError("SMTP_PORT must be an integer (e.g. 587).") from exc

    sender = os.environ.get("BRIEF_FROM", "").strip() or user
    recipient = (to_override or os.environ.get("BRIEF_RECIPIENT", "").strip()
                 or _DEFAULT_RECIPIENT).strip()
    # Enforce the single-recipient invariant: a comma/whitespace-separated list would silently
    # fan the confidential brief out to every address via the To header. Reject it.
    if not recipient or any(c in recipient for c in ", \t") or recipient.count("@") != 1:
        raise MissingSMTPConfigError(
            "BRIEF_RECIPIENT / --to must be exactly ONE email address (no lists). "
            f"Got: {recipient!r}"
        )
    return EmailConfig(host, port, user, password, sender, recipient)


def _build_message(
    cfg: EmailConfig,
    subject: str,
    body_text: str,
    attachments: list[tuple[str, bytes]] | None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.sender
    msg["To"] = cfg.recipient  # exactly one recipient by construction
    msg["Subject"] = subject
    msg.set_content(body_text)
    for filename, data in attachments or []:
        msg.add_attachment(
            data, maintype="application", subtype="pdf", filename=filename
        )
    return msg


def send_email(
    cfg: EmailConfig,
    subject: str,
    body_text: str,
    attachments: list[tuple[str, bytes]] | None = None,
) -> None:
    """Send one message to the single configured recipient over verified TLS. Raises on failure.

    Send-only: opens an SMTP submission connection, authenticates, sends, quits. No mailbox is ever
    read. TLS uses a `create_default_context()` (certificate + hostname verification ON, unlike
    smtplib's default for a context-less `starttls()`), so the credentials and the brief can't be
    captured by a network MITM. Port 465 uses implicit TLS (SMTP_SSL); anything else uses STARTTLS.
    The password lives only in `cfg` and is never logged.
    """
    msg = _build_message(cfg, subject, body_text, attachments)
    context = ssl.create_default_context()
    if cfg.port == 465:
        with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=60, context=context) as smtp:
            smtp.login(cfg.user, cfg.password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cfg.host, cfg.port, timeout=60) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(cfg.user, cfg.password)
            smtp.send_message(msg)


def send_failure_notice(
    cfg: EmailConfig, subject: str, summary: str, traceback_text: str
) -> None:
    """Email a plain-text failure notice (error summary + short traceback). Raises on send failure."""
    body = (
        "The scheduled meeting-brief job failed and produced no packet.\n\n"
        f"Error: {summary}\n\n"
        "Traceback (most recent calls):\n"
        f"{traceback_text}\n"
    )
    send_email(cfg, subject, body)
