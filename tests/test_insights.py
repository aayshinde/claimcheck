import json

from claimcheck import config, insights, service
from claimcheck.rules import load_case

CASE = load_case("cedar_creek")


def _gold():
    return {g["external_id"]: g for g in json.loads((config.DOCS_DIR / "gold.json").read_text())}


def test_planted_discrepancies_are_found_and_nothing_else(demo, ids):
    gold = _gold()
    for ext, cid in ids.items():
        d = service.claimant_detail(cid)
        found = {x["what"] for x in insights.discrepancies(d)}
        planted = gold[ext]["discrepancies"]
        if not planted:
            assert not found, f"{ext}: false alarm {found}"
        # a planted mismatch can only be detected when the matching record was read and trusted
        elif "years" in planted and any(f["fact_type"] == "exposure" and f["verified"] for f in d["facts"]):
            assert "When they were in Cedar Creek" in found
        elif "condition" in planted and any(f["fact_type"] == "diagnosis" and f["verified"] for f in d["facts"]):
            assert "Which illness they have" in found


def test_deficiencies_for_missing_records(demo, ids):
    gold = _gold()
    for ext, cid in ids.items():
        miss = insights.deficiencies(service.claimant_detail(cid))
        if gold[ext]["scenario"] == "missing_exposure_doc":
            assert "Proof of residence or employment" in miss
        if gold[ext]["scenario"] == "self_reported_dx":
            assert "Doctor's record of the diagnosis" in miss


def test_valuation_only_for_qualified(demo, ids):
    for cid in ids.values():
        d = service.claimant_detail(cid)
        v = insights.valuation(d, CASE)
        assert (v is not None) == ((d["decision"] or d["status"]) == "qualified")   # attorney decision wins
        if v:
            assert v["tier"] in ("Tier 1", "Tier 2", "Tier 3") and v["value"] == v["points"] * 1500


def test_census_and_pdf(demo, ids):
    details = [service.claimant_detail(c) for c in ids.values()]
    rows = insights.census_rows(details, CASE)
    assert len(rows) == len(details) and {"Matter", "Status", "Missing", "Inconsistencies"} <= set(rows[0])
    assert insights.summary_pdf(details[0], CASE)[:4] == b"%PDF"


def test_audit_chain_detects_tampering(demo):
    from claimcheck.db import AuditLog, session_scope, verify_audit_chain
    ok, n, _ = verify_audit_chain()
    assert ok and n > 10
    with session_scope() as s:
        entry = s.get(AuditLog, 3)
        entry.detail = {**entry.detail, "tampered": True}
    ok, _, bad = verify_audit_chain()
    assert not ok and bad == 3
    with session_scope() as s:   # restore so other tests see an intact log
        entry = s.get(AuditLog, 3)
        entry.detail = {k: v for k, v in entry.detail.items() if k != "tampered"}
    assert verify_audit_chain()[0]


def test_agent_rejects_free_text_record_types(demo, ids):
    from claimcheck.agent import Tools
    r = Tools(ids["CC-1000"]).call("draft_records_request", {"provider": "X", "record_type": "CT scan", "reason": "r"})
    assert "error" in r
