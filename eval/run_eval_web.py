"""Phase 5 web eval runner — deterministic (web payloads are MOCKED; Notion is real).

For each case we inject a fixed list of web items as the draft's WEB CONTEXT (no network) and
grade how the brief folds them: relevant news cited in "What's changed", nothing-found leaves the
brief unchanged, a different same-named entity is excluded, and a web claim contradicting the CRM
contact is ignored. Asserts ZERO writes and NO invented URLs; exits nonzero on any failure.

The nondeterministic real-web fetch (`web.gather_web_news`) is intentionally NOT exercised here —
it is covered only by the non-gating live smoke test (tests/test_web_smoke.py).

Usage:  python eval/run_eval_web.py [--model opus] [--judge-model opus]
"""

import argparse
import asyncio
import contextvars
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_agent_sdk
from claude_agent_sdk import AssistantMessage, ToolUseBlock, UserMessage

import brief_agent.agent as agent
from brief_agent.config import MissingAPIKeyError, MissingNotionTokenError, ensure_credentials
from brief_agent.web import _to_items, format_web_context
from eval.cases_web import WEB_CASES
from eval.graders import judge_case, programmatic_grade_web

RESULTS_DIR = Path(__file__).resolve().parent / "results"

_CAP: contextvars.ContextVar[dict | None] = contextvars.ContextVar("web_cap", default=None)


def _result_text(m: UserMessage) -> str:
    out: list[str] = []
    c = m.content
    if isinstance(c, str):
        out.append(c)
    elif isinstance(c, list):
        for b in c:
            inner = getattr(b, "content", None)
            if isinstance(inner, str):
                out.append(inner)
            elif isinstance(inner, list):
                for x in inner:
                    if isinstance(x, dict):
                        out.append(x.get("text") or json.dumps(x))
            elif getattr(b, "text", None):
                out.append(b.text)
    return "\n".join(o for o in out if o)


def _tee(*args, **kwargs):
    agen = claude_agent_sdk.query(*args, **kwargs)
    cap = _CAP.get()

    async def wrapper():
        async for m in agen:
            if cap is not None:
                if isinstance(m, AssistantMessage):
                    for b in m.content:
                        if isinstance(b, ToolUseBlock):
                            cap["tool_calls"].append(b.name)
                elif isinstance(m, UserMessage):
                    txt = _result_text(m)
                    if txt:
                        cap["context"].append(txt)
            yield m

    return wrapper()


async def run_case(case, model, judge_model, sem) -> dict:
    row = {"id": case["id"], "expect": case["expect"]}
    items = _to_items(case["web_items"])
    web_ctx = format_web_context(items) if items else None
    web_urls = [it.url for it in items] if items else None
    cap = {"tool_calls": [], "context": []}
    _CAP.set(cap)
    async with sem:
        print(f"  briefing {case['id']} (expect {case['expect']}) ...", file=sys.stderr)
        try:
            result = await agent.draft_brief(
                case["target"], model=model, web_context=web_ctx, web_urls=web_urls
            )
        except Exception as e:  # noqa: BLE001
            row.update({"error": f"{type(e).__name__}: {e}", "overall_pass": False})
            return row

        brief = result.text
        prog = programmatic_grade_web(case, brief, result.tool_calls, result.body_words, case["web_items"])

        # Judge: fold the offered web items into the ground truth so cited web facts are
        # traceable (and clearly mark them as company-level, ignore-if-unrelated).
        context = "\n\n".join(cap["context"])
        if items:
            context += (
                "\n\n=== WEB SOURCES (additional ground truth — company-level only; the brief "
                "should ignore unrelated entities and any claim about a person) ===\n"
                + format_web_context(items)
            )
        judge_input = {"kind": "account", "input": case["company"], "note": "web-enriched brief"}
        judge = await judge_case(judge_input, brief, context, judge_model)

        row.update({
            "brief": brief, "body_words": result.body_words, "retried": result.retried,
            "tool_calls": sorted(set(result.tool_calls)), "wrote": result.made_any_write,
            "web_offered": [it.url for it in items], "web_cited": result.web_cited,
            "sources": result.sources, "programmatic": prog, "judge": judge,
            "overall_pass": prog["passed"] and judge["passed"],
        })
        return row


async def run_suite(model: str, judge_model: str) -> dict:
    sem = asyncio.Semaphore(3)
    with patch.object(agent, "query", _tee):
        rows = await asyncio.gather(*[run_case(c, model, judge_model, sem) for c in WEB_CASES])
    rows = list(rows)
    passed = sum(1 for r in rows if r.get("overall_pass"))
    any_wrote = any(r.get("wrote") for r in rows)
    return {
        "model": model, "judge_model": judge_model,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {"total": len(rows), "passed": passed, "failed": len(rows) - passed},
        "zero_writes": not any_wrote,
        "cases": rows,
    }


def _failed_checks(row: dict) -> str:
    bad = [n for n, ok, _ in row.get("programmatic", {}).get("checks", []) if not ok]
    return ",".join(bad) if bad else "ok"


def scorecard_md(report: dict) -> str:
    s = report["summary"]
    lines = [
        f"# Web eval scorecard — model `{report['model']}` (judge `{report['judge_model']}`)",
        f"_{report['timestamp']}_  ·  **{s['passed']}/{s['total']} passed**  ·  "
        f"writes: {'0 ✅' if report['zero_writes'] else '⚠ writes seen'}",
        "",
        "| case | expect | checks | ground | correct | tone | cited | overall |",
        "|------|--------|--------|--------|---------|------|-------|---------|",
    ]
    for r in report["cases"]:
        if "error" in r:
            lines.append(f"| {r['id']} | {r.get('expect','')} | ERROR | – | – | – | – | ❌ |")
            continue
        j = r["judge"]
        prog = "ok" if r["overall_pass"] else _failed_checks(r)
        cited = len(r.get("web_cited", []))
        lines.append(
            f"| {r['id']} | {r['expect']} | {prog} | {j['grounding']} | {j['correctness']} | "
            f"{j['tone']} | {cited} | {'✅' if r['overall_pass'] else '❌'} |"
        )
    fails = [r for r in report["cases"] if not r.get("overall_pass")]
    if fails:
        lines += ["", "## Failures"]
        for r in fails:
            if "error" in r:
                lines.append(f"- **{r['id']}**: {r['error']}")
                continue
            bits = []
            if not r["programmatic"]["passed"]:
                bits.append("programmatic: " + _failed_checks(r))
            j = r["judge"]
            for dim in ("grounding", "correctness", "tone"):
                if j[dim] < 4:
                    bits.append(f"{dim}={j[dim]} ({j[f'{dim}_reason'][:120]})")
            lines.append(f"- **{r['id']}**: " + "; ".join(bits))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run_eval_web")
    ap.add_argument("--model", default="opus", help="agent model (Phase 5 default: opus)")
    ap.add_argument("--judge-model", default="opus")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    # Headless auth: require ANTHROPIC_API_KEY (model) + NOTION_TOKEN (CRM) before any model call.
    try:
        ensure_credentials()
    except (MissingAPIKeyError, MissingNotionTokenError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Running WEB eval on model '{args.model}' (judge '{args.judge_model}')…", file=sys.stderr)
    report = asyncio.run(run_suite(args.model, args.judge_model))

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = report["timestamp"].replace(":", "").replace("-", "")
    base = RESULTS_DIR / f"web-{args.model}-{stamp}"
    base.with_suffix(".json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md = scorecard_md(report)
    base.with_suffix(".md").write_text(md, encoding="utf-8")

    print("\n" + md)
    print(f"\nSaved: {base.with_suffix('.json').name}, {base.with_suffix('.md').name}", file=sys.stderr)
    return 0 if report["summary"]["failed"] == 0 and report["zero_writes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
