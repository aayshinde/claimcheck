"""Ingestion: PDF -> page text (OCR for image-only pages) -> document type -> chunks with embeddings.

Page numbers are kept all the way through, so every extracted fact and every search hit can cite document + page.
Re-ingesting the same file (same SHA-256 for the same claimant) is a no-op.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pymupdf
from sqlalchemy import select

from claimcheck import config, llm
from claimcheck.db import Chunk, Claimant, Document, Page, audit, session_scope

DOC_TYPES = [   # (doc_type, phrases on the first page); first match wins
    ("intake", ["intake questionnaire"]),
    ("utility_record", ["water utility", "residential service history"]),
    ("employment_record", ["employment verification"]),
    ("pathology_report", ["pathology report"]),
    ("specialist_note", ["consultation note"]),
    ("visit_note", ["visit note"]),
    ("bill", ["statement of charges", "itemized"]),
]
MIN_TEXT_CHARS = 40          # fewer characters than this on a page -> treat as scanned and OCR it
CHUNK_CHARS, CHUNK_OVERLAP = 900, 150


def doc_path(stored: str) -> Path:
    """Where a document's PDF lives now. Paths are stored absolute; a database copied to another machine (the hosted
    demo bundle) finds the same file under DATA_DIR/cms_downloads/<matter id>/<filename>."""
    p = Path(stored)
    if p.exists():
        return p
    return config.DATA_DIR / "cms_downloads" / p.parent.name / p.name


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def ocr_page(page: pymupdf.Page) -> str:
    png = page.get_pixmap(dpi=300).tobytes("png")
    r = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "4"], input=png, capture_output=True, timeout=120)
    return r.stdout.decode("utf-8", "ignore")


def classify(first_page_text: str) -> str:
    t = " ".join(first_page_text.lower().split())
    for doc_type, phrases in DOC_TYPES:
        if any(p in t for p in phrases):
            return doc_type
    return "unknown"


def chunk_text(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= CHUNK_CHARS:
        return [text] if text else []
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + CHUNK_CHARS])
        i += CHUNK_CHARS - CHUNK_OVERLAP
    return out


def read_pdf(path: Path) -> tuple[list[tuple[str, bool]], int]:
    """Returns [(page_text, used_ocr)] and the page count."""
    pages = []
    with pymupdf.open(path) as doc:
        for p in doc:
            text = p.get_text("text")
            if len(text.strip()) < MIN_TEXT_CHARS and ocr_available():
                pages.append((ocr_page(p), True))
            else:
                pages.append((text, False))
        return pages, doc.page_count


def ingest_file(claimant_id: int, path: Path, actor: str = "system", role: str = "system") -> int | None:
    """Ingest one PDF for a claimant. Returns the document id, or None if it was already ingested."""
    path = Path(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with session_scope() as s:
        if s.scalar(select(Document.id).where(Document.claimant_id == claimant_id, Document.sha256 == sha)):
            return None
        pages, n = read_pdf(path)
        doc = Document(claimant_id=claimant_id, filename=path.name, path=str(path.resolve()), sha256=sha,
                       doc_type=classify(pages[0][0] if pages else ""), n_pages=n,
                       ocr_used=any(o for _, o in pages))
        s.add(doc)
        s.flush()
        texts, metas = [], []
        for i, (text, used_ocr) in enumerate(pages, start=1):
            s.add(Page(document_id=doc.id, page_no=i, text=text, ocr=used_ocr))
            for c in chunk_text(text):
                texts.append(c)
                metas.append(i)
        for c, page_no, vec in zip(texts, metas, llm.embed(texts, claimant_id) if texts else []):
            s.add(Chunk(claimant_id=claimant_id, document_id=doc.id, page_no=page_no, text=c, embedding=vec))
        audit(s, actor, role, "ingest_document", "document", doc.id, filename=path.name, doc_type=doc.doc_type,
              pages=n, ocr=doc.ocr_used)
        return doc.id


def get_or_create_claimant(external_id: str, name: str, dob=None, case_id: str | None = None) -> int:
    with session_scope() as s:
        c = s.scalar(select(Claimant).where(Claimant.external_id == external_id))
        if c is None:
            c = Claimant(external_id=external_id, name=name, dob=dob)
            if case_id:
                c.case_id = case_id
            s.add(c)
            s.flush()
            audit(s, "system", "system", "create_claimant", "claimant", c.id, external_id=external_id)
        return c.id


def ingest_folder(claimant_id: int, folder: Path) -> list[int]:
    return [d for f in sorted(Path(folder).glob("*.pdf")) if (d := ingest_file(claimant_id, f)) is not None]
