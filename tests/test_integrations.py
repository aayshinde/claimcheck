import json

from fastapi.testclient import TestClient

from claimcheck import config
from claimcheck.integrations import mock_cms, webhooks
from claimcheck.integrations.webhooks import sign

KEY = {"X-API-Key": config.CMS_API_KEY}


def test_cms_requires_key(demo):
    c = TestClient(mock_cms.app)
    assert c.get("/matters").status_code == 401
    assert c.get("/matters", headers=KEY).status_code == 200


def test_cms_blocks_path_traversal(demo):
    c = TestClient(mock_cms.app)
    assert c.get("/matters/CC-1000/documents/..%2F..%2Fgold.json", headers=KEY).status_code == 404


def test_push_writes_status_back(demo):
    m = TestClient(mock_cms.app).get("/matters/CC-1000", headers=KEY).json()
    assert m["claimcheck_status"] in ("qualified", "needs_review", "not_qualified")


def test_webhook_signature(demo):
    c = TestClient(mock_cms.app)
    body = json.dumps({"event": "x", "data": {}}).encode()
    assert c.post("/webhooks/claimcheck", content=body, headers={"X-ClaimCheck-Signature": "sha256=bad"}).status_code == 401
    assert c.post("/webhooks/claimcheck", content=body, headers={"X-ClaimCheck-Signature": sign(body)}).status_code == 200


def test_notify_records_delivery(demo):
    c = TestClient(mock_cms.app)
    assert webhooks.notify("claimant.status_changed", {"claimant_id": 1}, url="/webhooks/claimcheck", http=c)
