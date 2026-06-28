"""Dead-man's-switch heartbeat (Phase 8).

On a fully successful scheduled run, the job pings a watchdog URL (healthchecks.io-style). Configure
the watchdog to expect that ping shortly after the scheduled send time: if the host is down, cron is
broken, or the machine is asleep, the job never runs, the ping never arrives, and the watchdog's
missed-ping alert fires — telling you the brief didn't go out even though nothing could email you.

The ping is strictly BEST-EFFORT: it is pinged only AFTER the email has already sent, and any failure
here is swallowed so it can never crash the run, change the exit code, or block delivery. We ping
ONLY on success — a failed run intentionally stays silent so the watchdog alarm is what fires.
"""

import os
import sys
import urllib.request

_HEARTBEAT_ENV = "HEARTBEAT_URL"


def heartbeat_url() -> str:
    """The configured watchdog URL (empty string if unset/disabled)."""
    return os.environ.get(_HEARTBEAT_ENV, "").strip()


def ping(url: str, *, timeout: int = 10) -> bool:
    """GET the watchdog URL, best-effort. Returns True on a 2xx-ish success, False on any failure.

    Never raises: a watchdog outage must not affect the run. The URL is a monitor handle, not a
    secret, so it is safe to mention in a log line.
    """
    if not url:
        return False
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed monitor URL
            return 200 <= getattr(resp, "status", 200) < 400
    except Exception as exc:  # noqa: BLE001 - best-effort; swallow everything
        print(f"heartbeat ping failed (ignored): {type(exc).__name__}", file=sys.stderr)
        return False
