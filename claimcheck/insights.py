"""What a case team asks once the first pass is done:

  discrepancies   does the client's own account match the records? (credibility, fraud risk, cross-examination)
  deficiencies    what is still missing for this claimant, so the records team knows what to chase
  valuation       settlement tier, points and value under the case's grid (the demo grid is fictional)
  census_rows     one row per claimant, the spreadsheet courts and funders ask for

All deterministic and explainable. Only trusted facts (quote-verified or reviewer-approved) are compared.
"""
from __future__ import annotations

import io
import re
from datetime import date

from sqlalchemy import select

from claimcheck import plain
from claimcheck.db import Document, Page, session_scope
from claimcheck.extract import parse_date
from claimcheck.rules import trusted

# The intake questionnaire is the firm's own form, so a fixed template reads it reliably.
YEARS_RE = re.compile(r"Approximate years in Cedar Creek:\s*(\d{4})\s*[–-]\s*(\d{4})")
CLAIM_RE = re.compile(r"Condition claimed:\s*(.+?)\s+Approximate year of diagnosis:\s*(\d{4})", re.S)


def intake_claims(claimant_id: int) -> dict:
    with session_scope() as s:
        texts = s.scalars(select(Page.text).join(Document).where(Document.claimant_id == claimant_id,
                                                                 Document.doc_type == "intake")).all()
    t = " ".join(" ".join(x.split()) for x in texts)
    out = {}
    if m := YEARS_RE.search(t):
        out["years"] = (int(m.group(1)), int(m.group(2)))
    if m := CLAIM_RE.search(t):
        out["condition"], out["dx_year"] = m.group(1).strip(), int(m.group(2))
    return out


def _trusted(detail: dict, fact_type: str) -> list[dict]:
    return [f for f in detail["facts"] if f["fact_type"] == fact_type and trusted(f)]


def discrepancies(detail: dict) -> list[dict]:
    """[{'what', 'client_says', 'records_say', 'proof_fact'}] where the intake and the records disagree."""
    claims, out = intake_claims(detail["id"]), []
    exp = _trusted(detail, "exposure")
    if "years" in claims and exp:
        v = exp[0]["value"]
        s, e = parse_date(v.get("start")), parse_date(v.get("end"))
        cs, ce = claims["years"]
        if s and e and (abs(cs - s.year) > 1 or abs(ce - e.year) > 1):
            out.append({"what": "When they were in Cedar Creek", "client_says": f"{cs}–{ce}",
                        "records_say": f"{s.year}–{e.year}", "proof_fact": exp[0]})
    dx = _trusted(detail, "diagnosis")
    if "condition" in claims and dx:
        said, rec = plain.plain_condition(claims["condition"]), plain.plain_condition(dx[0]["value"].get("condition"))
        if said != rec:
            out.append({"what": "Which illness they have", "client_says": said, "records_say": rec, "proof_fact": dx[0]})
        d = parse_date(dx[0]["value"].get("date"))
        if d and "dx_year" in claims and abs(claims["dx_year"] - d.year) > 1:
            out.append({"what": "When they were diagnosed", "client_says": str(claims["dx_year"]),
                        "records_say": str(d.year), "proof_fact": dx[0]})
    return out


def deficiencies(detail: dict) -> list[str]:
    rules = {r["rule_id"]: r for r in detail["rules"]}
    types = {f["fact_type"] for f in detail["facts"]}
    out = []
    if rules.get("exposure_location", {}).get("status") == "unknown" and "exposure" not in types:
        out.append("Proof of residence or employment")
    if rules.get("qualifying_diagnosis", {}).get("status") == "unknown" and "diagnosis" not in types:
        out.append("Doctor's record of the diagnosis")
    if any(not f["verified"] and f["review_status"] == "pending" and f["fact_type"] in ("exposure", "diagnosis")
           for f in detail["facts"]):
        out.append("A person to confirm a record")
    return out


def valuation(detail: dict, case: dict) -> dict | None:
    """Tier, points and value for a qualified claimant under the case's settlement grid (None otherwise)."""
    grid = case.get("settlement_grid")
    if not grid or (detail.get("decision") or detail["status"]) != "qualified":
        return None
    dx = [f for f in _trusted(detail, "diagnosis")
          if str(f["value"].get("icd10", "")).upper()[:3] in grid["base_points"]]
    if not dx:
        return None
    ws, we = case["exposure"]["window_start"], case["exposure"]["window_end"]
    days = 0
    for f in _trusted(detail, "exposure"):
        s, e = parse_date(f["value"].get("start")), parse_date(f["value"].get("end"))
        if s and e and s <= we and e >= ws:
            days += (min(e, we) - max(s, ws)).days + 1
    years = days / 365.25
    base = max(grid["base_points"][str(f["value"]["icd10"]).upper()[:3]] for f in dx)
    factor = max((r["factor"] for r in grid["exposure_multiplier"] if years >= r["min_years"]), default=1.0)
    points = round(base * factor)
    tier = next(t["name"] for t in grid["tiers"] if points >= t["min_points"])
    return {"tier": tier, "points": points, "value": points * grid["dollars_per_point"], "years": round(years, 1),
            "base": base, "factor": factor}


def census_rows(details: list[dict], case: dict) -> list[dict]:
    rows = []
    for d in details:
        exp = _trusted(d, "exposure")
        dx = _trusted(d, "diagnosis")
        val = valuation(d, case)
        rows.append({
            "Matter": d["external_id"], "Claimant": d["name"], "Date of birth": d["dob"],
            "Status": plain.HEADLINE.get(d["status"], ("", d["status"]))[1],
            "Attorney decision": d["decision"] or "",
            "Exposure start": exp[0]["value"].get("start") if exp else "",
            "Exposure end": exp[0]["value"].get("end") if exp else "",
            "Diagnosis": plain.plain_condition(dx[0]["value"].get("condition")) if dx else "",
            "ICD-10": dx[0]["value"].get("icd10") if dx else "",
            "Diagnosis date": dx[0]["value"].get("date") if dx else "",
            "Settlement tier (fictional grid)": val["tier"] if val else "",
            "Points": val["points"] if val else "",
            "Missing": "; ".join(deficiencies(d)),
            "Inconsistencies": "; ".join(f"{x['what']}: client says {x['client_says']}, records say {x['records_say']}"
                                         for x in discrepancies(d)),
        })
    return rows


def summary_pdf(detail: dict, case: dict) -> bytes:
    """One-page claimant summary an attorney can attach to the file."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    ex = plain.explain(detail, case)
    st = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                            topMargin=0.8 * inch, bottomMargin=0.8 * inch, title=f"ClaimCheck summary {detail['external_id']}")
    mark = {"✅": "[MET]", "❌": "[NOT MET]", "⚠️": "[CHECK]"}
    story = [Paragraph(f"Claimant summary: {detail['name']}", st["Title"]),
             Paragraph(f"Matter {detail['external_id']} · DOB {detail['dob'] or 'n/a'} · {case['name']}", st["Italic"]),
             Spacer(1, 10), Paragraph(f"<b>Result:</b> {ex['headline']}", st["Heading3"])]
    for r in ex["rows"]:
        src = f" <i>(source: {r['proof_label']})</i>" if r["proof_label"] else ""
        story.append(Paragraph(f"<b>{mark.get(r['icon'], '')} {r['rule']}.</b> {r['sentence']}.{src}", st["BodyText"]))
    for x in discrepancies(detail):
        story.append(Paragraph(f"<b>[INCONSISTENT] {x['what']}:</b> client says {x['client_says']}, "
                               f"records say {x['records_say']}.", st["BodyText"]))
    if v := valuation(detail, case):
        story.append(Paragraph(f"<b>Settlement tier (fictional demo grid):</b> {v['tier']}, {v['points']} points.",
                               st["BodyText"]))
    if miss := deficiencies(detail):
        story.append(Paragraph("<b>Still needed:</b> " + "; ".join(miss) + ".", st["BodyText"]))
    story += [Spacer(1, 8), Paragraph(f"<b>Next step:</b> {ex['next_step']}", st["BodyText"]), Spacer(1, 18),
              Paragraph(f"Prepared by ClaimCheck on {date.today():%B %d, %Y} for attorney review. Every finding links "
                        "to a source page in the claimant file. Not legal advice; synthetic demo data.", st["Italic"])]
    doc.build(story)
    return buf.getvalue()
