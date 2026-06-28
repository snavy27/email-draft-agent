"""Phase 4 orchestration: a calendar day in, one daily briefing packet out.

Batches the existing per-meeting engine (`brief_agent.agent.draft_brief`) over a day's events:
- internal-only events are skipped (listed, not briefed);
- external events get a person-centered brief via the meeting-aware engine;
- external events with no CRM match get a deterministic calendar-only STUB (nothing invented).

Everything is ordered by start time and rendered into one packet, with a provenance sidecar
(calendar event id + the Notion pages used per brief). The packet records every tool call across
the run so the caller can ASSERT zero writes to both Calendar and Notion.
"""

import asyncio
import os
from dataclasses import dataclass, field
from datetime import date

from .agent import (
    _ALL_WRITE_SET,
    DEFAULT_MODEL,
    MeetingHint,
    draft_brief,
)
from .calendar import CalEvent, fetch_day_events, parse_events
from .web import format_web_context, gather_web_news

_MAX_CONCURRENCY = 3  # cap concurrent per-meeting drafts (matches the eval suite)

# Per-meeting wall-clock budget. One meeting's whole pipeline (its Notion gather, web research, and
# Claude draft all run inside `_brief_one`) must finish within this, else that ONE meeting is failed
# and the rest of the packet still ships. Override with BRIEF_MEETING_TIMEOUT (seconds).
_PER_MEETING_TIMEOUT = int(os.environ.get("BRIEF_MEETING_TIMEOUT", "300") or "300")


@dataclass
class PacketItem:
    """One line of the day: a full brief, a calendar-only stub, or a skipped internal event."""

    event_id: str
    title: str
    when: str
    status: str            # "briefed" | "stub" | "skipped" | "failed"
    sort_key: float
    person: str | None = None
    text: str = ""         # rendered brief/stub markdown ("" for skipped/failed)
    reason: str = ""       # why a "failed" item could not complete (empty otherwise)
    sources: dict = field(default_factory=dict)
    body_words: int = 0
    retried: bool = False
    tool_calls: list[str] = field(default_factory=list)
    web_sources: list = field(default_factory=list)  # web items offered (Phase 5)
    web_cited: list = field(default_factory=list)     # web URLs the brief actually cited


@dataclass
class DayPacket:
    day: date
    items: list[PacketItem]    # briefed + stub, in start order
    skipped: list[PacketItem]  # internal, in start order
    tool_calls: list[str] = field(default_factory=list)  # every call across the whole run

    @property
    def briefed(self) -> int:
        return sum(1 for i in self.items if i.status == "briefed")

    @property
    def stubs(self) -> int:
        return sum(1 for i in self.items if i.status == "stub")

    @property
    def failed(self) -> int:
        """External meetings whose brief could not be produced (error/timeout) — isolated."""
        return sum(1 for i in self.items if i.status == "failed")

    @property
    def total(self) -> int:
        return len(self.items) + len(self.skipped)

    @property
    def write_tool_calls(self) -> list[str]:
        """Any write tool (Calendar or Notion) seen anywhere in the run — must be empty."""
        return [t for t in self.tool_calls if t in _ALL_WRITE_SET]

    @property
    def made_any_write(self) -> bool:
        return bool(self.write_tool_calls)


# --------------------------------------------------------------------------- #
# Deterministic stub + metadata-time prepend (no model, nothing invented)
# --------------------------------------------------------------------------- #
def _stub_brief(event: CalEvent) -> str:
    """A minimal calendar-only brief for an event with no CRM match. Invents nothing."""
    a = event.attendee or {}
    person = a.get("name") or "(unknown attendee)"
    email = f" ({a['email']})" if a.get("email") else ""
    company = event.company_token or "the company"
    return (
        f"# Meeting Brief — {event.title}\n\n"
        f"**When:** {event.when_str()} · **Who:** {person}{email} · "
        f"**Purpose:** {event.title}\n"
        f"**Sources:** No CRM match — calendar details only.\n\n"
        f"_No CRM match — calendar details only. {person} / {company} was not found in the "
        f"Notion CRM, so there is no account context to brief. Confirm the record in Notion "
        f"before the meeting._"
    )


def _prepend_meeting_time(brief: str, when: str) -> str:
    """Prepend the real calendar meeting time to the brief's metadata line.

    Guarantees the metadata line leads with the actual meeting time regardless of what the
    model wrote into `When:`. Idempotent-ish: only the first `**When:**` line is touched.
    """
    lines = brief.split("\n")
    for i, ln in enumerate(lines):
        if "**when:**" in ln.lower():
            if ln.lstrip().startswith("**Meeting:**"):
                return brief  # already prepended
            lines[i] = f"**Meeting:** {when} · " + ln
            return "\n".join(lines)
    return brief  # no metadata line found — leave untouched


def _item_sources(
    result_sources: dict, event_id: str, status: str,
    web_sources: list | None = None, web_cited: list | None = None,
) -> dict:
    """Provenance record for the sidecar: event id + Notion pages (CRM) + web sources."""
    src = result_sources or {}
    return {
        "event_id": event_id,
        "status": status,
        "account": src.get("account"),
        "contacts": src.get("contacts", []),
        "meetings": src.get("meetings", []),
        "web": web_sources or [],          # company-level news offered to the draft
        "web_cited": web_cited or [],      # the URLs actually cited in the brief
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _hint(event: CalEvent) -> MeetingHint:
    a = event.attendee or {}
    return MeetingHint(
        person=a.get("name", ""),
        email=a.get("email", ""),
        company=event.company_token,
        when=event.when_str(),
        title=event.title,
        description=event.description,
        event_id=event.id,
    )


def _failed_item(event: CalEvent, reason: str) -> PacketItem:
    """A per-meeting failure note: this ONE meeting could not be briefed; the packet still ships."""
    a = event.attendee or {}
    return PacketItem(
        event_id=event.id, title=event.title, when=event.when_str(),
        status="failed", sort_key=event.sort_key(), person=a.get("name"),
        text="", reason=reason,
        sources={"event_id": event.id, "status": "failed", "reason": reason},
    )


async def _brief_one_safe(
    event: CalEvent, model: str, sem: asyncio.Semaphore, enable_web: bool
) -> PacketItem:
    """Isolate one meeting: hold the concurrency slot, bound it by a timeout, never propagate.

    The semaphore is acquired here (NOT inside the timeout) so queue-wait doesn't count against the
    per-meeting budget. Any error or timeout becomes a `failed` PacketItem so a single bad meeting
    cannot sink the whole packet.
    """
    async with sem:
        try:
            return await asyncio.wait_for(
                _brief_one(event, model, enable_web), _PER_MEETING_TIMEOUT
            )
        except asyncio.TimeoutError:
            return _failed_item(event, f"timed out after {_PER_MEETING_TIMEOUT}s")
        except Exception as exc:  # noqa: BLE001 - isolate; the rest of the day still ships
            return _failed_item(event, f"{type(exc).__name__}: {exc}")


async def _brief_one(event: CalEvent, model: str, enable_web: bool) -> PacketItem:
    """Draft one external event into a PacketItem (full brief, or stub on no CRM match).

    When `enable_web`, first gathers company-level web news (read-only) and injects it into the
    draft. Web tool calls are returned in the item's tool_calls so the packet's zero-writes
    assertion covers them. A no-CRM-match event becomes a stub and the web context is discarded
    (no resolved account to attach company news to). Raises on error — `_brief_one_safe` isolates it.
    """
    a = event.attendee or {}
    web_calls: list[str] = []
    web_sources: list = []
    web_ctx = web_urls = None
    if enable_web:
        items, web_calls = await gather_web_news(event.company_token, model)
        web_sources = [it.to_dict() for it in items]
        if items:
            web_ctx = format_web_context(items)
            web_urls = [it.url for it in items]
    result = await draft_brief(
        event.company_token, model=model, meeting=_hint(event),
        web_context=web_ctx, web_urls=web_urls,
    )

    if result.unresolved:
        # Agent could not resolve the account/person → deterministic calendar-only stub.
        # Web is company-level enrichment for a RESOLVED account, so it is dropped here.
        return PacketItem(
            event_id=event.id, title=event.title, when=event.when_str(),
            status="stub", sort_key=event.sort_key(), person=a.get("name"),
            text=_stub_brief(event),
            sources=_item_sources(result.sources, event.id, "stub"),
            tool_calls=result.tool_calls + web_calls,
        )

    return PacketItem(
        event_id=event.id, title=event.title, when=event.when_str(),
        status="briefed", sort_key=event.sort_key(), person=a.get("name"),
        text=_prepend_meeting_time(result.text, event.when_str()),
        sources=_item_sources(
            result.sources, event.id, "briefed", web_sources, result.web_cited
        ),
        body_words=result.body_words,
        retried=result.retried,
        tool_calls=result.tool_calls + web_calls,
        web_sources=web_sources,
        web_cited=result.web_cited,
    )


async def run_daily_briefing(
    events: list[CalEvent],
    model: str = DEFAULT_MODEL,
    *,
    fetch_tool_calls: list[str] | None = None,
    enable_web: bool = False,
    day: date | None = None,
) -> DayPacket:
    """Batch the per-meeting engine over a day's (already-parsed) events.

    `fetch_tool_calls` lets the caller fold the calendar-read agent's tool calls into the
    packet's zero-writes accounting. `enable_web` turns on Phase 5 company-news enrichment
    (default OFF so the deterministic eval path never hits the network). `day` labels the packet
    explicitly so an EMPTY day is still dated correctly (else it's inferred from the first event).
    `events` must be the output of `calendar.parse_events` (sorted by start time).
    """
    day = day or (events[0].start.date() if events else date.today())

    skipped: list[PacketItem] = [
        PacketItem(
            event_id=e.id,
            title=e.title,
            when=e.when_str(),
            status="skipped",
            sort_key=e.sort_key(),
        )
        for e in events
        if e.is_internal
    ]

    external = [e for e in events if not e.is_internal]
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)
    # Each meeting is isolated + timeout-bounded; a failure becomes a "failed" item, never an
    # exception, so one bad meeting cannot sink the packet.
    items = await asyncio.gather(*(_brief_one_safe(e, model, sem, enable_web) for e in external))
    items = sorted(items, key=lambda i: i.sort_key)

    tool_calls = list(fetch_tool_calls or [])
    for i in items:
        tool_calls.extend(i.tool_calls)

    return DayPacket(day=day, items=items, skipped=skipped, tool_calls=tool_calls)


# --------------------------------------------------------------------------- #
# Shared orchestration helpers (used by BOTH the CLI and the scheduled run, so the two
# paths cannot drift — they fetch, brief, and build provenance identically).
# --------------------------------------------------------------------------- #
async def build_day_packet(
    day: date, model: str = DEFAULT_MODEL, *, enable_web: bool = True
) -> DayPacket:
    """Read a day's calendar (read-only) and brief every external meeting into one DayPacket."""
    raw, fetch_calls = await fetch_day_events(day, model)
    events = parse_events(raw)
    return await run_daily_briefing(
        events, model=model, fetch_tool_calls=fetch_calls, enable_web=enable_web, day=day
    )


def packet_provenance(packet: DayPacket, day: date, model: str) -> dict:
    """The combined `.sources.json` sidecar: per-brief event id + Notion pages used + web sources."""
    return {
        "date": day.isoformat(),
        "model": model,
        "counts": {
            "total": packet.total,
            "briefed": packet.briefed,
            "unresolved": packet.stubs,
            "failed": packet.failed,
            "skipped": len(packet.skipped),
        },
        "items": [i.sources for i in packet.items],
        "skipped": [
            {"event_id": s.event_id, "status": "skipped", "title": s.title}
            for s in packet.skipped
        ],
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_packet(packet: DayPacket) -> str:
    """Render the combined daily packet: header + counts + skipped + could-not-complete + briefs."""
    day_str = packet.day.strftime("%A %d %B %Y")
    counts = [f"{packet.briefed} briefed", f"{packet.stubs} unresolved"]
    if packet.failed:
        counts.append(f"{packet.failed} could not complete")
    counts.append(f"{len(packet.skipped)} skipped (internal)")
    out: list[str] = [
        f"# Daily briefing — {day_str}",
        "",
        f"{packet.total} meetings · " + " / ".join(counts),
        "",
        "## Skipped (internal)",
    ]
    if packet.skipped:
        out += [f"- {s.when} — {s.title}" for s in packet.skipped]
    else:
        out.append("_None._")

    # Per-meeting failures: a compact section (in start order) so the rest of the day still ships.
    failures = [i for i in packet.items if i.status == "failed"]
    if failures:
        out += ["", "## Could not complete"]
        out += [f"- {i.when} — {i.title} — {i.reason}" for i in failures]

    # Full briefs / stubs, in start order (failures already summarised above).
    for item in packet.items:
        if item.status == "failed":
            continue
        out += ["", "---", "", item.text]

    return "\n".join(out) + "\n"
