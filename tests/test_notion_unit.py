"""Pure unit tests for the in-process Notion MCP server (brief_agent/notion_mcp.py).

Offline + deterministic: `_request` is monkeypatched to return canned Notion API JSON, so these
exercise the search filtering, page rendering (title + properties + relations + body), the
`<parent-data-source url="collection://…">` provenance marker, id/URL parsing, and the MCP tool
wrappers' success/error envelopes — with no network and no model. Run: python tests/test_notion_unit.py
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brief_agent.notion_mcp as nm

A_DS = nm.ACCOUNTS_DS
C_DS = nm.CONTACTS_DS


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --- canned fixtures -------------------------------------------------------- #
_ACCT_PAGE = {
    "object": "page",
    "id": "11111111-1111-1111-1111-111111111111",
    "url": "https://app.notion.com/p/Acme-111",
    "parent": {"type": "data_source_id", "data_source_id": A_DS},
    "properties": {
        "Account": {"type": "title", "title": [{"plain_text": "Acme Corp"}]},
        "ARR": {"type": "number", "number": 1000000},
        "Relationship Status": {"type": "select", "select": {"name": "Healthy"}},
        "Contact Style": {"type": "rich_text", "rich_text": [{"plain_text": "Direct"}]},
        "Contacts": {"type": "relation", "relation": [{"id": "c-1"}, {"id": "c-2"}]},
        "Meetings": {"type": "relation", "relation": [{"id": "m-1"}]},
        "Empty": {"type": "rich_text", "rich_text": []},
    },
}

_SEARCH_RESULTS = {
    "results": [
        {"object": "page", "id": "acc-1", "url": "u1", "parent": {"data_source_id": A_DS},
         "properties": {"Account": {"type": "title", "title": [{"plain_text": "Acme Corp"}]}}},
        {"object": "page", "id": "con-1", "url": "u2", "parent": {"data_source_id": C_DS},
         "properties": {"Name": {"type": "title", "title": [{"plain_text": "Jane Doe"}]}}},
    ]
}


def _fake_request(method, path, body=None):
    if path == "/search":
        return _SEARCH_RESULTS
    if path.startswith("/pages/") and path.endswith("/markdown"):
        return {"markdown": "## Notes\nGrowing fast."}
    if path.startswith("/pages/"):
        return _ACCT_PAGE
    raise nm.NotionError(f"unexpected path {path}")


# --- tests ------------------------------------------------------------------ #
def test_norm_id_parses_url_and_collection():
    assert_(nm._norm_id("collection://" + A_DS) == A_DS.replace("-", ""), "collection ref → bare id")
    assert_(nm._norm_id("https://app.notion.com/p/Acme-Corp-38cbf93e8ed88175b8adf8db0620efa5")
            == "38cbf93e8ed88175b8adf8db0620efa5", "url → trailing uuid")


def test_search_filters_to_requested_data_source():
    nm._request = _fake_request
    out = nm._do_search("Acme", "collection://" + A_DS)
    assert_(out["count"] == 1, "only the Accounts-DS result should remain")
    assert_(out["results"][0]["title"] == "Acme Corp", "kept the right page")
    # Unfiltered search returns both.
    assert_(nm._do_search("x")["count"] == 2, "unfiltered search returns all results")


def test_fetch_renders_marker_title_props_relations_body():
    nm._request = _fake_request
    f = nm._do_fetch("11111111-1111-1111-1111-111111111111")
    assert_(f["title"] == "Acme Corp", "title from the title-typed property")
    assert_(f["url"] == "https://app.notion.com/p/Acme-111", "page url surfaced")
    text = f["text"]
    assert_(f'collection://{A_DS}' in text, "provenance marker carries the data-source id")
    assert_("ARR: 1000000" in text and "Relationship Status: Healthy" in text, "props rendered")
    assert_("Empty" not in text, "empty properties are dropped, not rendered blank")
    assert_("c-1" in text and "c-2" in text and "m-1" in text, "relations rendered as fetchable ids")
    assert_("Growing fast." in text, "markdown body folded in")


def test_tool_wrappers_envelope_ok_and_error():
    nm._request = _fake_request
    res = asyncio.run(nm.notion_fetch.handler({"id": "11111111-1111-1111-1111-111111111111"}))
    payload = json.loads(res["content"][0]["text"])
    assert_(payload["title"] == "Acme Corp", "wrapper returns the rendered page as JSON text")
    assert_(not res.get("is_error"), "successful fetch is not an error envelope")

    def _boom(*a, **k):
        raise nm.NotionError("HTTP 503")
    nm._request = _boom
    err = asyncio.run(nm.notion_search.handler({"query": "x"}))
    assert_(err.get("is_error") is True, "a Notion failure surfaces as is_error so the brief degrades")
    assert_("503" in err["content"][0]["text"], "error message is passed through")


def _all():
    return [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]


def main() -> int:
    fails = 0
    for name, fn in _all():
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"FAIL  {name}\n        {type(e).__name__}: {e}")
    print(f"\n{len(_all()) - fails}/{len(_all())} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
