from claimcheck.rules import evaluate, load_case, overall

CASE = load_case("cedar_creek")


def fact(i, ft, doc_type, verified=True, review="pending", **v):
    return {"id": i, "fact_type": ft, "value": v, "verified": verified, "review_status": review, "doc_type": doc_type}


EXP = fact(1, "exposure", "utility_record", start="1990-01-01", end="1992-01-01", location="Cedar Creek Township")
DX = fact(2, "diagnosis", "pathology_report", icd10="C64.2", condition="RCC", date="2015-05-01")


def status(facts):
    return overall(evaluate(CASE, facts))[0]


def test_qualified():
    assert status([EXP, DX]) == "qualified"


def test_outside_window_fails():
    late = fact(1, "exposure", "utility_record", start="2008-01-01", end="2010-01-01", location="Cedar Creek")
    assert status([late, DX]) == "not_qualified"


def test_short_exposure_fails():
    short = fact(1, "exposure", "employment_record", start="1990-01-01", end="1990-01-20", location="Cedar Creek")
    assert status([short, DX]) == "not_qualified"


def test_non_qualifying_diagnosis_fails():
    assert status([EXP, fact(2, "diagnosis", "specialist_note", icd10="I10", date="2015-01-01")]) == "not_qualified"


def test_diagnosis_before_exposure_fails():
    early = fact(2, "diagnosis", "pathology_report", icd10="C67.9", date="1989-01-01")
    assert status([EXP, early]) == "not_qualified"


def test_unverified_evidence_needs_review_not_pass():
    assert status([fact(1, "exposure", "utility_record", verified=False, start="1990-01-01", end="1992-01-01"), DX]) \
        == "needs_review"


def test_reviewer_approval_and_rejection():
    unverified = fact(1, "exposure", "utility_record", verified=False, review="approved", start="1990-01-01",
                      end="1992-01-01")
    assert status([unverified, DX]) == "qualified"
    assert status([{**EXP, "review_status": "rejected"}, DX]) == "needs_review"


def test_self_report_is_not_proof():
    claimed = fact(3, "claimed_condition", "intake", condition="Kidney cancer", year=2015)
    diag_in_intake = fact(4, "diagnosis", "intake", icd10="C64.9", date="2015-01-01")
    assert status([EXP, claimed, diag_in_intake]) == "needs_review"
