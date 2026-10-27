"""REST API. Interactive docs at /docs.

    uvicorn claimcheck.api:app --reload

Auth (demo): send `X-User: a.rivera` (attorney) or `X-User: j.chen` (paralegal). The role comes from the server-side
user directory, never from the client. In production this would be the firm's SSO (OIDC) with groups mapped to roles.
"""
from __future__ import annotations

import shutil
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from claimcheck import config, evaluate, rules, search, service
from claimcheck.db import init_db
from claimcheck.ingest import get_or_create_claimant, ingest_file
from claimcheck.integrations.cms import CMSClient

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="ClaimCheck API", version="1.0", lifespan=lifespan,
              description="Mass tort claimant qualification with cited evidence, human review and an audit trail.")


@app.exception_handler(PermissionError)
async def _forbidden(_: Request, e: PermissionError):
    return JSONResponse({"detail": str(e)}, status_code=403)


@app.exception_handler(LookupError)
async def _not_found(_: Request, e: LookupError):
    return JSONResponse({"detail": str(e)}, status_code=404)


@app.exception_handler(ValueError)
async def _bad_request(_: Request, e: ValueError):
    return JSONResponse({"detail": str(e)}, status_code=422)


class User(BaseModel):
    name: str
    role: str


def current_user(x_user: str = Header(default="")) -> User:
    role = service.DEMO_USERS.get(x_user)
    if not role:
        raise HTTPException(401, "unknown user: send X-User (demo users: a.rivera = attorney, j.chen = paralegal)")
    return User(name=x_user, role=role)


# ------------------------------------------------------------------------------------------------ schemas
class ClaimantIn(BaseModel):
    external_id: str = Field(examples=["CC-2001"])
    name: str
    dob: date | None = None
    case_id: str = config.DEFAULT_CASE


class FactReview(BaseModel):
    action: str = Field(pattern="^(approve|reject|edit)$")
    value: dict | None = None
    note: str = ""


class Decision(BaseModel):
    decision: str = Field(pattern="^(qualified|not_qualified)$")
    note: str = ""


class Question(BaseModel):
    question: str = Field(examples=["When was the claimant diagnosed and by whom?"])


class RequestReview(BaseModel):
    approve: bool


# ------------------------------------------------------------------------------------------------ routes
@app.get("/health")
def health():
    from claimcheck import llm
    return {"ok": True, "llm": config.CHAT_MODEL if llm.llm_enabled() else "offline", "database": config.DATABASE_URL.split(":")[0]}


@app.get("/cases")
def cases(_: User = Depends(current_user)):
    return [{"id": c["id"], "name": c["name"], "rules": c["rules"]} for c in rules.list_cases()]


@app.get("/claimants")
def list_claimants(_: User = Depends(current_user)):
    return service.claimant_summary()


@app.post("/claimants", status_code=201)
def create_claimant(body: ClaimantIn, _: User = Depends(current_user)):
    return {"id": get_or_create_claimant(body.external_id, body.name, body.dob, body.case_id)}


@app.get("/claimants/{claimant_id}")
def get_claimant(claimant_id: int, _: User = Depends(current_user)):
    d = service.claimant_detail(claimant_id)
    for f in d["facts"]:
        f.pop("path", None)
    for doc in d["documents"]:
        doc.pop("path", None)
    return d


@app.post("/claimants/{claimant_id}/documents", status_code=201)
def upload_document(claimant_id: int, file: UploadFile = File(...), user: User = Depends(current_user)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(415, "PDF files only")
    service.claimant_detail(claimant_id)            # 404 if missing
    folder = config.DATA_DIR / "uploads" / str(claimant_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / file.filename.replace("/", "_")
    with path.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    doc_id = ingest_file(claimant_id, path, actor=user.name, role=user.role)
    result = service.process_claimant(claimant_id) if doc_id else None
    return {"document_id": doc_id, "duplicate": doc_id is None, "qualification": result}


@app.post("/claimants/{claimant_id}/process")
def process(claimant_id: int, method: str = "auto", _: User = Depends(current_user)):
    return service.process_claimant(claimant_id, method)


@app.get("/claimants/{claimant_id}/qualification")
def qualification(claimant_id: int, _: User = Depends(current_user)):
    d = service.claimant_detail(claimant_id)
    return {"status": d["status"], "reason": d["reason"], "decision": d["decision"], "rules": d["rules"]}


@app.get("/claimants/{claimant_id}/chronology")
def chronology(claimant_id: int, _: User = Depends(current_user)):
    return {"events": search.chronology(claimant_id), "treatment_gaps": search.treatment_gaps(claimant_id),
            "medical_specials_usd": search.medical_specials(claimant_id)}


@app.post("/claimants/{claimant_id}/ask")
def ask(claimant_id: int, q: Question, _: User = Depends(current_user)):
    return search.answer(claimant_id, q.question)


@app.post("/claimants/{claimant_id}/agent")
def agent(claimant_id: int, user: User = Depends(current_user)):
    return service.run_agent(claimant_id, user.name, user.role)


@app.post("/claimants/{claimant_id}/decision")
def decide(claimant_id: int, body: Decision, user: User = Depends(current_user)):
    return service.decide_claimant(claimant_id, body.decision, user.name, user.role, body.note)


@app.post("/facts/{fact_id}/review")
def review_fact(fact_id: int, body: FactReview, user: User = Depends(current_user)):
    return service.review_fact(fact_id, body.action, user.name, user.role, body.value, body.note)


@app.post("/records-requests/{request_id}/review")
def review_request(request_id: int, body: RequestReview, user: User = Depends(current_user)):
    return service.review_request(request_id, body.approve, user.name, user.role)


@app.get("/review-queue")
def review_queue(_: User = Depends(current_user)):
    return [{k: v for k, v in r.items() if k != "path"} for r in service.review_queue()]


@app.get("/audit")
def audit_log(limit: int = 200, user: User = Depends(current_user)):
    service.require(user.role, "attorney")
    return service.audit_trail(limit)


@app.get("/metrics")
def metrics(_: User = Depends(current_user)):
    return evaluate.load_results()


@app.post("/integrations/cms/sync")
def cms_sync(user: User = Depends(current_user)):
    service.require(user.role, "attorney")
    client = CMSClient()
    pulled = client.pull(actor=user.name)
    for c in service.claimant_summary():
        if c["status"] == "pending":
            service.process_claimant(c["id"])
    return {"pull": pulled, "push": client.push(actor=user.name)}
