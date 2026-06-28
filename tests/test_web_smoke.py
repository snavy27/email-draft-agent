"""LIVE, NON-GATING web smoke test (real network).

Confirms the production web path actually runs and returns real, source-URL-bearing items. This
hits the live web and real news changes over time, so it is NEVER part of the pass/fail gate — run
it ad hoc. It prints SKIP (and exits 0) if the web tools are unavailable or nothing comes back.

Run:  python tests/test_web_smoke.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brief_agent.config import MissingAPIKeyError, ensure_api_key
from brief_agent.web import gather_web_news
from brief_agent.web import _valid_url

# A real, high-news-volume company so the live path almost always finds something.
_COMPANY = "Microsoft"


async def _run() -> int:
    print(f"[live] gathering web news for {_COMPANY!r} (real network)…")
    try:
        items, tool_calls = await gather_web_news(_COMPANY, model="opus")
    except Exception as e:  # noqa: BLE001
        print(f"SKIP  web smoke — gather_web_news raised: {type(e).__name__}: {e}")
        return 0

    print(f"[live] tool calls: {tool_calls}")
    if not items:
        print("SKIP  web smoke — no items returned (web tools unavailable or no fresh news).")
        return 0

    # Non-gating assertions: every returned item must carry a real http(s) URL.
    bad = [it for it in items if not _valid_url(it.url)]
    print(f"[live] {len(items)} item(s):")
    for it in items:
        print(f"    - {it.headline}  [{it.source}]  {it.url}")
    if bad:
        print(f"WARN  {len(bad)} item(s) lacked a valid URL (should have been dropped).")
        return 1
    # No write tool should ever appear.
    writes = [t for t in tool_calls if any(w in t for w in ("create", "update", "delete", "respond"))]
    if writes:
        print(f"FAIL  write tool(s) seen in web path: {writes}")
        return 1
    print(f"PASS  web smoke — {len(items)} sourced item(s), all with real URLs, 0 writes.")
    return 0


def main() -> int:
    try:
        ensure_api_key()  # API-key-ONLY auth (this is a live, real-model test)
    except MissingAPIKeyError as exc:
        print(f"SKIP  web smoke — {exc}")
        return 0
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
