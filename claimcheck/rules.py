"""Case criteria engine. The LLM never decides qualification: it only extracts facts. These deterministic rules decide.

Each rule returns pass / fail / unknown with an explanation and the fact ids it relied on:
  pass     supported by trusted facts (quote-verified, or approved/edited by a reviewer)
  fail     trusted facts exist and contradict the rule
  unknown  no trusted evidence either way -> the claimant goes to the review queue

Overall: any fail -> not_qualified; else any unknown -> needs_review; else qualified (pending attorney sign-off).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

import yaml
from sqlalchemy import delete, select

from claimcheck import config
from claimcheck.db import Claimant, Fact, RuleResult, audit, session_scope
from claimcheck.extract import parse_date


@dataclass
class Outcome:
    rule_id: str
    label: str
    status: str
    explanation: str
    fact_ids: list[int] = field(default_factory=list)


@lru_cache(maxsize=8)
def load_case(case_id: str = config.DEFAULT_CASE) -> dict:
    case = yaml.safe_load((config.CASES_DIR / f"{case_id}.yaml").read_text())
    for k in ("window_start", "window_end"):
        case["exposure"][k] = parse_date(case["exposure"][k])
    return case


def list_cases() -> list[dict]:
    return [load_case(p.stem) for p in sorted(config.CASES_DIR.glob("*.yaml"))]


def trusted(f: dict) -> bool:
    return f["review_status"] in ("approved", "edited") or (f["verified"] and f["review_status"] != "rejected")


def _merge(intervals: list[tuple[date, date]]) -> list[tuple[date, date]]:
    out: list[list[date]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def evaluate(case: dict, facts: list[dict]) -> list[Outcome]:
    """facts: dicts with id, fact_type, value, verified, review_status, doc_type."""
    ex, dx = case["exposure"], case["diagnosis"]
    labels = {r["id"]: r["label"] for r in case["rules"]}
    ws, we = ex["window_start"], ex["window_end"]
    live = [f for f in facts if f["review_status"] != "rejected"]

    # ---- exposure
    exposures = []
    for f in live:
        if f["fact_type"] != "exposure" or f["doc_type"] not in ex["evidence_doc_types"]:
            continue
        s, e = parse_date(f["value"].get("start")), parse_date(f["value"].get("end"))
        loc = str(f["value"].get("location") or "").lower()
        if s and e and (not loc or any(k in loc for k in ex["location_keywords"])):
            exposures.append((f, s, e))
    good = [(f, s, e) for f, s, e in exposures if trusted(f)]
    in_window = [(f, max(s, ws), min(e, we)) for f, s, e in good if s <= we and e >= ws]

    if in_window:
        r1 = Outcome("exposure_location", labels["exposure_location"], "pass",
                     "Documented Cedar Creek exposure overlapping the window: " +
                     "; ".join(f"{a} to {b}" for _, a, b in in_window), [f["id"] for f, _, _ in in_window])
    elif good:
        r1 = Outcome("exposure_location", labels["exposure_location"], "fail",
                     f"Documented exposure falls outside {ws} – {we}: " +
                     "; ".join(f"{s} to {e}" for _, s, e in good), [f["id"] for f, _, _ in good])
    elif exposures:
        r1 = Outcome("exposure_location", labels["exposure_location"], "unknown",
                     "An exposure period was found but its source quote could not be verified; check the record.",
                     [f["id"] for f, _, _ in exposures])
    else:
        r1 = Outcome("exposure_location", labels["exposure_location"], "unknown",
                     "No utility or employment record documenting Cedar Creek exposure is on file.")

    if in_window:
        days = sum((b - a).days + 1 for a, b in _merge([(a, b) for _, a, b in in_window]))
        r2 = Outcome("exposure_duration", labels["exposure_duration"], "pass" if days >= ex["min_days"] else "fail",
                     f"{days} days of documented exposure inside the window (minimum {ex['min_days']}).",
                     [f["id"] for f, _, _ in in_window])
    elif r1.status == "fail":
        r2 = Outcome("exposure_duration", labels["exposure_duration"], "fail", "0 days inside the window.", r1.fact_ids)
    else:
        r2 = Outcome("exposure_duration", labels["exposure_duration"], "unknown", "Depends on documented exposure.")

    # ---- diagnosis
    diagnoses = [f for f in live if f["fact_type"] == "diagnosis" and f["doc_type"] in dx["evidence_doc_types"]]
    good_dx = [f for f in diagnoses if trusted(f)]
    qual = [f for f in good_dx if str(f["value"].get("icd10", "")).upper()[:3] in dx["qualifying"]]
    claimed = [f for f in live if f["fact_type"] == "claimed_condition"]
    if qual:
        r3 = Outcome("qualifying_diagnosis", labels["qualifying_diagnosis"], "pass",
                     "; ".join(f"{f['value'].get('condition')} ({f['value'].get('icd10')}) = "
                               f"{dx['qualifying'][str(f['value']['icd10']).upper()[:3]]}" for f in qual),
                     [f["id"] for f in qual])
    elif good_dx:
        r3 = Outcome("qualifying_diagnosis", labels["qualifying_diagnosis"], "fail",
                     "Documented diagnosis is not a qualifying condition: " +
                     "; ".join(f"{f['value'].get('condition')} ({f['value'].get('icd10')})" for f in good_dx),
                     [f["id"] for f in good_dx])
    elif diagnoses:
        r3 = Outcome("qualifying_diagnosis", labels["qualifying_diagnosis"], "unknown",
                     "A diagnosis was extracted but could not be verified against the record.", [f["id"] for f in diagnoses])
    else:
        why = "No pathology report or specialist note with a diagnosis is on file."
        if claimed:
            why += f" The claimant self-reports: {claimed[0]['value'].get('condition')} (not proof)."
        r3 = Outcome("qualifying_diagnosis", labels["qualifying_diagnosis"], "unknown", why, [f["id"] for f in claimed])

    # ---- timing
    if qual and in_window and dx.get("must_follow_exposure", True):
        first_exp = min(s for f, s, e in good)
        first_dx = min(parse_date(f["value"].get("date")) or date.max for f in qual)
        ok = first_dx >= first_exp
        r4 = Outcome("diagnosis_after_exposure", labels["diagnosis_after_exposure"], "pass" if ok else "fail",
                     f"First exposure {first_exp}; diagnosis {first_dx}.",
                     [f["id"] for f in qual] + [f["id"] for f, _, _ in in_window])
    elif r1.status == "fail" or r3.status == "fail":
        r4 = Outcome("diagnosis_after_exposure", labels["diagnosis_after_exposure"], "unknown",
                     "Not applicable: an earlier criterion failed.")
    else:
        r4 = Outcome("diagnosis_after_exposure", labels["diagnosis_after_exposure"], "unknown",
                     "Needs both a documented exposure and a qualifying diagnosis.")
    return [r1, r2, r3, r4]


def overall(outcomes: list[Outcome]) -> tuple[str, str]:
    fails = [o for o in outcomes if o.status == "fail"]
    if fails:
        return "not_qualified", fails[0].explanation
    unknown = [o for o in outcomes if o.status == "unknown"]
    if unknown:
        return "needs_review", unknown[0].explanation
    return "qualified", "All criteria supported by verified evidence. Awaiting attorney sign-off."


def fact_dicts(s, claimant_id: int) -> list[dict]:
    rows = s.scalars(select(Fact).where(Fact.claimant_id == claimant_id)).all()
    return [{"id": f.id, "fact_type": f.fact_type, "value": f.value, "verified": f.verified,
             "review_status": f.review_status, "doc_type": f.document.doc_type, "page_no": f.page_no} for f in rows]


def qualify_claimant(claimant_id: int, actor: str = "system", role: str = "system") -> dict:
    with session_scope() as s:
        c = s.get(Claimant, claimant_id)
        outcomes = evaluate(load_case(c.case_id), fact_dicts(s, claimant_id))
        status, reason = overall(outcomes)
        s.execute(delete(RuleResult).where(RuleResult.claimant_id == claimant_id))
        s.add_all(RuleResult(claimant_id=claimant_id, rule_id=o.rule_id, label=o.label, status=o.status,
                             explanation=o.explanation, fact_ids=o.fact_ids) for o in outcomes)
        previous = c.status
        c.status, c.status_reason = status, reason
        audit(s, actor, role, "qualify", "claimant", claimant_id, status=status, previous=previous)
        external_id = c.external_id
    if status != previous:
        from claimcheck.integrations import webhooks
        webhooks.notify("claimant.status_changed", {"claimant_id": claimant_id, "external_id": external_id,
                                                    "status": status, "previous": previous, "reason": reason})
    return {"status": status, "reason": reason, "rules": [o.__dict__ for o in outcomes]}
