import json

from claimcheck import config, service
from claimcheck.agent import Tools, run_agent


def _scenario(name):
    return [g["external_id"] for g in json.loads((config.DOCS_DIR / "gold.json").read_text()) if g["scenario"] == name]


def test_offline_agent_drafts_request_and_never_changes_status(demo, ids):
    cid = ids[_scenario("missing_exposure_doc")[0]]
    before = service.claimant_detail(cid)["status"]
    r = run_agent(cid, "offline")
    assert any(s["tool"] == "draft_records_request" for s in r["steps"])
    d = service.claimant_detail(cid)
    assert d["status"] == before and d["decision"] is None
    assert all(x["status"] == "draft" for x in d["records_requests"])


def test_duplicate_requests_not_created(demo, ids):
    cid = ids[_scenario("missing_exposure_doc")[0]]
    run_agent(cid, "offline")
    reqs = service.claimant_detail(cid)["records_requests"]
    assert len(reqs) == len({r["record_type"] for r in reqs})


def test_unknown_tool_rejected(demo, ids):
    assert "error" in Tools(ids["CC-1000"]).call("decide_claimant", {"decision": "qualified"})


def test_only_attorney_approves_requests(demo, ids):
    cid = ids[_scenario("missing_exposure_doc")[0]]
    rid = service.claimant_detail(cid)["records_requests"][0]["id"]
    import pytest
    with pytest.raises(PermissionError):
        service.review_request(rid, True, "j.chen", "paralegal")
    assert service.review_request(rid, True, "a.rivera", "attorney")["status"] == "approved"


def test_silent_llm_falls_back_to_planner_summary(demo, ids, monkeypatch):
    from types import SimpleNamespace

    from claimcheck import llm
    calls = []

    def fake_chat(msgs, purpose, claimant_id=None, tools=None, **kw):
        calls.append(1)
        return SimpleNamespace(content="", tool_calls=None)   # a small model that stops without a word

    monkeypatch.setattr(llm, "chat", fake_chat)
    cid = ids[_scenario("self_reported_dx")[0]]
    r = run_agent(cid, "llm")
    assert len(calls) == 3                                     # first try + two nudges
    assert "rule-based planner" in r["summary"]
    assert any("safety net" in s["tool"] for s in r["steps"])  # required draft still created
