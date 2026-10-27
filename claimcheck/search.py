"""Retrieval over a claimant's file, medical chronology, treatment gaps, and cited Q&A.

Search is always scoped to ONE claimant: the agent and the Q&A box can never see another claimant's records.
Postgres uses pgvector's cosine distance operator in SQL; SQLite loads the claimant's chunks and ranks them with numpy.
"""
from __future__ import annotations

import re
from datetime import date

import numpy as np
from sqlalchemy import select, text

from claimcheck import config, llm
from claimcheck.db import Chunk, Document, Fact, is_postgres, session_scope
from claimcheck.rules import trusted


def search_records(claimant_id: int, query: str, k: int = 5) -> list[dict]:
    q = llm.embed([query], claimant_id)[0]
    with session_scope() as s:
        if is_postgres():
            rows = s.execute(text(
                "SELECT c.id, c.document_id, c.page_no, c.text, 1 - (c.embedding <=> CAST(:q AS vector)) AS score "
                "FROM chunks c WHERE c.claimant_id = :cid ORDER BY c.embedding <=> CAST(:q AS vector) LIMIT :k"),
                {"q": "[" + ",".join(f"{x:.6f}" for x in q) + "]", "cid": claimant_id, "k": k}).all()
            hits = [(r.id, r.document_id, r.page_no, r.text, float(r.score)) for r in rows]
        else:
            chunks = s.scalars(select(Chunk).where(Chunk.claimant_id == claimant_id)).all()
            if not chunks:
                return []
            m = np.array([c.embedding for c in chunks], dtype=np.float32)
            qv = np.array(q, dtype=np.float32)
            sims = m @ qv / (np.linalg.norm(m, axis=1) * (np.linalg.norm(qv) or 1) + 1e-9)
            order = np.argsort(-sims)[:k]
            hits = [(chunks[i].id, chunks[i].document_id, chunks[i].page_no, chunks[i].text, float(sims[i])) for i in order]
        docs = {d.id: d for d in s.scalars(select(Document).where(Document.id.in_({h[1] for h in hits})))}
        return [{"chunk_id": h[0], "document_id": h[1], "filename": docs[h[1]].filename, "doc_type": docs[h[1]].doc_type,
                 "page_no": h[2], "text": h[3], "score": round(h[4], 3)} for h in hits]


def chronology(claimant_id: int, include_unverified: bool = False) -> list[dict]:
    """Dated events from trusted facts (verified or reviewer-approved), oldest first."""
    rows = []
    with session_scope() as s:
        for f in s.scalars(select(Fact).where(Fact.claimant_id == claimant_id)):
            d = {"id": f.id, "verified": f.verified, "review_status": f.review_status}
            if not (include_unverified or trusted(d)) or f.event_date is None:
                continue
            v = f.value
            label = {
                "exposure": lambda: f"Exposure ({v.get('kind', '')}) {v.get('start')} to {v.get('end')}",
                "diagnosis": lambda: f"Diagnosis: {v.get('condition')} ({v.get('icd10')})",
                "visit": lambda: f"Visit: {v.get('provider', '')} at {v.get('facility', '')}: {v.get('reason', '')}",
                "charge": lambda: f"Charge: CPT {v.get('cpt', '')} {v.get('description', '')}: ${float(v.get('amount', 0)):,.2f}",
                "date_of_birth": lambda: "Date of birth",
            }.get(f.fact_type, lambda: f.fact_type)()
            rows.append({"date": f.event_date, "event": label, "fact_type": f.fact_type, "fact_id": f.id,
                         "document": f.document.filename, "page": f.page_no, "verified": f.verified,
                         "amount": float(v["amount"]) if f.fact_type == "charge" and v.get("amount") is not None else None,
                         "end": date.fromisoformat(v["end"]) if f.fact_type == "exposure" and v.get("end") else None})
    return sorted(rows, key=lambda r: r["date"])


def treatment_gaps(claimant_id: int, min_days: int = config.TREATMENT_GAP_DAYS) -> list[dict]:
    """Periods with no medical care longer than min_days, counted from the diagnosis onward (defense counsel will ask)."""
    dates = sorted({r["date"] for r in chronology(claimant_id) if r["fact_type"] in ("visit", "diagnosis")})
    return [{"from": a, "to": b, "days": (b - a).days} for a, b in zip(dates, dates[1:]) if (b - a).days > min_days]


def medical_specials(claimant_id: int) -> float:
    return round(sum(r["amount"] for r in chronology(claimant_id) if r["amount"] is not None), 2)


QA_PROMPT = """Answer the question about this claimant using ONLY the numbered sources.
Cite every claim with its source number in square brackets, e.g. [S2]. If the sources do not contain the answer,
say "Not found in the claimant's records." Be brief.

{sources}

Question: {question}"""


def answer(claimant_id: int, question: str, k: int = 5) -> dict:
    hits = search_records(claimant_id, question, k)
    if not hits:
        return {"answer": "No documents on file for this claimant.", "sources": [], "mode": "none"}
    if not llm.llm_enabled():
        return {"answer": "Offline mode: the most relevant passages are shown below.", "sources": hits, "mode": "extractive"}
    src = "\n\n".join(f"[S{i}] ({h['filename']}, page {h['page_no']}): {h['text'][:1500]}" for i, h in enumerate(hits, 1))
    msg = llm.chat([{"role": "user", "content": QA_PROMPT.format(sources=src, question=question)}], "qa", claimant_id)
    out = msg.content or ""
    cited = {int(n) for n in re.findall(r"\[S(\d+)\]", out)}
    bad = sorted(n for n in cited if not 1 <= n <= len(hits))
    if bad or (not cited and "not found" not in out.lower()):
        out += "\n\n⚠️ Citation check failed: this answer cites sources that do not exist or none at all. Verify manually."
    return {"answer": out, "sources": hits, "mode": "llm", "cited": sorted(cited)}
