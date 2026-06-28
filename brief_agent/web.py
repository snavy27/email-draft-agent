"""Phase 5 web enrichment — company-level recent news for "What's changed".

`gather_web_news` is a read-only web research SUB-AGENT (its own `query()` call with the
built-in `WebSearch`/`WebFetch` tools). It returns structured, source-URL-bearing news items
that the orchestrator injects into the draft as WEB CONTEXT — the model folds only relevant,
cited items into the existing "What's changed" section.

Why a separable function (Option B): the SDK runs Claude Code in a subprocess, so its built-in
web tools can't be intercepted from Python. Keeping web behind this one function means the eval
suite stays deterministic by injecting fixed `WebItem`s (no network), while production uses the
real tools. It also lets us ENFORCE the honesty rule at the boundary: every item must carry a
real http(s) URL or it is dropped here, before the model ever sees it.

Web is COMPANY-LEVEL ONLY. It is never used to identify, verify, or correct the CRM contact —
the CRM is the sole source of truth for who you're meeting.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from .agent import CALENDAR_WRITE_TOOLS, NOTION_WRITE_TOOLS, WEB_READ_TOOLS, _result_error

# The six company-level news categories in scope (recency ~last 6 months).
WEB_CATEGORIES = ["funding", "launch", "earnings", "M&A", "leadership", "incident"]

_MAX_ITEMS = 6  # keep the injected context tight

# Recency window for "What's changed" web news. Items older than this (or with no parseable
# date) are dropped IN PYTHON before the draft ever sees them — so "every web claim is recent
# and sourced" is a code invariant, not a prompt hope. Override with WEB_RECENCY_DAYS.
_RECENCY_DAYS = int(os.environ.get("WEB_RECENCY_DAYS", "183") or "183")
_FUTURE_SKEW_DAYS = 2  # tolerate small clock/timezone skew on a "published today" item


@dataclass
class WebItem:
    """One company-level news item. `url` is mandatory — no URL, no item."""

    headline: str
    url: str
    date: str = ""
    category: str = ""
    summary: str = ""
    source: str = ""  # url domain, e.g. "reuters.com"

    def to_dict(self) -> dict:
        return {
            "headline": self.headline, "url": self.url, "date": self.date,
            "category": self.category, "summary": self.summary, "source": self.source,
        }


def _valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except (ValueError, AttributeError):
        return False


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except (ValueError, AttributeError):
        return ""


def _extract_json_array(text: str) -> list:
    """Tolerantly pull a JSON array out of the model's reply. Returns [] on any failure.

    Web enrichment is best-effort context, never load-bearing — a malformed reply must
    degrade to "no web news", never crash the brief.
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        obj = json.loads(t[start : end + 1])
        return obj if isinstance(obj, list) else []
    except (ValueError, TypeError):
        return []


def _build_item(r: dict) -> WebItem | None:
    """Build one WebItem from a raw dict, or None if it lacks a valid http(s) URL / headline."""
    if not isinstance(r, dict):
        return None
    url = (r.get("url") or "").strip()
    headline = (r.get("headline") or r.get("title") or "").strip()
    if not _valid_url(url) or not headline:
        return None  # enforcement: no source URL (or no headline) -> not a usable item
    return WebItem(
        headline=headline,
        url=url,
        date=str(r.get("date", "")).strip(),
        category=str(r.get("category", "")).strip(),
        summary=str(r.get("summary", "")).strip(),
        source=_domain(url),
    )


def _to_items(raw: list) -> list[WebItem]:
    """Build WebItems from raw dicts, DROPPING any without a valid http(s) URL. (URL check only.)"""
    items: list[WebItem] = []
    for r in raw:
        it = _build_item(r)
        if it is not None:
            items.append(it)
        if len(items) >= _MAX_ITEMS:
            break
    return items


def _parse_date(s: str) -> date | None:
    """Parse a news item's date. Accepts ISO `YYYY-MM-DD` and `YYYY-MM`; None on anything else."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    parts = s.split("-")
    if len(parts) == 2:
        try:
            return date(int(parts[0]), int(parts[1]), 1)
        except (ValueError, TypeError):
            return None
    return None


def validate_web_items(
    raw: list, *, now: date, max_age_days: int = _RECENCY_DAYS
) -> list[WebItem]:
    """The grounding gate: keep only items with a real URL AND a parseable, in-window date.

    Runs in Python BEFORE drafting, so the model is only ever offered validated items. Drops any
    item without a resolvable source URL (via `_build_item`) and any whose date is missing,
    unparseable, in the future beyond a small skew, or older than `max_age_days`. Caps at _MAX_ITEMS.
    """
    items: list[WebItem] = []
    for r in raw:
        it = _build_item(r)
        if it is None:
            continue
        d = _parse_date(it.date)
        if d is None:
            continue  # strict: no verifiable date -> cannot confirm recency -> drop
        age = (now - d).days
        if age < -_FUTURE_SKEW_DAYS or age > max_age_days:
            continue  # future-dated (beyond skew) or older than the window -> drop
        items.append(it)
        if len(items) >= _MAX_ITEMS:
            break
    return items


_WEB_SYSTEM = """\
You are a read-only company-news researcher. You use ONLY the web search/fetch tools to read
public news; you never write anything anywhere. You return data (a JSON array), nothing else."""

_WEB_TASK = """\
Find RECENT, COMPANY-LEVEL news about the company "{company}" — only these categories:
funding rounds, product launches, earnings/financials, mergers & acquisitions, leadership
changes, and major incidents/outages. Only items from roughly the LAST 6 MONTHS.

Search the web, then fetch the most relevant results to confirm the facts and capture exact
source URLs. Output ONLY a JSON array (no prose, no code fences); each element:
{{"headline","url","date","category","summary"}}
- "url" MUST be the real article URL you fetched (no invented or placeholder URLs).
- "summary" is one factual sentence.
- Include ONLY items that are clearly about THIS company (ignore unrelated entities that merely
  share the name). If you find nothing relevant, output []."""


async def gather_web_news(company: str, model: str = "opus") -> tuple[list[WebItem], list[str]]:
    """Research company-level recent news. Returns (items, tool_calls).

    Read-only: web tools only; all Notion/Calendar writes hard-denied; non-interactive. Items
    without a valid source URL are dropped. Best-effort — returns [] rather than raising on a
    transient web/parse failure (the brief then stands on CRM alone).
    """
    options = ClaudeAgentOptions(
        system_prompt=_WEB_SYSTEM,
        model=model,
        allowed_tools=WEB_READ_TOOLS + ["ToolSearch"],
        disallowed_tools=NOTION_WRITE_TOOLS + CALENDAR_WRITE_TOOLS + ["AskUserQuestion"],
        permission_mode="dontAsk",
        setting_sources=[],  # built-in web tools need no connectors
        max_turns=12,
    )
    tool_calls: list[str] = []
    texts: list[str] = []
    try:
        async for message in query(prompt=_WEB_TASK.format(company=company), options=options):
            if isinstance(message, AssistantMessage):
                for b in message.content:
                    if isinstance(b, ToolUseBlock):
                        tool_calls.append(b.name)
                    elif isinstance(b, TextBlock):
                        texts.append(b.text)
            elif isinstance(message, ResultMessage) and message.is_error:
                # transient (rate limit / overload) — degrade to no web news, keep the tool log
                _ = _result_error(message)
                return [], tool_calls
    except Exception:  # noqa: BLE001 - web is best-effort; never let it break the brief
        return [], tool_calls
    # Grounding gate: only URL-bearing, in-recency-window items reach the draft.
    items = validate_web_items(_extract_json_array("".join(texts)), now=date.today())
    return items, tool_calls


def format_web_context(items: list[WebItem]) -> str:
    """Render web items as a labeled, numbered block for injection into the draft prompt."""
    if not items:
        return "WEB CONTEXT (company-level recent news): none found."
    lines = ["WEB CONTEXT (company-level recent news — each line has its source URL):"]
    for i, it in enumerate(items, 1):
        meta = " · ".join(x for x in (it.date, it.category, it.source) if x)
        suffix = f" ({meta})" if meta else ""
        lines.append(f"[{i}] {it.headline}{suffix}\n    {it.summary}\n    SOURCE: {it.url}")
    return "\n".join(lines)
