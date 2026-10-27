import json

from claimcheck import config, search, service


def test_demo_never_falsely_qualifies(demo):
    assert demo["false_qualifications"] == []
    assert demo["facts"]["verified"]["overall"]["precision"] >= 0.95


def test_missing_records_go_to_review(demo, ids):
    gold = json.loads((config.DOCS_DIR / "gold.json").read_text())
    by = {c["external_id"]: c for c in service.claimant_summary()}
    for g in gold:
        if g["scenario"] in ("missing_exposure_doc", "self_reported_dx"):
            assert by[g["external_id"]]["status"] == "needs_review"


def test_scanned_record_is_ocrd(demo, ids):
    from claimcheck.ingest import ocr_available
    d = service.claimant_detail(ids["CC-1011"])
    assert any(x["ocr"] for x in d["documents"]) == ocr_available()


def test_search_is_scoped_to_one_claimant(demo, ids):
    hits = search.search_records(ids["CC-1000"], "water service Cedar Creek", k=20)
    docs = {x["id"] for x in service.claimant_detail(ids["CC-1000"])["documents"]}
    assert hits and all(h["document_id"] in docs for h in hits)


def test_chronology_sorted(demo, ids):
    ev = search.chronology(ids["CC-1001"])
    assert [e["date"] for e in ev] == sorted(e["date"] for e in ev)
