"""Plain-English explanations for people who don't read JSON: firm leadership, attorneys, clients.

Turns a claimant's rule results and facts into short sentences ("Lived in Cedar Creek from Jul 1993 to Dec 2000")
and a next step. Pure presentation: it never changes a decision, it only explains the one the rules engine made.
"""
from __future__ import annotations

from datetime import date

from claimcheck.extract import parse_date

PLAIN_RULE = {
    "exposure_location": "Lived or worked in Cedar Creek between 1985 and 2005",
    "exposure_duration": "Was there for at least 30 days",
    "qualifying_diagnosis": "Has a doctor-confirmed illness covered by the case",
    "diagnosis_after_exposure": "Got sick after the exposure began",
}
PLAIN_DOC = {"utility_record": "Water utility record", "employment_record": "Employment letter",
             "pathology_report": "Pathology report", "specialist_note": "Specialist's note",
             "visit_note": "Clinic visit note", "bill": "Medical bill", "intake": "Client questionnaire"}
HEADLINE = {"qualified": ("✅", "Qualifies for the case"), "not_qualified": ("❌", "Does not qualify"),
            "needs_review": ("⚠️", "Needs a person to check"), "pending": ("⏳", "Not reviewed yet")}


def _mon(d) -> str:
    d = parse_date(d)
    return d.strftime("%b %Y") if d else "?"


def _day(d) -> str:
    d = parse_date(d)
    return d.strftime("%b %-d, %Y") if d else "?"


def _span(start, end) -> str:
    s, e = parse_date(start), parse_date(end)
    if not (s and e):
        return ""
    days = (e - s).days + 1
    return f"{days} days" if days < 365 else f"about {days / 365.25:.1f} years"


PLAIN_TERMS = [("hepatocellular", "liver cancer"), ("renal cell", "kidney cancer"), ("urothelial", "bladder cancer"),
               ("lymphoma", "lymphoma"), ("parkinson", "Parkinson's disease"), ("melanoma", "skin cancer (melanoma)"),
               ("diabetes", "type 2 diabetes"), ("hypertension", "high blood pressure")]


def plain_condition(text: str) -> str:
    t = (text or "").lower()
    return next((p for k, p in PLAIN_TERMS if k in t), t or "an illness")


def _illness(value: dict, case: dict) -> str:
    code = str(value.get("icd10", "")).upper()[:3]
    return case["diagnosis"]["qualifying"].get(code) or plain_condition(value.get("condition"))


def explain(detail: dict, case: dict) -> dict:
    """{'icon', 'headline', 'rows': [{'icon','rule','sentence','proof_fact'}], 'next_step'}."""
    facts = {f["id"]: f for f in detail["facts"]}
    rows, shown = [], set()
    for r in detail["rules"]:
        ev = [facts[i] for i in r["fact_ids"] if i in facts]
        exp = [f for f in ev if f["fact_type"] == "exposure"]
        dx = [f for f in ev if f["fact_type"] == "diagnosis"]
        claimed = [f for f in ev if f["fact_type"] == "claimed_condition"]
        s, rid = r["status"], r["rule_id"]
        sentence = r["explanation"]
        if rid == "exposure_location":
            if exp and s in ("pass", "fail"):
                v = exp[0]["value"]
                where = "Worked at the Cedar Creek plant" if v.get("kind") == "employment" else "Lived in Cedar Creek"
                sentence = f"{where} from {_mon(v.get('start'))} to {_mon(v.get('end'))}"
                if s == "fail":
                    sentence += ", which is outside the 1985–2005 window"
            elif exp:
                sentence = "We found dates, but couldn't confirm them against the record. A person should check."
            else:
                sentence = "No proof yet that they lived or worked in Cedar Creek."
        elif rid == "exposure_duration":
            if exp and s == "pass":
                sentence = f"That's {_span(exp[0]['value'].get('start'), exp[0]['value'].get('end'))}, more than the 30 days required"
            elif exp and s == "fail":
                sentence = f"Only {_span(exp[0]['value'].get('start'), exp[0]['value'].get('end'))} inside the window; the case requires at least 30"
            else:
                sentence = "Can't tell until we have proof of where they lived or worked."
        elif rid == "qualifying_diagnosis":
            if dx and s == "pass":
                v = dx[0]["value"]
                name = _illness(v, case)
                name = name if name.startswith("Parkinson") else name[0].lower() + name[1:]
                sentence = f"A doctor confirmed {name} on {_day(v.get('date'))}"
            elif dx and s == "fail":
                sentence = f"Diagnosed with {plain_condition(dx[0]['value'].get('condition'))}, which this case doesn't cover"
            elif dx:
                sentence = "A diagnosis was found but couldn't be confirmed against the record. A person should check."
            elif claimed:
                sentence = (f"Says they have {plain_condition(claimed[0]['value'].get('condition'))}, "
                            "but there's no doctor's record yet")
            else:
                sentence = "No doctor's record of a diagnosis yet."
        elif rid == "diagnosis_after_exposure":
            if s == "pass":
                sentence = "Yes, the diagnosis came after the exposure started"
            elif s == "fail":
                sentence = "No, they were diagnosed before they were ever exposed"
            else:
                sentence = "Can't tell yet." if "Not applicable" not in r["explanation"] else "Not needed: another check already failed."
        proof = next((f for f in ev if f["fact_type"] in ("exposure", "diagnosis")), None)
        if proof and proof["id"] in shown:      # show each piece of evidence once
            proof = None
        if proof:
            shown.add(proof["id"])
        rows.append({"icon": {"pass": "✅", "fail": "❌", "unknown": "⚠️"}[s], "rule": PLAIN_RULE.get(rid, r["label"]),
                     "sentence": sentence, "proof_fact": proof,
                     "proof_label": f"{PLAIN_DOC.get(proof['doc_type'], proof['doc_type'])}, page {proof['page_no']}"
                     if proof else None})
    icon, headline = HEADLINE.get(detail["status"], ("", detail["status"]))
    return {"icon": icon, "headline": headline, "rows": rows, "next_step": next_step(detail)}


def next_step(detail: dict) -> str:
    if detail.get("decision"):
        return f"Done: an attorney signed off ({detail['decided_by']})."
    st = detail["status"]
    if st == "qualified":
        return "Ready for an attorney to sign off."
    if st == "not_qualified":
        return "Recommend closing this file; an attorney confirms with one click."
    drafts = [r for r in detail.get("records_requests", []) if r["status"] == "draft"]
    if drafts:
        r = drafts[0]
        return f"A letter asking {r['provider']} for the missing records is drafted and waiting for an attorney's OK."
    return "A paralegal should look at the flagged record (about 2 minutes)."


def describe_fact(f: dict) -> str:
    """One fact as a sentence, e.g. 'Lived in Cedar Creek from Jul 1993 to Dec 2000'."""
    v = f["value"]
    t = f["fact_type"]
    if t == "exposure":
        where = "Worked at the Cedar Creek plant" if v.get("kind") == "employment" else "Lived in Cedar Creek"
        return f"{where} from {_mon(v.get('start'))} to {_mon(v.get('end'))}"
    if t == "diagnosis":
        return f"Diagnosed with {plain_condition(v.get('condition'))} on {_day(v.get('date'))}"
    if t == "visit":
        return f"Doctor visit on {_day(v.get('date'))}" + (f" at {v['facility']}" if v.get("facility") else "")
    if t == "charge":
        try:
            return f"Medical bill of ${float(v.get('amount')):,.2f} on {_day(v.get('date'))}"
        except (TypeError, ValueError):
            return f"Medical bill on {_day(v.get('date'))}"
    if t == "date_of_birth":
        return f"Born {_day(v.get('dob'))}"
    return t.replace("_", " ")
