# ClaimCheck: notes for AI assistants working in this repo

Mass tort claimant qualification: PDFs → verified facts → case criteria → human review. Synthetic data only.

## Non-negotiable design rules
- The LLM only extracts facts. `rules.py` (deterministic, criteria in `cases/*.yaml`) decides qualification.
- Every fact carries a verbatim `quote`; `extract.verify()` must pass before a fact counts as evidence.
  Unverified facts → "unknown" → needs_review. Never "fail" and never "pass" on unverified evidence.
- The agent drafts; people decide. No agent tool may change a status, a decision, or send anything.
- All writes go through `service.py` or `db.audit()`. Permission checks live in `service.py`, not in the UI or API.
- Search and agent tools are scoped to one claimant.
- Everything must run offline (no LLM, SQLite). LLM paths need a fallback.

## Commands
- `python -m claimcheck.pipeline demo`: rebuild synthetic data + database + eval (offline, ~5 s)
- `python -m pytest -q`: offline tests; `TEST_DATABASE_URL=postgresql+psycopg://...` runs them on Postgres
- `scripts/run_llm_evals.sh`: hybrid and LLM evaluations with Ollama (slow on 8 GB machines)
- `streamlit run app.py` / `uvicorn claimcheck.api:app` / `uvicorn claimcheck.integrations.mock_cms:app --port 8001`

## Style
Dense docstrings explaining *why*; type hints; SQLAlchemy 2.0 style; keep functions small and testable.
