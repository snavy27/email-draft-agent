"""Pure unit tests for Phase 5 web enrichment — no network, no model.

Covers `web.py` parsing/validation/formatting and the `programmatic_grade_web` honesty checks
(no-invented-URLs, appears/empty/excluded/contact-protected).

Run:  python tests/test_web_unit.py     (self-contained)
  or: pytest tests/test_web_unit.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brief_agent.web import _extract_json_array, _to_items, _valid_url, format_web_context
from eval.cases_web import WEB_CASES
from eval.graders import programmatic_grade_web

_C = {c["id"]: c for c in WEB_CASES}
_BODY = " ".join(["w"] * 290)


def _brief(whats_changed: str, sources_line: str) -> str:
    return (
        "# Meeting Brief — Verizon\n\n"
        f"**When:** x · **Who:** Greg Sullivan · **Purpose:** p · **Sources:** {sources_line}\n\n"
        "---\n\n## Bottom line\n" + _BODY + "\n"
        "## Who you're meeting\n- **Greg Sullivan** — VP Ops\n"
        "## What's changed since you last spoke\n" + whats_changed + "\n"
        "## Likely to come up\n- x\n## Your goals & talking points\n- **G:** do\n"
        "## Watch-outs\n- w\n## Desired outcome\n- o"
    )


# --- web.py ---------------------------------------------------------------- #
def test_valid_url():
    assert _valid_url("https://a.com/x")
    assert _valid_url("http://a.com")
    assert not _valid_url("ftp://a.com")
    assert not _valid_url("not a url")
    assert not _valid_url("")


def test_to_items_drops_urlless_and_bad_scheme():
    raw = [
        {"headline": "Good", "url": "https://reuters.com/x", "category": "funding"},
        {"headline": "No URL", "url": ""},
        {"headline": "Bad scheme", "url": "ftp://x"},
        {"url": "https://x.com/only-url-no-headline"},
    ]
    items = _to_items(raw)
    assert len(items) == 1
    assert items[0].headline == "Good"
    assert items[0].source == "reuters.com"


def test_extract_json_array_tolerant():
    assert _extract_json_array('prefix [{"a":1}] suffix') == [{"a": 1}]
    assert _extract_json_array("```json\n[]\n```") == []
    assert _extract_json_array("no array here") == []   # degrades, never raises
    assert _extract_json_array("[broken json") == []


def test_format_web_context_empty_vs_items():
    assert "none found" in format_web_context([])
    items = _to_items([{"headline": "Verizon acquires Frontier", "url": "https://news.example.com/x"}])
    block = format_web_context(items)
    assert "Verizon acquires Frontier" in block
    assert "https://news.example.com/x" in block


# --- grader: no_invented_urls + per-case behavior -------------------------- #
def test_grade_appears_passes_when_cited():
    url = _C["web_relevant"]["cited_url"]
    b = _brief(
        f"- Agreed to acquire Frontier's fiber business ({url}).",
        f"Verizon (account) · Web: {url}",
    )
    g = programmatic_grade_web(_C["web_relevant"], b, [], 290, _C["web_relevant"]["web_items"])
    assert g["passed"], [n for n, ok, _ in g["checks"] if not ok]


def test_grade_rejects_invented_url():
    b = _brief(
        "- Agreed to acquire Frontier (https://news.example.com/INVENTED).",
        "Verizon (account) · Web: https://news.example.com/INVENTED",
    )
    g = programmatic_grade_web(_C["web_relevant"], b, [], 290, _C["web_relevant"]["web_items"])
    failed = [n for n, ok, _ in g["checks"] if not ok]
    assert "no_invented_urls" in failed


def test_grade_empty_no_padding():
    b = _brief("- Two Q2 outages hurt trust.", "Verizon (account)")
    g = programmatic_grade_web(_C["web_empty"], b, [], 290, _C["web_empty"]["web_items"])
    assert g["passed"], [n for n, ok, _ in g["checks"] if not ok]


def test_grade_excludes_wrong_entity():
    bad = _brief("- A Verizon coffee chain opened its 50th location.", "Verizon (account)")
    g = programmatic_grade_web(_C["web_wrong_entity"], bad, [], 290, _C["web_wrong_entity"]["web_items"])
    assert not g["passed"]
    assert "wrong_entity_excluded" in [n for n, ok, _ in g["checks"] if not ok]


def test_grade_protects_contact():
    bad = _brief("- Named Jordan Vance new VP of Network Operations.", "Verizon (account)")
    g = programmatic_grade_web(_C["web_contradicts_contact"], bad, [], 290, _C["web_contradicts_contact"]["web_items"])
    assert not g["passed"]
    assert "contradicting_person_ignored" in [n for n, ok, _ in g["checks"] if not ok]


# --------------------------------------------------------------------------- #
def _all_tests():
    return [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]


def main() -> int:
    failures = 0
    for name, fn in _all_tests():
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {name}\n        {type(e).__name__}: {e}")
    total = len(_all_tests())
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
