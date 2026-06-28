# Meeting Brief Agent

A Python agent built on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python)
that drafts one-page meeting briefs. Give it an account name (or point it at a day's calendar) and
it gathers its own context from a Notion CRM — and recent company news from the web — then writes a
tight, CEO-ready brief. It is **read-only everywhere** and runs **fully headless** (API-key auth, no
interactive login). Its first rule is honesty: if a fact isn't in the sources, it says `Unknown`
rather than inventing one.

## Project phases

The agent was built in seven phases. Each adds a capability without disturbing the brief **format**
(the section list, gold example, honesty rule, and ~250–350-word one-page limit have been fixed
since Phase 1) or the **read-only / zero-writes** guarantee.

| Phase | Theme | What it added |
|-------|-------|---------------|
| **1** | Draft from pasted text | The core writer: raw source material in → a one-page brief out, following the format spec and honesty rule (`SYSTEM_PROMPT` + `PROMPT_TEMPLATE` in `prompt.py`). |
| **2** | Agentic over the Notion CRM | Give it just an account name/subject; it searches Notion itself, fetches the account page, follows the **Contacts** and **Meetings** relations, and drafts from only those pages. Read-only, never writing to Notion. |
| **3** | Eval harness + hardening | An LLM-as-judge + programmatic eval suite (`eval/`). Two post-baseline fixes: a **length validate-and-retry** loop (≤2 rewrites toward an internal ~320-word target; acceptance stays exactly 250–350) and a **provenance sidecar** (`*.sources.json`) recording the real Notion page URLs used, while the CEO-facing Sources line keeps readable page names. |
| **4** | Calendar-driven daily packet | `--calendar` reads a day's events, **skips internal-only meetings**, and drafts one brief per external meeting — **centered on the specific person** you're meeting (matched to a CRM contact by name + company, not exact email). No CRM match → a calendar-only **stub** that invents nothing. No new sections — gathered history sharpens the existing ones. |
| **5** | Web enrichment for "What's changed" | A read-only web sub-agent gathers recent **company-level** news (funding, launches, earnings, M&A, leadership moves, major incidents; ~last 6 months) and folds only relevant, **source-cited** items into the existing "What's changed" section. Never used to identify or correct the person (the CRM is the sole source of truth for contacts); a web fact appears only if a fetched source URL supports it, else it's omitted — the brief never pads. |
| **6** | API-key-only authentication | Cut model auth over to `ANTHROPIC_API_KEY` only — no Claude Code session auth, no fallback, fully headless, fail-fast on a missing key. |
| **7** | Connector decoupling (current) | Replaced the `claude.ai` MCP connectors (which the API key disables) with credentials we own: the CRM is served by an **in-process, read-only Notion MCP server** (`notion_mcp.py`, `NOTION_TOKEN` auth) and the calendar by a direct **service-account** read (`gcal.py`, `calendar.readonly`). The whole pipeline now runs headless with no `claude.ai` login anywhere. |

## Requirements

- **Python 3.10+**
- **Node.js** (the SDK runs the Claude Code CLI under the hood)
- **`ANTHROPIC_API_KEY` (required).** Authenticates the model. The agent runs fully headless —
  no session login, no fallback. If it's missing, every entry point fails fast.
- **`NOTION_TOKEN` (required).** A Notion **internal integration** token
  (https://www.notion.so/my-integrations). The CRM is read through our **own** read-only Notion
  MCP server (`brief_agent/notion_mcp.py`), authenticated by this token — **not** the claude.ai
  connector. Setting `ANTHROPIC_API_KEY` disables the claude.ai connectors, so a standalone token
  is what keeps the CRM reachable headless. Share the integration with the **Accounts**,
  **Meetings**, and **Contacts** databases (each: ••• → *Connections*), or it sees nothing.
- **Google Calendar (only for the live `--calendar` CLI).** A Google **service account** with the
  Calendar API enabled, whose email you've shared your calendar with (read-only). The evals mock
  the calendar, so this is not needed to run the eval suite.

Model auth (API key) and the CRM/calendar (their own tokens) are fully decoupled from any
`claude.ai` login — the whole pipeline runs headless. Missing credentials fail fast with a clear,
secret-free message.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in your secrets (see below)
```

Set the secrets in `.env` (or export them in your shell):

```bash
ANTHROPIC_API_KEY=sk-ant-...          # model auth (required)
NOTION_TOKEN=ntn_...                  # CRM auth (required); share the 3 DBs with the integration
GOOGLE_SERVICE_ACCOUNT_FILE=service-account.json   # live --calendar only
GOOGLE_CALENDAR_ID=you@example.com                 # live --calendar only
```

Secrets are never printed, committed, or written to any output; `.env`, `*.key`, and
`service-account.json` are gitignored. A missing **required** credential fails fast with a clear,
secret-free message before any model call.

## Usage

### Single brief (Phase 2)

```bash
python main.py "Meridian" --out brief.md
```

- `target` (positional) — account name or meeting subject to brief on.
- `--out`, `-o` — where to write the brief. Defaults to `brief.md`.
- `--model`, `-m` — model alias (`opus`, `sonnet`, `haiku`) or full ID.
  Defaults to `opus`; can also be set with the `BRIEF_AGENT_MODEL` env var.

### Daily packet (Phase 4)

```bash
python main.py --calendar --date 2026-06-29 --out day.md
```

- `--calendar` — read the calendar for a day and brief every external meeting.
- `--date` — `today` | `tomorrow` (default) | `YYYY-MM-DD`.
- `--out` — output path (defaults to `day.md` in calendar mode).
- `--no-web` — disable web enrichment (web is **on by default** in both modes). The Sources line
  keeps CRM page names and a separate `Web:` segment for any cited article URLs.

The packet leads with a header (`N meetings · X briefed / Y unresolved / Z skipped`), lists the
skipped internal meetings, then the briefs in start-time order. Internal-only meetings (no
external attendee) are skipped; external meetings with no CRM match get a minimal calendar-only
**stub** that invents nothing. Each brief is centered on the actual attendee — when a company has
several CRM contacts, the brief leads with the one in the meeting and treats the others as
background. Attendees are matched to CRM contacts by **name + company**, not by exact email.

Examples:

```bash
python main.py "Orbit Telecom"                 # single brief, default Opus -> brief.md
python main.py "Meridian" --model sonnet -o m.md
python main.py --calendar                      # tomorrow's packet -> day.md
python main.py --calendar --date today -m sonnet
```

The output is written to `--out` (plus a `*.sources.json` provenance sidecar). An audit trail
prints to stderr, ending in a confirmation that **zero** write tools were used (Notion in single
mode; Notion **and** Calendar in calendar mode).

## How it works

```
main.py → brief_agent/cli.py → brief_agent/agent.py (draft_brief)
                                      │
                                      ├─ claude_agent_sdk.query() — AGENTIC loop
                                      │     system prompt: gather-from-Notion contract
                                      │                    + format spec  (brief_agent/prompt.py)
                                      │     tools (read-only): notion-search, notion-fetch
                                      │       served by our OWN in-process Notion MCP server
                                      │       (brief_agent/notion_mcp.py, NOTION_TOKEN auth)
                                      │       1. search Accounts DS → resolve to one account
                                      │       2. fetch the account page (properties + body)
                                      │       3. follow Contacts + Meetings relations
                                      │       4. draft the brief from only those pages
                                      │
                                      └─ length validate-and-retry (≤2 rewrites to 250–350 words)
```

The CRM is read through a small **in-process, read-only Notion MCP server**
(`brief_agent/notion_mcp.py`) that re-exposes `notion-search`/`notion-fetch` on top of the Notion
REST API + a `NOTION_TOKEN` integration token — **not** the claude.ai connector (which the API key
disables). It exposes no write tool at all, so Notion is read-only by construction.

In calendar mode, `brief_agent/gcal.py` reads the day's events directly from Google Calendar via a
**service account** (scope `calendar.readonly` — writes are physically impossible), the pure
`brief_agent/calendar.py` normalises them, and `brief_agent/daily.py` batches the engine above over
each external meeting, ordering the results into one packet. A meeting-aware gather
(`SYSTEM_PROMPT_NOTION_MEETING` in `prompt.py`) centers each brief on the specific attendee — the
output format is identical to Phase 2.

Web enrichment (`brief_agent/web.py`) is a separate read-only sub-agent (`WebSearch`/`WebFetch`)
that returns structured, URL-bearing news items; the orchestrator injects them as WEB CONTEXT into
the draft, which folds only relevant, cited items into "What's changed". Keeping web behind one
Python function lets the eval suite mock it for a deterministic gate (`eval/run_eval_web.py`) while
production uses the real tools (`tests/test_web_smoke.py` covers the live path, non-gating).

### Read-only safety

- The Notion MCP server (`notion_mcp.py`) exposes **only** search + fetch — it has no
  create/update/delete tool, so a Notion write is impossible, not merely denied. The calendar read
  uses the `calendar.readonly` OAuth scope, so a calendar write is impossible at the credential
  level. Web access (`WebSearch`/`WebFetch`) is read-only by nature.
- `allowed_tools` is still whitelisted to `notion-search`/`notion-fetch` (+ `WebSearch`/`WebFetch`
  for the web sub-agent), and every known Notion/Calendar write tool name stays in
  `disallowed_tools` — defense in depth, in case a claude.ai connector were ever re-enabled.
- `permission_mode="dontAsk"` denies anything not allow-listed without prompting (non-interactive).
- `BriefResult.made_any_write` / `DayPacket.made_any_write` are asserted `False`; the CLI and all
  eval suites error out if any write tool name is ever seen.

### Notes on the CRM

The CRM is three linked Notion databases — Accounts, Meetings, Contacts (data source IDs
live in `brief_agent/prompt.py`). The agent gathers context via **search → fetch →
follow relations**; it does **not** use `query_data_sources`/SQL (that requires a Notion
Business plan + AI, which isn't assumed here).

### Honesty rule

If a fact isn't in Notion, the brief marks it `Unknown` rather than inventing it. If the
input doesn't resolve to an account, the brief says so in the metadata line instead of
fabricating one — a CEO acting on a made-up detail is the worst failure mode.

## Tests & evals

Two layers, both runnable headless:

```bash
# Deterministic, offline unit tests (no model, no network) — fast regression guard
python tests/test_resilience.py        # degradation, length-retry, write-detection
python tests/test_calendar_unit.py     # calendar parsing / internal detection / ordering
python tests/test_web_unit.py          # web item parsing + URL hygiene
python tests/test_notion_unit.py       # Notion MCP server: search/fetch/provenance marker

# Model-backed eval suites (LLM-as-judge + programmatic checks) — require the credentials above
python eval/run_eval.py --model opus            # account briefs (incl. no-match / ambiguous)
python eval/run_eval_calendar.py --model opus   # calendar packet + person-precision (mocked calendar)
python eval/run_eval_web.py --model opus        # web folding: cited / empty / wrong-entity / contact-protected (mocked web)
```

Each eval runner writes a scorecard (`eval/results/*.md` + `.json`) and **exits non-zero if any
case fails or any write tool is ever seen**. The calendar and web evals **mock** their external
inputs so the gate is deterministic; the live web path has a separate non-gating smoke test
(`tests/test_web_smoke.py`).

## The brief format

Title → metadata line → Bottom line → Who you're meeting → What's changed since you last
spoke → Likely to come up → Your goals & talking points → Watch-outs → Desired outcome.
~250–350 words, one page. Full spec in `brief_agent/prompt.py` — fixed since Phase 1.
