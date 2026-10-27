"""CMS integration client: pull matters + documents into ClaimCheck, push results back.

`http` can be any httpx-style client. In production it's a real httpx.Client pointed at the CMS; the demo and tests pass
a FastAPI TestClient wrapping the mock CMS, so the exact same integration code runs without a server.
"""
from __future__ import annotations

import hashlib

import httpx
from sqlalchemy import select

from claimcheck import config
from claimcheck.db import Claimant, audit, session_scope
from claimcheck.extract import parse_date
from claimcheck.ingest import get_or_create_claimant, ingest_file

DOWNLOAD_DIR = config.DATA_DIR / "cms_downloads"


class CMSClient:
    def __init__(self, base_url: str = config.CMS_BASE_URL, api_key: str = config.CMS_API_KEY, http=None):
        self.http = http or httpx.Client(base_url=base_url, timeout=30)
        self.headers = {"X-API-Key": api_key}

    def _get(self, path: str, **params):
        r = self.http.get(path, headers=self.headers, params=params or None)
        r.raise_for_status()
        return r

    def pull(self, case_code: str = config.DEFAULT_CASE, actor: str = "system") -> dict:
        """Create claimants for new matters and ingest any documents not seen before (dedup by SHA-256)."""
        matters = self._get("/matters", case_code=case_code).json()
        new_claimants = new_docs = 0
        for m in matters:
            with session_scope() as s:
                existed = s.scalar(select(Claimant.id).where(Claimant.external_id == m["matter_id"])) is not None
            cid = get_or_create_claimant(m["matter_id"], m["client_name"], parse_date(m.get("dob")), m["case_code"])
            new_claimants += not existed
            folder = DOWNLOAD_DIR / m["matter_id"]
            folder.mkdir(parents=True, exist_ok=True)
            for d in self._get(f"/matters/{m['matter_id']}/documents").json():
                path = folder / d["filename"]
                if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != d["sha256"]:
                    path.write_bytes(self._get(f"/matters/{m['matter_id']}/documents/{d['filename']}").content)
                new_docs += ingest_file(cid, path, actor="cms_sync", role="system") is not None
        with session_scope() as s:
            audit(s, actor, "system", "cms_pull", "integration", None, matters=len(matters),
                  new_claimants=new_claimants, new_documents=new_docs)
        return {"matters": len(matters), "new_claimants": new_claimants, "new_documents": new_docs}

    def push(self, actor: str = "system") -> dict:
        """Write ClaimCheck status and attorney decisions back to the matching CMS matters."""
        with session_scope() as s:
            rows = [(c.external_id, c.status, c.decision, c.status_reason) for c in s.scalars(select(Claimant))]
        pushed = 0
        for ext, status, decision, reason in rows:
            r = self.http.patch(f"/matters/{ext}", headers=self.headers,
                                json={"claimcheck_status": status, "attorney_decision": decision})
            pushed += r.status_code == 200
        with session_scope() as s:
            audit(s, actor, "system", "cms_push", "integration", None, pushed=pushed)
        return {"pushed": pushed}


def in_process_client() -> CMSClient:
    from fastapi.testclient import TestClient
    from claimcheck.integrations.mock_cms import app
    return CMSClient(http=TestClient(app))
