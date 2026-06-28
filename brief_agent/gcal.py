"""Read-only Google Calendar reader (Phase 7 — connector decoupling).

Replaces the claude.ai Google Calendar *connector* with a direct, headless read via a Google
**service account**, authenticated by a JSON key — independent of any claude.ai login (which the
API key disables). The day's events are read ONCE up front; drafting itself never touches the
calendar, so this is a plain data fetch, not a model-facing tool.

READ-ONLY BY SCOPE: the only OAuth scope requested is `calendar.readonly`, so a write is not merely
un-attempted — the credential physically cannot mutate the calendar. `fetch_day_events` therefore
returns an empty tool-call list: there are no calendar tool calls to audit, and zero writes is
guaranteed by construction.

`google-api-python-client` + `google-auth` are imported lazily inside the functions so the rest of
the package (and the pure `calendar.py` parser the evals use) stays importable without them.
"""

import asyncio
import os
import re
from datetime import date, datetime, timedelta

_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
_SA_FILE_ENV = "GOOGLE_SERVICE_ACCOUNT_FILE"
_CAL_ID_ENV = "GOOGLE_CALENDAR_ID"
_TAG_RE = re.compile(r"<[^>]+>")
# Whole-read timeout for the calendar. The google client is SYNCHRONOUS, so we run it in a thread
# and bound it with asyncio.wait_for — a hung calendar then fails the run (→ failure email) rather
# than blocking the event loop forever. Override with CAL_TIMEOUT (seconds).
_CAL_TIMEOUT = int(os.environ.get("CAL_TIMEOUT", "60") or "60")


class CalendarAuthError(RuntimeError):
    """Google Calendar credentials are missing or unusable — surfaced to the CLI cleanly."""


def _calendar_id() -> str:
    cid = os.environ.get(_CAL_ID_ENV, "").strip()
    if not cid:
        raise CalendarAuthError(
            f"{_CAL_ID_ENV} is not set. Add it to .env (your calendar address) — see .env.example."
        )
    return cid


def _service():
    """Build a read-only Calendar API client from the service-account key. Lazy google imports."""
    saf = os.environ.get(_SA_FILE_ENV, "").strip()
    if not saf:
        raise CalendarAuthError(
            f"{_SA_FILE_ENV} is not set. Point it at your service-account JSON — see .env.example."
        )
    if not os.path.exists(saf):
        raise CalendarAuthError(f"{_SA_FILE_ENV}={saf!r} does not exist.")
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as e:  # pragma: no cover - env-dependent
        raise CalendarAuthError(
            "google-api-python-client / google-auth are not installed "
            "(pip install -r requirements.txt)."
        ) from e
    try:
        creds = service_account.Credentials.from_service_account_file(saf, scopes=_SCOPES)
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:  # noqa: BLE001 - bad key file, etc.
        raise CalendarAuthError(f"could not load service-account credentials: {e}") from e


def _day_bounds(svc, cid: str, day: date) -> tuple[str, str]:
    """RFC3339 [start, end) spanning the LOCAL calendar day (uses the calendar's own time zone)."""
    try:
        from zoneinfo import ZoneInfo
        tz_name = svc.calendars().get(calendarId=cid).execute().get("timeZone", "UTC")
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - fall back to UTC bounds if tz lookup fails
        from datetime import timezone
        tz = timezone.utc
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _clean_description(desc: str) -> str:
    """Strip HTML tags and truncate to ~300 chars (mirrors the old fetch contract)."""
    if not desc:
        return ""
    text = _TAG_RE.sub("", desc).strip()
    return text[:300]


def _read_day_blocking(day: date) -> list[dict]:
    """SYNCHRONOUS calendar read (creds build + day-bounds + events list). Runs in a worker thread."""
    cid = _calendar_id()
    svc = _service()
    time_min, time_max = _day_bounds(svc, cid, day)
    resp = (
        svc.events()
        .list(
            calendarId=cid,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        )
        .execute()
    )
    raw: list[dict] = []
    for e in resp.get("items", []):
        raw.append(
            {
                "id": e.get("id", ""),
                "summary": e.get("summary", "(no title)"),
                "description": _clean_description(e.get("description", "")),
                "start": e.get("start", {}),
                "end": e.get("end", {}),
                "attendees": e.get("attendees", []) or [],
                "organizer": e.get("organizer", {}),
                "creator": e.get("creator", {}),
            }
        )
    return raw


async def fetch_day_events(day: date, model: str | None = None) -> tuple[list[dict], list[str]]:
    """Read a day's events read-only via the service account, bounded by `_CAL_TIMEOUT`.

    Returns (raw_events, tool_calls). `model` is accepted for signature compatibility with the
    former agentic fetch but is unused (no model is involved). `tool_calls` is always empty: the
    read uses a read-only scope, so there is nothing to audit and zero writes is guaranteed.
    The raw events use the standard Google Calendar shape that `calendar.parse_events` consumes.

    The google client is synchronous, so the read runs in a worker thread under `asyncio.wait_for`;
    a hung calendar raises `CalendarAuthError` (→ whole-run failure email) instead of hanging.
    """
    try:
        raw = await asyncio.wait_for(asyncio.to_thread(_read_day_blocking, day), _CAL_TIMEOUT)
    except asyncio.TimeoutError as e:
        raise CalendarAuthError(
            f"Google Calendar read timed out after {_CAL_TIMEOUT}s."
        ) from e
    return raw, []
