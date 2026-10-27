"""A small stand-in for a plaintiff firm's case management system (CMS), so the integration can be demonstrated end to end.

Real firms run products such as Litify, Filevine or a custom system; the shape is similar: matters (one per claimant),
documents attached to matters, custom status fields, and webhooks. ClaimCheck pulls new matters and their documents,
and pushes back qualification status and attorney decisions.

    uvicorn claimcheck.integrations.mock_cms:app --port 8001
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from claimcheck import config
from claimcheck.integrations.webhooks import sign

STORE = config.DATA_DIR / "mock_cms.json"
_lock = threading.Lock()

app = FastAPI(title="Mock Firm CMS", description="Stand-in for a law firm case management system (demo only).")


def _load() -> dict:
    return json.loads(STORE.read_text()) if STORE.exists() else {"matters": {}, "events": []}


def _save(db: dict) -> None:
    STORE.write_text(json.dumps(db, indent=1, default=str))


def require_key(x_api_key: str = Header(default="")):
    if not hmac.compare_digest(x_api_key, config.CMS_API_KEY):
        raise HTTPException(401, "invalid API key")


def seed_from_gold() -> int:
    """Create one matter per synthetic claimant (demo setup)."""
    gold = json.loads((config.DOCS_DIR / "gold.json").read_text())
    db = {"matters": {}, "events": []}
    for g in gold:
        db["matters"][g["external_id"]] = {
            "matter_id": g["external_id"], "client_name": g["name"], "dob": g["dob"], "case_code": "cedar_creek",
            "intake_date": str(date.today()), "claimcheck_status": None, "attorney_decision": None, "notes": []}
    with _lock:
        _save(db)
    return len(db["matters"])


class MatterUpdate(BaseModel):
    claimcheck_status: str | None = None
    attorney_decision: str | None = None
    note: str | None = None


@app.get("/matters", dependencies=[Depends(require_key)])
def list_matters(case_code: str | None = None):
    return [m for m in _load()["matters"].values() if case_code is None or m["case_code"] == case_code]


@app.get("/matters/{matter_id}", dependencies=[Depends(require_key)])
def get_matter(matter_id: str):
    m = _load()["matters"].get(matter_id)
    if not m:
        raise HTTPException(404, "matter not found")
    return m


@app.patch("/matters/{matter_id}", dependencies=[Depends(require_key)])
def update_matter(matter_id: str, upd: MatterUpdate):
    with _lock:
        db = _load()
        m = db["matters"].get(matter_id)
        if not m:
            raise HTTPException(404, "matter not found")
        if upd.claimcheck_status is not None:
            m["claimcheck_status"] = upd.claimcheck_status
        if upd.attorney_decision is not None:
            m["attorney_decision"] = upd.attorney_decision
        if upd.note:
            m["notes"].append(upd.note)
        _save(db)
    return m


@app.get("/matters/{matter_id}/documents", dependencies=[Depends(require_key)])
def list_documents(matter_id: str):
    folder = config.DOCS_DIR / matter_id
    if not folder.is_dir():
        return []
    return [{"filename": p.name, "size": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(folder.glob("*.pdf"))]


@app.get("/matters/{matter_id}/documents/{filename}", dependencies=[Depends(require_key)])
def download_document(matter_id: str, filename: str):
    path = (config.DOCS_DIR / matter_id / filename).resolve()
    if config.DOCS_DIR.resolve() not in path.parents or not path.is_file():   # no path traversal
        raise HTTPException(404, "document not found")
    return FileResponse(path, media_type="application/pdf")


@app.post("/webhooks/claimcheck")
async def receive_webhook(request: Request, x_claimcheck_signature: str = Header(default="")):
    body = await request.body()
    if not hmac.compare_digest(sign(body), x_claimcheck_signature):
        raise HTTPException(401, "bad signature")
    with _lock:
        db = _load()
        db["events"].append(json.loads(body))
        _save(db)
    return {"received": True}


@app.get("/events", dependencies=[Depends(require_key)])
def events():
    return _load()["events"][-100:]
