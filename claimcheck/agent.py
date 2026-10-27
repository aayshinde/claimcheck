"""Case Review Agent: works one claimant's file and prepares the next steps for a human.

The agent can look things up and DRAFT work; it cannot decide anything. Guardrails:
  - tools are scoped to a single claimant id fixed at the start of the run (no cross-claimant access)
  - no tool changes a qualification status or attorney decision
  - records requests are created as drafts that only an attorney can approve
  - bounded number of steps; every tool call and result is stored in agent_runs and the audit log

Two planners share the same tools: an LLM with tool calling (Ollama/OpenAI-compatible), and a deterministic offline
planner used when no LLM is configured or the LLM call fails.
"""
from __future__ import annotations

import json

from sqlalchemy import select

from claimcheck import llm, search
from claimcheck.db import AgentRun, Claimant, Document, Fact, RecordsRequest, RuleResult, audit, session_scope

MAX_STEPS = 8

TOOLS = [
    {"type": "function", "function": {
        "name": "get_qualification", "description": "Current result of each case criterion for this claimant.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "list_documents", "description": "Documents on file for this claimant, with their types.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "search_records", "description": "Search this claimant's records. Returns passages with file and page.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "find_treatment_gaps", "description": "Gaps in medical treatment longer than min_days.",
        "parameters": {"type": "object", "properties": {"min_days": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "draft_records_request",
        "description": "Draft (not send) a request for missing records. An attorney must approve it.",
        "parameters": {"type": "object", "properties": {
            "provider": {"type": "string", "description": "Who holds the records, e.g. Cedar Creek Municipal Water Utility"},
            "record_type": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["provider", "record_type", "reason"]}}},
    {"type": "function", "function": {
        "name": "flag_for_review", "description": "Leave a note for the reviewing paralegal/attorney.",
        "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}}},
]

SYSTEM = """You are the Case Review Agent at a plaintiff law firm working a mass tort claimant file ({case}).
Your job: find out why the claimant is not yet fully qualified and prepare next steps for a human reviewer.
Start with get_qualification. For each criterion that is 'unknown', check list_documents and search_records.
If a required record is missing, CALL draft_records_request yourself (to the utility, employer, or treating provider).
Do not tell the human to draft it: drafting is your job, approving is theirs.
If evidence exists but is unverified, CALL flag_for_review with the file and page to check.
Call find_treatment_gaps with min_days 90. You cannot change qualification status. Do not invent facts.
Finish with at most 5 short bullet points (under 90 words total) for the attorney."""


class Tools:
    def __init__(self, claimant_id: int, actor: str = "case_review_agent"):
        self.cid, self.actor, self.notes = claimant_id, actor, []

    def get_qualification(self) -> list[dict]:
        with session_scope() as s:
            return [{"rule": r.rule_id, "status": r.status, "explanation": r.explanation}
                    for r in s.scalars(select(RuleResult).where(RuleResult.claimant_id == self.cid))]

    def list_documents(self) -> list[dict]:
        with session_scope() as s:
            return [{"filename": d.filename, "doc_type": d.doc_type, "pages": d.n_pages, "ocr": d.ocr_used}
                    for d in s.scalars(select(Document).where(Document.claimant_id == self.cid))]

    def search_records(self, query: str) -> list[dict]:
        return [{"file": h["filename"], "page": h["page_no"], "text": h["text"][:400]}
                for h in search.search_records(self.cid, query, k=3)]

    def find_treatment_gaps(self, min_days: int = 90) -> list[dict]:
        return [{"from": str(g["from"]), "to": str(g["to"]), "days": g["days"]}
                for g in search.treatment_gaps(self.cid, int(min_days or 90))]

    def draft_records_request(self, provider: str, record_type: str, reason: str) -> dict:
        with session_scope() as s:
            c = s.get(Claimant, self.cid)
            dup = s.scalar(select(RecordsRequest).where(RecordsRequest.claimant_id == self.cid,
                                                        RecordsRequest.record_type == record_type,
                                                        RecordsRequest.status != "rejected"))
            if dup:
                return {"request_id": dup.id, "status": dup.status, "note": "a request for this record already exists"}
            body = (f"To the Records Custodian, {provider}:\n\nOn behalf of our client {c.name} "
                    f"(DOB {c.dob or 'on file'}), and pursuant to the enclosed signed authorization, we request "
                    f"{record_type}.\n\nPurpose: {reason}\n\nPlease send certified copies to our office. "
                    f"Thank you.\n\n[DRAFT prepared by ClaimCheck for attorney review]")
            r = RecordsRequest(claimant_id=self.cid, provider=provider, record_type=record_type, reason=reason,
                               body=body, created_by=self.actor)
            s.add(r)
            s.flush()
            audit(s, self.actor, "agent", "draft_records_request", "records_request", r.id, provider=provider)
            return {"request_id": r.id, "status": "draft (needs attorney approval)"}

    def flag_for_review(self, note: str) -> dict:
        self.notes.append(note)
        with session_scope() as s:
            audit(s, self.actor, "agent", "flag_for_review", "claimant", self.cid, note=note)
        return {"flagged": True}

    def call(self, name: str, args: dict):
        if name not in {t["function"]["name"] for t in TOOLS}:
            return {"error": f"unknown tool {name}"}
        try:
            return getattr(self, name)(**(args or {}))
        except TypeError as e:
            return {"error": f"bad arguments: {e}"}


# ----------------------------------------------------------------------------------------------- planners
def _offline_plan(t: Tools) -> tuple[list[dict], str]:
    steps, summary = [], []

    def run(name, **args):
        res = t.call(name, args)
        steps.append({"tool": name, "args": args, "result": res})
        return res

    rules = {r["rule"]: r for r in run("get_qualification")}
    docs = run("list_documents")
    types = {d["doc_type"] for d in docs}
    with session_scope() as s:
        claimed = s.scalar(select(Fact).where(Fact.claimant_id == t.cid, Fact.fact_type == "claimed_condition"))
        claimed = claimed.value.get("condition") if claimed else None
        unverified = [(f.fact_type, f.document.filename, f.page_no) for f in s.scalars(
            select(Fact).where(Fact.claimant_id == t.cid, Fact.verified.is_(False), Fact.review_status == "pending"))]
        facility = s.scalar(select(Fact).where(Fact.claimant_id == t.cid, Fact.fact_type == "visit"))
        facility = facility.value.get("facility") if facility else "treating physician"

    if rules.get("exposure_location", {}).get("status") == "unknown":
        hits = run("search_records", query="Cedar Creek water service address employment dates")
        if not types & {"utility_record", "employment_record"}:
            run("draft_records_request", provider="Cedar Creek Municipal Water Utility",
                record_type="residential water service history, 1985-2005",
                reason="Intake reports residence in Cedar Creek, but no utility or employment record is on file.")
            summary.append("No exposure record on file: drafted a request to the Cedar Creek water utility.")
        else:
            src = hits[0] if hits else {"file": "the exposure record", "page": 1}
            run("flag_for_review", note=f"Exposure dates could not be verified automatically; check {src['file']} p.{src['page']}.")
            summary.append(f"Exposure record exists but needs manual verification ({src['file']}).")
    if rules.get("qualifying_diagnosis", {}).get("status") == "unknown":
        run("search_records", query="diagnosis pathology report ICD-10 assessment")
        if not types & {"pathology_report", "specialist_note"}:
            run("draft_records_request", provider=facility,
                record_type="pathology report and diagnosing physician's notes",
                reason=f"Claimant self-reports {claimed or 'a qualifying condition'}; no medical record of the diagnosis is on file.")
            summary.append(f"Diagnosis only self-reported ({claimed or 'unspecified'}): drafted a records request to {facility}.")
        else:
            run("flag_for_review", note="Diagnosis record present but extraction could not be verified; review it.")
            summary.append("Diagnosis record exists but needs manual verification.")
    for ft, fn, pg in unverified[:3]:
        run("flag_for_review", note=f"Unverified {ft} fact: confirm against {fn} page {pg}.")
    gaps = run("find_treatment_gaps", min_days=90)
    if gaps:
        summary.append("Treatment gaps over 90 days: " + "; ".join(f"{g['from']} → {g['to']} ({g['days']} days)" for g in gaps)
                       + ". Ask the client about care during these periods.")
    fails = [r for r in rules.values() if r["status"] == "fail"]
    if fails:
        summary.append("Fails criteria: " + "; ".join(r["explanation"] for r in fails) + " Recommend attorney confirm and close.")
    if not summary:
        summary.append("All criteria supported by verified evidence; ready for attorney sign-off.")
    return steps, "\n".join(f"- {x}" for x in summary)


def _llm_plan(t: Tools, case_name: str) -> tuple[list[dict], str]:
    msgs = [{"role": "system", "content": SYSTEM.format(case=case_name)},
            {"role": "user", "content": "Review this claimant's file and prepare next steps."}]
    steps = []
    for _ in range(MAX_STEPS):
        m = llm.chat(msgs, "agent", t.cid, tools=TOOLS)
        if not m.tool_calls:
            return steps, m.content or ""
        msgs.append({"role": "assistant", "content": m.content or "",
                     "tool_calls": [tc.model_dump() for tc in m.tool_calls]})
        for tc in m.tool_calls:
            args = llm.parse_json(tc.function.arguments) if tc.function.arguments else {}
            res = t.call(tc.function.name, args)
            steps.append({"tool": tc.function.name, "args": args, "result": res})
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(res, default=str)[:3000]})
    return steps, "Stopped after the step limit; see the steps above."


def _ensure_requests(t: Tools, steps: list[dict]) -> list[dict]:
    """Safety net for small models that describe an action instead of taking it: if a required record type is missing
    and the LLM drafted no request, run the deterministic planner's checks and keep only its draft requests."""
    if any(st["tool"] == "draft_records_request" for st in steps):
        return []
    extra, _ = _offline_plan(Tools(t.cid, t.actor + ":safety_net"))
    return [{**st, "tool": st["tool"] + " (safety net)"} for st in extra if st["tool"] == "draft_records_request"]


def run_agent(claimant_id: int, mode: str = "auto", actor: str = "case_review_agent") -> dict:
    from claimcheck.rules import load_case
    t = Tools(claimant_id, actor)
    with session_scope() as s:
        case_name = load_case(s.get(Claimant, claimant_id).case_id)["name"]
    mode = ("llm" if llm.llm_enabled() else "offline") if mode == "auto" else mode
    try:
        if mode == "llm":
            steps, summary = _llm_plan(t, case_name)
            steps += _ensure_requests(t, steps)
        else:
            steps, summary = _offline_plan(t)
    except Exception as e:   # LLM down or malformed tool calls: fall back, and say so
        steps, summary = _offline_plan(t)
        summary = f"(LLM unavailable: {type(e).__name__}; used the offline planner)\n" + summary
        mode = "offline"
    with session_scope() as s:
        run = AgentRun(claimant_id=claimant_id, mode=mode, summary=summary, steps=steps)
        s.add(run)
        s.flush()
        audit(s, actor, "agent", "agent_run", "claimant", claimant_id, run_id=run.id, mode=mode, steps=len(steps))
        return {"run_id": run.id, "mode": mode, "summary": summary, "steps": steps}
