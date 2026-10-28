# Architecture

## The problem

In an environmental mass tort, a plaintiff firm may sign thousands of claimants. Before a claim is filed or valued,
someone has to read each claimant's records and check them against the case criteria: *Was this person exposed, where,
when, for how long? Do they have a qualifying diagnosis, documented by a doctor, made after the exposure?* Today that
is paralegal hours per file, it is inconsistent between reviewers, and the reasons behind a decision are hard to audit.

ClaimCheck does the first pass: it extracts the facts with citations, applies the criteria, sends anything uncertain
to a person, and drafts the follow-up work. People make every decision.

## Components

```
                 ┌──────────────────────────┐       ┌──────────────────────────┐
                 │ Streamlit dashboard      │       │ REST API (FastAPI)       │
                 │ review queue, evidence,  │       │ /claimants /facts /audit │
                 │ accuracy & cost          │       │ /ask /agent /integrations│
                 └────────────┬─────────────┘       └────────────┬─────────────┘
                              └──────────────┬───────────────────┘
                                   service layer (roles, audit)
          ┌───────────────┬──────────────────┼──────────────────┬───────────────────┐
     ingest.py       extract.py          rules.py           agent.py          integrations/
     PDF → pages     facts + quote       case criteria      Case Review       cms.py (pull/push)
     OCR, doc type   verification        (YAML) → status    Agent (tools)     webhooks.py (signed)
     chunks+vectors  pattern | llm |                                          mock_cms.py
                     hybrid
          └───────────────┴──────────────────┼──────────────────┴───────────────────┘
                              SQLite  or  PostgreSQL + pgvector
                 claimants · documents · pages · chunks · facts · rule_results
                 records_requests · agent_runs · audit_log · llm_calls · webhook_deliveries
```

Both the dashboard and the API are thin clients of `service.py`, so a permission check or an audit entry can't be
skipped by using one interface instead of the other.

## Key decisions

| Decision | Why | Trade-off |
|---|---|---|
| **The LLM extracts facts; deterministic rules decide.** | A qualification decision must be explainable and repeatable. Rules in YAML can be read and changed by the case team. | Criteria with real judgment (e.g. "consistent with exposure") still need a person. That's intended. |
| **Every fact must quote its source, and code checks the quote.** | Hallucinations are the main risk with LLMs on legal records. The check catches invented quotes, and also correct quotes paired with wrongly copied dates or codes. | Some correct facts end up "unverified" (e.g. OCR noise) and cost a reviewer a click. |
| **Unverified means "needs review", never "fail".** | A missed extraction must not silently reject a real claimant, and an invented one must not qualify a claimant. | More items in the review queue at first. |
| **Hybrid extraction: free patterns first, LLM only on pages they can't read.** | Most pages follow known layouts. Paying for the LLM on every page buys little. | Two extractors to maintain. Both feed the same verification step. |
| **Local LLM by default (Ollama), any OpenAI-compatible API as an option.** | Medical records are PHI. A local model means no data leaves the firm and no per-token fee. | Smaller models are slower and weaker; measured on the Accuracy page. |
| **One database for data and vectors (pgvector).** | One backup, one access-control model, joins between vector hits and claimant data. No separate vector service to run. | At very large scale a dedicated vector store may be faster; not needed here. |
| **SQLite fallback.** | Clone-and-run in seconds; hosted demo with no infrastructure. | Vector search is in-memory numpy on SQLite (fine for one claimant's file). |
| **The agent drafts, people approve.** | Agents are useful for legwork, but a records request goes out under an attorney's name. | Slower than full automation, by design. |
| **Integrations go through a client with an injectable HTTP layer.** | The same code talks to a real CMS or runs in-process against the mock in tests. | The mock is simpler than a real CMS (no pagination or rate limits yet). |

## Data model

- `facts.value` is typed JSON per `fact_type` (`exposure`, `diagnosis`, `visit`, `charge`, `date_of_birth`,
  `claimed_condition`), plus `quote`, `verified`, `match_score`, `method`, and a review status with who and when.
- `rule_results` stores the explanation and the fact ids behind each criterion, so the UI can show "why" with a link
  to the exact page.
- `audit_log` is append-only and hash-chained (each entry's SHA-256 covers the previous entry), so any edit or
  deletion of history is detectable (`verify_audit_chain()`, shown on the Behind the scenes page). It records each ingestion, extraction, review, decision (with override flag and note),
  agent tool call, CMS sync and webhook.
- `llm_calls` records tokens, latency and cost for every model call. It feeds the cost panel and would feed budget alerts.

## Case-team insights (`insights.py`)

- **Inconsistencies:** the firm's intake form is parsed with a fixed template and compared with *trusted* facts only.
  A mismatch is a flag for a person, never an automatic rejection.
- **Missing records:** derived from the criteria left "unknown", so the records team gets one list for all claimants.
- **Valuation:** a settlement grid in the case YAML (points by illness × exposure-length multiplier, tiers, dollars per
  point). The demo grid is fictional; the point is that the same verified facts feed valuation and reporting.

## Security and privacy

- Roles are resolved server-side (demo user directory; production: SSO groups). Paralegals review facts; only attorneys
  decide claimants, approve records requests, see the full audit log or run CMS syncs.
- Agent tools are bound to one claimant id at the start of a run. Search is always filtered by claimant.
- Webhooks are signed with HMAC-SHA256. The mock CMS checks the signature and an API key, and blocks path traversal on
  document downloads.
- Only synthetic data is used. Production would add encryption at rest, a BAA with any hosted model vendor (or local
  models only), retention policies, and a privilege review before any document leaves the firm.

## What production would add

Queue-based processing (one job per document, retries), SSO, row-level security per case team, object storage for
PDFs, the firm's real CMS connector (pagination, rate limits, incremental sync by `updated_since`), monitoring of
extraction drift (verified rate per document type over time), and a regular re-evaluation on a labelled sample of real
files.
