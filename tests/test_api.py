import pytest
from fastapi.testclient import TestClient

from claimcheck.api import app

ATTY, PARA = {"X-User": "a.rivera"}, {"X-User": "j.chen"}


@pytest.fixture
def api(demo):
    return TestClient(app)


def test_requires_known_user(api):
    assert api.get("/claimants").status_code == 401
    assert api.get("/claimants", headers={"X-User": "mallory"}).status_code == 401
    assert api.get("/claimants", headers=PARA).status_code == 200


def test_paralegal_cannot_decide_or_read_audit(api, ids):
    assert api.post(f"/claimants/{ids['CC-1000']}/decision", headers=PARA, json={"decision": "qualified"}).status_code == 403
    assert api.get("/audit", headers=PARA).status_code == 403


def test_override_requires_note(api, ids):
    cid = ids["CC-1002"]   # outside window -> not_qualified
    r = api.post(f"/claimants/{cid}/decision", headers=ATTY, json={"decision": "qualified"})
    assert r.status_code == 422
    r = api.post(f"/claimants/{cid}/decision", headers=ATTY, json={"decision": "qualified", "note": "settlement terms"})
    assert r.status_code == 200
    assert any(a["action"] == "claimant_decision" and a["detail"]["override"] for a in api.get("/audit", headers=ATTY).json())


def test_fact_review_requalifies(api, ids):
    queue = api.get("/review-queue", headers=PARA).json()
    if not queue:   # the pattern extractor only emits verifiable facts; simulate an unverified LLM extraction
        from claimcheck.db import Fact, session_scope
        with session_scope() as s:
            f = s.get(Fact, 1)
            f.verified, f.review_status = False, "pending"
        queue = api.get("/review-queue", headers=PARA).json()
    item = queue[0]
    r = api.post(f"/facts/{item['fact_id']}/review", headers=PARA, json={"action": "approve"})
    assert r.status_code == 200 and r.json()["status"] in ("qualified", "not_qualified", "needs_review")


def test_upload_dedupes(api, ids, tmp_path):
    from claimcheck import config
    pdf = config.DOCS_DIR / "CC-1000" / "01_intake_questionnaire.pdf"
    with pdf.open("rb") as fh:
        r = api.post(f"/claimants/{ids['CC-1000']}/documents", headers=PARA, files={"file": (pdf.name, fh, "application/pdf")})
    assert r.status_code == 201 and r.json()["duplicate"] is True


def test_chronology_and_ask(api, ids):
    assert "events" in api.get(f"/claimants/{ids['CC-1001']}/chronology", headers=PARA).json()
    a = api.post(f"/claimants/{ids['CC-1001']}/ask", headers=PARA, json={"question": "When was the diagnosis?"}).json()
    assert a["mode"] == "extractive" and a["sources"]
