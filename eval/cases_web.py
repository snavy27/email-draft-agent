"""Phase 5 web eval cases — MOCKED web payloads (no live network).

Account-mode cases on Verizon. The account is a REAL company (so its web news is realistic), but
its CRM contact — Greg Sullivan, VP Operations — is FICTIONAL and is NOT Verizon's real exec. That
makes the "never reconcile the contact against the web" rule a genuine test: even when the web
surfaces a real-looking Verizon ops leader, the brief must keep Greg Sullivan as the person.

URLs use an obvious mock domain — they are never fetched; the grader only checks that any cited
URL is one we offered.

Covers the four required behaviors:
  (a) relevant news        -> folded into "What's changed", cited
  (b) nothing found        -> brief unchanged, no web URL, no padding
  (c) different same-name  -> excluded (no wrong-company contamination)
  (d) contradicts contact  -> ignored (CRM remains the source of truth for people)
"""

_RELEVANT = {
    "headline": "Verizon agrees to acquire Frontier Communications' fiber business for $9.6B",
    "url": "https://news.example.com/verizon-frontier-acquisition",
    "date": "2026-05-12",
    "category": "M&A",
    "summary": "Verizon announced a deal to acquire Frontier's fiber assets for $9.6B to expand "
    "its broadband footprint.",
}

_WRONG_ENTITY = {
    "headline": "Verizon Cafe, an artisan coffee chain, opens its 50th location",
    "url": "https://news.example.com/verizon-cafe-50th",
    "date": "2026-04-20",
    "category": "launch",
    "summary": "The artisan coffee chain Verizon Cafe opened its 50th store. (A small food "
    "business unrelated to the telecom company.)",
}

_CONTRADICTS_CONTACT = {
    "headline": "Verizon replaces VP of Operations Greg Sullivan with Jordan Vance",
    "url": "https://news.example.com/verizon-vp-operations-change",
    "date": "2026-05-30",
    "category": "leadership",
    "summary": "Verizon named Jordan Vance as VP of Operations, succeeding Greg Sullivan, who "
    "is leaving the company. (Directly contradicts the CRM contact — must be ignored.)",
}

# Cross-contamination markers must be DISTINCTIVE — other accounts' fictional contact names,
# never a real-company name like "Target" that collides with the common word ("uptime target").
_BASE_MUST_NOT = ["Sarah Chen", "Priya Nair"]

WEB_CASES = [
    {
        "id": "web_relevant",
        "target": "Verizon",
        "company": "Verizon",
        "expect": "appears",
        "web_items": [_RELEVANT],
        "appears_token": "Frontier",
        "cited_url": _RELEVANT["url"],
        "must_appear": ["Greg Sullivan"],
        "must_not_appear": _BASE_MUST_NOT,
    },
    {
        "id": "web_empty",
        "target": "Verizon",
        "company": "Verizon",
        "expect": "empty",
        "web_items": [],
        "must_appear": ["Greg Sullivan", "outage"],
        "must_not_appear": _BASE_MUST_NOT,
    },
    {
        "id": "web_wrong_entity",
        "target": "Verizon",
        "company": "Verizon",
        "expect": "excluded",
        "web_items": [_WRONG_ENTITY],
        "absent_tokens": ["coffee", "cafe", "50th location"],
        "must_appear": ["Greg Sullivan"],
        "must_not_appear": _BASE_MUST_NOT,
    },
    {
        "id": "web_contradicts_contact",
        "target": "Verizon",
        "company": "Verizon",
        "expect": "contact_protected",
        "web_items": [_CONTRADICTS_CONTACT],
        "absent_tokens": ["Jordan Vance"],
        "must_appear": ["Greg Sullivan"],
        "must_not_appear": _BASE_MUST_NOT,
    },
]
