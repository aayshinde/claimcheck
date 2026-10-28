"""Evaluation against the synthetic ground truth (data/documents/gold.json).

Reports, per extraction method:
  - fact precision / recall per fact type, for all extracted facts and for verified facts only
  - qualification accuracy and a confusion matrix
  - false qualifications: claimants marked qualified who should not be (the costly error for a firm or funder)
  - LLM usage: calls, tokens, latency, actual cost, and the same tokens priced at the reference paid-API rate
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from sqlalchemy import func, select

from claimcheck import config
from claimcheck.db import Claimant, Fact, LLMCall, session_scope

TYPES = ["exposure", "diagnosis", "visit", "charge"]


def _match(ft: str, pred: dict, gold: dict) -> bool:
    if ft == "exposure":
        return str(pred.get("start")) == gold["start"] and str(pred.get("end")) == gold["end"]
    if ft == "diagnosis":
        return str(pred.get("icd10", "")).upper() == gold["icd10"].upper() and str(pred.get("date")) == gold["date"]
    if ft == "visit":
        return str(pred.get("date")) == gold["date"]
    if ft == "charge":
        try:
            return str(pred.get("date")) == gold["date"] and abs(float(pred.get("amount")) - gold["amount"]) < 0.01
        except (TypeError, ValueError):
            return False
    return False


def _prf(tp: int, n_pred: int, n_gold: int) -> dict:
    p = tp / n_pred if n_pred else 0.0
    r = tp / n_gold if n_gold else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0,
            "tp": tp, "predicted": n_pred, "gold": n_gold}


def evaluate(method_label: str | None = None, write: bool = True) -> dict:
    gold = {g["external_id"]: g for g in json.loads((config.DOCS_DIR / "gold.json").read_text())}
    counts = {scope: defaultdict(Counter) for scope in ("all", "verified")}
    status_pairs, methods = [], Counter()
    with session_scope() as s:
        for c in s.scalars(select(Claimant)):
            g = gold.get(c.external_id)
            if g is None:
                continue
            status_pairs.append((g["expected_status"], c.status, c.external_id))
            facts = s.scalars(select(Fact).where(Fact.claimant_id == c.id, Fact.fact_type.in_(TYPES))).all()
            for scope in counts:
                preds = [f for f in facts if scope == "all" or f.verified]
                for ft in TYPES:
                    gl = [x for x in g["facts"] if x["fact_type"] == ft]
                    pl = [f for f in preds if f.fact_type == ft]
                    used, tp = set(), 0
                    for f in pl:
                        methods[f.method] += scope == "all"
                        for i, x in enumerate(gl):
                            if i not in used and x["file"] == f.document.filename and _match(ft, f.value, x):
                                used.add(i)
                                tp += 1
                                break
                    counts[scope][ft].update(tp=tp, pred=len(pl), gold=len(gl))
        usage = s.execute(select(func.count(), func.coalesce(func.sum(LLMCall.input_tokens), 0),
                                 func.coalesce(func.sum(LLMCall.output_tokens), 0),
                                 func.coalesce(func.avg(LLMCall.latency_ms), 0),
                                 func.coalesce(func.sum(LLMCall.latency_ms), 0),
                                 func.coalesce(func.sum(LLMCall.cost_usd), 0.0))).one()
        model = s.scalar(select(LLMCall.model).where(LLMCall.purpose == "extract").limit(1))

    # inconsistency detection: planted intake-vs-records mismatches found / false alarms
    from claimcheck import insights, service
    planted = found = false_alarms = 0
    for c in service.claimant_summary():
        g = gold.get(c["external_id"])
        if g is None:
            continue
        hits = insights.discrepancies(service.claimant_detail(c["id"]))
        planted += len(g.get("discrepancies", []))
        found += min(len(hits), len(g.get("discrepancies", [])))
        false_alarms += max(0, len(hits) - len(g.get("discrepancies", [])))

    n = len(status_pairs)
    facts = {scope: {ft: _prf(v["tp"], v["pred"], v["gold"]) for ft, v in per.items()} for scope, per in counts.items()}
    for scope, per in counts.items():
        tot = sum((v for v in per.values()), Counter())
        facts[scope]["overall"] = _prf(tot["tp"], tot["pred"], tot["gold"])
    calls, tin, tout, avg_ms, sum_ms, cost = usage
    method = method_label or (methods.most_common(1)[0][0] if methods else "none")
    result = {
        "method": method, "model": model if method == "llm" else None, "claimants": n,
        "facts": facts,
        "status_accuracy": round(sum(e == p for e, p, _ in status_pairs) / n, 3) if n else 0.0,
        "confusion": dict(Counter(f"{e} -> {p}" for e, p, _ in status_pairs)),
        "false_qualifications": [x for e, p, x in status_pairs if p == "qualified" and e != "qualified"],
        "inconsistencies": {"planted": planted, "found": found, "false_alarms": false_alarms},
        "missed": [{"claimant": x, "expected": e, "got": p} for e, p, x in status_pairs if e != p],
        "llm_usage": {"calls": calls, "input_tokens": int(tin), "output_tokens": int(tout),
                      "avg_latency_s": round(float(avg_ms) / 1000, 2),
                      "seconds_per_claimant": round(float(sum_ms) / 1000 / n, 1) if n else 0,
                      "actual_cost_usd": round(float(cost), 4),
                      "reference_cost_usd": round((tin * config.REFERENCE_PRICE_IN + tout * config.REFERENCE_PRICE_OUT) / 1e6, 4),
                      "reference_cost_per_claimant_usd": round((tin * config.REFERENCE_PRICE_IN + tout * config.REFERENCE_PRICE_OUT)
                                                               / 1e6 / n, 5) if n else 0,
                      "reference_price": f"{config.REFERENCE_MODEL_NAME}: ${config.REFERENCE_PRICE_IN}/"
                                         f"${config.REFERENCE_PRICE_OUT} per 1M tokens in/out"},
    }
    if write:
        config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (config.RESULTS_DIR / f"eval_{method}.json").write_text(json.dumps(result, indent=1, default=str))
    return result


def load_results() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(config.RESULTS_DIR.glob("eval_*.json"))]
