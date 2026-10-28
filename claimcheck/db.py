"""Database schema and session handling (SQLAlchemy 2.0).

Runs on SQLite (default, zero setup) or PostgreSQL + pgvector (set DATABASE_URL). The only dialect-specific piece is the
chunk embedding column: a pgvector `vector(768)` on Postgres, a JSON array on SQLite (searched with numpy instead).

Entity overview:
  claimants ─< documents ─< pages ─< chunks (text + embedding)
  claimants ─< facts (extracted, quote-verified, reviewed)  ─< rule_results (per case criterion)
  claimants ─< agent_runs, records_requests
  audit_log, llm_calls, webhook_deliveries are append-only operational tables.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import lru_cache

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, event,
                        select, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.types import TypeDecorator

from claimcheck import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Embedding(TypeDecorator):
    """pgvector on Postgres, JSON list elsewhere."""
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector
            return dialect.type_descriptor(Vector(config.EMBED_DIM))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        return None if value is None else [float(x) for x in value]

    def process_result_value(self, value, dialect):
        return None if value is None else [float(x) for x in value]


class Base(DeclarativeBase):
    pass


class Claimant(Base):
    __tablename__ = "claimants"
    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)   # matter id in the firm's CMS
    name: Mapped[str] = mapped_column(String(120))
    dob: Mapped[date | None] = mapped_column(Date)
    case_id: Mapped[str] = mapped_column(String(60), default=config.DEFAULT_CASE)
    status: Mapped[str] = mapped_column(String(20), default="pending")   # pending|qualified|not_qualified|needs_review
    status_reason: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str | None] = mapped_column(String(20))            # attorney sign-off: qualified|not_qualified
    decided_by: Mapped[str | None] = mapped_column(String(60))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    documents: Mapped[list["Document"]] = relationship(back_populates="claimant", cascade="all, delete-orphan")
    facts: Mapped[list["Fact"]] = relationship(back_populates="claimant", cascade="all, delete-orphan")
    rule_results: Mapped[list["RuleResult"]] = relationship(back_populates="claimant", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    claimant_id: Mapped[int] = mapped_column(ForeignKey("claimants.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    doc_type: Mapped[str] = mapped_column(String(40), default="unknown")
    n_pages: Mapped[int] = mapped_column(Integer, default=0)
    ocr_used: Mapped[bool] = mapped_column(Boolean, default=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    claimant: Mapped[Claimant] = relationship(back_populates="documents")
    pages: Mapped[list["Page"]] = relationship(back_populates="document", cascade="all, delete-orphan",
                                               order_by="Page.page_no")


class Page(Base):
    __tablename__ = "pages"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)             # 1-based
    text: Mapped[str] = mapped_column(Text)
    ocr: Mapped[bool] = mapped_column(Boolean, default=False)

    document: Mapped[Document] = relationship(back_populates="pages")


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(primary_key=True)
    claimant_id: Mapped[int] = mapped_column(ForeignKey("claimants.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding = mapped_column(Embedding)


class Fact(Base):
    __tablename__ = "facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    claimant_id: Mapped[int] = mapped_column(ForeignKey("claimants.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    page_no: Mapped[int] = mapped_column(Integer)
    fact_type: Mapped[str] = mapped_column(String(30), index=True)   # exposure|diagnosis|visit|charge|date_of_birth
    value: Mapped[dict] = mapped_column(JSON)                        # typed payload, see extract.FACT_SCHEMAS
    event_date: Mapped[date | None] = mapped_column(Date)
    quote: Mapped[str] = mapped_column(Text)                         # verbatim support from the page
    verified: Mapped[bool] = mapped_column(Boolean, default=False)   # quote found on the cited page
    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    method: Mapped[str] = mapped_column(String(20))                  # llm|pattern
    review_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|rejected|edited
    reviewed_by: Mapped[str | None] = mapped_column(String(60))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    claimant: Mapped[Claimant] = relationship(back_populates="facts")
    document: Mapped[Document] = relationship()


class RuleResult(Base):
    __tablename__ = "rule_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    claimant_id: Mapped[int] = mapped_column(ForeignKey("claimants.id", ondelete="CASCADE"), index=True)
    rule_id: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(10))                  # pass|fail|unknown
    explanation: Mapped[str] = mapped_column(Text)
    fact_ids: Mapped[list] = mapped_column(JSON, default=list)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    claimant: Mapped[Claimant] = relationship(back_populates="rule_results")


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    claimant_id: Mapped[int] = mapped_column(ForeignKey("claimants.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(10))                    # llm|offline
    summary: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[list] = mapped_column(JSON, default=list)          # [{tool, args, result}]
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RecordsRequest(Base):
    __tablename__ = "records_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    claimant_id: Mapped[int] = mapped_column(ForeignKey("claimants.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(200))
    record_type: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|approved|rejected
    created_by: Mapped[str] = mapped_column(String(60))
    approved_by: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(60))
    role: Mapped[str] = mapped_column(String(20))                    # attorney|paralegal|system|agent
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(40))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    hash: Mapped[str | None] = mapped_column(String(64))      # sha256(previous hash + this entry): tamper-evident chain


class LLMCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    purpose: Mapped[str] = mapped_column(String(30))                 # extract|agent|qa|embed
    model: Mapped[str] = mapped_column(String(80))
    claimant_id: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)      # actual API cost (0 for local models)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    event: Mapped[str] = mapped_column(String(60))
    url: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON)
    status_code: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------------------------------------------------- engine
@lru_cache(maxsize=4)
def get_engine(url: str | None = None):
    url = url or config.DATABASE_URL
    if url.startswith("sqlite:///"):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return engine


def is_postgres() -> bool:
    return get_engine().dialect.name == "postgresql"


def init_db(drop: bool = False) -> None:
    engine = get_engine()
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    if drop:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":   # approximate-nearest-neighbour index for vector search at scale
        with engine.begin() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks "
                              "USING hnsw (embedding vector_cosine_ops)"))


@lru_cache(maxsize=4)
def _sessionmaker(url: str):
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


@contextmanager
def session_scope():
    s = _sessionmaker(config.DATABASE_URL)()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _entry_hash(prev: str | None, ts: datetime, actor, role, action, entity, entity_id, detail) -> str:
    payload = json.dumps([prev or "", ts.isoformat(), actor, role, action, entity, entity_id, detail],
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def audit(s, actor: str, role: str, action: str, entity: str, entity_id=None, **detail) -> None:
    """Append-only audit trail: every write by a person, the agent or the system goes through here.
    Each entry's hash covers the previous entry's hash, so editing or deleting any past entry breaks the chain."""
    detail = json.loads(json.dumps(detail, default=str))
    eid = None if entity_id is None else str(entity_id)
    ts = utcnow()
    prev = s.scalar(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1))
    s.add(AuditLog(ts=ts, actor=actor, role=role, action=action, entity=entity, entity_id=eid, detail=detail,
                   hash=_entry_hash(prev, ts, actor, role, action, entity, eid, detail)))
    s.flush()


def verify_audit_chain() -> tuple[bool, int, int | None]:
    """(intact, entries checked, id of the first broken entry)."""
    prev, n = None, 0
    with session_scope() as s:
        for a in s.scalars(select(AuditLog).order_by(AuditLog.id)):
            n += 1
            if a.hash != _entry_hash(prev, a.ts, a.actor, a.role, a.action, a.entity, a.entity_id, a.detail):
                return False, n, a.id
            prev = a.hash
    return True, n, None
