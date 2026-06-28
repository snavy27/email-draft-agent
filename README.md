# Meeting-Brief Agent

A Python agent built on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python)
that auto-drafts one-page meeting-prep briefs for a CEO. Each morning it reads the day's calendar,
pulls the right **person and company** from a Notion CRM, enriches with recent public web news,
drafts a one-page brief per external meeting, renders the day's packet to **PDF**, and **emails** it.
It runs on a schedule **and** on demand, **fully headless** (Claude API — no interactive login).

Honesty-first: **read-only** access to your data, every claim traceable to a source, missing facts
marked `Unknown`, never fabricated.

## Guarantees (don't weaken these)

- **Read-only.** The agent never writes to Notion or the calendar. Email (plus an optional heartbeat
  ping) are the **only** outbound actions, and every run asserts **0 writes** before sending.
- **Honesty.** A fact ships only if a source supports it (a CRM page or a cited web URL); otherwise
  it is omitted or marked `Unknown`. The CRM is the sole source of truth for *who* you're meeting —
  web is never used to identify or "correct" a contact.
- **Format is fixed.** `brief_agent/prompt.py` holds the section order, gold example, and
  ~250–350-word one-page limit — unchanged since Phase 1. Editing it invalidates the eval baselines.

## Project phases

The agent was built in eight phases. Each adds a capability without disturbing the brief **format**
or the **read-only / zero-writes** guarantee above.

| Phase | Theme | What it added |
|-------|-------|---------------|
| **1** | Draft from pasted text | The core writer: raw source material in → a one-page brief out, following the format spec and honesty rule (`SYSTEM_PROMPT` + `PROMPT_TEMPLATE` in `prompt.py`). |
| **2** | Agentic over the Notion CRM | Give it just an account name/subject; it searches Notion itself, fetches the account page, follows the **Contacts** and **Meetings** relations, and drafts from only those pages. Read-only, never writing to Notion. |
| **3** | Eval harness + hardening | An LLM-as-judge + programmatic eval suite (`eval/`). Two post-baseline fixes: a **length validate-and-retry** loop (≤2 rewrites toward an internal ~320-word target; acceptance stays exactly 250–350) and a **provenance sidecar** (`*.sources.json`) recording the real Notion page URLs used, while the CEO-facing Sources line keeps readable page names. |
| **4** | Calendar-driven daily packet | `--calendar` reads a day's events, **skips internal-only meetings**, and drafts one brief per external meeting — **centered on the specific person** you're meeting (matched to a CRM contact by name + company, not exact email). No CRM match → a calendar-only **stub** that invents nothing. |
| **5** | Web enrichment for "What's changed" | A read-only web sub-agent gathers recent **company-level** news (funding, launches, earnings, M&A, leadership moves, major incidents; ~last 6 months) and folds only relevant, **source-cited** items into the existing "What's changed" section. Never used to identify or correct the person; a web fact appears only if a fetched source URL supports it, else it's omitted — the brief never pads. |
| **6** | API-key-only authentication | Cut model auth over to `ANTHROPIC_API_KEY` only — no Claude Code session auth, no fallback, fully headless, fail-fast on a missing key. |
| **7** | Connector decoupling | Replaced the `claude.ai` MCP connectors (which the API key disables) with credentials we own: the CRM is served by an **in-process, read-only Notion MCP server** (`notion_mcp.py`, `NOTION_TOKEN` auth) and the calendar by a direct **service-account** read (`gcal.py`, `calendar.readonly`). The whole pipeline runs headless with no `claude.ai` login anywhere. |
| **8** | Delivery + production hardening (current) | The scheduled run: render the packet to **PDF** and **email** it (verified TLS, single recipient, send-only). Plus hardening: **code-enforced web grounding** (URL + recency validated in Python before drafting), **per-meeting failure isolation + timeouts** (one bad meeting can't sink the packet), a **dead-man's-switch heartbeat**, empty days treated as a normal quiet packet (not a false alarm), and a "never fails silently" failure-email wrapper. |

## Requirements

- **Python 3.10+** and **Node.js** (the SDK runs the Claude Code CLI under the hood).
- **`ANTHROPIC_API_KEY` (required).** Authenticates the model, fully headless — no session login, no
  fallback. Missing ⇒ every entry point fails fast.
- **`NOTION_TOKEN` (required).** A Notion **internal integration** token. The CRM is read through our
  **own** read-only Notion MCP server (`brief_agent/notion_mcp.py`) — **not** the claude.ai connector
  (setting `ANTHROPIC_API_KEY` disables that connector, so a standalone token is what keeps the CRM
  reachable headless). Share the integration with the **Accounts**, **Meetings**, and **Contacts**
  databases or it sees nothing.
- **Google Calendar (for `--calendar` and the scheduled run).** A Google **service account** with the
  Calendar API enabled, whose email you've shared your calendar with (read-only). The evals mock the
  calendar, so this is not needed to run the eval suite.
- **SMTP (for the scheduled run only).** Any SMTP submission server; Gmail + an App Password is the
  tested path. This is the single outbound channel for the daily PDF.

Model auth (API key) and the CRM/calendar/email (their own tokens) are fully decoupled from any
`claude.ai` login. Every missing **required** credential fails fast with a clear, secret-free message
before any model call.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in your secrets (see below)
```

Set the secrets in `.env` (or export them in your shell):

```bash
# Model + CRM (required for every run)
ANTHROPIC_API_KEY=sk-ant-...          # model auth; headless, no fallback
NOTION_TOKEN=ntn_...                  # Notion internal-integration token

# Google Calendar (live --calendar / scheduled run only)
GOOGLE_SERVICE_ACCOUNT_FILE=service-account.json
GOOGLE_CALENDAR_ID=you@example.com

# Email delivery (scheduled run only — the one outbound action)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS=your-16-char-app-password   # Gmail App Password (NOT your login password)
BRIEF_FROM=you@gmail.com              # defaults to SMTP_USER if blank
BRIEF_RECIPIENT=you@example.com       # exactly ONE address (no comma lists)

# Optional monitoring + tuning (sensible defaults shown)
HEARTBEAT_URL=                        # healthchecks.io-style; pinged after a successful send
# WEB_RECENCY_DAYS=183   BRIEF_MEETING_TIMEOUT=300   CAL_TIMEOUT=60   NOTION_TIMEOUT=30
```

One-time provider setup:

1. **Claude API key** — from https://console.anthropic.com/.
2. **Notion CRM** — create an internal integration at notion.so/my-integrations (read-content is
   enough; leave insert/update/delete off). Open each of **Accounts**, **Meetings**, **Contacts** →
   ••• → *Connections* and add the integration.
3. **Google Calendar** — in Google Cloud Console enable the **Calendar API**, create a **service
   account**, add a **JSON key**, save it as `service-account.json` in the project root, then share
   your calendar (Settings and sharing → *Share with specific people*) with the service account's
   email, permission **"See all event details"**.
4. **Email (Gmail)** — enable **2-Step Verification**, create an **App Password**, and paste its 16
   characters as `SMTP_PASS` (spaces are fine — they're stripped for Gmail).

> All secrets live in `.env` (gitignored); `service-account.json` and `*.key` are gitignored too.
> Secrets are never printed, committed, or written to any output (PDF, markdown, or sidecar).

## Usage

### Single brief (Phase 2)

```bash
python main.py "Meridian" --out brief.md
```

- `target` (positional) — account name or meeting subject to brief on.
- `--out`, `-o` — where to write the brief. Defaults to `brief.md`.
- `--model`, `-m` — model alias (`opus`, `sonnet`, `haiku`) or full ID. Defaults to `opus`; can also
  be set with `BRIEF_AGENT_MODEL`.

### Daily packet (Phase 4) — generate locally, no email

```bash
python main.py --calendar --date 2026-06-29 --out day.md
```

- `--calendar` — read the calendar for a day and brief every external meeting.
- `--date` — `today` | `tomorrow` (default) | `YYYY-MM-DD`.
- `--no-web` — disable web enrichment (web is **on by default**).

The packet leads with a header (`N meetings · X briefed / Y unresolved / Z skipped`), lists skipped
internal meetings, then the briefs in start-time order. Each brief is centered on the actual attendee
— when a company has several CRM contacts, the brief leads with the one in the meeting and treats the
others as background. Output goes to `--out` plus a `*.sources.json` provenance sidecar; an audit
trail prints to stderr ending in a confirmation that **zero** write tools were used.

### Scheduled daily run — render to PDF and email it

`scheduled_run.py` is the single entry point a scheduler invokes — and the same command you run by
hand to test. It builds the day's packet, renders a **PDF**, and emails it to one recipient.

```bash
python scheduled_run.py                     # today (Europe/Paris), email the PDF  (what cron runs)
python scheduled_run.py --date 2026-06-29   # a specific day (today | tomorrow | YYYY-MM-DD)
python scheduled_run.py --no-email          # build + save under out/, do NOT send
python scheduled_run.py --to me@example.com # one-off recipient override
```

Make targets wrap the same command:

```bash
make brief                        # today, email the PDF
make brief-local                  # save locally, skip send
make brief-date DATE=2026-06-29   # a specific day, email it
```

On every run the PDF (plus the packet `.md` and `.sources.json` sidecar) is written to
`out/day-<date>.*`, then emailed unless `--no-email` is given. Deterministic filenames mean re-running
a day overwrites them, so the job is **idempotent and safe to re-run**. Default model is **Opus**
(production bar); `--model sonnet` is cheaper for iteration but runs near the length limit.

## Schedule it

The job is a plain CLI command; point any scheduler at it.

**Recommended — GitHub Actions** (no always-on machine needed). The committed workflow
`.github/workflows/daily-brief.yml` runs daily and can be triggered manually (`workflow_dispatch`).
Add these repo **Secrets**: `ANTHROPIC_API_KEY`, `NOTION_TOKEN`, `GOOGLE_CALENDAR_ID`,
`GOOGLE_SERVICE_ACCOUNT_JSON` (the whole key file's contents), `SMTP_USER`, `SMTP_PASS`,
`BRIEF_RECIPIENT`. The workflow writes the service-account JSON from its secret, runs the job, then
deletes it.

GitHub cron is **UTC**: the workflow uses `30 4 * * *` ≈ **06:30 Europe/Paris** in summer (CEST); in
winter (CET) the same line fires at 05:30 local. The job always computes "today" in Europe/Paris, so
the *date* is correct year-round — only the send time shifts by an hour across DST. Edit the cron line
for an exact local time.

**Equivalents:**
- **cron** (always-on host): `30 4 * * * cd /path/to/repo && /usr/bin/python scheduled_run.py`
- **Windows Task Scheduler**: a Daily trigger running `python C:\path\to\scheduled_run.py` with
  "Start in" set to the repo folder.

> A self-hosted scheduler needs an always-on host — a laptop asleep at 6:30am silently misses the
> run. GitHub Actions avoids that, and the **heartbeat** (below) catches it if a run is ever missed.

## Reliability & failure handling

The whole run is wrapped so it **never fails silently**, and failures are scoped so one bad meeting
doesn't lose the day:

- **Per-meeting isolation + timeouts.** Each meeting is briefed independently under a timeout
  (`BRIEF_MEETING_TIMEOUT`, 300s, covering its Notion + web + model work; inner limits `CAL_TIMEOUT`
  60s, `NOTION_TIMEOUT` 30s). A meeting that errors or hangs becomes a `Could not complete: … —
  <reason>` line in the packet; the other briefs still ship and the run still succeeds (exit 0).
- **Whole-run failure → failure email.** Only an unrecoverable failure (calendar unreachable, render
  or send error, a detected write) emails a **FAILURE notice** with a short traceback and exits
  non-zero. With `--no-email`, the same error prints to the console and exits non-zero (no email).
- **Empty day is normal.** A meeting-free day ships a quiet "no external meetings" packet (exit 0),
  not a scary alert.
- **Dead-man's switch.** On a fully successful run the job pings `HEARTBEAT_URL` **after** the email
  sends. Point a healthchecks.io-style monitor at it, set to expect that daily ping shortly after the
  send time: a no-show (host down, cron broken, laptop asleep) alerts you even though the job never
  ran. The ping is best-effort — its own failure never affects the run.

If you get a failure email:

| Symptom | Likely cause | Fix |
|---|---|---|
| "auth" / 401 | API key missing/expired | check `ANTHROPIC_API_KEY` |
| Notion empty / "not found" | DB not shared with the integration | re-share Accounts/Meetings/Contacts |
| "calendar … timed out" / unreachable | wrong calendar ID, share not propagated, or a hang | verify `GOOGLE_CALENDAR_ID`; wait 5–10 min after sharing |
| 429 / rate limit | transient | usually self-resolves on retry; re-run if needed |
| `535 BadCredentials` on send | wrong/again-not-an App Password, or 2-Step Verification off | regenerate the Gmail App Password |
| No email arrived | SMTP creds or spam | check `SMTP_*`; check spam |
| A `Could not complete` line in the packet | that one meeting errored/timed out | the rest still shipped; check the reason, re-run if needed |

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

scheduled_run.py → brief_agent/daily.py (build_day_packet)
        gcal.py (read events) → calendar.py (parse) → per-meeting draft_brief (isolated + timeout)
        → render_packet → brief_agent/pdf.py (PDF) → brief_agent/mailer.py (email) → heartbeat ping
```

The CRM is read through a small **in-process, read-only Notion MCP server** (`notion_mcp.py`) that
re-exposes `notion-search`/`notion-fetch` on top of the Notion REST API + a `NOTION_TOKEN` token —
**not** the claude.ai connector (which the API key disables). It exposes no write tool at all, so
Notion is read-only by construction.

In calendar mode, `gcal.py` reads the day's events directly via a **service account** (scope
`calendar.readonly` — writes are physically impossible), `calendar.py` normalises them, and
`daily.py` batches the engine over each external meeting (each isolated + timeout-bounded), ordering
the results into one packet. `pdf.py` renders the packet to PDF and `mailer.py` emails it.

Web enrichment (`web.py`) is a separate read-only sub-agent (`WebSearch`/`WebFetch`) that returns
structured news items. Before any reach the draft, `validate_web_items` enforces grounding **in
Python**: every item must carry a real http(s) source URL **and** a parseable date within the recency
window (`WEB_RECENCY_DAYS`, default 183) — anything else is dropped. The drafter has no web tools, so
it can only fold validated, cited items into "What's changed"; it cannot fetch or invent.

### Read-only safety

- The Notion MCP server exposes **only** search + fetch — no create/update/delete tool exists, so a
  Notion write is impossible, not merely denied. The calendar read uses the `calendar.readonly`
  OAuth scope, so a calendar write is impossible at the credential level. Web access is read-only by
  nature.
- `allowed_tools` is whitelisted to `notion-search`/`notion-fetch` (+ `WebSearch`/`WebFetch` for the
  web sub-agent); every known Notion/Calendar write tool name stays in `disallowed_tools` (defense in
  depth) and `permission_mode="dontAsk"` denies anything else without prompting.
- `BriefResult.made_any_write` / `DayPacket.made_any_write` are asserted `False`; the CLI, the
  scheduled run, and all eval suites error out if any write tool name is ever seen.

### Notes on the CRM

Three linked Notion databases — Accounts, Meetings, Contacts (data source IDs live in `prompt.py`).
The agent gathers context via **search → fetch → follow relations**; it does **not** use
`query_data_sources`/SQL (that requires a Notion Business plan + AI, not assumed here). Attendees are
matched to CRM contacts by **name + company**, not by exact email.

### Honesty rule

If a fact isn't in the sources, the brief marks it `Unknown` rather than inventing it. If the input
doesn't resolve to an account, the brief says so in the metadata line instead of fabricating one — a
CEO acting on a made-up detail is the worst failure mode.

## Tests & evals

```bash
# Offline unit tests (no model, no network, no creds) — fast regression guard
python -m pytest -q tests/
#   test_resilience      degradation, length-retry, write-detection
#   test_calendar_unit   parsing / internal detection / ordering / partial-failure + timeout isolation
#   test_web_unit        web parsing + URL hygiene + recency grounding
#   test_notion_unit     Notion MCP server: search / fetch / provenance marker
#   test_pdf_unit        packet → valid PDF, Unicode-safe
#   test_mailer_unit     one recipient, verified-TLS context, password never serialized
#   test_heartbeat_unit  best-effort ping (success + swallowed failure)

# Model-backed eval suites (LLM-as-judge + programmatic checks) — require ANTHROPIC_API_KEY + NOTION_TOKEN
python -m eval.run_eval            # account briefs (incl. no-match / ambiguous)
python -m eval.run_eval_calendar   # daily packet + person-precision + partial-failure (mocked calendar)
python -m eval.run_eval_web        # web folding: cited / empty / wrong-entity / contact-protected (mocked web)

# Live smoke tests (real network, non-gating)
RUN_LIVE_NOTION=1 python tests/test_notion_injection_live.py
```

Each eval runner writes a scorecard (`eval/results/*.md` + `.json`) and **exits non-zero if any case
fails or any write tool is ever seen**. The calendar and web evals **mock** their external inputs
(the web eval validates against a fixed reference date) so the gate stays deterministic; the live web
path has a separate non-gating smoke test (`tests/test_web_smoke.py`). Opus is the regression bar.

## Project layout

```
brief_agent/
  prompt.py         the brief format/spec (source of truth — fixed since Phase 1)
  agent.py          the per-meeting draft loop (gather -> draft -> length retry)
  daily.py          calendar batching -> daily packet (per-meeting isolation + timeouts)
  calendar.py       pure event parser
  gcal.py           read-only Google Calendar fetch (service account, timeout-bounded)
  notion_mcp.py     in-process read-only Notion MCP server (CRM)
  web.py            web enrichment + Python grounding gate (URL + recency)
  pdf.py            packet markdown -> PDF (fpdf2)
  mailer.py         send-only email (one recipient, verified TLS)
  heartbeat.py      best-effort dead-man's-switch ping
  config.py         headless credential loading + fail-fast
  cli.py            single brief / daily packet (writes .md, no email)
scheduled_run.py    the scheduled & on-demand entry point: packet -> PDF -> email -> heartbeat
.github/workflows/daily-brief.yml   scheduled GitHub Actions job
Makefile            make brief / brief-local / brief-date shortcuts
eval/               eval runners + cases + results/ baselines
tests/              unit + resilience + live smoke tests
out/                generated packets / PDFs (gitignored)
.env / .env.example credentials (real / placeholders)
service-account.json  Google service-account key (gitignored)
```

## The brief format

Title → metadata line → Bottom line → Who you're meeting → What's changed since you last spoke →
Likely to come up → Your goals & talking points → Watch-outs → Desired outcome. ~250–350 words, one
page. Full spec in `brief_agent/prompt.py` — fixed since Phase 1.

## Known limits

- **Sonnet** occasionally runs ~1 word over the length cap and had one grounding slip on the harder
  calendar task; **Opus is clean** — use it for production.
- Web enrichment is **company-level** news only, ~last 6 months.
- Contacts are your private CRM records; they intentionally won't match a real company's public execs,
  and the agent won't reconcile them against the web.
- The PDF uses core latin-1 fonts, so a name with non-latin-1 characters (e.g. CJK/Cyrillic) renders
  as `?`. Embedding a Unicode TTF is a known follow-up.
