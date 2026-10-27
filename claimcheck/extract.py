"""Fact extraction with source verification.

Two extractors produce the same typed facts:
  pattern  regular expressions over the known record layouts (free, instant, brittle on unusual phrasing)
  llm      a local or hosted LLM reads each page and returns JSON facts with verbatim quotes

Neither is trusted directly. `verify()` checks that every quote actually appears on the cited page (exact, or fuzzy at
>= FUZZY_QUOTE_THRESHOLD to tolerate OCR noise) AND that the dates and codes in the structured value appear in that
quote. Facts that fail are kept but marked unverified; the rules engine treats them as "needs review", never as proof.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from rapidfuzz import fuzz
from sqlalchemy import delete, select

from claimcheck import config, llm
from claimcheck.db import Claimant, Document, Fact, audit, session_scope

FACT_SCHEMAS = {
    "exposure": "kind (residence|employment), location, start (YYYY-MM-DD), end (YYYY-MM-DD)",
    "diagnosis": "condition, icd10, date (YYYY-MM-DD, date diagnosed or reported)",
    "visit": "date (YYYY-MM-DD), provider, facility, reason",
    "charge": "date (YYYY-MM-DD), cpt, description, amount (number)",
    "date_of_birth": "dob (YYYY-MM-DD)",
    "claimed_condition": "condition, year (self-reported by the claimant)",
}
DATE_FIELDS = {"exposure": ["start", "end"], "diagnosis": ["date"], "visit": ["date"], "charge": ["date"],
               "date_of_birth": ["dob"]}
QUOTE_SEP = " … "

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RE = re.compile(rf"(\d{{1,2}}/\d{{1,2}}/\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}}|(?:{MONTHS})\s+\d{{1,2}},\s*\d{{4}}|"
                     rf"\d{{1,2}}\s+(?:{MONTHS})\s+\d{{4}})")
ICD_RE = re.compile(r"\b([A-TV-Z]\d{2}(?:\.\d{1,2})?)\b")


def parse_date(s) -> date | None:
    if isinstance(s, date):
        return s
    s = re.sub(r"\s+", " ", str(s or "")).strip().rstrip(".")
    for f in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%B %d,%Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def norm(s: str) -> str:
    s = (s or "").replace("’", "'").replace("–", "-").replace("—", "-").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


# --------------------------------------------------------------------------------------------- verification
def verify(fact_type: str, value: dict, quote: str, page_text: str) -> tuple[bool, float]:
    """(verified, match score 0-100). Every quote part must be on the page, and the value's dates/codes in the quote."""
    parts = [p for p in (quote or "").split(QUOTE_SEP) if p.strip()]
    page = norm(page_text)
    if not parts or not page:
        return False, 0.0
    scores = []
    for p in parts:
        q = norm(p)
        scores.append(100.0 if q in page else fuzz.partial_ratio(q, page))
    score = min(scores)
    if score < config.FUZZY_QUOTE_THRESHOLD:
        return False, score
    quoted_dates = {parse_date(m) for m in DATE_RE.findall(" ".join(parts))}
    for f in DATE_FIELDS.get(fact_type, []):
        d = parse_date(value.get(f))
        if d is None or d not in quoted_dates:
            return False, score
    if fact_type == "diagnosis":
        code = str(value.get("icd10") or "").upper()
        if not ICD_RE.fullmatch(code):          # e.g. OCR read "I10" as "110": faithful to the page, still not valid
            return False, score
        if code.lower() not in norm(" ".join(parts)):
            return False, score
    if fact_type == "charge" and value.get("amount") is not None:
        if f"{float(value['amount']):,.2f}" not in " ".join(parts):
            return False, score
    return True, score


def locate_quote(fact_type: str, value: dict, page_text: str) -> str:
    """Find the page's own sentences that contain the fact's dates (and ICD code). Used when a model returns correct-looking
    values without a usable quote. The quote is then the record's text, not the model's, and verify() still requires every
    date and code to be in it, so a value that isn't on the page stays unverified."""
    sentences = [x.strip() for x in re.split(r"(?<=[.;])\s+|\n+", page_text) if x.strip()]
    needles = [parse_date(value.get(f)) for f in DATE_FIELDS.get(fact_type, [])]
    picked: list[str] = []
    for d in needles:
        hit = next((x for x in sentences if d and d in {parse_date(m) for m in DATE_RE.findall(x)}), None)
        if hit is None:
            return ""
        if hit not in picked:
            picked.append(hit)
    if fact_type == "diagnosis":
        code = str(value.get("icd10") or "").upper()
        hit = next((x for x in sentences if code and code in x.upper()), None)
        if hit is None:
            return ""
        if hit not in picked:
            picked.append(hit)
    return QUOTE_SEP.join(picked)


# --------------------------------------------------------------------------------------------- pattern extractor
def _sentence_with(text: str, start: int, end: int) -> str:
    a = max(text.rfind(". ", 0, start) + 2, 0) if text.rfind(". ", 0, start) >= 0 else 0
    b = text.find(". ", end)
    return text[a: (b + 1 if b >= 0 else len(text))].strip()


def pattern_extract(doc_type: str, page_text: str) -> list[dict]:
    t = re.sub(r"\s+", " ", page_text)
    out: list[dict] = []
    if doc_type in ("utility_record", "employment_record"):
        kind = "residence" if doc_type == "utility_record" else "employment"
        loc = "Cedar Creek Township" if "cedar creek" in t.lower() else None
        m = re.search(rf"(?:Service start date|Date of hire):\s*{DATE_RE.pattern}.{{0,40}}?"
                      rf"(?:Service end date|Date of separation):\s*{DATE_RE.pattern}", t)
        if not m:   # narrative: two dates joined by a range word in one sentence
            m = re.search(rf"(?:from|beginning)\s+{DATE_RE.pattern}\s+(?:through|to|until|and ending)\s+{DATE_RE.pattern}", t)
        if m:
            out.append({"fact_type": "exposure", "kind": kind, "location": loc, "start": str(parse_date(m.group(1))),
                        "end": str(parse_date(m.group(2))), "quote": _sentence_with(t, m.start(), m.end())})
    elif doc_type in ("pathology_report", "specialist_note"):
        dm = re.search(rf"Date of (?:report|visit):\s*{DATE_RE.pattern}", t)
        cm = re.search(r"FINAL DIAGNOSIS:\s*(.+?)\.\s", t) or re.search(r"Assessment:\s*(.+?)\s*\(ICD-10", t)
        im = re.search(r"ICD-10:?\s*([A-Z]\d{2}(?:\.\d{1,2})?)", t)
        if dm and cm and im:
            a, b = min(dm.start(), cm.start(), im.start()), max(dm.end(), cm.end(), im.end())
            out.append({"fact_type": "diagnosis", "condition": cm.group(1).strip(), "icd10": im.group(1),
                        "date": str(parse_date(dm.group(1))), "quote": t[a:b]})
    elif doc_type == "visit_note":
        m = re.search(rf"Visit date:\s*{DATE_RE.pattern}\s*Provider:\s*(Dr\. [A-Za-z'\-]+)\s*Facility:\s*(.+?)\s*"
                      rf"Chief complaint:\s*(.+?)\.", t)
        if m:
            out.append({"fact_type": "visit", "date": str(parse_date(m.group(1))), "provider": m.group(2),
                        "facility": m.group(3), "reason": m.group(4), "quote": m.group(0)})
        else:
            m = re.search(rf"seen in clinic on\s+{DATE_RE.pattern}\s+by\s+(Dr\. [A-Za-z'\-]+)\s+at\s+(.+?)\s+for\s+(.+?)\.", t)
            if m:
                out.append({"fact_type": "visit", "date": str(parse_date(m.group(1))), "provider": m.group(2),
                            "facility": m.group(3), "reason": m.group(4), "quote": m.group(0)})
    elif doc_type == "bill":
        for m in re.finditer(r"(\d{2}/\d{2}/\d{4})\s+(\d{5})\s+(.+?)\s+\$([\d,]+\.\d{2})", t):
            out.append({"fact_type": "charge", "date": str(parse_date(m.group(1))), "cpt": m.group(2),
                        "description": m.group(3), "amount": float(m.group(4).replace(",", "")), "quote": m.group(0)})
    elif doc_type == "intake":
        m = re.search(rf"Date of birth:\s*{DATE_RE.pattern}", t)
        if m:
            out.append({"fact_type": "date_of_birth", "dob": str(parse_date(m.group(1))), "quote": m.group(0)})
        m = re.search(r"Condition claimed:\s*(.+?)\s+Approximate year of diagnosis:\s*(\d{4})", t)
        if m:
            out.append({"fact_type": "claimed_condition", "condition": m.group(1), "year": int(m.group(2)),
                        "quote": m.group(0)})
    return out


# --------------------------------------------------------------------------------------------- llm extractor
LLM_PROMPT = """You extract facts from one page of a claimant's legal/medical file for a mass tort case.
Document type: {doc_type}

Return JSON: {{"facts": [ ... ]}}. Each fact is a flat object with "fact_type", its fields, and "quote", e.g.
{{"fact_type": "visit", "date": "2019-03-14", "provider": "Dr. Lee", "facility": "Mercy Clinic", "reason": "back pain",
  "quote": "Visit date: 03/14/2019 Provider: Dr. Lee Facility: Mercy Clinic Chief complaint: back pain"}}
Allowed fact types and fields:
{schemas}

Rules:
- Only extract what the page states. Do not infer or guess. If nothing applies, return {{"facts": []}}.
- "quote" must be copied VERBATIM from the page and must contain every date, code and amount you report.
  If the supporting words are in separate places, give a list of verbatim quotes.
- Dates as YYYY-MM-DD. Amounts as numbers without $.
- An intake questionnaire is self-reported: use "claimed_condition" there, never "diagnosis".
- Exposure = a period living at an address (utility/residence records) or working at a site (employment records).
- Medical shorthand: a diagnosis may be labelled "Impression", "Assessment", "Final diagnosis" or "Dx"; "Dx code" is the
  ICD-10 code; for pathology, the "signed out" or report date is the diagnosis date. "DOS" = date of service (a visit),
  "CC" = chief complaint (visit reason), "A/P" = assessment and plan.

PAGE TEXT:
\"\"\"{text}\"\"\""""


def llm_extract(doc_type: str, page_text: str, claimant_id: int | None = None) -> list[dict]:
    schemas = "\n".join(f"- {k}: {v}" for k, v in FACT_SCHEMAS.items())
    msg = llm.chat([{"role": "user", "content": LLM_PROMPT.format(doc_type=doc_type, schemas=schemas,
                                                                  text=page_text[:6000])}],
                   purpose="extract", claimant_id=claimant_id, json_mode=True, max_tokens=700)
    facts = llm.parse_json(msg.content).get("facts", [])
    out = []
    for f in facts if isinstance(facts, list) else []:
        if not isinstance(f, dict) or f.get("fact_type") not in FACT_SCHEMAS:
            continue
        for nest in ("fields", "value", "values"):   # small models sometimes nest the fields
            if isinstance(f.get(nest), dict):
                f.update(f.pop(nest))
        q = f.get("quote", "")
        f["quote"] = QUOTE_SEP.join(map(str, q)) if isinstance(q, list) else str(q)
        if f["fact_type"] == "diagnosis" and doc_type == "intake":
            f["fact_type"] = "claimed_condition"   # guard: self-report is never a diagnosis
        out.append(f)
    return out


# --------------------------------------------------------------------------------------------- orchestration
def _key(f: dict) -> tuple:
    v = f["value"]
    return (f["fact_type"], v.get("start") or v.get("date") or v.get("dob"), v.get("icd10"), v.get("amount"), f["document_id"])


def extract_claimant(claimant_id: int, method: str = "auto", actor: str = "system") -> dict:
    """(Re-)extract facts for one claimant. Reviewed facts are kept; pending ones are replaced.
    method: pattern | llm | hybrid (pattern first, LLM for pages it could not read) | auto (hybrid if an LLM is set)."""
    method = ("hybrid" if llm.llm_enabled() else "pattern") if method == "auto" else method
    with session_scope() as s:
        docs = s.scalars(select(Document).where(Document.claimant_id == claimant_id)).all()
        pages = [(d.id, d.doc_type, p.page_no, p.text) for d in docs for p in d.pages]
        s.execute(delete(Fact).where(Fact.claimant_id == claimant_id, Fact.review_status == "pending"))
        kept = {_key({"fact_type": f.fact_type, "value": f.value, "document_id": f.document_id})
                for f in s.scalars(select(Fact).where(Fact.claimant_id == claimant_id))}

    new, n_verified, errors, n_located = [], 0, 0, 0
    for doc_id, doc_type, page_no, text in pages:
        try:
            if method == "llm":
                raw = llm_extract(doc_type, text, claimant_id)
            else:
                raw = pattern_extract(doc_type, text)
                # hybrid: pay for the LLM only on pages the free extractor could not read
                if method == "hybrid" and doc_type != "unknown" and not any(
                        verify(f["fact_type"], {k: v for k, v in f.items() if k not in ("fact_type", "quote")},
                               f["quote"], text)[0] for f in raw):
                    raw = llm_extract(doc_type, text, claimant_id)
        except Exception:
            errors += 1
            raw = pattern_extract(doc_type, text)   # degrade gracefully, still verified the same way
        for f in raw:
            ft, quote = f.pop("fact_type"), f.pop("quote", "")
            value = {k: v for k, v in f.items() if v is not None}
            ok, score = verify(ft, value, quote, text)
            if not ok and method != "pattern" and ft in DATE_FIELDS:
                located = locate_quote(ft, value, text)
                if located:
                    quote = located
                    ok, score = verify(ft, value, quote, text)
                    n_located += 1
            d = parse_date(value.get("start") or value.get("date") or value.get("dob"))
            item = {"fact_type": ft, "value": value, "document_id": doc_id}
            if _key(item) in kept:
                continue
            kept.add(_key(item))
            new.append(Fact(claimant_id=claimant_id, document_id=doc_id, page_no=page_no, fact_type=ft, value=value,
                            event_date=d, quote=quote, verified=ok, match_score=round(score, 1), method=method))
            n_verified += ok
    with session_scope() as s:
        s.add_all(new)
        audit(s, actor, "system", "extract_facts", "claimant", claimant_id, method=method, facts=len(new),
              verified=n_verified, page_errors=errors, quotes_located=n_located)
    return {"method": method, "facts": len(new), "verified": n_verified, "page_errors": errors,
            "quotes_located": n_located}


def claimant_ids() -> list[int]:
    with session_scope() as s:
        return list(s.scalars(select(Claimant.id).order_by(Claimant.id)))
