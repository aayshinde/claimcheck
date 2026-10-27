"""Service layer: the operations people perform. The FastAPI app and the Streamlit dashboard both call these, so
permission checks and audit logging live in one place.

Roles:
  paralegal  review facts (approve / reject / edit), run the agent, flag items
  attorney   everything a paralegal can do, plus final claimant decisions and approving records requests
"""
from __future__ import annotations

from sqlalchemy import func, select

from claimcheck import agent, extract, rules
from claimcheck.db import (AgentRun, AuditLog, Claimant, Fact, RecordsRequest, RuleResult, audit, session_scope,
                           utcnow)

DEMO_USERS = {"a.rivera": "attorney", "j.chen": "paralegal"}   # stand-in for SSO groups
ROLE_RANK = {"paralegal": 1, "attorney": 2}


def require(role: str, needed: str) -> None:
    if ROLE_RANK.get(role, 0) < ROLE_RANK[needed]:
        raise PermissionError(f"this action requires the {needed} role (you are '{role}')")


def process_claimant(claimant_id: int, method: str = "auto") -> dict:
    ex = extract.extract_claimant(claimant_id, method)
    q = rules.qualify_claimant(claimant_id)
    return {"extraction": ex, **q}


def review_fact(fact_id: int, action: str, user: str, role: str, value: dict | None = None, note: str = "") -> dict:
    require(role, "paralegal")
    if action not in ("approve", "reject", "edit"):
        raise ValueError("action must be approve, reject or edit")
    with session_scope() as s:
        f = s.get(Fact, fact_id)
        if f is None:
            raise LookupError("fact not found")
        before = dict(f.value)
        if action == "edit":
            if not value:
                raise ValueError("edit needs a value")
            f.value = {**f.value, **value}
            f.event_date = extract.parse_date(f.value.get("start") or f.value.get("date") or f.value.get("dob"))
        f.review_status = {"approve": "approved", "reject": "rejected", "edit": "edited"}[action]
        f.reviewed_by, f.reviewed_at = user, utcnow()
        audit(s, user, role, f"fact_{action}", "fact", fact_id, before=before, after=f.value, note=note)
        cid = f.claimant_id
    return rules.qualify_claimant(cid, actor=user, role=role)


def decide_claimant(claimant_id: int, decision: str, user: str, role: str, note: str = "") -> dict:
    """Attorney sign-off. Overriding the system's recommendation requires a written reason."""
    require(role, "attorney")
    if decision not in ("qualified", "not_qualified"):
        raise ValueError("decision must be qualified or not_qualified")
    with session_scope() as s:
        c = s.get(Claimant, claimant_id)
        if decision != c.status and not note.strip():
            raise ValueError(f"system status is '{c.status}'; a note is required to decide '{decision}'")
        c.decision, c.decided_by, c.decided_at = decision, user, utcnow()
        audit(s, user, role, "claimant_decision", "claimant", claimant_id, decision=decision, system_status=c.status,
              override=decision != c.status, note=note)
        out = {"claimant_id": claimant_id, "decision": decision, "external_id": c.external_id}
    from claimcheck.integrations import webhooks
    webhooks.notify("claimant.decided", out)
    return out


def review_request(request_id: int, approve: bool, user: str, role: str) -> dict:
    require(role, "attorney")
    with session_scope() as s:
        r = s.get(RecordsRequest, request_id)
        r.status, r.approved_by = ("approved" if approve else "rejected"), user
        audit(s, user, role, "records_request_" + r.status, "records_request", request_id)
        return {"request_id": request_id, "status": r.status}


def run_agent(claimant_id: int, user: str, role: str, mode: str = "auto") -> dict:
    require(role, "paralegal")
    with session_scope() as s:
        audit(s, user, role, "start_agent", "claimant", claimant_id)
    return agent.run_agent(claimant_id, mode)


# ------------------------------------------------------------------------------------------------ read models
def claimant_summary() -> list[dict]:
    with session_scope() as s:
        pending = dict(s.execute(select(Fact.claimant_id, func.count()).where(
            Fact.verified.is_(False), Fact.review_status == "pending").group_by(Fact.claimant_id)).all())
        drafts = dict(s.execute(select(RecordsRequest.claimant_id, func.count()).where(
            RecordsRequest.status == "draft").group_by(RecordsRequest.claimant_id)).all())
        return [{"id": c.id, "external_id": c.external_id, "name": c.name, "status": c.status, "reason": c.status_reason,
                 "decision": c.decision, "decided_by": c.decided_by, "documents": len(c.documents),
                 "unverified_facts": pending.get(c.id, 0), "draft_requests": drafts.get(c.id, 0)}
                for c in s.scalars(select(Claimant).order_by(Claimant.external_id))]


def claimant_detail(claimant_id: int) -> dict:
    with session_scope() as s:
        c = s.get(Claimant, claimant_id)
        if c is None:
            raise LookupError("claimant not found")
        facts = [{"id": f.id, "fact_type": f.fact_type, "value": f.value, "quote": f.quote, "verified": f.verified,
                  "match_score": f.match_score, "method": f.method, "review_status": f.review_status,
                  "reviewed_by": f.reviewed_by, "document_id": f.document_id, "filename": f.document.filename,
                  "doc_type": f.document.doc_type, "path": f.document.path, "page_no": f.page_no}
                 for f in s.scalars(select(Fact).where(Fact.claimant_id == claimant_id).order_by(Fact.event_date))]
        rr = [{"rule_id": r.rule_id, "label": r.label, "status": r.status, "explanation": r.explanation,
               "fact_ids": r.fact_ids} for r in s.scalars(select(RuleResult).where(RuleResult.claimant_id == claimant_id))]
        docs = [{"id": d.id, "filename": d.filename, "doc_type": d.doc_type, "pages": d.n_pages, "ocr": d.ocr_used,
                 "path": d.path} for d in c.documents]
        reqs = [{"id": r.id, "provider": r.provider, "record_type": r.record_type, "reason": r.reason, "body": r.body,
                 "status": r.status, "created_by": r.created_by, "approved_by": r.approved_by}
                for r in s.scalars(select(RecordsRequest).where(RecordsRequest.claimant_id == claimant_id))]
        run = s.scalar(select(AgentRun).where(AgentRun.claimant_id == claimant_id).order_by(AgentRun.id.desc()))
        return {"id": c.id, "external_id": c.external_id, "name": c.name, "dob": c.dob, "case_id": c.case_id,
                "status": c.status, "reason": c.status_reason, "decision": c.decision, "decided_by": c.decided_by,
                "rules": rr, "facts": facts, "documents": docs, "records_requests": reqs,
                "agent_run": None if run is None else {"id": run.id, "mode": run.mode, "summary": run.summary,
                                                       "steps": run.steps, "started_at": run.started_at}}


def review_queue() -> list[dict]:
    """Unverified facts awaiting a person, plus claimants that need review."""
    with session_scope() as s:
        return [{"fact_id": f.id, "claimant_id": f.claimant_id, "external_id": f.claimant.external_id,
                 "fact_type": f.fact_type, "value": f.value, "quote": f.quote, "match_score": f.match_score,
                 "filename": f.document.filename, "page_no": f.page_no, "path": f.document.path}
                for f in s.scalars(select(Fact).where(Fact.verified.is_(False), Fact.review_status == "pending")
                                   .order_by(Fact.claimant_id))]


def audit_trail(limit: int = 200) -> list[dict]:
    with session_scope() as s:
        return [{"ts": a.ts, "actor": a.actor, "role": a.role, "action": a.action, "entity": a.entity,
                 "entity_id": a.entity_id, "detail": a.detail}
                for a in s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))]
