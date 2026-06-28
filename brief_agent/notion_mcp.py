"""In-process, read-only Notion MCP server (Phase 7 — connector decoupling).

Replaces the claude.ai Notion *connector* with a standalone MCP server we own, authenticated
solely by a Notion integration token (`NOTION_TOKEN`). This is what makes the agent run fully
headless on API-key model auth: the claude.ai login is gone (and is in fact disabled the moment
`ANTHROPIC_API_KEY` is set), so the CRM must come from a server that does not depend on it.

It deliberately re-exposes the SAME two tools the gather contract was written against —
`notion-search` and `notion-fetch` — with the SAME compact result shape (a JSON object whose
`text` carries a `<parent-data-source url="collection://…">` marker), so the Phase 2/4/5 gather
prompt and the provenance parser in `agent.py` keep working almost verbatim. Only the transport
changed: claude.ai connector → Notion REST API + our token.

READ-ONLY BY CONSTRUCTION: this server exposes ONLY search + fetch. There is no create/update/
delete/move/comment tool here at all, so a Notion write is not merely denied — it is impossible.

Runs in-process via `create_sdk_mcp_server`; no node/npx subprocess. HTTP is plain `urllib`
(no new dependency), executed off the event loop with `asyncio.to_thread`.
"""

import asyncio
import json
import os
import re
import urllib.error
import urllib.request

from claude_agent_sdk import create_sdk_mcp_server, tool

# The CRM's three Notion data sources (these ids are BOTH the claude.ai `collection://` ids and,
# under Notion API version 2025-09-03, the `data_source_id`s — verified against the live workspace).
ACCOUNTS_DS = "9eef9efb-0005-4b25-a907-42cfd913b668"
MEETINGS_DS = "d0cc704c-3689-4964-bbb0-1635ec233ebf"
CONTACTS_DS = "63ce8309-6e71-4dd7-9b4a-2c26efb4865c"
_CRM_DS = {ACCOUNTS_DS, MEETINGS_DS, CONTACTS_DS}

_API = "https://api.notion.com/v1"
_VERSION = "2025-09-03"  # data-sources API: pages live under `data_source_id` parents
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")

# The MCP server name; full tool names become mcp__brief_notion__notion-search / …notion-fetch.
SERVER_NAME = "brief_notion"


class NotionError(RuntimeError):
    """A Notion REST call failed — surfaced to the model so it emits an UNRESOLVED brief."""


def _token() -> str:
    tok = os.environ.get("NOTION_TOKEN", "").strip()
    if not tok:
        raise NotionError("NOTION_TOKEN is not set — cannot reach the CRM.")
    return tok


def _request(method: str, path: str, body: dict | None = None) -> dict:
    """One Notion REST call. Raises NotionError on any HTTP/transport failure."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{_API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Notion-Version": _VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:  # noqa: BLE001
            pass
        raise NotionError(f"Notion API HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise NotionError(f"Notion API unreachable: {type(e).__name__}: {e}") from e


def _norm_id(ref: str) -> str:
    """Extract a bare uuid from an id, a page URL, or a `collection://…` data-source URL."""
    m = _UUID_RE.search(ref or "")
    return m.group(0).replace("-", "") if m else (ref or "").strip()


def _title_of(page: dict) -> str:
    """The page's title — found by property TYPE (`title`), so it works across DBs."""
    for v in (page.get("properties") or {}).values():
        if v.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in v.get("title", [])).strip()
    return page.get("title") and "".join(
        t.get("plain_text", "") for t in page["title"]
    ).strip() or "(untitled)"


def _parent_ds(obj: dict) -> str | None:
    """The data_source id a page/result belongs to (for CRM categorisation)."""
    parent = obj.get("parent") or {}
    return parent.get("data_source_id")


def _render_prop(name: str, v: dict) -> str | None:
    """Render one property to a readable `Name: value` line (relations handled separately)."""
    t = v.get("type")
    if t == "title":
        return None  # rendered as the page title, not a body line
    if t == "rich_text":
        txt = "".join(x.get("plain_text", "") for x in v.get("rich_text", [])).strip()
        return f"{name}: {txt}" if txt else None
    if t == "number":
        n = v.get("number")
        return f"{name}: {n}" if n is not None else None
    if t in ("select", "status"):
        node = v.get(t)
        return f"{name}: {node['name']}" if node else None
    if t == "multi_select":
        names = [o["name"] for o in v.get("multi_select", [])]
        return f"{name}: {', '.join(names)}" if names else None
    if t == "date":
        d = v.get("date")
        if not d:
            return None
        span = d.get("start", "")
        if d.get("end"):
            span += f" → {d['end']}"
        return f"{name}: {span}" if span else None
    if t == "people":
        names = [p.get("name", "") for p in v.get("people", [])]
        names = [n for n in names if n]
        return f"{name}: {', '.join(names)}" if names else None
    if t == "checkbox":
        return f"{name}: {'yes' if v.get('checkbox') else 'no'}"
    if t == "url":
        return f"{name}: {v.get('url')}" if v.get("url") else None
    if t == "email":
        return f"{name}: {v.get('email')}" if v.get("email") else None
    if t == "phone_number":
        return f"{name}: {v.get('phone_number')}" if v.get("phone_number") else None
    if t == "unique_id":
        uid = v.get("unique_id") or {}
        num = uid.get("number")
        if num is None:
            return None
        prefix = uid.get("prefix")
        return f"{name}: {prefix + '-' if prefix else ''}{num}"
    if t in ("formula", "rollup"):
        node = v.get(t) or {}
        inner = node.get(node.get("type"), "")
        if isinstance(inner, dict):
            inner = inner.get("name") or inner.get("start") or ""
        return f"{name}: {inner}" if inner else None
    return None


def _relations(props: dict) -> dict[str, list[str]]:
    """{property_name: [related page ids]} for every relation property with entries."""
    out: dict[str, list[str]] = {}
    for name, v in props.items():
        if v.get("type") == "relation":
            ids = [r.get("id") for r in v.get("relation", []) if r.get("id")]
            if ids:
                out[name] = ids
    return out


# --------------------------------------------------------------------------- #
# Core operations (pure-ish; directly unit-testable without the model)
# --------------------------------------------------------------------------- #
def _do_search(query: str, data_source_url: str = "") -> dict:
    """Search shared CRM content. If `data_source_url` is given, restrict to that data source."""
    payload: dict = {"query": query or "", "page_size": 25}
    res = _request("POST", "/search", payload).get("results", [])
    want = _norm_id(data_source_url) if data_source_url else ""

    def _id_eq(a: str | None, b: str) -> bool:
        return bool(a) and a.replace("-", "") == b

    items = []
    for r in res:
        obj = r.get("object")
        ds = _parent_ds(r)
        if want and not _id_eq(ds, want):
            continue
        if obj == "page":
            items.append({
                "id": r.get("id"),
                "title": _title_of(r),
                "url": r.get("url"),
                "data_source": ds,
            })
        elif obj == "data_source":
            items.append({
                "id": r.get("id"),
                "title": "".join(t.get("plain_text", "") for t in r.get("title", [])),
                "url": f"collection://{r.get('id')}",
                "object": "data_source",
            })
    return {"query": query, "count": len(items), "results": items}


def _render_page(page: dict, body_md: str) -> dict:
    """Build the compact notion-fetch payload the gather contract + provenance parser expect."""
    props = page.get("properties") or {}
    ds = _parent_ds(page)
    lines = [ln for ln in (_render_prop(n, v) for n, v in props.items()) if ln]
    rel = _relations(props)
    rel_block = ""
    if rel:
        rel_block = "\n\nRELATIONS (call notion-fetch on each id to read it):\n" + "\n".join(
            f"- {name}: " + ", ".join(ids) for name, ids in rel.items()
        )
    # The `<parent-data-source …>` marker keeps agent.py's _PARENT_COLLECTION_RE categorisation
    # working unchanged. It lives inside `text`, never in the CEO-facing brief.
    marker = f'<parent-data-source url="collection://{ds}">' if ds else ""
    text = (
        f"{marker}\n# {_title_of(page)}\n\n"
        + "\n".join(lines)
        + rel_block
        + (f"\n\n## Page body\n{body_md}" if body_md else "")
    )
    return {"title": _title_of(page), "url": page.get("url"), "text": text}


def _do_fetch(ref: str) -> dict:
    """Fetch a page (by id/URL) — properties + relations + body. Or a data source by collection URL."""
    nid = _norm_id(ref)
    if not nid:
        raise NotionError(f"notion-fetch: could not parse a Notion id from {ref!r}.")
    # A data-source reference (collection://… or a known DS id): return its identity, not a page.
    if (ref or "").startswith("collection://") or nid in {d.replace("-", "") for d in _CRM_DS}:
        ds = _request("GET", f"/data_sources/{nid}")
        title = "".join(t.get("plain_text", "") for t in ds.get("title", []))
        return {
            "title": title or "(data source)",
            "url": f"collection://{nid}",
            "text": f'<data-source url="collection://{nid}">\n{title}',
        }
    page = _request("GET", f"/pages/{nid}")
    # Body: markdown endpoint when available, else flatten block children to text.
    body_md = ""
    try:
        md = _request("GET", f"/pages/{nid}/markdown")
        body_md = md.get("markdown") or md.get("content") or ""
    except NotionError:
        try:
            blocks = _request("GET", f"/blocks/{nid}/children?page_size=100").get("results", [])
            body_md = _blocks_to_text(blocks)
        except NotionError:
            body_md = ""
    return _render_page(page, body_md)


def _blocks_to_text(blocks: list[dict]) -> str:
    """Very light block→text flattening (paragraphs, headings, list items, to-dos)."""
    out = []
    for b in blocks:
        t = b.get("type")
        node = b.get(t) or {}
        rt = node.get("rich_text")
        if isinstance(rt, list):
            line = "".join(x.get("plain_text", "") for x in rt).strip()
            if line:
                out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# MCP tool wrappers
# --------------------------------------------------------------------------- #
def _ok(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _err(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


@tool(
    "notion-search",
    "Search the Notion CRM for pages by name. Optionally restrict to one data source by passing "
    "its `data_source_url` (e.g. collection://<id>). Returns matching pages with id, title, url.",
    {"query": str, "data_source_url": str},
)
async def notion_search(args: dict) -> dict:
    try:
        out = await asyncio.to_thread(
            _do_search, args.get("query", ""), args.get("data_source_url", "") or ""
        )
        return _ok(out)
    except NotionError as e:
        return _err(str(e))


@tool(
    "notion-fetch",
    "Fetch one Notion page by id or URL (or a data source by collection:// URL). Returns its "
    "title, url, properties, relations (as ids to fetch), and body text.",
    {"id": str},
)
async def notion_fetch(args: dict) -> dict:
    ref = args.get("id") or args.get("url") or args.get("page_id") or ""
    try:
        out = await asyncio.to_thread(_do_fetch, ref)
        return _ok(out)
    except NotionError as e:
        return _err(str(e))


def build_notion_server():
    """The in-process, read-only Notion MCP server config for ClaudeAgentOptions.mcp_servers."""
    return create_sdk_mcp_server(
        name=SERVER_NAME, version="1.0.0", tools=[notion_search, notion_fetch]
    )
