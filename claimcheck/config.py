"""Central configuration. Every value can be overridden with an environment variable or .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _p(name: str, default: Path) -> Path:
    return Path(os.getenv(name, default))


DATA_DIR = _p("DATA_DIR", ROOT / "data")
DOCS_DIR = DATA_DIR / "documents"            # one folder of PDFs per claimant (synthetic)
INBOX_DIR = _p("INBOX_DIR", DATA_DIR / "inbox")  # folder intake: drop PDFs in inbox/<external_id>/
CASES_DIR = ROOT / "cases"                   # case criteria as YAML
RESULTS_DIR = _p("RESULTS_DIR", ROOT / "results")

# SQLite by default (zero setup). For Postgres + pgvector:
#   DATABASE_URL=postgresql+psycopg://claimcheck:claimcheck@localhost:5432/claimcheck
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'claimcheck.db'}")

# ---- LLM (optional) --------------------------------------------------------------------------------------------
# Any OpenAI-compatible endpoint. Default is a free local Ollama model; leave LLM_BASE_URL empty to run fully offline
# (pattern extractor + rule-based agent planner).
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")             # e.g. http://localhost:11434/v1
LLM_API_KEY = os.getenv("LLM_API_KEY", "local")
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen2.5:3b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "hash")           # "hash" (offline) or an embedding model, e.g. nomic-embed-text
EMBED_DIM = 768                                          # nomic-embed-text size; the hash embedder matches it
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "90"))

# USD per 1M tokens (input, output), used for the cost panel. Local models cost $0 in API fees. The reference price
# is what the same token volume would cost on a paid API: check your provider's current price list and set it here.
REFERENCE_PRICE_IN = float(os.getenv("REFERENCE_PRICE_IN", "0.15"))
REFERENCE_PRICE_OUT = float(os.getenv("REFERENCE_PRICE_OUT", "0.60"))
REFERENCE_MODEL_NAME = os.getenv("REFERENCE_MODEL_NAME", "paid API reference")

# ---- integrations ----------------------------------------------------------------------------------------------
CMS_BASE_URL = os.getenv("CMS_BASE_URL", "http://localhost:8001")   # mock firm case-management system
CMS_API_KEY = os.getenv("CMS_API_KEY", "demo-cms-key")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")                          # e.g. http://localhost:8001/webhooks/claimcheck

# ---- review policy ---------------------------------------------------------------------------------------------
FUZZY_QUOTE_THRESHOLD = int(os.getenv("FUZZY_QUOTE_THRESHOLD", "90"))   # quote must match the page at >= this score
TREATMENT_GAP_DAYS = int(os.getenv("TREATMENT_GAP_DAYS", "90"))
DEFAULT_CASE = os.getenv("DEFAULT_CASE", "cedar_creek")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "demo-webhook-secret")   # HMAC-SHA256 signature on every delivery
